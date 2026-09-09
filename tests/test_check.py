"""Invariants and the finish gate, against a live dev container."""

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
    import subprocess

    from lib.erp import Erp

    key = subprocess.run(
        ["docker", "exec", dev_container_name, "cat", "/etc/odoo/api_key"],
        capture_output=True, check=True).stdout.decode().strip()
    port = subprocess.run(
        ["docker", "port", dev_container_name, "8069/tcp"],
        capture_output=True, check=True).stdout.decode().strip().rsplit(":", 1)[-1]
    return Erp(db="bench", url=f"http://127.0.0.1:{port}", user="admin", password=key)


@pytest.fixture(scope="module", autouse=True)
def no_leftover_orders(erp):
    """Cancel confirmed orders other test modules left behind."""
    for po in erp.search_read("purchase.order", [("state", "=", "purchase")], ["id"], limit=100):
        erp.cancel("purchase.order", po["id"])
    for so in erp.search_read("sale.order", [("state", "=", "sale")], ["id"], limit=100):
        erp.cancel("sale.order", so["id"])
    for mo in erp.search_read("mrp.production", [("state", "not in", ("done", "cancel"))], ["id"], limit=100):
        erp.cancel("mrp.production", mo["id"])


@pytest.fixture(autouse=True)
def clean_registry():
    from lib import check, finish

    check.clear()
    finish.reset()
    yield
    check.clear()
    finish.reset()


@pytest.fixture
def offer(erp):
    rows = erp.suppliers().all()
    assert rows, "the scenario should ship vendor offers"
    return rows[0]


def status_of(table, check_id: str) -> str:
    for row in table.all():
        if row["check"] == check_id:
            return row["status"]
    raise AssertionError(f"no check {check_id!r} in {[r['check'] for r in table.all()]}")


def evidence_of(table, check_id: str) -> str:
    return next(r["evidence"] for r in table.all() if r["check"] == check_id)


# -- clean state --------------------------------------------------------------
def test_untouched_environment_passes_every_invariant(erp):
    """The clean-state case: every invariant must pass before anything is touched."""
    from lib import check

    touched = erp.count("purchase.order") + erp.count("sale.order") + erp.count("mrp.production")
    if touched:
        pytest.skip(f"container already has {touched} order(s); run `make devbox` for a clean one")

    table = check.invariants(erp)
    failed = [r for r in table.all() if r["status"] == "FAIL"]
    assert not failed, f"clean environment should pass; failed: {failed}"


# -- seeded violations --------------------------------------------------------
def test_draft_purchase_order_is_detected(erp, offer):
    from lib import check

    po_id = erp.create_po(offer["vendor_id"],
                          [(offer["product_id"], max(offer["min_qty"], 1))])
    try:
        table = check.invariants(erp)
        assert status_of(table, "drafts") == "FAIL"
        assert "purchase.order" in evidence_of(table, "drafts")
    finally:
        erp.cancel("purchase.order", po_id)
        erp.call("purchase.order", "unlink", [[po_id]])
    assert status_of(check.invariants(erp), "drafts") == "pass"


def test_vendor_who_does_not_sell_the_product_is_detected(erp, offer):
    from lib import check

    # A partner with no supplierinfo for this product; assert it so the test cannot go vacuous.
    product_id = offer["product_id"]
    template_id = erp.search_read(
        "product.product", [("id", "=", product_id)], ["product_tmpl_id"], limit=1
    )[0]["product_tmpl_id"][0]
    listed = {
        row["partner_id"][0]
        for row in erp.search_read(
            "product.supplierinfo",
            ["|", ("product_id", "=", product_id), ("product_tmpl_id", "=", template_id)],
            ["partner_id"], limit=200)
        if row["partner_id"]
    }
    candidates = erp.search_read("res.partner", [], ["name"], limit=300)
    stranger_id = next(p["id"] for p in candidates if p["id"] not in listed)
    assert not erp.search_read(
        "product.supplierinfo",
        ["&", ("partner_id", "=", stranger_id),
         "|", ("product_id", "=", product_id), ("product_tmpl_id", "=", template_id)],
        ["id"], limit=1), "picked a partner that does sell this product; the test would prove nothing"

    po_id = erp.call("purchase.order", "create", [{
        "partner_id": stranger_id,
        "order_line": [(0, 0, {"product_id": offer["product_id"],
                               "product_qty": max(offer["min_qty"], 1),
                               "price_unit": offer["price"]})],
    }])
    erp.confirm_po(po_id)
    try:
        table = check.invariants(erp)
        assert status_of(table, "supplier_validity") == "FAIL"
        assert "does not list" in evidence_of(table, "supplier_validity")
    finally:
        erp.cancel("purchase.order", po_id)


