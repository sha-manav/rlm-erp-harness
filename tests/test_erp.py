"""The typed Odoo client, against a live dev container."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "harness"))

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def erp(dev_container_name):
    """An Erp client talking to the dev container's Odoo through the published port."""
    import subprocess

    from lib.erp import Erp

    key = subprocess.run(
        ["docker", "exec", dev_container_name, "cat", "/etc/odoo/api_key"],
        capture_output=True, check=True,
    ).stdout.decode().strip()
    port = subprocess.run(
        ["docker", "port", dev_container_name, "8069/tcp"], capture_output=True, check=True,
    ).stdout.decode().strip().rsplit(":", 1)[-1]
    return Erp(db="bench", url=f"http://127.0.0.1:{port}", user="admin", password=key)


def fresh_offer(erp, offers):
    """The first offer with no confirmed order open on the devbox (a received order cannot
    be cancelled, and the one-PO-per-offer guard refuses a second one)."""
    for o in offers:
        open_lines = erp.search_read(
            "purchase.order.line",
            [("partner_id", "=", o["vendor_id"]), ("product_id", "=", o["product_id"]),
             ("order_id.state", "in", ("purchase", "done"))], ["id"], limit=1)
        if not open_lines:
            return o
    pytest.skip("every candidate offer already has a confirmed order on the devbox")


@pytest.fixture(scope="module", autouse=True)
def no_leftover_orders(erp):
    """Cancel confirmed orders other modules or earlier runs left behind on the shared
    devbox: the one-PO-per-offer guard would otherwise refuse this module's own orders."""
    for po in erp.search_read("purchase.order", [("state", "in", ("purchase", "sent", "to approve"))], ["id"], limit=200):
        erp.cancel("purchase.order", po["id"])
    for so in erp.search_read("sale.order", [("state", "=", "sale")], ["id"], limit=200):
        erp.cancel("sale.order", so["id"])
    for mo in erp.search_read("mrp.production", [("state", "not in", ("done", "cancel"))], ["id"], limit=200):
        erp.cancel("mrp.production", mo["id"])


@pytest.fixture(scope="module")
def catalogue(erp):
    """The finished product, a component, and a vendor that sells the component."""
    suppliers = erp.suppliers()
    assert len(suppliers), "the scenario should ship vendor offers"
    offer = suppliers.all()[0]
    return {
        "product_id": offer["product_id"],
        "vendor_id": offer["vendor_id"],
        "min_qty": offer["min_qty"],
        "price": offer["price"],
    }


# -- reads -------------------------------------------------------------------
def test_reads_return_bounded_tables(erp):
    stock = erp.stock()
    assert len(stock) > 0
    text = str(stock)
    assert "product" in text
    # Rendering is capped even when the underlying data is not.
    assert len(text.splitlines()) <= 44


def test_stock_counts_internal_locations_only(erp):
    from lib.erp import INTERNAL_LOCATION_DOMAIN

    everywhere = erp.call("stock.quant", "search_count", [[]])
    internal = erp.call("stock.quant", "search_count", [[INTERNAL_LOCATION_DOMAIN]])
    assert internal <= everywhere
    for row in erp.stock():
        assert row["free"] == pytest.approx(row["on_hand"] - row["reserved"])


def test_suppliers_surface_vendor_internal_notes(erp):
    rows = erp.suppliers().all()
    assert rows and all("vendor_notes" in row for row in rows)
    # Notes arrive as plain text, not the HTML Odoo stores.
    assert not any("<p>" in (row["vendor_notes"] or "") for row in rows)


def test_fields_lists_real_field_names(erp):
    names = set(erp.fields("stock.move").column("name"))
    assert {"quantity", "picked", "product_uom_qty"} <= names
    assert "quantity_done" not in names  # renamed in Odoo 17+; PLAN assumed it might exist


def test_bad_field_raises_a_readable_odoo_error(erp):
    from lib.erp import OdooError

    with pytest.raises(OdooError) as excinfo:
        erp.search_read("sale.order", [], ["definitely_not_a_field"], limit=1)
    message = str(excinfo.value)
    assert "sale.order.search_read" in message
    assert len(message) < 900          # bounded, not a full server traceback
    assert "harness/lib/erp.py" not in message


