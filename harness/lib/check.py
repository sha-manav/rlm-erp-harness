"""End-state invariants derived from ERP practice, never from grader code. Hard checks
block finish; soft ones are reported. Task-specific rules are registered by the agent.
"""

from __future__ import annotations

import datetime as _dt

from dataclasses import dataclass, field
from typing import Callable

from .fmt import Table

# The module itself is bound in the namespace as `check`; EXPORTS names the extra
# symbols to bind alongside it. Listing "check" here asked for lib.check.check.
EXPORTS = ["Check", "Rule"]

# When this library loaded — after the scenario was seeded, before the agent's first write.
# Documents created earlier belong to the scenario; those created later are the agent's.
SESSION_START = _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

# Documents the agent is responsible for finishing once it has created them.
DRAFT_STATES = {
    "sale.order": ("draft", "sent"),
    "purchase.order": ("draft", "sent", "to approve"),
    "mrp.production": ("draft",),
    "account.move": ("draft",),
}


@dataclass
class Check:
    id: str
    title: str
    hard: bool
    fn: Callable[[object], tuple[bool, str]]


@dataclass
class Rule:
    """A task-specific rule the agent writes from the instruction."""

    id: str
    description: str
    fn: Callable[[object], tuple[bool, str]]
    hard: bool = True


@dataclass
class _Registry:
    rules: list[Rule] = field(default_factory=list)


_registry = _Registry()


# -- invariants ---------------------------------------------------------------
def _no_dangling_drafts(client) -> tuple[bool, str]:
    """Appendix B.1 (hard) — nothing the agent created is left unconfirmed."""
    stragglers = []
    for model, states in DRAFT_STATES.items():
        rows = client.search_read(model, [("state", "in", list(states))], ["name", "state"], limit=100)
        stragglers += [f"{model} {r.get('name') or r['id']} ({r['state']})" for r in rows]
    if not stragglers:
        return True, "no draft sale/purchase/manufacturing/invoice records remain"
    shown = ", ".join(stragglers[:8])
    more = f" (+{len(stragglers) - 8} more)" if len(stragglers) > 8 else ""
    return False, f"{len(stragglers)} draft record(s): {shown}{more}"


def _stock_non_negative(client) -> tuple[bool, str]:
    """Appendix B.3 (hard) — no internal location holds a negative quantity."""
    rows = client.search_read(
        "stock.quant",
        [("location_id.usage", "=", "internal"), ("quantity", "<", 0)],
        ["product_id", "location_id", "quantity"], limit=50)
    if not rows:
        return True, "every internal location is non-negative"
    worst = ", ".join(
        f"{r['product_id'][1] if r['product_id'] else '?'} "
        f"@{r['location_id'][1] if r['location_id'] else '?'} = {r['quantity']}"
        for r in rows[:5])
    return False, f"{len(rows)} negative quant(s): {worst}"


def _supplier_validity(client) -> tuple[bool, str]:
    """Appendix B.4 (hard) — every PO line names a vendor who actually sells that product, at
    or above the vendor's minimum quantity, at that vendor's price for the tier.
    """
    orders = client.search_read(
        "purchase.order", [("state", "not in", ("cancel",))],
        ["name", "partner_id", "create_date"], limit=200)
    if not orders:
        return True, "no purchase orders to validate"
    by_id = {o["id"]: o for o in orders}
    lines = client.search_read(
        "purchase.order.line", [("order_id", "in", list(by_id))],
        ["order_id", "product_id", "product_qty", "price_unit"], limit=500)

    problems = []
    for line in lines:
        order = by_id.get(line["order_id"][0] if line["order_id"] else None)
        if not order or not order["partner_id"] or not line["product_id"]:
            continue
        vendor_id, vendor_name = order["partner_id"]
        product_id, product_name = line["product_id"]
        template = client.search_read(
            "product.product", [("id", "=", product_id)], ["product_tmpl_id"], limit=1)
        template_id = template[0]["product_tmpl_id"][0] if template else None
        offers = client.search_read(
            "product.supplierinfo",
            ["&", ("partner_id", "=", vendor_id),
             "|", ("product_id", "=", product_id), ("product_tmpl_id", "=", template_id)],
            ["min_qty", "price", "create_date"], limit=50, order="min_qty desc")
        # Confirming a PO makes Odoo add the vendor as a supplier, so offers created at or after the order are not evidence.
        offers = [
            offer for offer in offers
            if not (offer.get("create_date") and order.get("create_date")
                    and offer["create_date"] >= order["create_date"])
        ]
        if not offers:
            problems.append(f"{order['name']}: {vendor_name} does not list {product_name}")
            continue
        tiers = sorted(offers, key=lambda o: o["min_qty"] or 0)
        smallest = tiers[0]["min_qty"] or 0
        if line["product_qty"] < smallest:
            problems.append(
                f"{order['name']}: {line['product_qty']:g} of {product_name} is below "
                f"{vendor_name}'s minimum {smallest:g}")
            continue
        applicable = [o for o in tiers if line["product_qty"] >= (o["min_qty"] or 0)]
        expected = applicable[-1]["price"] if applicable else tiers[0]["price"]
        if abs((line["price_unit"] or 0) - expected) > 0.011:
            problems.append(
                f"{order['name']}: {product_name} priced {line['price_unit']:.2f}, "
                f"{vendor_name}'s tier price is {expected:.2f}")

    if not problems:
        return True, f"{len(lines)} purchase line(s) match a vendor offer and its tier"
    shown = "; ".join(problems[:5])
    more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
    return False, f"{len(problems)} problem(s): {shown}{more}"