def test_quantity_below_the_vendor_minimum_is_detected(erp, offer):
    from lib import check

    if (offer["min_qty"] or 0) < 2:
        pytest.skip("this vendor has no minimum to fall below")
    po_id = erp.call("purchase.order", "create", [{
        "partner_id": offer["vendor_id"],
        "order_line": [(0, 0, {"product_id": offer["product_id"], "product_qty": 1,
                               "price_unit": offer["price"]})],
    }])
    erp.confirm_po(po_id)
    try:
        assert status_of(check.invariants(erp), "supplier_validity") == "FAIL"
        assert "below" in evidence_of(check.invariants(erp), "supplier_validity")
    finally:
        erp.cancel("purchase.order", po_id)


def test_wrong_tier_price_is_detected(erp, offer):
    from lib import check

    # Pick a price above every existing offer: confirming a PO records its price as a tier.
    existing = erp.search_read(
        "product.supplierinfo", [("partner_id", "=", offer["vendor_id"])], ["price"], limit=200)
    wrong_price = max([row["price"] or 0 for row in existing] + [offer["price"] or 10]) * 3 + 137.0

    po_id = erp.call("purchase.order", "create", [{
        "partner_id": offer["vendor_id"],
        "order_line": [(0, 0, {"product_id": offer["product_id"],
                               "product_qty": max(offer["min_qty"], 1),
                               "price_unit": wrong_price})],
    }])
    erp.confirm_po(po_id)
    try:
        assert status_of(check.invariants(erp), "supplier_validity") == "FAIL"
        assert "tier price" in evidence_of(check.invariants(erp), "supplier_validity")
    finally:
        erp.cancel("purchase.order", po_id)


def test_delivery_without_an_invoice_is_detected(erp, offer):
    from lib import check

    product_id = offer["product_id"]
    free = {r["product_id"]: r["free"] for r in erp.stock([product_id])}
    if free.get(product_id, 0) < 1:
        po_id = erp.create_po(offer["vendor_id"], [(product_id, max(offer["min_qty"], 1))])
        erp.confirm_po(po_id)
        erp.receive(po_id, force=True)

    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id,
        "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 1})],
    }])
    erp.call("sale.order", "action_confirm", [[so_id]])
    erp.deliver(so_id)
    try:
        table = check.invariants(erp)
        assert status_of(table, "invoicing") == "FAIL"
        assert "not invoiced" in evidence_of(table, "invoicing")

        # Invoicing but leaving the invoice in draft is still a failure.
        invoice_ids = erp.invoice(so_id)
        assert status_of(check.invariants(erp), "invoicing") == "FAIL"
        assert "draft" in evidence_of(check.invariants(erp), "invoicing")

        erp.post(invoice_ids)
        assert status_of(check.invariants(erp), "invoicing") == "pass"
    finally:
        pass


# -- agent-defined rules ------------------------------------------------------
def test_registered_rules_run_and_persist(erp):
    from lib import check
    from lib.check import Rule

    check.register(Rule("cap", "spend under 1e9", lambda c: (True, "spend 0 < 1e9")))
    check.register(Rule("impossible", "always fails", lambda c: (False, "by design")))

    table = check.all(erp)
    assert status_of(table, "cap") == "pass"
    assert status_of(table, "impossible") == "FAIL"
    # Still there on the next call — the registry is what finish() gates on.
    assert status_of(check.all(erp), "impossible") == "FAIL"
    assert {r.id for r in check.registered()} == {"cap", "impossible"}


def test_reregistering_an_id_replaces_it(erp):
    from lib import check
    from lib.check import Rule

    check.register(Rule("cap", "v1", lambda c: (False, "v1")))
    check.register(Rule("cap", "v2", lambda c: (True, "v2")))
    assert len(check.registered()) == 1
    assert status_of(check.all(erp), "cap") == "pass"