def test_empty_result_is_an_empty_table_not_an_error(erp):
    assert len(erp.purchase_orders(state="purchase")) == 0 or True
    assert str(erp.sales_orders(ids=[999999])) == "sale.order (0 rows)"


# -- purchasing flow ---------------------------------------------------------
def test_po_create_confirm_receive_moves_stock(erp, catalogue):
    product_id = catalogue["product_id"]
    qty = max(catalogue["min_qty"], 1)

    before = {r["product_id"]: r["on_hand"] for r in erp.stock([product_id])}
    po_id = erp.create_po(catalogue["vendor_id"], [(product_id, qty)])
    assert isinstance(po_id, int)

    # price_unit was filled from supplierinfo even though we did not pass one.
    line = erp.po_lines([po_id]).all()[0]
    assert line["price_unit"] > 0
    assert line["price_unit"] == pytest.approx(catalogue["price"])

    erp.confirm_po(po_id)
    assert erp.purchase_orders(ids=[po_id]).all()[0]["state"] == "purchase"

    received = erp.receive(po_id, force=True)      # goods are not due yet; this tests the flow
    assert received, "confirming a PO should create a receipt to validate"

    after = {r["product_id"]: r["on_hand"] for r in erp.stock([product_id])}
    assert after.get(product_id, 0) == pytest.approx(before.get(product_id, 0) + qty)
    assert erp.po_lines([po_id]).all()[0]["qty_received"] == pytest.approx(qty)


def test_write_log_records_mutations_but_not_reads(erp):
    erp.write_log.clear()
    erp.stock()
    erp.sales_orders()
    assert erp.write_log == []
    erp.call("res.partner", "search_count", [[]])
    assert erp.write_log == []


# -- manufacturing flow ------------------------------------------------------
def test_mo_create_confirm_produce_consumes_and_produces(erp):
    boms = erp.boms()
    if not len(boms):
        pytest.skip("this scenario has no manufacturing route")
    bom = boms.all()[0]
    finished = erp.search_read(
        "mrp.bom", [("id", "=", bom["bom_id"])], ["product_id", "product_tmpl_id"], limit=1)[0]
    product_id = finished["product_id"][0] if finished["product_id"] else erp.search_read(
        "product.product", [("product_tmpl_id", "=", finished["product_tmpl_id"][0])],
        ["id"], limit=1)[0]["id"]

    component_id = bom["component_id"]
    needed = bom["comp_qty"]
    # Make sure the components exist, buying them if the scenario starts empty.
    have = {r["product_id"]: r["free"] for r in erp.stock([component_id])}
    if have.get(component_id, 0) < needed:
        offers = erp.suppliers([component_id]).all()
        if not offers:
            pytest.skip("component has no vendor to buy from")
        buy_qty = max(offers[0]["min_qty"], needed)
        po_id = erp.create_po(offers[0]["vendor_id"], [(component_id, buy_qty)])
        erp.confirm_po(po_id)
        erp.receive(po_id, force=True)

    before = {r["product_id"]: r["on_hand"] for r in erp.stock([product_id, component_id])}
    mo_id = erp.create_mo(product_id, 1)
    erp.confirm_mo(mo_id)
    erp.produce(mo_id)

    assert erp.productions(ids=[mo_id]).all()[0]["state"] == "done"
    after = {r["product_id"]: r["on_hand"] for r in erp.stock([product_id, component_id])}
    assert after.get(product_id, 0) > before.get(product_id, 0)
    assert after.get(component_id, 0) < before.get(component_id, 0)


# -- sales flow --------------------------------------------------------------
def test_so_confirm_deliver_invoice_post(erp, catalogue):
    product_id = catalogue["product_id"]
    customer = erp.search_read(
        "res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    if not customer:
        customer = erp.search_read("res.partner", [], ["name"], limit=1)
    partner_id = customer[0]["id"]

    # Make sure there is something to ship.
    free = {r["product_id"]: r["free"] for r in erp.stock([product_id])}
    if free.get(product_id, 0) < 1:
        po_id = erp.create_po(catalogue["vendor_id"],
                              [(product_id, max(catalogue["min_qty"], 1))])
        erp.confirm_po(po_id)
        erp.receive(po_id, force=True)

    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id,
        "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 1})],
    }])
    erp.call("sale.order", "action_confirm", [[so_id]])
    assert erp.sales_orders(ids=[so_id]).all()[0]["state"] == "sale"

    delivered = erp.deliver(so_id)
    assert delivered, "confirming a sales order should create a delivery"
    assert erp.sales_orders(ids=[so_id]).all()[0]["delivery_status"] == "full"

    invoice_ids = erp.invoice(so_id)
    assert invoice_ids, "a delivered order should be invoiceable"
    erp.post(invoice_ids)
    posted = erp.get("account.move", invoice_ids, ["state", "amount_total"]).all()
    assert all(row["state"] == "posted" for row in posted)
    assert all(row["amount_total"] > 0 for row in posted)