def _invoicing_complete(client) -> tuple[bool, str]:
    """Appendix B.7 (hard) — delivered orders are invoiced and the invoices are posted."""
    uninvoiced = client.search_read(
        "sale.order",
        [("state", "=", "sale"), ("invoice_status", "=", "to invoice"),
         ("delivery_status", "in", ("full", "partial"))],
        ["name", "delivery_status"], limit=50)
    drafts = client.search_read(
        "account.move", [("move_type", "=", "out_invoice"), ("state", "=", "draft")],
        ["name", "invoice_origin"], limit=50)

    problems = [f"{o['name']} delivered ({o['delivery_status']}) but not invoiced" for o in uninvoiced]
    problems += [f"invoice for {m.get('invoice_origin') or m['id']} still draft" for m in drafts]
    if not problems:
        return True, "every delivered order is invoiced and every invoice is posted"
    shown = "; ".join(problems[:6])
    more = f" (+{len(problems) - 6} more)" if len(problems) > 6 else ""
    return False, f"{len(problems)} problem(s): {shown}{more}"



def _add_days(stamp: str, days: float) -> str:
    """'YYYY-MM-DD HH:MM:SS' + days, as the same kind of string."""
    from datetime import datetime, timedelta

    base = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    return (base + timedelta(days=float(days))).strftime("%Y-%m-%d %H:%M:%S")


def _vendor_delay(client, vendor_id, product_id):
    """The vendor's lead time in days for this product, or None if it does not list it."""
    if not vendor_id or not product_id:
        return None
    template = client.search_read(
        "product.product", [("id", "=", product_id)], ["product_tmpl_id"], limit=1)
    template_id = template[0]["product_tmpl_id"][0] if template else None
    offers = client.search_read(
        "product.supplierinfo",
        ["&", ("partner_id", "=", vendor_id),
         "|", ("product_id", "=", product_id), ("product_tmpl_id", "=", template_id)],
        ["delay"], limit=10)
    if not offers:
        return None
    return max(o["delay"] or 0 for o in offers)


def _timeline_feasible(client) -> tuple[bool, str]:
    """Appendix B.5 (hard) — every receipt lands before the demand it feeds."""
    orders = client.search_read(
        "sale.order", [("state", "=", "sale"), ("commitment_date", "!=", False)],
        ["name", "commitment_date", "order_line"], limit=200)
    if not orders:
        return True, "no confirmed customer orders with a commitment date"

    # Latest date by which each product must be on hand.
    need_by: dict[int, str] = {}
    lines = client.search_read(
        "sale.order.line", [("order_id", "in", [o["id"] for o in orders])],
        ["order_id", "product_id", "product_uom_qty", "qty_delivered"], limit=500)
    due = {o["id"]: o["commitment_date"] for o in orders}
    for line in lines:
        if not line["product_id"]:
            continue
        if (line["product_uom_qty"] or 0) <= (line["qty_delivered"] or 0):
            continue                      # already shipped; its date no longer constrains
        product = line["product_id"][0]
        date = due.get(line["order_id"][0] if line["order_id"] else None)
        if date and (product not in need_by or date < need_by[product]):
            need_by[product] = date       # earliest outstanding demand binds

    if not need_by:
        return True, "every confirmed order line is already delivered"

    late = []
    po_lines = client.search_read(
        "purchase.order.line",
        [("product_id", "in", list(need_by)), ("state", "not in", ("cancel",))],
        ["order_id", "product_id", "date_planned", "product_qty", "qty_received"], limit=500)
    order_ids = sorted({l["order_id"][0] for l in po_lines if l["order_id"]})
    orders = {o["id"]: o for o in client.search_read(
        "purchase.order", [("id", "in", order_ids)], ["name", "partner_id", "date_order"],
        limit=500)} if order_ids else {}
    for line in po_lines:
        # Received lines are not exempt: receiving early does not make goods arrive early.
        product_id, product_name = line["product_id"]
        order = orders.get(line["order_id"][0]) if line["order_id"] else None
        needed = need_by.get(product_id)
        if not order or not needed:
            continue
        # Arrival is the later of date_planned and order date + lead time; Odoo stores whatever date it is given.
        arrives = line["date_planned"] or ""
        delay = _vendor_delay(client, order["partner_id"][0] if order["partner_id"] else None,
                              product_id)
        if order.get("date_order") and delay is not None:
            earliest = _add_days(order["date_order"], delay)
            if earliest > arrives:
                arrives = earliest
        if arrives and arrives[:10] > needed[:10]:
            reason = (f"planned {line['date_planned'][:10]}, vendor lead time {delay}d from "
                      f"{order['date_order'][:10]} makes it {arrives[:10]}"
                      if delay is not None and arrives != (line["date_planned"] or "")
                      else f"arrives {arrives[:10]}")
            late.append(f"{order['name']}: {product_name} {reason}, needed {needed[:10]}")

    if late:
        shown = "; ".join(late[:5])
        more = f" (+{len(late) - 5} more)" if len(late) > 5 else ""
        return False, (
            f"{len(late)} receipt(s) land after the demand they feed: {shown}{more}. "
            "Fix: erp.feasible_vendors(product_id, qty, need_by) lists only vendors who can "
            "land it in time, with the arrival date to use as date_planned; if it is empty, "
            "cover the demand from stock or manufacturing, or state in the summary that no "
            "vendor can make the date. Do not receive goods before they can arrive.")
    return True, f"every outstanding receipt lands on or before its need date ({len(need_by)} product(s))"