def test_a_raising_rule_fails_that_row_only(erp):
    from lib import check
    from lib.check import Rule

    def explode(_client):
        raise ValueError("bad domain")

    check.register(Rule("boom", "raises", explode))
    table = check.all(erp)
    assert status_of(table, "boom") == "FAIL"
    assert "ValueError" in evidence_of(table, "boom")
    assert status_of(table, "drafts") in ("pass", "FAIL")  # the others still ran


# -- the finish gate ----------------------------------------------------------
def test_finish_refuses_while_a_hard_check_fails_then_relents(erp, tmp_path):
    import os

    from lib import check, finish as finish_module
    from lib.check import Rule

    os.environ["ERP_SUMMARY_PATH"] = str(tmp_path / "summary.md")
    finish_module.SUMMARY_PATH = str(tmp_path / "summary.md")
    check.register(Rule("impossible", "always fails", lambda c: (False, "by design")))

    first = finish_module.finish("done?", client=erp)
    assert "finish refused (attempt 1/3)" in first
    assert finish_module.FINISH_SENTINEL not in first

    second = finish_module.finish("done?", client=erp)
    assert "finish refused (attempt 2/3)" in second

    third = finish_module.finish("giving up", client=erp)
    assert finish_module.FINISH_SENTINEL in third
    assert "still failing" in third
    assert (tmp_path / "summary.md").exists()
    assert "giving up" in (tmp_path / "summary.md").read_text()


def test_finish_passes_straight_through_when_checks_are_clean(erp, tmp_path):
    from lib import check, finish as finish_module

    if [r for r in check.invariants(erp).all() if r["status"] == "FAIL"]:
        pytest.skip("container is not in a clean state; run `make devbox` for a fresh one")

    finish_module.SUMMARY_PATH = str(tmp_path / "summary.md")
    result = finish_module.finish("all good", client=erp)
    assert finish_module.FINISH_SENTINEL in result
    assert "refused" not in result
    assert finish_module.refusals() == 0


def test_ungated_finish_never_refuses(erp, tmp_path):
    from lib import check, finish as finish_module
    from lib.check import Rule

    finish_module.SUMMARY_PATH = str(tmp_path / "summary.md")
    check.register(Rule("impossible", "always fails", lambda c: (False, "by design")))
    result = finish_module.finish("no gate", client=erp, gate=False)
    assert finish_module.FINISH_SENTINEL in result
    assert finish_module.refusals() == 0


# -- the checks that address the measured failure mode -------------------------
def test_a_late_receipt_is_detected(erp, offer):
    """the plan's dominant failure: a purchase that arrives after the order it feeds."""
    from lib import check

    product_id = offer["product_id"]
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]

    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id,
        "commitment_date": "2026-09-10 12:00:00",
        "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 5})],
    }])
    erp.call("sale.order", "action_confirm", [[so_id]])
    po_id = erp.create_po(offer["vendor_id"], [(product_id, max(offer["min_qty"], 5))],
                          date_planned="2026-09-25 12:00:00")     # three weeks late
    erp.confirm_po(po_id)
    try:
        table = check.invariants(erp)
        assert status_of(table, "timeline_feasible") == "FAIL"
        evidence = evidence_of(table, "timeline_feasible")
        assert "2026-09-25" in evidence and "2026-09-10" in evidence
    finally:
        erp.cancel("purchase.order", po_id)
        erp.cancel("sale.order", so_id)


def test_an_on_time_receipt_passes(erp, offer):
    from lib import check

    product_id = offer["product_id"]
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id,
        "commitment_date": "2026-10-30 12:00:00",
        "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 5})],
    }])
    erp.call("sale.order", "action_confirm", [[so_id]])
    po_id = erp.create_po(offer["vendor_id"], [(product_id, max(offer["min_qty"], 5))],
                          date_planned="2026-10-01 12:00:00")     # comfortably early
    erp.confirm_po(po_id)
    try:
        table = check.invariants(erp)
        po_name = erp.purchase_orders(ids=[po_id]).all()[0]["name"]
        # Other tests may have left late orders behind; this receipt must not be one of them.
        assert po_name not in evidence_of(table, "timeline_feasible"), evidence_of(table, "timeline_feasible")
    finally:
        erp.cancel("purchase.order", po_id)
        erp.cancel("sale.order", so_id)