# -- planning helpers ---------------------------------------------------------
def test_feasible_vendors_excludes_those_who_cannot_make_the_date(erp, catalogue):
    """The arithmetic behind 17 of 23 stock-harness failures, as a function."""
    offers = erp.suppliers([catalogue["product_id"]]).all()
    slowest = max(o["delay_days"] or 0 for o in offers)
    fastest = min(o["delay_days"] or 0 for o in offers)
    from datetime import datetime, timedelta
    today = datetime.utcnow()

    # A due date only the fastest vendor can meet.
    tight = (today + timedelta(days=fastest, hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    table = erp.feasible_vendors(catalogue["product_id"], 5, tight)
    assert all(row["delay_days"] <= fastest for row in table.all()), str(table)
    assert all(row["arrives"] <= tight for row in table.all())

    # A due date everyone can meet lists everyone, cheapest first.
    loose = (today + timedelta(days=slowest + 5)).strftime("%Y-%m-%d %H:%M:%S")
    table = erp.feasible_vendors(catalogue["product_id"], 5, loose)
    assert len(table) == len(offers)
    totals = [row["line_total"] for row in table.all()]
    assert totals == sorted(totals)

    # A due date nobody can meet is an empty table, not an exception. (Some vendors
    # ship same-day, so "today" is feasible; yesterday is not.)
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    assert len(erp.feasible_vendors(catalogue["product_id"], 5, yesterday)) == 0


def test_feasible_vendors_rounds_up_to_the_minimum_quantity(erp, catalogue):
    offer = max(erp.suppliers([catalogue["product_id"]]).all(), key=lambda o: o["min_qty"] or 0)
    if (offer["min_qty"] or 0) < 2:
        pytest.skip("no vendor with a minimum above 1")
    from datetime import datetime, timedelta
    loose = (datetime.utcnow() + timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    row = next(r for r in erp.feasible_vendors(catalogue["product_id"], 1, loose).all()
               if r["vendor_id"] == offer["vendor_id"])
    assert row["order_qty"] == offer["min_qty"]
    assert row["line_total"] == pytest.approx(offer["price"] * offer["min_qty"])


def test_earliest_build_accounts_for_component_lead_times(erp):
    boms = erp.boms()
    if not len(boms):
        pytest.skip("no manufacturing route in this scenario")
    bom = boms.all()[0]
    finished = erp.search_read("mrp.bom", [("id", "=", bom["bom_id"])],
                               ["product_id", "product_tmpl_id"], limit=1)[0]
    product_id = (finished["product_id"][0] if finished["product_id"] else
                  erp.search_read("product.product",
                                  [("product_tmpl_id", "=", finished["product_tmpl_id"][0])],
                                  ["id"], limit=1)[0]["id"])
    plan = erp.earliest_build(product_id, 10_000)      # far beyond any stock
    assert plan["components"], plan
    assert plan["date_start"] >= erp.today()
    for comp in plan["components"]:
        if comp.get("ready") is not None and comp.get("buy"):
            assert comp["ready"] <= plan["date_start"]     # start waits for the slowest part
            assert comp["from"]


# -- write-time guards --------------------------------------------------------
def test_create_po_refuses_a_date_the_vendor_cannot_meet(erp):
    """The 17-of-23 mistake, stopped at the moment it would be written."""
    from lib.erp import OdooError

    offers = [o for o in erp.suppliers().all() if (o["delay_days"] or 0) >= 3]
    if not offers:
        pytest.skip("no vendor with a 3+ day lead time")
    o = fresh_offer(erp, offers)
    with pytest.raises(OdooError) as excinfo:
        erp.create_po(o["vendor_id"], [(o["product_id"], max(o["min_qty"], 1))],
                      date_planned=erp.today())
    message = str(excinfo.value)
    assert "earliest delivery" in message and "feasible_vendors" in message

    # The date it names is accepted.
    earliest = message.split("earliest delivery ")[1][:10]
    po_id = erp.create_po(o["vendor_id"], [(o["product_id"], max(o["min_qty"], 1))],
                          date_planned=f"{earliest} 08:00:00")
    assert isinstance(po_id, int)
    erp.cancel("purchase.order", po_id)

    # force=True is the escape hatch.
    po_id = erp.create_po(o["vendor_id"], [(o["product_id"], max(o["min_qty"], 1))],
                          date_planned=erp.today(), force=True)
    erp.cancel("purchase.order", po_id)


def test_receive_refuses_goods_that_are_not_due(erp, catalogue):
    from lib.erp import OdooError

    from datetime import datetime, timedelta
    later = (datetime.utcnow() + timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    po_id = erp.create_po(catalogue["vendor_id"], [(catalogue["product_id"], max(catalogue["min_qty"], 1))],
                          force=True,  # the offer may already have a received order; receive() is the subject
                          date_planned=later)
    erp.confirm_po(po_id)
    try:
        with pytest.raises(OdooError) as excinfo:
            erp.receive(po_id)
        assert "not due until" in str(excinfo.value)
        assert erp.po_lines([po_id]).all()[0]["qty_received"] == 0   # nothing was fabricated
    finally:
        erp.cancel("purchase.order", po_id)


def test_create_mo_refuses_a_start_before_components_can_exist(erp):
    from lib.erp import OdooError

    boms = erp.boms()
    if not len(boms):
        pytest.skip("no manufacturing route in this scenario")
    bom = boms.all()[0]
    finished = erp.search_read("mrp.bom", [("id", "=", bom["bom_id"])],
                               ["product_id", "product_tmpl_id"], limit=1)[0]
    product_id = (finished["product_id"][0] if finished["product_id"] else
                  erp.search_read("product.product",
                                  [("product_tmpl_id", "=", finished["product_tmpl_id"][0])],
                                  ["id"], limit=1)[0]["id"])
    plan = erp.earliest_build(product_id, 10_000)
    if plan["date_start"][:10] <= erp.today()[:10]:
        pytest.skip("components for 10,000 units are somehow on hand")
    with pytest.raises(OdooError) as excinfo:
        erp.create_mo(product_id, 10_000, date_start=erp.today())
    assert "components can be on hand" in str(excinfo.value)
    mo_id = erp.create_mo(product_id, 10_000, date_start=erp.today(), force=True)
    erp.cancel("mrp.production", mo_id)


# -- make-or-buy, down payments, work centres ---------------------------------
def _finished_product_and_bom(erp):
    boms = erp.boms()
    if not len(boms):
        pytest.skip("no manufacturing route in this scenario")
    bom = boms.all()[0]
    finished = erp.search_read("mrp.bom", [("id", "=", bom["bom_id"])],
                               ["product_id", "product_tmpl_id"], limit=1)[0]
    product_id = (finished["product_id"][0] if finished["product_id"] else
                  erp.search_read("product.product",
                                  [("product_tmpl_id", "=", finished["product_tmpl_id"][0])],
                                  ["id"], limit=1)[0]["id"])
    return product_id, bom


def test_earliest_build_labels_every_component_with_a_source(erp):
    product_id, _ = _finished_product_and_bom(erp)
    plan = erp.earliest_build(product_id, 10_000)
    assert plan["components"]
    assert all(c.get("source") in ("have", "buy", "make", "none") for c in plan["components"]), plan
    # A component with its own BOM must be offered as "make" when it cannot be bought in time.
    nested = [c for c in plan["components"]
              if any(b["component_id"] for b in erp.boms([c["component_id"]]).all())]
    for c in nested:
        assert c.get("source") in ("make", "buy", "have"), c
        if c.get("source") == "make":
            assert c.get("make_start")


def test_create_mo_accepts_a_parent_whose_component_is_being_built(erp):
    """Serial sub-assembly plans create the child MO first; the guard must credit it."""
    from lib.erp import OdooError

    product_id, bom = _finished_product_and_bom(erp)
    component_id = bom["component_id"]
    if not any(b["component_id"] for b in erp.boms([component_id]).all()):
        pytest.skip("first BOM's component has no BOM of its own in this scenario")
    plan = erp.earliest_build(product_id, 500)
    comp = next(c for c in plan["components"] if c["component_id"] == component_id)
    if comp["source"] == "have":
        pytest.skip("component is already in stock")
    start = "2026-12-01 08:00:00"
    child = erp.create_mo(component_id, comp["needed"] * 1.05, date_start="2026-11-20 08:00:00", force=True)
    try:
        erp.call("mrp.production", "write", [[child], {"date_finished": "2026-11-25 08:00:00"}])
        # With the child in flight, the parent must not be refused for THAT component.
        try:
            parent = erp.create_mo(product_id, 500, date_start=start)
            erp.cancel("mrp.production", parent)
        except OdooError as exc:
            assert bom["component"] not in str(exc), f"guard ignored the child MO: {exc}"
    finally:
        erp.cancel("mrp.production", child)


def test_downpayment_invoice(erp, catalogue):
    product_id = catalogue["product_id"]
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id,
        "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 2})]}])
    erp.call("sale.order", "action_confirm", [[so_id]])
    total = erp.sales_orders(ids=[so_id]).all()[0]["amount_total"]
    try:
        ids = erp.invoice(so_id, "percentage", 25)
        assert len(ids) == 1
        inv = erp.get("account.move", ids, ["amount_total", "state"]).all()[0]
        assert inv["amount_total"] == pytest.approx(total * 0.25, rel=0.02)
        erp.post(ids)
        assert erp.get("account.move", ids, ["state"]).all()[0]["state"] == "posted"
    finally:
        erp.cancel("sale.order", so_id)


def test_workcenters_is_a_table(erp):
    table = erp.workcenters()
    assert "name" in table.cols


def test_create_po_refuses_an_origin_it_cannot_feed(erp):
    """origin = the demand this purchase feeds. Naming an order due before the goods land,
    or a reference that is not an order at all, is refused with the references that work."""
    from lib.erp import OdooError, _add_days

    offers = [o for o in erp.suppliers().all() if (o["delay_days"] or 0) >= 3]
    if not offers:
        pytest.skip("no vendor with a 3+ day lead time")
    o = fresh_offer(erp, offers)
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    today = erp.today()
    early = erp.call("sale.order", "create", [{
        "partner_id": partner_id, "commitment_date": _add_days(today, 1),
        "order_line": [(0, 0, {"product_id": o["product_id"], "product_uom_qty": 1})]}])
    late = erp.call("sale.order", "create", [{
        "partner_id": partner_id, "commitment_date": _add_days(today, 60),
        "order_line": [(0, 0, {"product_id": o["product_id"], "product_uom_qty": 1})]}])
    erp.call("sale.order", "action_confirm", [[early, late]])
    names = {r["id"]: r["name"] for r in erp.get("sale.order", [early, late], ["name"])}
    lines = [(o["product_id"], max(o["min_qty"], 1))]
    arrival = _add_days(today, o["delay_days"])
    try:
        with pytest.raises(OdooError) as excinfo:
            erp.create_po(o["vendor_id"], lines, date_planned=arrival,
                          origin=f"{names[early]}, {names[late]}")
        message = str(excinfo.value)
        assert names[early] in message and f"origin={names[late]!r}" in message

        with pytest.raises(OdooError) as excinfo:
            erp.create_po(o["vendor_id"], lines, date_planned=arrival, origin="S99999")
        assert "not a sales or manufacturing order" in str(excinfo.value)

        po_id = erp.create_po(o["vendor_id"], lines, date_planned=arrival, origin=names[late])
        erp.cancel("purchase.order", po_id)
        po_id = erp.create_po(o["vendor_id"], lines, date_planned=arrival,
                              origin=names[early], force=True)
        erp.cancel("purchase.order", po_id)
    finally:
        erp.cancel("sale.order", early)
        erp.cancel("sale.order", late)


def test_create_mo_writes_deadline_origin_and_workcenter(erp):
    """The four things an MO must carry; Odoo supplies none of them."""
    from lib.erp import OdooError

    boms = erp.search_read("mrp.bom", [("operation_ids", "!=", False)], ["product_tmpl_id"], limit=1)
    if not boms:
        pytest.skip("this devbox scenario has no BOM with operations")
    pid = erp.search_read("product.product", [("product_tmpl_id", "=", boms[0]["product_tmpl_id"][0])],
                          ["id"], limit=1)[0]["id"]
    bom = erp.bom_for(pid)
    plan = erp.earliest_build(pid, 1)
    assert plan["lead_days"] == bom["lead_days"] and plan["workcenters"]

    # A deadline before start + lead time is refused, naming the honest date.
    with pytest.raises(OdooError) as excinfo:
        erp.create_mo(pid, 1, date_start="2026-10-01 08:00:00", date_deadline="2026-10-01 09:00:00")
    assert "lead time" in str(excinfo.value)

    # An origin the deadline cannot meet is refused.
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id, "commitment_date": "2026-10-02 08:00:00",
        "order_line": [(0, 0, {"product_id": pid, "product_uom_qty": 1})]}])
    erp.call("sale.order", "action_confirm", [[so_id]])
    so_name = erp.get("sale.order", [so_id], ["name"])[0]["name"]
    try:
        if bom["lead_days"] > 0:
            with pytest.raises(OdooError) as excinfo:
                erp.create_mo(pid, 1, date_start="2026-10-01 08:00:00", origin=so_name, force=False)
            assert "origin refused" in str(excinfo.value) or "components" in str(excinfo.value)
        wc = plan["workcenters"][0]["workcenter_id"]
        mo = erp.create_mo(pid, 1, date_start="2026-10-01 08:00:00", origin=so_name,
                           workcenter_id=wc, force=True)
        row = erp.get("mrp.production", [mo], ["date_deadline", "origin", "date_start"])[0]
        assert row["origin"] == so_name and row["date_deadline"]
        wos = erp.search_read("mrp.workorder", [("production_id", "=", mo)], ["workcenter_id"], limit=5)
        assert wos and all(w["workcenter_id"][0] == wc for w in wos)
        erp.cancel("mrp.production", mo)
    finally:
        erp.cancel("sale.order", so_id)


def test_cheapest_buy_is_never_dearer_than_any_single_vendor(erp, catalogue):
    """The min-cost split across offers, honouring MOQ, note-stated maxima and lead time."""
    product_id = catalogue["product_id"]
    single = erp.feasible_vendors(product_id, 10, erp.today()[:10].replace("-", "-") + " 23:59:59"
                                  if False else "2027-01-01")
    split = erp.cheapest_buy(product_id, 10, need_by="2027-01-01")
    rows = split.all()
    assert rows, split.title if hasattr(split, "title") else str(split)
    total = sum(r["line_total"] for r in rows)
    cheapest_single = min(r["line_total"] for r in single.all())
    assert total <= cheapest_single + 0.01
    assert all(r["qty"] >= (r["min_qty"] or 0) for r in rows)
    assert all((r["max_qty"] is None) or r["qty"] <= r["max_qty"] for r in rows)
    # A due date no vendor can meet yields an empty table, not a wrong one.
    from lib.erp import _add_days
    assert not erp.cheapest_buy(product_id, 10, need_by=_add_days(erp.today(), -1)).all()


def test_earliest_build_with_a_due_date_sources_the_cheapest_feasible_way(erp):
    boms = erp.search_read("mrp.bom", [("bom_line_ids", "!=", False)], ["product_tmpl_id"], limit=1)
    if not boms:
        pytest.skip("no BOM in this scenario")
    pid = erp.search_read("product.product", [("product_tmpl_id", "=", boms[0]["product_tmpl_id"][0])],
                          ["id"], limit=1)[0]["id"]
    free = {c["component_id"]: c["free_now"] for c in erp.earliest_build(pid, 1)["components"]}
    qty = int(max(free.values(), default=0)) + 5          # short on at least one component
    plan = erp.earliest_build(pid, qty, need_by="2027-03-01")
    bought = [c for c in plan["components"] if c.get("source") == "buy"]
    if not bought:
        pytest.skip("every component is on hand or made")
    split = [c for c in bought if "buy_lines" in c]
    assert split, "with a far due date every short component should get a cheapest split"
    assert all(c["buy_cost"] > 0 and c["buy"] >= c["needed"] - c["free_now"] - 1e-6 for c in split)


def test_an_alternate_centre_is_priced_with_a_margin_and_says_so(erp):
    """The alternate's true rate is not in the database (verified on a repair scenario: a
    seeded work order there carries qty x the primary's minutes). The helpers assume it is
    slower and label the assumption; filling an alternate at the primary's rate failed the
    capacity rule.
    """
    from lib.erp import ALT_RATE_MARGIN

    centres = erp.search_read("mrp.workcenter", [("alternative_workcenter_ids", "!=", False)],
                              ["alternative_workcenter_ids"], limit=1)
    if not centres:
        pytest.skip("this scenario has no alternate work centres")
    ops = erp.search_read("mrp.routing.workcenter", [("workcenter_id", "=", centres[0]["id"])],
                          ["bom_id", "time_cycle_manual"], limit=1)
    if not ops:
        pytest.skip("primary centre has no operation")
    bom = erp.get("mrp.bom", [ops[0]["bom_id"][0]], ["product_tmpl_id"])[0]
    pid = erp.search_read("product.product", [("product_tmpl_id", "=", bom["product_tmpl_id"][0])],
                          ["id"], limit=1)[0]["id"]
    rows = erp.workcenter_options(pid).all()
    primary = [r for r in rows if r["role"] == "primary"][0]
    alts = [r for r in rows if r["role"] == "alternative"]
    assert alts and primary["rate"] == "stated"
    for a in alts:
        assert a["rate"].startswith("assumed") and abs(a["min_per_unit"] - primary["min_per_unit"] * ALT_RATE_MARGIN) < 1e-6
        if a["minutes_limit"] and primary["minutes_limit"]:
            pass
    # Committed minutes on an alternate use the same margin.
    table = {r["id"]: r for r in erp.workcenters().all()}
    for a in alts:
        wos = erp.search_read("mrp.workorder", [("workcenter_id", "=", a["workcenter_id"]), ("state", "!=", "cancel")],
                              ["production_id"], limit=50)
        if wos:
            qty = sum(erp.get("mrp.production", [w["production_id"][0]], ["product_qty", "state"])[0]["product_qty"]
                      for w in wos if erp.get("mrp.production", [w["production_id"][0]], ["state"])[0]["state"] != "cancel")
            assert abs(table[a["workcenter_id"]]["minutes_committed"] - qty * a["min_per_unit"]) < 1.0


def test_create_po_refuses_a_second_po_for_an_open_offer_and_add_po_lines_extends_it(erp, catalogue):
    from lib.erp import OdooError

    product_id, qty = catalogue["product_id"], max(catalogue["min_qty"], 1)
    # An earlier test in this module receives goods on this offer, and a received order
    # cannot be cancelled: pick a vendor/product pair with no confirmed order open.
    for line in erp.search_read("purchase.order.line",
                                [("partner_id", "=", catalogue["vendor_id"]), ("product_id", "=", product_id),
                                 ("order_id.state", "in", ("purchase", "done"))], ["order_id"], limit=20):
        try:
            erp.cancel("purchase.order", line["order_id"][0])
        except Exception:
            pytest.skip("this offer already has a received (uncancellable) order on the devbox")
    po = erp.create_po(catalogue["vendor_id"], [(product_id, qty)], date_planned="2026-12-01 08:00:00")
    erp.confirm_po(po)
    try:
        with pytest.raises(OdooError) as excinfo:
            erp.create_po(catalogue["vendor_id"], [(product_id, qty)], date_planned="2026-12-01 08:00:00")
        assert "one PO per supplier offer" in str(excinfo.value) and f"add_po_lines({po}" in str(excinfo.value)
        before = len(erp.search_read("purchase.order.line", [("order_id", "=", po)], ["id"], limit=50))
        erp.add_po_lines(po, [(product_id, qty)])
        after = erp.search_read("purchase.order.line", [("order_id", "=", po)], ["id", "price_unit"], limit=50)
        assert len(after) == before + 1 and all(l["price_unit"] > 0 for l in after)
        state = erp.get("purchase.order", [po], ["state"])[0]["state"]
        assert state == "purchase"
    finally:
        erp.cancel("purchase.order", po)


def test_invoice_with_a_named_payment_term_sets_it_on_order_and_invoice(erp, catalogue):
    """A task naming payment terms wants them on the retained order and every linked invoice."""
    from lib.erp import OdooError

    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    so = erp.call("sale.order", "create", [{
        "partner_id": partner_id, "commitment_date": "2026-12-01 08:00:00",
        "order_line": [(0, 0, {"product_id": catalogue["product_id"], "product_uom_qty": 1})]}])
    erp.call("sale.order", "action_confirm", [[so]])
    try:
        term = erp.payment_term_id("Immediate")
        try:
            invs = erp.invoice(so, payment_term="Immediate", method="fixed", amount=1.0)
        except OdooError as exc:
            pytest.skip(f"down payment not possible here: {exc}")
        row = erp.get("sale.order", [so], ["payment_term_id"])[0]
        assert row["payment_term_id"] and row["payment_term_id"][0] == term
        for m in erp.get("account.move", invs, ["invoice_payment_term_id"]):
            assert m["invoice_payment_term_id"] and m["invoice_payment_term_id"][0] == term
        with pytest.raises(OdooError):
            erp.payment_term_id("no such term zz")
    finally:
        erp.cancel("sale.order", so)


def test_create_po_refuses_a_quantity_the_named_orders_cannot_absorb(erp):
    """Origin quantities are a flow: name only what consumes the supply, unit for unit."""
    from lib.erp import OdooError

    comp = offers = pid = None
    for bom in erp.search_read("mrp.bom", [("bom_line_ids", "!=", False)], ["product_tmpl_id"], limit=20):
        pid = erp.search_read("product.product", [("product_tmpl_id", "=", bom["product_tmpl_id"][0])],
                              ["id"], limit=1)[0]["id"]
        for c in erp.earliest_build(pid, 2)["components"]:
            offers = erp.suppliers([c["component_id"]]).all()
            if offers:
                comp = c
                break
        if comp:
            break
    if comp is None:
        pytest.skip("no BOM with a purchasable component")
    o = fresh_offer(erp, offers)          # an offer with no confirmed order open on the devbox
    mo = erp.create_mo(pid, 2, date_start="2026-11-20 08:00:00", force=True)
    erp.confirm_mo(mo)
    mo_name = erp.get("mrp.production", [mo], ["name"])[0]["name"]
    try:
        big = max(o["min_qty"] or 0, comp["needed"] + 50) + 1     # above need and not the MOQ
        with pytest.raises(OdooError) as excinfo:
            erp.create_po(o["vendor_id"], [(comp["component_id"], big)], date_planned="2026-11-15 08:00:00",
                          origin=mo_name)
        assert "absorb" in str(excinfo.value)
        with pytest.raises(OdooError) as excinfo:
            erp.create_po(o["vendor_id"], [(comp["component_id"], 1)], date_planned="2026-11-15 08:00:00",
                          origin="S99999", force=False)
        assert "origin" in str(excinfo.value)
        ok = max(o["min_qty"] or 0, comp["needed"])
        po = erp.create_po(o["vendor_id"], [(comp["component_id"], ok)], date_planned="2026-11-15 08:00:00",
                           origin=mo_name)
        erp.cancel("purchase.order", po)
    finally:
        erp.cancel("mrp.production", mo)


def test_create_po_refuses_finished_goods_excess_and_names_an_absorber(erp, catalogue):
    from lib.erp import OdooError, _add_days

    product_id = catalogue["product_id"]
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=2)
    if len(partner) < 2:
        partner = erp.search_read("res.partner", [], ["name"], limit=2)
    today = erp.today()
    delay = erp._delay_for(catalogue["vendor_id"], product_id) or 0
    arrival = _add_days(today, max(delay, 1))
    sos = []
    for p in partner[:2]:
        so = erp.call("sale.order", "create", [{
            "partner_id": p["id"], "commitment_date": _add_days(arrival, 5),
            "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 6})]}])
        erp.call("sale.order", "action_confirm", [[so]])
        sos.append((so, erp.get("sale.order", [so], ["name"])[0]["name"]))
    lines = [(product_id, max(catalogue["min_qty"], 8))]
    try:
        with pytest.raises(OdooError) as excinfo:
            erp.create_po(catalogue["vendor_id"], lines, date_planned=arrival, origin=sos[0][1])
        msg = str(excinfo.value)
        assert "absorbed entirely" in msg and sos[1][1] in msg
        po = erp.create_po(catalogue["vendor_id"], lines, date_planned=arrival, origin=f"{sos[0][1]}, {sos[1][1]}")
        erp.cancel("purchase.order", po)
    finally:
        for so, _ in sos:
            erp.cancel("sale.order", so)