def _mo_feasible(client) -> tuple[bool, str]:
    """Appendix B.6 (hard) — every MO's components are on hand when it starts."""
    productions = client.search_read(
        "mrp.production", [("state", "not in", ("done", "cancel"))],
        ["name", "product_id", "product_qty", "date_start", "date_deadline", "date_finished",
         "bom_id"], limit=100)
    if not productions:
        return True, "no unfinished manufacturing orders"

    stock: dict[int, float] = {}
    for quant in client.search_read(
            "stock.quant", [("location_id.usage", "=", "internal")],
            ["product_id", "quantity"], limit=2000):
        if quant["product_id"]:
            stock[quant["product_id"][0]] = stock.get(quant["product_id"][0], 0.0) + (quant["quantity"] or 0)

    # Outstanding receipts, dated honestly.
    arrivals: dict[int, list[tuple[str, float, str]]] = {}
    po_lines = client.search_read(
        "purchase.order.line", [("state", "in", ("purchase", "done"))],
        ["product_id", "product_qty", "qty_received", "date_planned", "order_id"], limit=500)
    order_ids = sorted({l["order_id"][0] for l in po_lines if l["order_id"]})
    orders = {o["id"]: o for o in client.search_read(
        "purchase.order", [("id", "in", order_ids)], ["name", "partner_id", "date_order"],
        limit=500)} if order_ids else {}
    for line in po_lines:
        outstanding = (line["product_qty"] or 0) - (line["qty_received"] or 0)
        if outstanding <= 0 or not line["product_id"] or not line["order_id"]:
            continue
        order = orders.get(line["order_id"][0])
        arrives = line["date_planned"] or ""
        if order and order.get("date_order") and order.get("partner_id"):
            delay = _vendor_delay(client, order["partner_id"][0], line["product_id"][0])
            if delay is not None:
                arrives = max(arrives, _add_days(order["date_order"], delay))
        arrivals.setdefault(line["product_id"][0], []).append(
            (arrives[:10], outstanding, order["name"] if order else "PO"))
    for lst in arrivals.values():
        lst.sort()

    def bring_in(product_id: int, by: str) -> None:
        pending = arrivals.get(product_id) or []
        while pending and pending[0][0] <= by:
            stock[product_id] = stock.get(product_id, 0.0) + pending.pop(0)[1]

    problems = []
    for mo in sorted(productions, key=lambda m: (m["date_start"] or "9999", m["id"])):
        start = mo.get("date_start")
        if not start or not mo.get("bom_id"):
            continue                      # mo_schedule reports a missing start
        bom = client.search_read("mrp.bom", [("id", "=", mo["bom_id"][0])],
                                 ["product_qty", "bom_line_ids"], limit=1)
        if not bom or not bom[0].get("bom_line_ids"):
            continue
        multiplier = (mo["product_qty"] or 0) / (bom[0]["product_qty"] or 1.0)
        for component in client.call("mrp.bom.line", "read", [bom[0]["bom_line_ids"]],
                                     {"fields": ["product_id", "product_qty"]}):
            if not component["product_id"]:
                continue
            cid, cname = component["product_id"]
            required = (component["product_qty"] or 0) * multiplier
            bring_in(cid, start[:10])
            have = stock.get(cid, 0.0)
            if have + 1e-6 < required:
                pending = arrivals.get(cid) or []
                nxt = (f"; next {pending[0][1]:g} arrive {pending[0][0]} on {pending[0][2]}"
                       if pending else "; nothing more on order")
                problems.append(f"{mo['name']} starts {start[:10]} needing {required:g} {cname}, "
                                f"only {have:g} on hand by then{nxt}")
                stock[cid] = 0.0
            else:
                stock[cid] = have - required
        finish = mo.get("date_deadline") or mo.get("date_finished") or start
        if mo["product_id"]:
            arrivals.setdefault(mo["product_id"][0], []).append(
                (finish[:10], mo["product_qty"] or 0, mo["name"]))
            arrivals[mo["product_id"][0]].sort()

    if problems:
        shown = "; ".join(problems[:5])
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        return False, (
            f"{len(problems)} manufacturing order(s) start before their components exist: {shown}{more}. "
            "Fix: erp.earliest_build(product_id, qty) gives the start the components allow, "
            "crediting purchases that land in time; order the shortfall first (origin = the MO "
            "reference, date_planned on or before its date_start) or start the MO later.")
    return True, f"every unfinished MO has its components on hand at start ({len(productions)})"