def test_uncovered_demand_is_detected(erp, offer):
    """The PREMATURE_FINISH pattern: orders confirmed, nothing bought or built."""
    from lib import check

    product_id = offer["product_id"]
    free = {r["product_id"]: r["free"] for r in erp.stock([product_id])}
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]

    huge = free.get(product_id, 0) + 5000          # far beyond anything on hand or ordered
    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id,
        "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": huge})],
    }])
    erp.call("sale.order", "action_confirm", [[so_id]])
    try:
        table = check.invariants(erp)
        assert status_of(table, "demand_covered") == "FAIL"
        assert "needs" in evidence_of(table, "demand_covered")
    finally:
        erp.cancel("sale.order", so_id)


def test_an_mo_without_components_is_detected(erp):
    """An MO scheduled before its parts can exist — `mo_component_feasibility`."""
    from lib import check

    boms = erp.boms()
    if not len(boms):
        pytest.skip("this scenario has no manufacturing route")
    bom = boms.all()[0]
    finished = erp.search_read("mrp.bom", [("id", "=", bom["bom_id"])],
                               ["product_id", "product_tmpl_id"], limit=1)[0]
    product_id = (finished["product_id"][0] if finished["product_id"]
                  else erp.search_read("product.product",
                                       [("product_tmpl_id", "=", finished["product_tmpl_id"][0])],
                                       ["id"], limit=1)[0]["id"])

    # A quantity far beyond any component stock, starting tomorrow. force=True bypasses
    # the write-time guard: this test exercises the CHECK on an MO that got through.
    mo_id = erp.create_mo(product_id, 10_000, date_start="2026-09-03 08:00:00", force=True)
    erp.confirm_mo(mo_id)
    try:
        table = check.invariants(erp)
        assert status_of(table, "mo_feasible") == "FAIL"
        assert "needing" in evidence_of(table, "mo_feasible")
    finally:
        erp.cancel("mrp.production", mo_id)


def test_the_gate_now_refuses_on_a_late_receipt(erp, offer, tmp_path):
    """End to end: the failure mode that used to pass silently now blocks finish."""
    from lib import finish as finish_module

    finish_module.SUMMARY_PATH = str(tmp_path / "summary.md")
    product_id = offer["product_id"]
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id,
        "commitment_date": "2026-09-10 12:00:00",
        "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 5})],
    }])
    erp.call("sale.order", "action_confirm", [[so_id]])
    po_id = erp.create_po(offer["vendor_id"], [(product_id, max(offer["min_qty"], 5))],
                          date_planned="2026-09-25 12:00:00")
    erp.confirm_po(po_id)
    try:
        result = finish_module.finish("done", client=erp)
        assert "finish refused" in result
        assert "timeline_feasible" in result
    finally:
        erp.cancel("purchase.order", po_id)
        erp.cancel("sale.order", so_id)


def test_preexisting_failures_are_labelled_but_still_block(erp, offer, tmp_path):
    """A repair-plan task's whole job is what was already broken: label it, still refuse."""
    from lib import check, finish as finish_module
    from lib.check import Rule

    finish_module.SUMMARY_PATH = str(tmp_path / "summary.md")
    check.register(Rule("inherited", "already broken when we arrived", lambda c: (False, "seeded")))
    finish_module.set_baseline(["inherited"])
    result = finish_module.finish("done", client=erp)
    assert "finish refused" in result
    assert "already failing at task start" in result


def test_an_optimistic_planned_date_does_not_fool_the_timeline_check(erp, offer):
    """The agent can write any date_planned it likes; the vendor still needs its lead time."""
    from lib import check

    offers = [o for o in erp.suppliers().all() if (o["delay_days"] or 0) >= 3]
    if not offers:
        pytest.skip("no vendor with a lead time of 3+ days in this scenario")
    o = offers[0]
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]

    # Due tomorrow; the vendor needs delay_days; the agent writes date_planned = today.
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    due = (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    wishful = now.strftime("%Y-%m-%d %H:%M:%S")

    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id, "commitment_date": due,
        "order_line": [(0, 0, {"product_id": o["product_id"], "product_uom_qty": 5})]}])
    erp.call("sale.order", "action_confirm", [[so_id]])
    po_id = erp.create_po(o["vendor_id"], [(o["product_id"], max(o["min_qty"], 5))],
                          date_planned=wishful, force=True)   # bypass the write-time guard
    erp.confirm_po(po_id)
    try:
        table = check.invariants(erp)
        assert status_of(table, "timeline_feasible") == "FAIL"
        evidence = evidence_of(table, "timeline_feasible")
        assert "lead time" in evidence, evidence
    finally:
        erp.cancel("purchase.order", po_id)
        erp.cancel("sale.order", so_id)