def _demand_covered(client) -> tuple[bool, str]:
    """Appendix B.2 (hard) — every confirmed order line has supply behind it."""
    lines = client.search_read(
        "sale.order.line", [("order_id.state", "=", "sale")],
        ["order_id", "product_id", "product_uom_qty", "qty_delivered"], limit=500)
    if not lines:
        return True, "no confirmed customer order lines"

    shortfalls = []
    for line in lines:
        if not line["product_id"]:
            continue
        outstanding = (line["product_uom_qty"] or 0) - (line["qty_delivered"] or 0)
        if outstanding <= 0:
            continue
        product_id, product_name = line["product_id"]
        free = sum(
            (q["quantity"] or 0) - (q["reserved_quantity"] or 0)
            for q in client.search_read(
                "stock.quant",
                [("product_id", "=", product_id), ("location_id.usage", "=", "internal")],
                ["quantity", "reserved_quantity"], limit=100))
        incoming = sum(
            (p["product_qty"] or 0) - (p["qty_received"] or 0)
            for p in client.search_read(
                "purchase.order.line",
                [("product_id", "=", product_id), ("state", "not in", ("cancel",))],
                ["product_qty", "qty_received"], limit=100))
        building = sum(
            m["product_qty"] or 0
            for m in client.search_read(
                "mrp.production",
                [("product_id", "=", product_id), ("state", "not in", ("done", "cancel"))],
                ["product_qty"], limit=100))
        if free + incoming + building + 1e-6 < outstanding:
            shortfalls.append(
                f"{line['order_id'][1] if line['order_id'] else '?'}: {product_name} needs "
                f"{outstanding:g}, have {free:g} free + {incoming:g} incoming + {building:g} building")

    if shortfalls:
        shown = "; ".join(shortfalls[:5])
        more = f" (+{len(shortfalls) - 5} more)" if len(shortfalls) > 5 else ""
        return False, f"{len(shortfalls)} uncovered demand line(s): {shown}{more}"
    return True, f"every confirmed order line has supply behind it ({len(lines)} line(s))"


def _no_fabricated_receipts(client) -> tuple[bool, str]:
    """Appendix B.5, second half (hard) — nothing was received before it could arrive."""
    done = client.search_read(
        "stock.picking",
        [("picking_type_id.code", "=", "incoming"), ("state", "=", "done")],
        ["name", "date_done", "purchase_id", "origin"], limit=200)
    if not done:
        return True, "no validated receipts"
    problems = []
    for picking in done:
        po_id = picking["purchase_id"][0] if picking.get("purchase_id") else None
        if not po_id:
            continue
        order = client.search_read(
            "purchase.order", [("id", "=", po_id)],
            ["name", "partner_id", "date_order", "state"], limit=1)
        if not order or not order[0].get("date_order") or not order[0]["partner_id"]:
            continue
        order = order[0]
        if order.get("state") == "cancel":
            continue            # a cancelled order's receipt is not part of any plan
        lines = client.search_read(
            "purchase.order.line", [("order_id", "=", po_id)], ["product_id"], limit=50)
        for line in lines:
            if not line["product_id"]:
                continue
            delay = _vendor_delay(client, order["partner_id"][0], line["product_id"][0])
            if delay is None or delay <= 0:
                continue
            earliest = _add_days(order["date_order"], delay)
            received = (picking.get("date_done") or "")[:19]
            if received and received[:10] < earliest[:10]:
                problems.append(
                    f"{picking['name']} ({order['name']}): {line['product_id'][1]} received "
                    f"{received[:10]}, vendor lead time {delay}d from {order['date_order'][:10]} "
                    f"means it cannot arrive before {earliest[:10]}")
                break
    if problems:
        shown = "; ".join(problems[:4])
        more = f" (+{len(problems) - 4} more)" if len(problems) > 4 else ""
        return False, (f"{len(problems)} receipt(s) validated before the goods could arrive: "
                       f"{shown}{more}. This is fabricated stock. Cancel the receipt (leave the "
                       "PO confirmed) unless the task explicitly says the goods are already here.")
    return True, f"every validated receipt is on or after its vendor's earliest arrival ({len(done)})"


def _origin_tokens(origin: str) -> list[str]:
    return [t.strip() for t in (origin or "").split(",") if t.strip()]


def _origin_consistent(client) -> tuple[bool, str]:
    """Appendix B.5, third part (hard) — every document's origin names only demand it feeds."""
    docs: list[tuple[str, str, str, str, bool]] = []    # (name, kind, origin, arrives, ours)
    for order in client.search_read(
            "purchase.order", [("state", "in", ("purchase", "done"))],
            ["name", "origin", "partner_id", "date_order", "date_planned", "create_date"], limit=200):
        arrives = order.get("date_planned") or ""
        if order.get("date_order") and order.get("partner_id"):
            lines = client.search_read(
                "purchase.order.line", [("order_id", "=", order["id"])], ["product_id"], limit=50)
            delays = [_vendor_delay(client, order["partner_id"][0], l["product_id"][0])
                      for l in lines if l["product_id"]]
            delays = [d for d in delays if d is not None]
            if delays:
                earliest = _add_days(order["date_order"], max(delays))
                if earliest > arrives:
                    arrives = earliest
        docs.append((order["name"], "PO", order.get("origin") or "", arrives,
                     (order.get("create_date") or "") >= SESSION_START))
    for mo in client.search_read(
            "mrp.production", [("state", "not in", ("cancel",))],
            ["name", "origin", "date_deadline", "date_finished", "date_start", "create_date",
             "state"], limit=200):
        finish = mo.get("date_deadline") or mo.get("date_finished") or mo.get("date_start") or ""
        docs.append((mo["name"], "MO", mo.get("origin") or "", finish,
                     (mo.get("create_date") or "") >= SESSION_START and mo.get("state") != "done"))
    if not docs:
        return True, "no confirmed purchase or manufacturing orders"

    tokens = sorted({t for _, _, origin, _, _ in docs for t in _origin_tokens(origin)})
    need: dict[str, str | None] = {}
    if tokens:
        for so in client.search_read(
                "sale.order", [("name", "in", tokens), ("state", "not in", ("cancel",))],
                ["name", "commitment_date"], limit=200):
            need[so["name"]] = so.get("commitment_date") or None
        for mo in client.search_read(
                "mrp.production", [("name", "in", tokens), ("state", "not in", ("cancel",))],
                ["name", "date_start"], limit=200):
            need.setdefault(mo["name"], mo.get("date_start") or None)

    problems, fixes = [], []
    for name, kind, origin, arrives, ours in docs:
        toks = _origin_tokens(origin)
        if not toks:
            # Seeded documents never carry an origin and are not ours to edit; only a
            # document created in this session (after the library loaded) is judged.
            if ours:
                problems.append(f"{name} has an empty origin")
            continue
        unknown = [t for t in toks if t not in need]
        late = [t for t in toks if t in need and need[t] and arrives and need[t][:10] < arrives[:10]]
        ok = [t for t in toks if t in need and t not in late]
        if unknown:
            problems.append(f"{name} names {', '.join(unknown[:4])}, which is not a sales or "
                            "manufacturing order reference")
        if late:
            due = ", ".join(f"{t} (needs it by {need[t][:10]})" for t in late[:4])
            verb = "lands" if kind == "PO" else "finishes"
            problems.append(f"{name} {verb} {arrives[:10]} but its origin names {due}")
            fixes.append(f"{name}: origin = {', '.join(ok) if ok else '<the orders it does reach in time>'!r}")
    if problems:
        shown = "; ".join(problems[:4])
        more = f" (+{len(problems) - 4} more)" if len(problems) > 4 else ""
        hint = (" Fix: " + "; ".join(fixes[:3]) + ".") if fixes else ""
        return False, (
            f"{len(problems)} document(s) whose origin misstates what they feed: {shown}{more}. "
            "origin lists exactly the orders whose need date this document meets — a finished-"
            "goods PO or MO names sales orders, a component PO names the MOs it supplies — "
            "comma-separated exact references, nothing else." + hint +
            " To feed the others too, buy from a vendor who lands in time (erp.feasible_vendors) "
            "or start earlier (erp.earliest_build).")
    return True, f"every purchase and manufacturing order names demand it reaches in time ({len(docs)})"


def _max_flow(supplies: list[tuple[str, float, list[str]]], demand: dict[str, float]) -> dict[str, float]:
    """Units of each supply that its named demands can absorb (Edmonds-Karp on a tiny
    bipartite graph). Returns {supply_id: absorbed}."""
    from collections import deque

    src, sink = "__s__", "__t__"
    cap: dict[str, dict[str, float]] = {}

    def add(u, v, c):
        cap.setdefault(u, {})[v] = cap.get(u, {}).get(v, 0.0) + c
        cap.setdefault(v, {}).setdefault(u, 0.0)

    for sid, qty, refs in supplies:
        add(src, sid, qty)
        for ref in refs:
            add(sid, ref, qty)
    for ref, qty in demand.items():
        add(ref, sink, qty)
    absorbed = {sid: 0.0 for sid, _, _ in supplies}
    while True:
        parent = {src: None}
        queue = deque([src])
        while queue and sink not in parent:
            u = queue.popleft()
            for v, c in cap.get(u, {}).items():
                if c > 1e-9 and v not in parent:
                    parent[v] = u
                    queue.append(v)
        if sink not in parent:
            break
        path, v = [], sink
        while parent[v] is not None:
            path.append((parent[v], v))
            v = parent[v]
        bottleneck = min(cap[u][v] for u, v in path)
        for u, v in path:
            cap[u][v] -= bottleneck
            cap[v][u] += bottleneck
        absorbed[path[-1][1]] += bottleneck
    return absorbed