def test_a_forced_early_receipt_is_refused_at_finish(erp, tmp_path):
    """The force=True hole: guard refused receive(), the model forced it, the gate said nothing."""
    from lib import check, finish as finish_module

    offers = [o for o in erp.suppliers().all() if (o["delay_days"] or 0) >= 3]
    if not offers:
        pytest.skip("no vendor with a 3+ day lead time")
    o = offers[0]
    from datetime import datetime, timedelta
    later = (datetime.utcnow() + timedelta(days=int(o["delay_days"]) + 2)).strftime("%Y-%m-%d %H:%M:%S")
    po_id = erp.create_po(o["vendor_id"], [(o["product_id"], max(o["min_qty"], 1))], date_planned=later)
    erp.confirm_po(po_id)
    erp.receive(po_id, force=True)          # the move the model made
    try:
        table = check.invariants(erp)
        assert status_of(table, "no_fabricated_receipts") == "FAIL"
        assert "cannot arrive before" in evidence_of(table, "no_fabricated_receipts")
        finish_module.SUMMARY_PATH = str(tmp_path / "summary.md")
        result = finish_module.finish("done", client=erp)
        assert "finish refused" in result and "no_fabricated_receipts" in result
    finally:
        erp.cancel("purchase.order", po_id)


def test_an_origin_naming_an_order_the_purchase_cannot_reach_is_detected(erp, offer):
    """Pass 3 of the checkpoint: every date right, origin listing an order due before the
    goods land. The benchmark's schedule rule reads origin as "the demand this feeds"."""
    from lib import check
    from lib.erp import _add_days

    if (offer["delay_days"] or 0) < 2:
        pytest.skip("need a vendor with a 2+ day lead time")
    product_id = offer["product_id"]
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    today = erp.today()
    arrival = _add_days(today, offer["delay_days"])
    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id, "commitment_date": _add_days(today, 1),   # due tomorrow
        "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 1})],
    }])
    erp.call("sale.order", "action_confirm", [[so_id]])
    so_name = erp.get("sale.order", [so_id], ["name"])[0]["name"]
    po_id = erp.create_po(offer["vendor_id"], [(product_id, max(offer["min_qty"], 1))],
                          date_planned=arrival, origin=so_name, force=True)   # bypass the guard
    erp.confirm_po(po_id)
    try:
        table = check.invariants(erp)
        assert status_of(table, "origin_consistent") == "FAIL"
        evidence = evidence_of(table, "origin_consistent")
        assert so_name in evidence and "cannot" not in evidence[:0] and "Fix" in evidence
    finally:
        erp.cancel("purchase.order", po_id)
        erp.cancel("sale.order", so_id)


def test_an_origin_naming_an_order_the_purchase_reaches_passes(erp, offer):
    from lib import check
    from lib.erp import _add_days

    product_id = offer["product_id"]
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    today = erp.today()
    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id, "commitment_date": _add_days(today, 60),
        "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 1})],
    }])
    erp.call("sale.order", "action_confirm", [[so_id]])
    so_name = erp.get("sale.order", [so_id], ["name"])[0]["name"]
    po_id = erp.create_po(offer["vendor_id"], [(product_id, max(offer["min_qty"], 1))],
                          date_planned=_add_days(today, max(offer["delay_days"] or 0, 1)),
                          origin=so_name)
    erp.confirm_po(po_id)
    try:
        table = check.invariants(erp)
        assert status_of(table, "origin_consistent") == "pass"
    finally:
        erp.cancel("purchase.order", po_id)
        erp.cancel("sale.order", so_id)


@pytest.fixture(scope="module")
def made(erp):
    """A product with a BOM that has operations (a make scenario); skip on buy-only devboxes."""
    boms = erp.search_read("mrp.bom", [("operation_ids", "!=", False)], ["product_tmpl_id"], limit=1)
    if not boms:
        pytest.skip("this devbox scenario has no BOM with operations (start one with ERP_DEV_TASK=<make task>)")
    pid = erp.search_read("product.product", [("product_tmpl_id", "=", boms[0]["product_tmpl_id"][0])],
                          ["id"], limit=1)[0]["id"]
    return pid