def _origin_flow(client) -> tuple[bool, str]:
    """Hard — every purchase or manufacturing order's quantity is absorbed by the demand its
    origin names.
    """
    # Demand: what each SO needs of each product, and what each open MO needs of each
    # component (its raw moves).
    so_need: dict[tuple[str, int], float] = {}
    for line in client.search_read(
            "sale.order.line", [("order_id.state", "in", ("sale", "done"))],
            ["order_id", "product_id", "product_uom_qty"], limit=500):
        if line["product_id"] and line["order_id"]:
            key = (line["order_id"][1], line["product_id"][0])
            so_need[key] = so_need.get(key, 0.0) + (line["product_uom_qty"] or 0)
    mo_rows = client.search_read(
        "mrp.production", [("state", "not in", ("cancel",))],
        ["name", "product_id", "product_qty", "origin", "move_raw_ids"], limit=200)
    mo_need: dict[tuple[str, int], float] = {}
    raw_ids = [m for mo in mo_rows for m in (mo.get("move_raw_ids") or [])]
    if raw_ids:
        for mv in client.call("stock.move", "read", [raw_ids],
                              {"fields": ["raw_material_production_id", "product_id", "product_uom_qty", "state"]}):
            if mv.get("state") == "cancel" or not mv.get("product_id") or not mv.get("raw_material_production_id"):
                continue
            key = (mv["raw_material_production_id"][1], mv["product_id"][0])
            mo_need[key] = mo_need.get(key, 0.0) + (mv["product_uom_qty"] or 0)

    # Supplies: confirmed PO lines and open MOs, grouped by product.
    supplies: dict[int, list[tuple[str, float, list[str], float | None]]] = {}
    for line in client.search_read(
            "purchase.order.line", [("order_id.state", "in", ("purchase", "done"))],
            ["order_id", "partner_id", "product_id", "product_qty"], limit=500):
        if not line["product_id"] or not line["order_id"]:
            continue
        po = client.search_read("purchase.order", [("id", "=", line["order_id"][0])], ["origin"], limit=1)
        tokens = _origin_tokens((po[0].get("origin") or "") if po else "")
        moq = None
        if line.get("partner_id"):
            offers = client._offers_for(line["partner_id"][0], line["product_id"][0], ["min_qty"])
            tiers = [(o["min_qty"] or 0) for o in offers if (o["min_qty"] or 0) <= (line["product_qty"] or 0) + 0.01]
            moq = max(tiers) if tiers else None          # the tier that applies to this quantity
        supplies.setdefault(line["product_id"][0], []).append(
            (line["order_id"][1], line["product_qty"] or 0, tokens, moq))
    for mo in mo_rows:
        if mo["product_id"]:
            supplies.setdefault(mo["product_id"][0], []).append(
                (mo["name"], mo["product_qty"] or 0, _origin_tokens(mo.get("origin") or ""), None))

    problems = []
    for product_id, rows in supplies.items():
        rows = [r for r in rows if r[2]]          # origin_consistent reports empty origins
        if not rows:
            continue
        demand: dict[str, float] = {}
        for ref in {t for r in rows for t in r[2]}:
            demand[ref] = so_need.get((ref, product_id), 0.0) + mo_need.get((ref, product_id), 0.0)
        # Each supply must place at least one unit on every order it names; the rest flows freely.
        flow_rows = []
        mo_names = {mo["name"] for mo in mo_rows}
        for name, qty, tokens, moq in rows:
            capacity = sum(demand.get(t, 0.0) for t in tokens)
            must_place = qty
            # MOQ excess is allowed on component purchases only; finished goods must be fully absorbed. (`all` here is check.all, not the builtin.)
            component_supply = bool(tokens) and set(tokens) <= mo_names
            if component_supply and capacity + 1e-6 < qty and moq is not None and abs(qty - moq) <= 0.01:
                must_place = capacity
            flow_rows.append((name, must_place, tokens, qty, capacity))
        absorbed = _max_flow([(n, m, t) for n, m, t, _, _ in flow_rows], demand)
        for name, must_place, tokens, qty, capacity in flow_rows:
            zero = [t for t in tokens if demand.get(t, 0.0) < 1 - 1e-6]
            if zero:
                problems.append(f"{name} names {', '.join(zero[:3])}, which need none of this product")
                continue
            if absorbed.get(name, 0.0) + 1e-6 < must_place:
                problems.append(
                    f"{name} supplies {qty:g} but the orders it names can absorb only "
                    f"{absorbed.get(name, 0.0):g} more (they need {capacity:g} in total, shared "
                    "with other supplies)")
    if problems:
        shown = "; ".join(problems[:4])
        more = f" (+{len(problems) - 4} more)" if len(problems) > 4 else ""
        return False, (
            f"{len(problems)} order(s) whose quantity does not trace to the demand they name: {shown}{more}. "
            "Buy or make what the named orders need (erp.earliest_build / erp.cheapest_buy give the "
            "exact shortfall) and name every order the quantity is for. A finished-goods purchase "
            "must be absorbed entirely by the sales orders it names — when the vendor's minimum "
            "exceeds the shortfall, add orders due on or after its arrival (orders delivered from "
            "stock still count). Only a component purchase at the vendor's minimum may exceed the "
            "MOs it names.")
    return True, f"every purchase and manufacturing quantity traces to the demand it names ({sum(len(r) for r in supplies.values())})"


def _mo_schedule(client) -> tuple[bool, str]:
    """Appendix B.6, first half (hard) — an MO carries the dates and the work centre a plan
    needs.
    """
    productions = client.search_read(
        "mrp.production", [("state", "not in", ("done", "cancel"))],
        ["name", "date_start", "date_deadline", "bom_id"], limit=100)
    if not productions:
        return True, "no unfinished manufacturing orders"
    problems = []
    for mo in productions:
        if not mo.get("date_start"):
            problems.append(f"{mo['name']}: no date_start")
            continue
        lead, operations = 0, []
        if mo.get("bom_id"):
            bom = client.search_read("mrp.bom", [("id", "=", mo["bom_id"][0])],
                                     ["produce_delay", "operation_ids"], limit=1)
            if bom:
                lead = bom[0].get("produce_delay") or 0
                operations = bom[0].get("operation_ids") or []
        earliest = _add_days(mo["date_start"], lead)
        if not mo.get("date_deadline"):
            problems.append(f"{mo['name']}: no date_deadline (Odoo leaves it empty); "
                            f"earliest honest finish is {earliest[:10]}")
        elif mo["date_deadline"][:10] < earliest[:10]:
            problems.append(f"{mo['name']}: date_deadline {mo['date_deadline'][:10]} is before "
                            f"date_start {mo['date_start'][:10]} + {lead}d lead time = {earliest[:10]}")
        workorders = client.search_read(
            "mrp.workorder", [("production_id", "=", mo["id"]), ("state", "!=", "cancel")],
            ["name", "workcenter_id"], limit=20)
        if operations and not workorders:
            problems.append(f"{mo['name']}: its BOM has operations but the MO has no work order")
        for wo in workorders:
            if not wo.get("workcenter_id"):
                problems.append(f"{mo['name']}: work order {wo['name']!r} has no work centre")
    if problems:
        shown = "; ".join(problems[:5])
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        return False, (
            f"{len(problems)} manufacturing order(s) not schedulable: {shown}{more}. Fix: "
            "erp.create_mo(product_id, qty, date_start=..., date_deadline=..., origin=..., "
            "workcenter_id=...) writes all four (date_deadline defaults to start + lead time); "
            "erp.earliest_build(product_id, qty) gives the dates; for an existing MO write "
            "date_deadline / the work order's workcenter_id.")
    return True, f"every unfinished MO has a start, a feasible due date and a work centre ({len(productions)})"


def _workcenter_capacity(client) -> tuple[bool, str]:
    """Hard — no work centre is booked past the minute limit its Internal Notes state."""
    from .erp import _minutes_limit

    try:
        centres, _operations, committed = client._workcenter_load()
    except Exception as exc:
        if "not exist" in str(exc) or "Object" in str(exc):
            return True, "no work centres"
        raise
    problems, judged = [], 0
    for c in centres:
        limit = _minutes_limit(c.get("note") or "")
        if limit is None:
            continue
        judged += 1
        used = committed.get(c["id"], 0.0)
        if used > limit + 0.01:
            problems.append(f"{c['name']}: {used:.0f} min booked vs a {limit:.0f} min limit "
                            f"(over by {used - limit:.0f})")
    if problems:
        return False, (
            f"{len(problems)} work centre(s) over their stated limit: {'; '.join(problems[:4])}. "
            "Fix: erp.workcenter_options(product_id) shows each centre's minutes free and "
            "units that fit; split the quantity into MOs on different centres "
            "(workcenter_id=...), or cover the remainder by purchase.")
    return True, (f"every work centre with a stated limit is within it ({judged})" if judged
                  else "no work centre states a minute limit")


def _so_invoiced(client) -> tuple[bool, str]:
    """Soft — confirmed sales orders with no posted invoice, as a reminder."""
    orders = client.search_read(
        "sale.order", [("state", "in", ("sale", "done"))], ["name", "invoice_ids", "invoice_status"],
        limit=200)
    if not orders:
        return True, "no confirmed sales orders"
    inv_ids = sorted({i for o in orders for i in (o.get("invoice_ids") or [])})
    posted = {m["id"] for m in client.search_read(
        "account.move", [("id", "in", inv_ids), ("state", "=", "posted")], ["id"], limit=500)} if inv_ids else set()
    missing = [o["name"] for o in orders if not any(i in posted for i in (o.get("invoice_ids") or []))]
    if missing:
        return False, (f"{len(missing)} confirmed order(s) without a posted invoice: {', '.join(missing[:8])}"
                       f"{' …' if len(missing) > 8 else ''}. Fine under a deliver-then-invoice policy; if "
                       "the task says to invoice after confirming (or names payment terms / a down "
                       "payment), apply it to every retained order: erp.invoice(so_id, "
                       "payment_term=...)")
    return True, f"every confirmed sales order has a posted invoice ({len(orders)})"


def _po_consolidated(client) -> tuple[bool, str]:
    """Hard — one purchase order per supplier offer (vendor × product)."""
    lines = client.search_read(
        "purchase.order.line",
        [("order_id.state", "in", ("purchase", "done"))],      # drafts are the drafts check's
        ["order_id", "partner_id", "product_id"], limit=500)
    offers: dict[tuple, set] = {}
    names: dict[int, str] = {}
    for line in lines:
        if not line["product_id"] or not line["partner_id"] or not line["order_id"]:
            continue
        key = (line["partner_id"][0], line["product_id"][0])
        offers.setdefault(key, set()).add(line["order_id"][0])
        names[line["order_id"][0]] = line["order_id"][1]
        names[key] = f"{line['partner_id'][1]} / {line['product_id'][1]}"
    split = {key: pos for key, pos in offers.items() if len(pos) > 1}
    if split:
        shown = "; ".join(f"{names[key]}: {', '.join(sorted(names[p] for p in pos))}"
                          for key, pos in list(split.items())[:4])
        more = f" (+{len(split) - 4} more)" if len(split) > 4 else ""
        return False, (f"{len(split)} supplier offer(s) split across purchase orders: {shown}{more}. "
                       "One PO per vendor and product: move the lines onto one order "
                       "(erp.add_po_lines(po_id, lines)) and cancel the other.")
    return True, f"every supplier offer sits on a single purchase order ({len(offers)})"