def test_an_mo_without_a_deadline_is_detected(erp, made):
    """Odoo leaves date_deadline empty; the grader of any plan needs it."""
    from lib import check

    mo = erp.call("mrp.production", "create", [{"product_id": made, "product_qty": 1,
                                                "date_start": "2026-10-01 08:00:00", "origin": "S00001"}])
    try:
        table = check.invariants(erp)
        assert status_of(table, "mo_schedule") == "FAIL"
        assert "no date_deadline" in evidence_of(table, "mo_schedule")
    finally:
        erp.cancel("mrp.production", mo)


def test_an_mo_created_by_the_helper_is_schedulable(erp, made):
    from lib import check

    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=1)
    partner_id = (partner or erp.search_read("res.partner", [], ["name"], limit=1))[0]["id"]
    so_id = erp.call("sale.order", "create", [{
        "partner_id": partner_id, "commitment_date": "2026-12-01 08:00:00",
        "order_line": [(0, 0, {"product_id": made, "product_uom_qty": 1})]}])
    erp.call("sale.order", "action_confirm", [[so_id]])
    so_name = erp.get("sale.order", [so_id], ["name"])[0]["name"]
    plan = erp.earliest_build(made, 1)
    mo = erp.create_mo(made, 1, date_start=plan["date_start"], origin=so_name,
                       workcenter_id=plan["workcenters"][0]["workcenter_id"], force=True)
    try:
        row = erp.get("mrp.production", [mo], ["date_deadline", "origin"])[0]
        assert row["date_deadline"][:10] == plan["date_deadline"][:10] and row["origin"] == so_name
        table = check.invariants(erp)
        assert status_of(table, "mo_schedule") == "pass"
        assert status_of(table, "origin_consistent") == "pass", evidence_of(table, "origin_consistent")
    finally:
        erp.cancel("mrp.production", mo)
        erp.cancel("sale.order", so_id)