def _so_has_commitment_date(client) -> tuple[bool, str]:
    """Soft — every confirmed customer order has a commitment date."""
    rows = client.search_read(
        "sale.order", [("state", "=", "sale")], ["name", "commitment_date"], limit=200)
    missing = [r["name"] for r in rows if not r.get("commitment_date")]
    if missing:
        return False, (f"{len(missing)} confirmed SO(s) without a commitment_date: "
                       f"{', '.join(missing[:6])}. Set it to the customer's due date.")
    return True, f"every confirmed sales order has a commitment date ({len(rows)})"


INVARIANTS: list[Check] = [
    Check("drafts", "no dangling draft documents", True, _no_dangling_drafts),
    Check("stock_non_negative", "stock is non-negative everywhere", True, _stock_non_negative),
    Check("supplier_validity", "PO lines match a vendor offer, MOQ and tier price", True,
          _supplier_validity),
    Check("invoicing", "delivered orders invoiced, invoices posted", True, _invoicing_complete),
    # The three that address 74% of the stock harness's coded failures. Without them the
    # gate refused nothing: 19 finish calls, 0 refusals on the first C_full dev40 run.
    Check("demand_covered", "every confirmed order line has supply behind it", True,
          _demand_covered),
    Check("timeline_feasible", "receipts land before the demand they feed", True,
          _timeline_feasible),
    Check("mo_feasible", "MO components are on hand when it starts", True, _mo_feasible),
    Check("no_fabricated_receipts", "nothing received before it could arrive", True,
          _no_fabricated_receipts),
    # Origin and MO-schedule checks: a document must be able to feed the orders it names, and an MO needs a deadline and a work centre.
    Check("origin_consistent", "each PO/MO origin names only demand it reaches in time", True,
          _origin_consistent),
    Check("origin_flow", "each PO/MO quantity is absorbed by the demand it names", True, _origin_flow),
    Check("mo_schedule", "every MO has a start, a feasible due date and a work centre", True,
          _mo_schedule),
    Check("workcenter_capacity", "no work centre is booked past its stated limit", True,
          _workcenter_capacity),
    Check("so_has_commitment_date", "every confirmed sales order has a commitment date", True,
          _so_has_commitment_date),
    Check("po_consolidated", "one purchase order per supplier offer", True, _po_consolidated),
    Check("so_invoiced", "confirmed orders carry a posted invoice (policy-dependent)", False, _so_invoiced),
]


# -- runner -------------------------------------------------------------------
def _client(client=None):
    if client is not None:
        return client
    from .erp import erp  # imported lazily so `check` works without a live connection

    return erp


def _run(items, client) -> list[dict]:
    rows = []
    for item in items:
        try:
            passed, evidence = item.fn(client)
        except Exception as exc:
            passed, evidence = False, f"check raised {type(exc).__name__}: {exc}"
        rows.append({
            "check": item.id,
            "hard": "hard" if item.hard else "soft",
            "status": "pass" if passed else "FAIL",
            "evidence": evidence,
        })
    return rows


def invariants(client=None) -> Table:
    """Run the generic end-state invariants."""
    return Table(_run(INVARIANTS, _client(client)),
                 ["check", "hard", "status", "evidence"], "invariants", max_rows=60)


def rules(client=None, rule_list: list[Rule] | None = None) -> Table:
    """Run the agent's task-specific rules."""
    chosen = _registry.rules if rule_list is None else rule_list
    items = [Check(r.id, r.description, r.hard, r.fn) for r in chosen]
    return Table(_run(items, _client(client)),
                 ["check", "hard", "status", "evidence"], "rules", max_rows=60)


def register(rule: Rule) -> Rule:
    """Keep a rule for every later `check.all()`; re-registering an id replaces it."""
    _registry.rules = [r for r in _registry.rules if r.id != rule.id] + [rule]
    return rule


def registered() -> list[Rule]:
    return list(_registry.rules)


def clear() -> None:
    _registry.rules = []


def all(client=None) -> Table:
    """Invariants plus every registered rule, in one table."""
    resolved = _client(client)
    rows = _run(INVARIANTS, resolved)
    rows += _run([Check(r.id, r.description, r.hard, r.fn) for r in _registry.rules], resolved)
    return Table(rows, ["check", "hard", "status", "evidence"], "checks", max_rows=60)


def failures(client=None, hard_only: bool = True) -> list[dict]:
    """The rows that failed — what `finish` gates on."""
    return [
        row for row in all(client).all()
        if row["status"] == "FAIL" and (not hard_only or row["hard"] == "hard")
    ]