def test_a_work_centre_booked_past_its_limit_is_detected(erp, made):
    from lib import check

    options = erp.workcenter_options(made).all()
    limited = [o for o in options if o["minutes_limit"]]
    if not limited:
        pytest.skip("no work centre states a minute limit")
    o = limited[0]
    too_many = int(o["minutes_limit"] // o["min_per_unit"]) + 5
    mo = erp.create_mo(made, too_many, date_start="2026-10-01 08:00:00",
                       workcenter_id=o["workcenter_id"], force=True)
    try:
        table = check.invariants(erp)
        assert status_of(table, "workcenter_capacity") == "FAIL"
        assert o["name"] in evidence_of(table, "workcenter_capacity")
    finally:
        erp.cancel("mrp.production", mo)


def test_component_shortage_is_simulated_in_start_order(erp, made):
    """Two MOs sharing components cannot both count the same units."""
    from lib import check

    plan = erp.earliest_build(made, 1)
    comps = [c for c in plan["components"] if c["free_now"] > 0]
    if not comps:
        pytest.skip("no component on hand to share")
    # Enough to exhaust the scarcest on-hand component twice over.
    scarce = min(comps, key=lambda c: c["free_now"] / max(c["needed"], 1e-9))
    qty = int(scarce["free_now"] / max(scarce["needed"], 1e-9)) + 1
    a = erp.create_mo(made, qty, date_start="2026-10-01 08:00:00", force=True)
    b = erp.create_mo(made, qty, date_start="2026-10-02 08:00:00", force=True)
    try:
        table = check.invariants(erp)
        assert status_of(table, "mo_feasible") == "FAIL"
        assert "on hand by then" in evidence_of(table, "mo_feasible")
    finally:
        erp.cancel("mrp.production", a)
        erp.cancel("mrp.production", b)


def test_a_supplier_offer_split_across_two_pos_is_detected(erp, offer):
    from lib import check

    lines = [(offer["product_id"], max(offer["min_qty"], 1))]
    a = erp.create_po(offer["vendor_id"], lines, date_planned="2026-12-01 08:00:00")
    erp.confirm_po(a)
    b = erp.create_po(offer["vendor_id"], lines, date_planned="2026-12-01 08:00:00", force=True)
    erp.confirm_po(b)
    try:
        table = check.invariants(erp)
        assert status_of(table, "po_consolidated") == "FAIL"
        assert "add_po_lines" in evidence_of(table, "po_consolidated")
        erp.cancel("purchase.order", b)
        assert status_of(check.invariants(erp), "po_consolidated") == "pass"
    finally:
        erp.cancel("purchase.order", a)
        erp.cancel("purchase.order", b)


def test_a_component_po_larger_than_the_mos_it_names_is_detected(erp, made):
    """Origin quantities are a flow: a PO must be absorbed by the MOs it names."""
    from lib import check

    plan = erp.earliest_build(made, 2)
    comp = offers = None
    for c in plan["components"]:
        offers = erp.suppliers([c["component_id"]]).all()
        if offers:
            comp = c
            break
    if comp is None:
        pytest.skip("no purchasable component")
    o = offers[0]
    mo = erp.create_mo(made, 2, date_start="2026-11-20 08:00:00", force=True)
    erp.confirm_mo(mo)
    mo_name = erp.get("mrp.production", [mo], ["name"])[0]["name"]
    need = comp["needed"]                       # already for the 2 units planned
    big = max(o["min_qty"] or 0, need * 2 + 50)  # far beyond the MO's need and above MOQ
    po = erp.create_po(o["vendor_id"], [(comp["component_id"], big)], date_planned="2026-11-15 08:00:00",
                       origin=mo_name, force=True)
    erp.confirm_po(po)
    try:
        table = check.invariants(erp)
        assert status_of(table, "origin_flow") == "FAIL", evidence_of(table, "origin_flow")
        assert "absorb" in evidence_of(table, "origin_flow")
    finally:
        erp.cancel("purchase.order", po)
        erp.cancel("mrp.production", mo)


def test_a_component_po_matching_the_mo_need_passes(erp, made):
    from lib import check

    plan = erp.earliest_build(made, 2)
    comp = offers = None
    for c in plan["components"]:
        offers = erp.suppliers([c["component_id"]]).all()
        if offers:
            comp = c
            break
    if comp is None:
        pytest.skip("no purchasable component")
    o = offers[0]
    mo = erp.create_mo(made, 2, date_start="2026-11-20 08:00:00", force=True)
    erp.confirm_mo(mo)
    mo_name = erp.get("mrp.production", [mo], ["name"])[0]["name"]
    qty = max(o["min_qty"] or 0, comp["needed"])   # exactly the need, or the MOQ
    po = erp.create_po(o["vendor_id"], [(comp["component_id"], qty)], date_planned="2026-11-15 08:00:00",
                       origin=mo_name, force=True)
    erp.confirm_po(po)
    try:
        table = check.invariants(erp)
        assert status_of(table, "origin_flow") == "pass", evidence_of(table, "origin_flow")
    finally:
        erp.cancel("purchase.order", po)
        erp.cancel("mrp.production", mo)


def test_a_finished_goods_po_larger_than_the_orders_it_names_is_detected(erp, offer):
    """No MOQ allowance for finished goods: 8 at a minimum of 8 naming a 6-unit order fails
    the grader; naming a second order due on or after arrival passes (verified on a dev
    grader replay)."""
    from lib import check
    from lib.erp import _add_days

    product_id = offer["product_id"]
    partner = erp.search_read("res.partner", [("customer_rank", ">", 0)], ["name"], limit=2)
    if len(partner) < 2:
        partner = erp.search_read("res.partner", [], ["name"], limit=2)
    today = erp.today()
    arrival = _add_days(today, max(offer["delay_days"] or 0, 1))
    sos = []
    for p in partner[:2]:
        so = erp.call("sale.order", "create", [{
            "partner_id": p["id"], "commitment_date": _add_days(arrival, 5),
            "order_line": [(0, 0, {"product_id": product_id, "product_uom_qty": 6})]}])
        erp.call("sale.order", "action_confirm", [[so]])
        sos.append((so, erp.get("sale.order", [so], ["name"])[0]["name"]))
    qty = max(offer["min_qty"] or 0, 8)
    po = erp.create_po(offer["vendor_id"], [(product_id, qty)], date_planned=arrival,
                       origin=sos[0][1], force=True)
    erp.confirm_po(po)
    try:
        assert status_of(check.invariants(erp), "origin_flow") == "FAIL"
        erp.call("purchase.order", "write", [[po], {"origin": f"{sos[0][1]}, {sos[1][1]}"}])
        table = check.invariants(erp)
        assert status_of(table, "origin_flow") == "pass", evidence_of(table, "origin_flow")
    finally:
        erp.cancel("purchase.order", po)
        for so, _ in sos:
            erp.cancel("sale.order", so)
