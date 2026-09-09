"""Typed Odoo client over XML-RPC: reads, planning helpers, and write helpers that refuse
infeasible writes (dates, origins, quantities) at write time.
"""

from __future__ import annotations

import os
import re
import xmlrpc.client
from typing import Any, Iterable, Sequence

from .fmt import Table

EXPORTS = ["erp", "Erp", "OdooError"]

ODOO_URL = os.environ.get("ODOO_URL", "http://127.0.0.1:8069")
ODOO_DB = os.environ.get("ODOO_DB", "bench")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
API_KEY_FILE = os.environ.get("ODOO_API_KEY_FILE", "/etc/odoo/api_key")

# Only internal locations are stock; stock.quant also holds supplier/customer/production virtual locations.
INTERNAL_LOCATION_DOMAIN = ("location_id.usage", "=", "internal")


def _password() -> str:
    for key in ("ODOO_PASSWORD", "ODOO_API_KEY"):
        value = os.environ.get(key)
        if value:
            return value
    try:
        return open(API_KEY_FILE).read().strip()
    except OSError:
        return "pass"


class OdooError(RuntimeError):
    """An Odoo-side failure, carrying Odoo's own message rather than our traceback."""

    def __init__(self, message: str, model: str = "", method: str = ""):
        self.model = model
        self.method = method
        # Odoo faults arrive as a full server traceback with the human-readable reason at
        # the end. The tail is the part worth spending context on.
        text = message.strip()
        if len(text) > 600:
            text = "…" + text[-600:]
        super().__init__(f"{model}.{method}: {text}" if model else text)


def _is_unserializable_reply(fault_text: str) -> bool:
    """True when the fault came from writing the response, not from running the method."""
    return "cannot marshal None" in fault_text and (
        "dumps" in fault_text or "dump_nil" in fault_text or "Marshaller" in fault_text
    )


def _name_of(value: Any) -> str:
    """`[7, 'Axis Assemblies']` -> `'Axis Assemblies'`; False -> ''."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[1]
    return "" if value in (False, None) else str(value)


def _id_of(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value if isinstance(value, int) else None


def _ids(value: int | Iterable[int] | None) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, int):
        return [value]
    return list(value)


# An alternate work centre's rate is not in the database; assume it is slower by this factor.
ALT_RATE_MARGIN = 1.5


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


def _minutes_limit(note: str) -> float | None:
    """A horizon-wide minute limit stated in a work centre's notes, if any."""
    text = _strip_html(note)
    found = [float(m.replace(",", "")) for m in
             re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(?:total\s+)?(?:processing\s+)?minutes", text, re.I)]
    return max(found) if found else None


def _max_qty_from_notes(notes: str, product_code: str | None) -> float | None:
    """A maximum order quantity a vendor's Internal Notes state for this product, if any."""
    text = _strip_html(notes or "")
    if not text or not re.search(r"max(?:imum)?\s+order\s+quantity", text, re.I):
        return None
    named = re.findall(r"max(?:imum)?\s+order\s+quantity\s+for\s+([^:\n]+?)\s*:\s*(\d[\d,]*(?:\.\d+)?)",
                       text, re.I)
    if named:
        for name, number in named:
            if product_code and product_code.lower() in name.lower():
                return float(number.replace(",", ""))
        return None
    m = re.search(r"max(?:imum)?\s+order\s+quantity\s*(?:is|of|=|:)?\s*(\d[\d,]*(?:\.\d+)?)", text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def _cheapest_split(offers: list[dict], qty: float) -> tuple[float, list[tuple[dict, int]]] | None:
    """Min-cost way to buy at least `qty` units across offers, each used at most once with
    `min_qty <= units <= max_qty`. Exact (dynamic programme over units bought); offers are
    few and quantities are hundreds, so this is instant. None if no combination reaches
    `qty` (every vendor capped below it).
    """
    import math

    need = int(math.ceil(qty - 1e-9))
    if need <= 0:
        return 0.0, []
    max_moq = max((int(math.ceil(o.get("min_qty") or 0)) for o in offers), default=0)
    cap = need + max_moq + 1
    best: dict[int, tuple[float, list]] = {0: (0.0, [])}
    for offer in offers:
        lo = max(1, int(math.ceil(offer.get("min_qty") or 0)))
        hi = int(offer["max_qty"]) if offer.get("max_qty") else cap
        if hi < lo:
            continue
        step = dict(best)
        for units, (cost, choice) in best.items():
            for q in range(lo, hi + 1):
                total_units = units + q
                if total_units > cap:
                    break
                cost_here = cost + q * (offer.get("price") or 0.0)
                if total_units not in step or cost_here < step[total_units][0]:
                    step[total_units] = (cost_here, choice + [(offer, q)])
        best = step
    reachable = [(c, ch) for u, (c, ch) in best.items() if u >= need]
    if not reachable:
        return None
    return min(reachable, key=lambda item: (item[0], sum(q for _, q in item[1])))


def _add_days(stamp: str, days: float) -> str:
    from datetime import datetime, timedelta

    base = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    return (base + timedelta(days=float(days))).strftime("%Y-%m-%d %H:%M:%S")


class Erp:
    """One authenticated connection to one database."""

    def __init__(self, db: str | None = None, url: str | None = None,
                 user: str | None = None, password: str | None = None):
        self.db = db or ODOO_DB
        self.url = url or ODOO_URL
        self.user = user or ODOO_USER
        self.password = password or _password()
        self.write_log: list[tuple] = []
        self._uid: int | None = None
        self._models = None
        self._clients: dict[str, "Erp"] = {}

    # -- plumbing -------------------------------------------------------------
    @property
    def uid(self) -> int:
        if self._uid is None:
            common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
            try:
                uid = common.authenticate(self.db, self.user, self.password, {})
            except Exception as exc:
                raise OdooError(str(exc), "auth", self.db) from None
            if not uid:
                raise OdooError(f"authentication refused for {self.user}@{self.db}", "auth", self.db)
            self._uid = uid
        return self._uid

    @property
    def models(self):
        if self._models is None:
            self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        return self._models

    def on(self, db: str) -> "Erp":
        """A client for another database on the same server (used by `state`)."""
        if db not in self._clients:
            self._clients[db] = Erp(db, self.url, self.user, self.password)
        return self._clients[db]

    def call(self, model: str, method: str, args: Sequence = (), kwargs: dict | None = None):
        """Raw `execute_kw`. Every non-read call is appended to `write_log`."""
        kwargs = kwargs or {}
        try:
            result = self.models.execute_kw(
                self.db, self.uid, self.password, model, method, list(args), kwargs
            )
        except xmlrpc.client.Fault as fault:
            if _is_unserializable_reply(fault.faultString):
                # Odoo 19 XML-RPC fails to serialise a None reply after the call has committed; write helpers confirm their own effect.
                result = None
            else:
                raise OdooError(fault.faultString, model, method) from None
        except Exception as exc:
            raise OdooError(str(exc), model, method) from None
        if method not in ("read", "search", "search_read", "search_count", "fields_get"):
            self.write_log.append((model, method, list(args), kwargs, result))
        return result

    # -- generic reads --------------------------------------------------------
    def search_read(self, model: str, domain: list, fields: list[str],
                    limit: int = 80, order: str | None = None) -> list[dict]:
        kwargs: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kwargs["order"] = order
        return self.call(model, "search_read", [domain], kwargs)

    def get(self, model: str, ids: int | Iterable[int], fields: list[str] | None = None) -> Table:
        rows = self.call(model, "read", [_ids(ids)], {"fields": fields} if fields else {})
        return Table(rows)

    def fields(self, model: str, like: str | None = None) -> Table:
        """What fields a model really has — the antidote to guessing names."""
        raw = self.call(model, "fields_get", [], {"attributes": ["type", "string", "relation"]})
        rows = [
            {"name": name, "type": spec.get("type"), "relation": spec.get("relation") or "",
             "label": spec.get("string")}
            for name, spec in sorted(raw.items())
            if not like or like.lower() in name.lower() or like.lower() in str(spec.get("string", "")).lower()
        ]
        return Table(rows, ["name", "type", "relation", "label"], f"{model} fields", max_rows=80)

    def count(self, model: str, domain: list | None = None) -> int:
        return self.call(model, "search_count", [domain or []])

    # -- domain reads ---------------------------------------------------------
    def sales_orders(self, state: str | None = None, due_before: str | None = None,
                     ids: Iterable[int] | None = None) -> Table:
        domain: list = []
        if ids is not None:
            domain.append(("id", "in", list(ids)))
        if state:
            domain.append(("state", "=", state))
        if due_before:
            domain.append(("commitment_date", "<=", due_before))
        rows = self.search_read(
            "sale.order", domain,
            ["name", "partner_id", "state", "commitment_date", "amount_total",
             "delivery_status", "invoice_status"],
            limit=200, order="commitment_date asc, id asc",
        )
        return Table(
            [{"id": r["id"], "name": r["name"], "partner": _name_of(r["partner_id"]),
              "state": r["state"], "commitment_date": r["commitment_date"],
              "amount_total": r["amount_total"], "delivery_status": r.get("delivery_status"),
              "invoice_status": r.get("invoice_status")} for r in rows],
            ["id", "name", "partner", "state", "commitment_date", "amount_total",
             "delivery_status", "invoice_status"], "sale.order",
        )

    def so_lines(self, so_ids: Iterable[int]) -> Table:
        rows = self.search_read(
            "sale.order.line", [("order_id", "in", list(so_ids))],
            ["order_id", "product_id", "product_uom_qty", "qty_delivered", "qty_invoiced",
             "price_unit"], limit=500, order="order_id asc, id asc",
        )
        return Table(
            [{"so": _name_of(r["order_id"]), "product_id": _id_of(r["product_id"]),
              "product": _name_of(r["product_id"]), "product_uom_qty": r["product_uom_qty"],
              "qty_delivered": r["qty_delivered"], "qty_invoiced": r["qty_invoiced"],
              "price_unit": r["price_unit"]} for r in rows],
            ["so", "product_id", "product", "product_uom_qty", "qty_delivered",
             "qty_invoiced", "price_unit"], "sale.order.line",
        )

    def stock(self, product_ids: Iterable[int] | None = None) -> Table:
        """On-hand / reserved / free per product, summed over internal locations only."""
        domain: list = [INTERNAL_LOCATION_DOMAIN]
        if product_ids is not None:
            domain.append(("product_id", "in", list(product_ids)))
        rows = self.search_read(
            "stock.quant", domain, ["product_id", "quantity", "reserved_quantity"], limit=1000,
        )
        totals: dict[int, dict] = {}
        for row in rows:
            pid = _id_of(row["product_id"])
            entry = totals.setdefault(
                pid, {"product_id": pid, "product": _name_of(row["product_id"]),
                      "on_hand": 0.0, "reserved": 0.0})
            entry["on_hand"] += row["quantity"] or 0.0
            entry["reserved"] += row["reserved_quantity"] or 0.0
        for entry in totals.values():
            entry["free"] = entry["on_hand"] - entry["reserved"]
        return Table(sorted(totals.values(), key=lambda r: r["product"]),
                     ["product_id", "product", "on_hand", "reserved", "free"], "stock on hand")

    def boms(self, product_ids: Iterable[int] | None = None) -> Table:
        domain: list = []
        if product_ids is not None:
            # A BOM points at either the variant or the template, so match both.
            templates = {
                _id_of(r["product_tmpl_id"])
                for r in self.search_read(
                    "product.product", [("id", "in", list(product_ids))], ["product_tmpl_id"], limit=500)
            }
            domain = ["|", ("product_id", "in", list(product_ids)),
                      ("product_tmpl_id", "in", list(templates))]
        bom_rows = self.search_read(
            "mrp.bom", domain, ["product_tmpl_id", "product_id", "product_qty", "bom_line_ids", "type"],
            limit=200)
        line_ids = [lid for row in bom_rows for lid in row.get("bom_line_ids", [])]
        lines = self.call(
            "mrp.bom.line", "read", [line_ids],
            {"fields": ["bom_id", "product_id", "product_qty"]}) if line_ids else []
        by_bom: dict[int, list] = {}
        for line in lines:
            by_bom.setdefault(_id_of(line["bom_id"]), []).append(line)

        out = []
        for bom in bom_rows:
            product = _name_of(bom["product_id"]) or _name_of(bom["product_tmpl_id"])
            for line in by_bom.get(bom["id"], []):
                out.append({
                    "bom_id": bom["id"], "product": product, "bom_qty": bom["product_qty"],
                    "component_id": _id_of(line["product_id"]),
                    "component": _name_of(line["product_id"]),
                    "comp_qty": line["product_qty"],
                })
            if not by_bom.get(bom["id"]):
                out.append({"bom_id": bom["id"], "product": product, "bom_qty": bom["product_qty"],
                            "component_id": None, "component": "(no components)", "comp_qty": 0})
        return Table(out, ["bom_id", "product", "bom_qty", "component_id", "component", "comp_qty"],
                     "mrp.bom")

    def suppliers(self, product_ids: Iterable[int] | None = None) -> Table:
        """Vendor offers from `product.supplierinfo`."""
        domain: list = []
        if product_ids is not None:
            templates = {
                _id_of(r["product_tmpl_id"])
                for r in self.search_read(
                    "product.product", [("id", "in", list(product_ids))], ["product_tmpl_id"], limit=500)
            }
            domain = ["|", ("product_id", "in", list(product_ids)),
                      ("product_tmpl_id", "in", list(templates))]
        rows = self.search_read(
            "product.supplierinfo", domain,
            ["product_id", "product_tmpl_id", "partner_id", "min_qty", "price", "delay",
             "currency_id"], limit=500, order="product_tmpl_id asc, price asc")
        variants = self._variants_of_templates(
            {_id_of(r["product_tmpl_id"]) for r in rows if r["product_tmpl_id"]})
        partner_ids = sorted({_id_of(r["partner_id"]) for r in rows if r["partner_id"]})
        notes = {}
        if partner_ids:
            for partner in self.call("res.partner", "read", [partner_ids], {"fields": ["comment"]}):
                notes[partner["id"]] = _strip_html(partner.get("comment") or "")
        return Table(
            [{"product_id": _id_of(r["product_id"]) or variants.get(_id_of(r["product_tmpl_id"])),
              "product": _name_of(r["product_id"]) or _name_of(r["product_tmpl_id"]),
              "vendor_id": _id_of(r["partner_id"]), "vendor": _name_of(r["partner_id"]),
              "min_qty": r["min_qty"], "price": r["price"], "delay_days": r["delay"],
              "currency": _name_of(r["currency_id"]),
              "vendor_notes": notes.get(_id_of(r["partner_id"]), "")} for r in rows],
            ["product_id", "product", "vendor_id", "vendor", "min_qty", "price", "delay_days",
             "currency", "vendor_notes"], "product.supplierinfo")

    def _variants_of_templates(self, template_ids: Iterable[int]) -> dict[int, int]:
        """template id -> one variant id, for offers recorded against the template."""
        template_ids = [t for t in template_ids if t]
        if not template_ids:
            return {}
        rows = self.search_read(
            "product.product", [("product_tmpl_id", "in", template_ids)],
            ["product_tmpl_id"], limit=1000)
        mapping: dict[int, int] = {}
        for row in rows:
            mapping.setdefault(_id_of(row["product_tmpl_id"]), row["id"])
        return mapping

    def purchase_orders(self, state: str | None = None, ids: Iterable[int] | None = None) -> Table:
        domain: list = []
        if ids is not None:
            domain.append(("id", "in", list(ids)))
        if state:
            domain.append(("state", "=", state))
        rows = self.search_read(
            "purchase.order", domain,
            ["name", "partner_id", "state", "date_planned", "amount_total", "receipt_status",
             "origin"], limit=200, order="id asc")
        return Table(
            [{"id": r["id"], "name": r["name"], "vendor": _name_of(r["partner_id"]),
              "state": r["state"], "date_planned": r["date_planned"],
              "amount_total": r["amount_total"], "receipt_status": r.get("receipt_status"),
              "origin": r.get("origin") or ""} for r in rows],
            ["id", "name", "vendor", "state", "date_planned", "amount_total", "receipt_status",
             "origin"], "purchase.order")

    def po_lines(self, po_ids: Iterable[int]) -> Table:
        rows = self.search_read(
            "purchase.order.line", [("order_id", "in", list(po_ids))],
            ["order_id", "product_id", "product_qty", "price_unit", "date_planned",
             "qty_received"], limit=500, order="order_id asc, id asc")
        return Table(
            [{"po": _name_of(r["order_id"]), "product_id": _id_of(r["product_id"]),
              "product": _name_of(r["product_id"]), "product_qty": r["product_qty"],
              "price_unit": r["price_unit"], "date_planned": r["date_planned"],
              "qty_received": r["qty_received"]} for r in rows],
            ["po", "product_id", "product", "product_qty", "price_unit", "date_planned",
             "qty_received"], "purchase.order.line")

    def productions(self, state: str | None = None, ids: Iterable[int] | None = None) -> Table:
        domain: list = []
        if ids is not None:
            domain.append(("id", "in", list(ids)))
        if state:
            domain.append(("state", "=", state))
        rows = self.search_read(
            "mrp.production", domain,
            ["name", "product_id", "product_qty", "state", "date_start", "date_finished",
             "components_availability"], limit=200, order="id asc")
        return Table(
            [{"id": r["id"], "name": r["name"], "product": _name_of(r["product_id"]),
              "product_qty": r["product_qty"], "state": r["state"], "date_start": r["date_start"],
              "date_finished": r.get("date_finished"),
              "components_availability": r.get("components_availability")} for r in rows],
            ["id", "name", "product", "product_qty", "state", "date_start", "date_finished",
             "components_availability"], "mrp.production")

    def pickings(self, picking_type: str | None = None, state: str | None = None,
                 origin: str | None = None) -> Table:
        domain: list = []
        if picking_type:
            domain.append(("picking_type_id.code", "=", picking_type))
        if state:
            domain.append(("state", "=", state))
        if origin:
            domain.append(("origin", "like", origin))
        rows = self.search_read(
            "stock.picking", domain,
            ["name", "picking_type_id", "origin", "state", "scheduled_date"],
            limit=200, order="scheduled_date asc, id asc")
        return Table(
            [{"id": r["id"], "name": r["name"], "picking_type": _name_of(r["picking_type_id"]),
              "origin": r.get("origin") or "", "state": r["state"],
              "scheduled_date": r["scheduled_date"]} for r in rows],
            ["id", "name", "picking_type", "origin", "state", "scheduled_date"], "stock.picking")

    def _workcenter_load(self) -> tuple[list[dict], list[dict], dict[int, float]]:
        """Work centres, their BOM operations, and minutes already committed per centre."""
        centres = self.search_read(
            "mrp.workcenter", [],
            ["name", "code", "note", "time_efficiency", "time_start", "time_stop",
             "costs_hour", "alternative_workcenter_ids"], limit=100)
        operations = self.search_read(
            "mrp.routing.workcenter", [],
            ["name", "workcenter_id", "bom_id", "time_cycle_manual", "time_cycle"], limit=200)
        op_minutes = {o["id"]: (o.get("time_cycle_manual") or o.get("time_cycle") or 0.0)
                      for o in operations}
        op_centre = {o["id"]: _id_of(o.get("workcenter_id")) for o in operations}

        def rate(operation_id, centre_id) -> tuple[float | None, bool]:
            """Minutes per unit at this centre, and whether that rate is stated."""
            minutes = op_minutes.get(operation_id)
            if not minutes:
                return None, False
            if op_centre.get(operation_id) == centre_id:
                return minutes, True
            return minutes * ALT_RATE_MARGIN, False

        committed: dict[int, float] = {}
        productions = {m["id"]: m for m in self.search_read(
            "mrp.production", [("state", "not in", ("cancel",))],
            ["product_qty", "state"], limit=500)}
        if productions:
            for wo in self.search_read(
                    "mrp.workorder",
                    [("production_id", "in", list(productions)), ("state", "!=", "cancel")],
                    ["workcenter_id", "operation_id", "production_id", "duration_expected"],
                    limit=1000):
                wc = _id_of(wo.get("workcenter_id"))
                mo = productions.get(_id_of(wo.get("production_id")))
                if wc is None or mo is None:
                    continue
                per_unit, _stated = rate(_id_of(wo.get("operation_id")), wc)
                minutes = (mo["product_qty"] or 0) * per_unit if per_unit else (wo.get("duration_expected") or 0)
                committed[wc] = committed.get(wc, 0.0) + minutes
        return centres, operations, committed

    def workcenters(self) -> Table:
        """Work centres with the numbers any assembly-capacity rule needs."""
        try:
            centres, operations, committed = self._workcenter_load()
        except OdooError as exc:
            if "not exist" in str(exc) or "Object" in str(exc):
                return Table([], ["id", "name"], "mrp.workcenter (not installed)")
            raise
        ops_by_wc: dict[int, list[str]] = {}
        for o in operations:
            wc = _id_of(o.get("workcenter_id"))
            if wc is not None:
                product = _name_of(o.get("bom_id"))
                ops_by_wc.setdefault(wc, []).append(
                    f"{product}: {o.get('time_cycle_manual') or o.get('time_cycle') or 0:g} min/unit")
        for c in centres:
            for alt in (c.get("alternative_workcenter_ids") or []):
                for line in ops_by_wc.get(c["id"], []):
                    ops_by_wc.setdefault(alt, []).append(
                        f"(alternate for {c['name']}: rate not stated, assumed {ALT_RATE_MARGIN}x) {line}")
        names = {c["id"]: c["name"] for c in centres}
        rows = []
        for c in centres:
            limit = _minutes_limit(c.get("note") or "")
            used = round(committed.get(c["id"], 0.0), 1)
            rows.append({
                "id": c["id"], "code": c.get("code") or "", "name": c["name"],
                "cost_per_hour": c.get("costs_hour"), "efficiency_pct": c.get("time_efficiency"),
                "minutes_limit": limit, "minutes_committed": used,
                "minutes_free": (round(limit - used, 1) if limit is not None else None),
                "operations": "; ".join(ops_by_wc.get(c["id"], [])),
                "alternatives": ", ".join(names.get(a, str(a)) for a in (c.get("alternative_workcenter_ids") or [])),
                "note": _strip_html(c.get("note") or "")[:220],
            })
        return Table(rows, ["id", "code", "name", "cost_per_hour", "efficiency_pct", "minutes_limit",
                            "minutes_committed", "minutes_free", "operations", "alternatives", "note"],
                     "mrp.workcenter")

    def workcenter_options(self, product_id: int) -> Table:
        """Where this product can be assembled, with per-unit minutes and cost, and the
        minutes each centre has left — the input to choosing / splitting work centres."""
        bom = self.bom_for(product_id)
        if not bom or not bom["operation_ids"]:
            return Table([], ["workcenter_id", "name"], "work-centre options (no operations)")
        centres, operations, committed = self._workcenter_load()
        by_id = {c["id"]: c for c in centres}
        rows = []
        for o in operations:
            if _id_of(o.get("bom_id")) != bom["id"]:
                continue
            stated = o.get("time_cycle_manual") or o.get("time_cycle") or 0.0
            primary = _id_of(o.get("workcenter_id"))
            for wc_id in [primary] + list((by_id.get(primary) or {}).get("alternative_workcenter_ids") or []):
                c = by_id.get(wc_id)
                if not c:
                    continue
                is_primary = wc_id == primary
                minutes = stated if is_primary else stated * ALT_RATE_MARGIN
                limit = _minutes_limit(c.get("note") or "")
                used = committed.get(wc_id, 0.0)
                rows.append({
                    "workcenter_id": wc_id, "name": c["name"], "code": c.get("code") or "",
                    "role": "primary" if is_primary else "alternative",
                    "min_per_unit": minutes,
                    "rate": "stated" if is_primary else f"assumed {ALT_RATE_MARGIN}x primary (not stated)",
                    "cost_per_unit": round((c.get("costs_hour") or 0) * minutes / 60, 2),
                    "minutes_limit": limit, "minutes_free": (round(limit - used, 1) if limit is not None else None),
                    "units_fit": (int((limit - used) // minutes) if limit is not None and minutes else None),
                })
        return Table(rows, ["workcenter_id", "name", "code", "role", "min_per_unit", "rate", "cost_per_unit",
                            "minutes_limit", "minutes_free", "units_fit"], "work-centre options")

    def invoices(self, move_type: str = "out_invoice", state: str | None = None) -> Table:
        domain: list = [("move_type", "=", move_type)] if move_type else []
        if state:
            domain.append(("state", "=", state))
        rows = self.search_read(
            "account.move", domain,
            ["name", "partner_id", "state", "amount_total", "invoice_origin", "payment_state"],
            limit=200, order="id asc")
        return Table(
            [{"id": r["id"], "name": r["name"], "partner": _name_of(r["partner_id"]),
              "state": r["state"], "amount_total": r["amount_total"],
              "invoice_origin": r.get("invoice_origin") or ""} for r in rows],
            ["id", "name", "partner", "state", "amount_total", "invoice_origin"], "account.move")

    # -- planning helpers -----------------------------------------------------
    def today(self) -> str:
        """The server's idea of now, as an Odoo datetime string (UTC)."""
        row = self.call("res.users", "read", [[self.uid]], {"fields": ["login_date"]})
        stamp = (row[0].get("login_date") if row else None) or ""
        if not stamp:
            from datetime import datetime, timezone
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return stamp[:19]

    def feasible_vendors(self, product_id: int, qty: float, need_by: str,
                         order_date: str | None = None) -> Table:
        """Vendors who can land `qty` of a product by `need_by`, with the arrival date."""
        from datetime import datetime, timedelta

        start = datetime.strptime((order_date or self.today())[:19], "%Y-%m-%d %H:%M:%S")
        due = datetime.strptime(need_by[:19] if len(need_by) > 10 else need_by + " 23:59:59",
                                "%Y-%m-%d %H:%M:%S")
        rows = []
        for offer in self.suppliers([product_id]).all():
            arrives = start + timedelta(days=int(offer["delay_days"] or 0))
            if arrives > due:
                continue
            shortfall = max(0.0, (offer["min_qty"] or 0) - qty)
            rows.append({
                "vendor_id": offer["vendor_id"], "vendor": offer["vendor"],
                "price": offer["price"], "min_qty": offer["min_qty"],
                "order_qty": max(qty, offer["min_qty"] or 0),
                "delay_days": offer["delay_days"],
                "arrives": arrives.strftime("%Y-%m-%d %H:%M:%S"),
                "slack_days": (due - arrives).days,
                "line_total": round(offer["price"] * max(qty, offer["min_qty"] or 0), 2),
                "vendor_notes": offer["vendor_notes"],
            })
        rows.sort(key=lambda r: (r["line_total"], -r["slack_days"]))
        # Point at cheapest_buy: hand-picked splits from this table lose on spend.
        return Table(rows, ["vendor_id", "vendor", "price", "min_qty", "order_qty",
                            "delay_days", "arrives", "slack_days", "line_total", "vendor_notes"],
                     f"vendors able to deliver {qty:g} of product {product_id} by {need_by[:10]} — "
                     f"for the PO lines to write, use erp.cheapest_buy({product_id}, {qty:g}, "
                     f"{need_by[:10]!r}) (min-cost split across these offers)")

    def cheapest_buy(self, product_id: int, qty: float, need_by: str | None = None,
                     order_date: str | None = None) -> Table:
        """The cheapest way to buy `qty` of a product: which vendors, how many from each."""
        from datetime import datetime, timedelta

        start = datetime.strptime((order_date or self.today())[:19], "%Y-%m-%d %H:%M:%S")
        due = None
        if need_by and len(need_by) <= 10:
            need_by = need_by + " 23:59:59"
        if need_by:
            due = datetime.strptime(need_by[:19] if len(need_by) > 10 else need_by + " 23:59:59",
                                    "%Y-%m-%d %H:%M:%S")
        product = self.get("product.product", [product_id], ["default_code"])
        code = (product[0].get("default_code") or None) if product else None
        offers = []
        for offer in self.suppliers([product_id]).all():
            arrives = start + timedelta(days=int(offer["delay_days"] or 0))
            if due is not None and arrives > due:
                continue
            offers.append({**offer, "arrives": arrives.strftime("%Y-%m-%d %H:%M:%S"),
                           "max_qty": _max_qty_from_notes(offer.get("vendor_notes") or "", code)})
        cols = ["vendor_id", "vendor", "qty", "price", "line_total", "min_qty", "max_qty",
                "delay_days", "date_planned"]
        when = f" by {need_by[:10]}" if need_by else ""
        if not offers:
            return Table([], cols, f"cheapest buy of {qty:g} × product {product_id}{when}: no vendor can land it in time")
        solved = _cheapest_split(offers, qty)
        if solved is None:
            return Table([], cols, f"cheapest buy of {qty:g} × product {product_id}{when}: vendor maxima sum below the quantity")
        total, lines = solved
        rows = [{"vendor_id": o["vendor_id"], "vendor": o["vendor"], "qty": q, "price": o["price"],
                 "line_total": round(q * (o["price"] or 0), 2), "min_qty": o["min_qty"],
                 "max_qty": o.get("max_qty"), "delay_days": o["delay_days"], "date_planned": o["arrives"]}
                for o, q in lines]
        units = sum(q for _, q in lines)
        return Table(rows, cols, f"cheapest buy of {qty:g} × product {product_id}{when}: "
                                 f"{units:g} units for ${total:,.2f} across {len(rows)} PO line(s)")

    def earliest_build(self, product_id: int, qty: float, order_date: str | None = None,
                       need_by: str | None = None, _depth: int = 0) -> dict:
        """When an MO for `qty` could start, given components on hand, purchasable, or
        buildable.
        """
        from datetime import datetime, timedelta

        start = datetime.strptime((order_date or self.today())[:19], "%Y-%m-%d %H:%M:%S")
        if need_by and len(need_by) <= 10:
            need_by = need_by + " 23:59:59"
        bom_rows = [r for r in self.boms([product_id]).all() if r["component_id"]]
        if not bom_rows:
            return {"date_start": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "components": [], "unsourceable": [], "note": "no BOM for this product"}
        batch = bom_rows[0]["bom_qty"] or 1.0
        free = {r["product_id"]: r["free"] for r in self.stock([r["component_id"] for r in bom_rows])}
        detail, unsourceable, latest = [], [], start
        for row in bom_rows:
            need = (row["comp_qty"] or 0) * qty / batch
            have = free.get(row["component_id"], 0.0)
            entry = {"component_id": row["component_id"], "component": row["component"],
                     "needed": round(need, 3), "free_now": have}
            if have + 1e-6 >= need:
                entry.update({"source": "have", "ready": start.strftime("%Y-%m-%d %H:%M:%S")})
                detail.append(entry)
                continue
            shortfall = need - have
            offers = sorted(self.suppliers([row["component_id"]]).all(),
                            key=lambda o: (o["delay_days"] or 0, o["price"] or 0))
            latest_start = (_add_days(need_by, -(self.bom_for(product_id) or {}).get("lead_days", 0))
                            if need_by else None)
            sub_bom = [b for b in self.boms([row["component_id"]]).all() if b["component_id"]]
            buy_ready = make_ready = None
            if offers and need_by:
                # With a due date the question is not "fastest" but "cheapest that makes
                # it": components must land by the latest start, need_by - lead time.
                plan = self.cheapest_buy(row["component_id"], shortfall, need_by=latest_start,
                                         order_date=order_date).all()
                if plan:
                    buy_ready = max(datetime.strptime(l["date_planned"][:19], "%Y-%m-%d %H:%M:%S")
                                    for l in plan)
                    entry.update({"buy": round(sum(l["qty"] for l in plan), 3),
                                  "from": ", ".join(f"{l['vendor']} ×{l['qty']:g}" for l in plan),
                                  "vendor_id": plan[0]["vendor_id"],
                                  "buy_lines": [(l["vendor_id"], l["qty"], l["price"], l["date_planned"]) for l in plan],
                                  "buy_cost": round(sum(l["line_total"] for l in plan), 2)})
            if offers and buy_ready is None:
                o = offers[0]
                buy_ready = start + timedelta(days=int(o["delay_days"] or 0))
                entry.update({"buy": round(max(shortfall, o["min_qty"] or 0), 3),
                              "from": o["vendor"], "vendor_id": o["vendor_id"]})
            if sub_bom and _depth < 3:
                sub = self.earliest_build(row["component_id"], shortfall, order_date,
                                          need_by=latest_start if need_by else None, _depth=_depth + 1)
                if not sub["unsourceable"]:
                    make_ready = datetime.strptime(sub["date_start"][:19], "%Y-%m-%d %H:%M:%S")
                    entry.update({"make": round(shortfall, 3), "make_start": sub["date_start"]})
            if buy_ready is None and make_ready is None:
                unsourceable.append(row["component"])
                entry.update({"source": "none", "ready": None})
            else:
                # Prefer whichever is ready sooner; ties go to buying (fewer moving parts).
                if make_ready is not None and (buy_ready is None or make_ready < buy_ready):
                    entry["source"], ready = "make", make_ready
                else:
                    entry["source"], ready = "buy", buy_ready
                entry["ready"] = ready.strftime("%Y-%m-%d %H:%M:%S")
                latest = max(latest, ready)
            detail.append(entry)
        bom = self.bom_for(product_id)
        lead = bom["lead_days"] if bom else 0
        result = {"date_start": latest.strftime("%Y-%m-%d %H:%M:%S"),
                  "lead_days": lead,
                  "date_deadline": (latest + timedelta(days=int(lead))).strftime("%Y-%m-%d %H:%M:%S"),
                  "components": detail, "unsourceable": unsourceable}
        if _depth == 0:
            options = self.workcenter_options(product_id).all()
            if options:
                result["workcenters"] = options
        return result

    # -- writes ---------------------------------------------------------------
    def create_po(self, vendor_id: int, lines: Sequence[tuple], date_planned: str | None = None,
                  origin: str | None = None, force: bool = False) -> int:
        """Create a purchase order. `lines` = [(product_id, qty[, price_unit[,
        date_planned]])].
        """
        if not force:
            today = self.today()
            for line in lines:
                product_id = line[0]
                line_date = line[3] if len(line) > 3 and line[3] else date_planned
                if not line_date:
                    continue
                delay = self._delay_for(vendor_id, product_id)
                if delay is None:
                    continue
                earliest = _add_days(today, delay)
                if line_date[:10] < earliest[:10]:
                    raise OdooError(
                        f"date_planned {line_date[:10]} for product {product_id} is before "
                        f"the vendor's earliest delivery {earliest[:10]} (order date {today[:10]} "
                        f"+ {delay}d lead time). Use that date, pick a faster vendor via "
                        f"erp.feasible_vendors(...), or pass force=True if you know better.",
                        "purchase.order", "create_po")
            if origin:
                self._check_origin_feeds(vendor_id, lines, date_planned, origin)
                for line in lines:
                    offers = self._offers_for(vendor_id, line[0], ["min_qty"])
                    # The tier that applies to this quantity (largest min_qty not above it).
                    tiers = [(o["min_qty"] or 0) for o in offers if (o["min_qty"] or 0) <= line[1] + 0.01]
                    moq = max(tiers) if tiers else None
                    line_date = line[3] if len(line) > 3 and line[3] else date_planned
                    delay = self._delay_for(vendor_id, line[0])
                    arrives = max(filter(None, [line_date or "", _add_days(self.today(), delay) if delay is not None else ""]), default="")
                    self._refuse_excess_over_origin(line[0], line[1], origin, moq, "this purchase", arrives)
            self._check_one_po_per_offer(vendor_id, lines)
        commands = []
        for line in lines:
            product_id, qty = line[0], line[1]
            price = line[2] if len(line) > 2 and line[2] is not None else self.vendor_price(
                vendor_id, product_id, qty)
            line_date = line[3] if len(line) > 3 and line[3] else date_planned
            values = {"product_id": product_id, "product_qty": qty, "price_unit": price}
            if line_date:
                values["date_planned"] = line_date
            commands.append((0, 0, values))

        order: dict[str, Any] = {"partner_id": vendor_id, "order_line": commands}
        if date_planned:
            order["date_planned"] = date_planned
        if origin:
            order["origin"] = origin
        return self.call("purchase.order", "create", [order])

    def _check_one_po_per_offer(self, vendor_id: int, lines: Sequence[tuple]) -> None:
        """Refuse a second open PO for a vendor/product that already has one."""
        products = [line[0] for line in lines]
        existing = self.search_read(
            "purchase.order.line",
            [("partner_id", "=", vendor_id), ("product_id", "in", products),
             ("order_id.state", "in", ("purchase", "done"))],
            ["order_id", "product_id", "product_qty"], limit=50)
        if not existing:
            return
        by_po: dict[str, list[str]] = {}
        for line in existing:
            by_po.setdefault(line["order_id"][1], []).append(
                f"{line['product_id'][1]} ×{line['product_qty']:g}")
        first_po_id = existing[0]["order_id"][0]
        shown = "; ".join(f"{po} already buys {', '.join(items)}" for po, items in by_po.items())
        raise OdooError(
            f"one PO per supplier offer: {shown} from this vendor. Add to it instead — "
            f"erp.add_po_lines({first_po_id}, {[(l[0], l[1]) for l in lines]!r}) — or cancel it "
            "and create one order for the full quantity. force=True writes a second PO anyway, "
            "and the finish check will refuse it.",
            "purchase.order", "create_po")

    def add_po_lines(self, po_id: int, lines: Sequence[tuple]) -> list[int]:
        """Add lines to an existing purchase order (draft or confirmed)."""
        order = self.get("purchase.order", [po_id], ["partner_id", "state", "date_planned"])[0]
        if order["state"] == "cancel":
            raise OdooError(f"PO {po_id} is cancelled; create a new order instead",
                            "purchase.order", "add_po_lines")
        vendor_id = _id_of(order["partner_id"])
        created = []
        for line in lines:
            product_id, qty = line[0], line[1]
            price = line[2] if len(line) > 2 and line[2] is not None else self.vendor_price(
                vendor_id, product_id, qty)
            values: dict[str, Any] = {"order_id": po_id, "product_id": product_id,
                                      "product_qty": qty, "price_unit": price}
            line_date = line[3] if len(line) > 3 and line[3] else order.get("date_planned")
            if line_date:
                values["date_planned"] = line_date
            created.append(self.call("purchase.order.line", "create", [values]))
        return created

    def _check_origin_feeds(self, vendor_id: int, lines: Sequence[tuple],
                            date_planned: str | None, origin: str) -> None:
        """Refuse an origin naming an order this purchase cannot feed (see `_refuse_bad_origin`)."""
        today = self.today()
        arrives = ""
        for line in lines:
            line_date = line[3] if len(line) > 3 and line[3] else date_planned
            delay = self._delay_for(vendor_id, line[0])
            candidate = max(filter(None, [line_date or "", _add_days(today, delay) if delay is not None else ""]), default="")
            if candidate > arrives:
                arrives = candidate
        self._refuse_bad_origin(arrives, origin, "this purchase lands")

    def _origin_needs(self, tokens: Sequence[str]) -> dict[str, str | None]:
        """Need date for each origin reference: a sales order's commitment_date, or a
        manufacturing order's date_start (a component feeds the MO that consumes it)."""
        need: dict[str, str | None] = {}
        for so in self.search_read("sale.order", [("name", "in", list(tokens)), ("state", "not in", ("cancel",))],
                                   ["name", "commitment_date"], limit=200):
            need[so["name"]] = so.get("commitment_date") or None
        for mo in self.search_read("mrp.production", [("name", "in", list(tokens)), ("state", "not in", ("cancel",))],
                                   ["name", "date_start"], limit=200):
            need.setdefault(mo["name"], mo.get("date_start") or None)
        return need

    def origin_capacity(self, product_id: int, tokens: Sequence[str]) -> dict[str, float]:
        """How much of `product_id` each named order can absorb: a sales order's line
        quantity, or a manufacturing order's need for it as a component."""
        need: dict[str, float] = {t: 0.0 for t in tokens}
        if not tokens:
            return need
        for line in self.search_read(
                "sale.order.line",
                [("order_id.name", "in", list(tokens)), ("product_id", "=", product_id),
                 ("order_id.state", "not in", ("cancel",))],
                ["order_id", "product_uom_qty"], limit=200):
            need[line["order_id"][1]] = need.get(line["order_id"][1], 0.0) + (line["product_uom_qty"] or 0)
        for mv in self.search_read(
                "stock.move",
                [("raw_material_production_id.name", "in", list(tokens)), ("product_id", "=", product_id),
                 ("state", "!=", "cancel")],
                ["raw_material_production_id", "product_uom_qty"], limit=200):
            name = mv["raw_material_production_id"][1]
            need[name] = need.get(name, 0.0) + (mv["product_uom_qty"] or 0)
        return need

    def _refuse_excess_over_origin(self, product_id: int, qty: float, origin: str,
                                   min_qty: float | None, what: str, arrives: str = "") -> None:
        """Refuse a quantity the named orders cannot absorb (excess allowed only when the
        quantity is exactly the vendor's minimum). The flow rule the grader of any plan
        applies: supply traces to the demand it names, unit for unit."""
        tokens = [t.strip() for t in origin.split(",") if t.strip()]
        if not tokens or qty <= 0:
            return
        need = self.origin_capacity(product_id, tokens)
        none = [t for t in tokens if need.get(t, 0.0) < 1 - 1e-6]
        capacity = sum(need.values())
        # The minimum-quantity allowance is for component purchases (origin names MOs). A
        # finished-goods purchase must be absorbed entirely by the sales orders it names.
        names_mos = bool(self.search_read("mrp.production", [("name", "in", tokens)], ["id"], limit=1))
        at_minimum = names_mos and min_qty is not None and abs(qty - min_qty) <= 0.01
        if none:
            raise OdooError(
                f"origin refused: {', '.join(none)} need none of product {product_id}; {what} names "
                f"only the orders that consume what it supplies. force=True writes it anyway.",
                "origin", "quantity")
        if capacity + 1e-6 < qty and not at_minimum:
            detail = ", ".join(f"{t} needs {need[t]:g}" for t in tokens[:6])
            hint = ""
            if not names_mos:
                # Which other sales orders for this product could absorb the excess: due on
                # or after arrival, not already named (delivered-from-stock orders count).
                domain = [("product_id", "=", product_id), ("order_id.state", "in", ("sale", "done")),
                          ("order_id.name", "not in", tokens)]
                if arrives:
                    domain.append(("order_id.commitment_date", ">=", arrives))
                others = self.search_read("sale.order.line", domain,
                                          ["order_id", "product_uom_qty"], limit=20)
                cands = sorted({(l["order_id"][1], l["product_uom_qty"] or 0) for l in others if l["order_id"]})
                if cands:
                    hint = (" Orders due on or after arrival that could absorb the extra "
                            f"{qty - capacity:g}: " + ", ".join(f"{n} ({q:g})" for n, q in cands[:6]) +
                            f" — e.g. origin={', '.join(tokens + [cands[0][0]])!r}.")
                else:
                    hint = " No other order for this product is due on or after arrival: buy only what the named orders need."
            raise OdooError(
                f"origin refused: {what} supplies {qty:g} but the orders it names absorb {capacity:g} "
                f"({detail}). A finished-goods purchase must be absorbed entirely by the sales orders "
                "it names; only a component purchase at the vendor's minimum may exceed the MOs it "
                f"names.{hint} force=True writes it anyway, and the finish check will refuse it.",
                "origin", "quantity")

    def _refuse_bad_origin(self, arrives: str, origin: str, what: str) -> None:
        """`origin` is the audit trail: "this document is for that order"."""
        tokens = [t.strip() for t in origin.split(",") if t.strip()]
        if not tokens:
            return
        need = self._origin_needs(tokens)
        unknown = [t for t in tokens if t not in need]
        late = [t for t in tokens if t in need and need[t] and arrives and need[t][:10] < arrives[:10]]
        if not unknown and not late:
            return
        ok = [t for t in tokens if t in need and t not in late]
        parts = []
        if unknown:
            parts.append(f"{', '.join(unknown)} is not a sales or manufacturing order reference "
                         "(origin takes exact references, comma-separated)")
        if late:
            due = ", ".join(f"{t} (needs it by {need[t][:10]})" for t in late)
            parts.append(f"{what} {arrives[:10]} but origin names {due}, which it cannot feed")
        raise OdooError(
            "origin refused: " + "; ".join(parts) + ". origin lists only the orders whose need "
            f"date this document meets — here origin={', '.join(ok)!r}"
            + (" (nothing on this list qualifies)" if not ok else "")
            + ". To feed the others, buy from a vendor who lands in time (erp.feasible_vendors) "
            "or start earlier (erp.earliest_build). force=True writes it anyway, and the finish "
            "check will refuse it.",
            "purchase.order", "origin")

    def _offers_for(self, vendor_id: int, product_id: int, fields: list[str]) -> list[dict]:
        """This vendor's supplierinfo rows for a product, whether stored on the variant or
        on its template (the seeds use the template)."""
        template = self.search_read(
            "product.product", [("id", "=", product_id)], ["product_tmpl_id"], limit=1)
        template_id = _id_of(template[0]["product_tmpl_id"]) if template else None
        return self.search_read(
            "product.supplierinfo",
            ["&", ("partner_id", "=", vendor_id),
             "|", ("product_id", "=", product_id), ("product_tmpl_id", "=", template_id)],
            fields, limit=20)

    def _delay_for(self, vendor_id: int, product_id: int):
        """Lead time in days for this vendor/product, or None if the vendor does not list it."""
        template = self.search_read(
            "product.product", [("id", "=", product_id)], ["product_tmpl_id"], limit=1)
        template_id = _id_of(template[0]["product_tmpl_id"]) if template else None
        offers = self.search_read(
            "product.supplierinfo",
            ["&", ("partner_id", "=", vendor_id),
             "|", ("product_id", "=", product_id), ("product_tmpl_id", "=", template_id)],
            ["delay"], limit=10)
        return max((o["delay"] or 0) for o in offers) if offers else None

    def vendor_price(self, vendor_id: int, product_id: int, qty: float) -> float:
        """The supplierinfo price for this vendor/product at this quantity tier."""
        template = self.search_read(
            "product.product", [("id", "=", product_id)], ["product_tmpl_id"], limit=1)
        template_id = _id_of(template[0]["product_tmpl_id"]) if template else None
        offers = self.search_read(
            "product.supplierinfo",
            ["&", ("partner_id", "=", vendor_id),
             "|", ("product_id", "=", product_id), ("product_tmpl_id", "=", template_id)],
            ["min_qty", "price"], limit=50, order="min_qty desc")
        for offer in offers:
            if qty >= (offer["min_qty"] or 0):
                return offer["price"]
        return offers[-1]["price"] if offers else 0.0

    def confirm_po(self, po_id: int):
        """`purchase.order.button_confirm`."""
        return self.call("purchase.order", "button_confirm", [[po_id]])

    def receive(self, po_id: int, force: bool = False) -> list[int]:
        """Validate every incoming picking of a PO. Returns the picking ids validated."""
        if not force:
            today = self.today()[:10]
            pending = [l for l in self.po_lines([po_id]).all()
                       if l["date_planned"] and l["date_planned"][:10] > today
                       and (l["qty_received"] or 0) < (l["product_qty"] or 0)]
            if pending:
                soonest = min(l["date_planned"][:10] for l in pending)
                raise OdooError(
                    f"goods on this PO are not due until {soonest} (today is {today}); "
                    "receiving now would record stock that has not arrived. A PO that arrives "
                    "before the customer's due date already covers that demand as it stands: "
                    "leave it confirmed, leave that delivery pending, and the checks will pass. "
                    "force=True is only for a task that explicitly says the goods are already "
                    "here -- a forced early receipt is refused at finish as fabricated stock, "
                    "so it will not make a check pass.",
                    "purchase.order", "receive")
        pickings = self.search_read(
            "stock.picking", [("id", "in", self._po_picking_ids(po_id)),
                              ("state", "not in", ("done", "cancel"))], ["name"], limit=50)
        for picking in pickings:
            self._validate_picking(picking["id"])
        return [p["id"] for p in pickings]

    def _po_picking_ids(self, po_id: int) -> list[int]:
        rows = self.call("purchase.order", "read", [[po_id]], {"fields": ["picking_ids"]})
        return rows[0]["picking_ids"] if rows else []

    def bom_for(self, product_id: int, bom_id: int | None = None) -> dict | None:
        """The BOM Odoo would use for this product: id, lead time (`produce_delay`, days),
        batch qty, and whether it carries operations (work orders need a work centre)."""
        template = self.search_read(
            "product.product", [("id", "=", product_id)], ["product_tmpl_id"], limit=1)
        template_id = _id_of(template[0]["product_tmpl_id"]) if template else None
        domain = [("id", "=", bom_id)] if bom_id else [
            ("product_tmpl_id", "=", template_id),
            "|", ("product_id", "=", product_id), ("product_id", "=", False)]
        rows = self.search_read("mrp.bom", domain,
                                ["produce_delay", "product_qty", "operation_ids"], limit=1)
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "lead_days": r.get("produce_delay") or 0,
                "batch_qty": r.get("product_qty") or 1.0,
                "operation_ids": r.get("operation_ids") or []}

    def create_mo(self, product_id: int, qty: float, bom_id: int | None = None,
                  date_start: str | None = None, date_deadline: str | None = None,
                  origin: str | None = None, workcenter_id: int | None = None,
                  force: bool = False) -> int:
        """`mrp.production.create`. Odoo picks the BOM if one is not named."""
        bom = self.bom_for(product_id, bom_id)
        lead = bom["lead_days"] if bom else 0
        if date_start and not date_deadline:
            date_deadline = _add_days(date_start, lead)
        if date_start and date_deadline and not force:
            earliest_finish = _add_days(date_start, lead)
            if date_deadline[:10] < earliest_finish[:10]:
                raise OdooError(
                    f"date_deadline {date_deadline[:10]} is before date_start {date_start[:10]} + "
                    f"the BOM's {lead}d lead time = {earliest_finish[:10]}. Use that date (or "
                    "omit date_deadline to get it), start earlier, or pass force=True.",
                    "mrp.production", "create_mo")
        if origin and not force:
            self._refuse_bad_origin(date_deadline or date_start or "", origin, "this MO finishes")
            self._refuse_excess_over_origin(product_id, qty, origin, None, "this MO")
        if date_start and not force:
            plan = self.earliest_build(product_id, qty)
            if plan.get("unsourceable"):
                raise OdooError(
                    f"no vendor lists these components: {plan['unsourceable']}; the MO "
                    "cannot be built from purchases. Check stock, BOMs, or pass force=True.",
                    "mrp.production", "create_mo")
            # Credit components already covered by confirmed POs or unfinished MOs landing in time.
            incoming_ok = True
            for comp in plan["components"]:
                if comp.get("source") in ("buy", "make"):
                    on_order = sum(
                        (l["product_qty"] or 0) - (l["qty_received"] or 0)
                        for l in self.search_read(
                            "purchase.order.line",
                            [("product_id", "=", comp["component_id"]),
                             ("state", "in", ("purchase", "done")),
                             ("date_planned", "<=", date_start)],
                            ["product_qty", "qty_received"], limit=50))
                    in_production = sum(
                        m["product_qty"] or 0
                        for m in self.search_read(
                            "mrp.production",
                            [("product_id", "=", comp["component_id"]),
                             ("state", "not in", ("done", "cancel")),
                             "|", ("date_finished", "<=", date_start),
                                  ("date_start", "<=", date_start)],
                            ["product_qty"], limit=50))
                    if on_order + in_production + comp["free_now"] + 1e-6 < comp["needed"]:
                        incoming_ok = False
            if not incoming_ok and date_start[:10] < plan["date_start"][:10]:
                raise OdooError(
                    f"date_start {date_start[:10]} is before the components can be on hand "
                    f"({plan['date_start'][:10]}). Order them first (erp.earliest_build says "
                    "what and from whom), start the MO no earlier than that, or pass "
                    "force=True.", "mrp.production", "create_mo")
        values: dict[str, Any] = {"product_id": product_id, "product_qty": qty}
        if bom_id:
            values["bom_id"] = bom_id
        if date_start:
            values["date_start"] = date_start
        if date_deadline:
            values["date_deadline"] = date_deadline
        if origin:
            values["origin"] = origin
        mo_id = self.call("mrp.production", "create", [values])
        if workcenter_id:
            workorders = self.search_read(
                "mrp.workorder", [("production_id", "=", mo_id)], ["id"], limit=20)
            if workorders:
                self.call("mrp.workorder", "write",
                          [[w["id"] for w in workorders], {"workcenter_id": workcenter_id}])
        return mo_id

    def confirm_mo(self, mo_id: int):
        """`mrp.production.action_confirm`."""
        return self.call("mrp.production", "action_confirm", [[mo_id]])

    def produce(self, mo_id: int, qty: float | None = None):
        """Reserve, set `qty_producing`, consume the components, and mark done."""
        self.call("mrp.production", "action_assign", [[mo_id]])
        rows = self.call("mrp.production", "read", [[mo_id]],
                         {"fields": ["product_qty", "move_raw_ids"]})
        ordered = rows[0]["product_qty"]
        producing = ordered if qty is None else qty
        self.call("mrp.production", "write", [[mo_id], {"qty_producing": producing}])

        raw_ids = rows[0].get("move_raw_ids") or []
        if raw_ids:
            moves = self.call("stock.move", "read", [raw_ids],
                              {"fields": ["product_uom_qty", "state"]})
            # Scale the bill of materials when producing less than the whole order.
            share = (producing / ordered) if ordered else 1.0
            for move in moves:
                if move["state"] in ("done", "cancel"):
                    continue
                self.call("stock.move", "write", [[move["id"]], {
                    "quantity": (move["product_uom_qty"] or 0.0) * share,
                    "picked": True,
                }])

        result = self.call("mrp.production", "button_mark_done", [[mo_id]])
        return self._run_wizard(result, active_model="mrp.production", active_ids=[mo_id])

    def deliver(self, so_id: int) -> list[int]:
        """Validate every outgoing picking of a sales order."""
        rows = self.call("sale.order", "read", [[so_id]], {"fields": ["picking_ids"]})
        picking_ids = rows[0]["picking_ids"] if rows else []
        pickings = self.search_read(
            "stock.picking", [("id", "in", picking_ids), ("state", "not in", ("done", "cancel"))],
            ["name"], limit=50)
        for picking in pickings:
            self._validate_picking(picking["id"])
        return [p["id"] for p in pickings]

    def payment_term_id(self, name: str) -> int:
        """The `account.payment.term` whose name contains `name` (e.g. "Immediate")."""
        rows = self.search_read("account.payment.term", [("name", "ilike", name)], ["name"], limit=5)
        if not rows:
            names = [r["name"] for r in self.search_read("account.payment.term", [], ["name"], limit=20)]
            raise OdooError(f"no payment term matching {name!r}; available: {names}",
                            "account.payment.term", "payment_term_id")
        exact = [r for r in rows if r["name"].lower() == name.lower()]
        return (exact or rows)[0]["id"]

    def invoice(self, so_id: int, method: str = "delivered", amount: float | None = None,
                payment_term: str | None = None) -> list[int]:
        """Create customer invoice(s) for a sales order via `sale.advance.payment.inv`."""
        # Set the payment term on the order first so invoices inherit it, then on the invoices too.
        term_id = self.payment_term_id(payment_term) if payment_term else None
        if term_id:
            self.call("sale.order", "write", [[so_id], {"payment_term_id": term_id}])
        if method not in ("delivered", "percentage", "fixed"):
            raise OdooError(f"method must be delivered, percentage or fixed, not {method!r}",
                            "sale.advance.payment.inv", "invoice")
        if method != "delivered" and amount is None:
            raise OdooError(f"method={method!r} needs an amount", "sale.advance.payment.inv", "invoice")
        values: dict[str, Any] = {"advance_payment_method": method}
        if method != "delivered":
            values["amount"] = amount if method == "percentage" else None
            values["fixed_amount"] = amount if method == "fixed" else None
            values = {k: v for k, v in values.items() if v is not None}
        before = set(self._so_invoice_ids(so_id))
        context = {"active_model": "sale.order", "active_ids": [so_id], "active_id": so_id}
        wizard_id = self.call(
            "sale.advance.payment.inv", "create", [values], {"context": context})
        self.call("sale.advance.payment.inv", "create_invoices", [[wizard_id]],
                  {"context": context})
        created = sorted(set(self._so_invoice_ids(so_id)) - before)
        if not created:
            raise OdooError(
                "create_invoices produced no invoice; check that the order is confirmed "
                "and something has been delivered",
                "sale.advance.payment.inv", "create_invoices")
        if term_id and created:
            self.call("account.move", "write", [created, {"invoice_payment_term_id": term_id}])
        return created

    def _so_invoice_ids(self, so_id: int) -> list[int]:
        rows = self.call("sale.order", "read", [[so_id]], {"fields": ["invoice_ids"]})
        return rows[0]["invoice_ids"] if rows else []

    def post(self, invoice_ids: int | Iterable[int]):
        """`account.move.action_post`."""
        return self.call("account.move", "action_post", [_ids(invoice_ids)])

    def cancel(self, model: str, ids: int | Iterable[int]):
        """Cancel records with whichever cancel method the model exposes."""
        methods = {
            "purchase.order": "button_cancel",
            "sale.order": "action_cancel",
            "mrp.production": "action_cancel",
            "stock.picking": "action_cancel",
            "account.move": "button_cancel",
        }
        return self.call(model, methods.get(model, "action_cancel"), [_ids(ids)])

    def _validate_picking(self, picking_id: int):
        """Reserve, write done quantities, validate, and clear any backorder wizard."""
        self.call("stock.picking", "action_assign", [[picking_id]])
        moves = self.search_read(
            "stock.move", [("picking_id", "=", picking_id), ("state", "not in", ("done", "cancel"))],
            ["product_uom_qty", "quantity"], limit=200)
        for move in moves:
            self.call("stock.move", "write",
                      [[move["id"]], {"quantity": move["product_uom_qty"], "picked": True}])
        result = self.call("stock.picking", "button_validate", [[picking_id]])
        return self._run_wizard(result, active_model="stock.picking", active_ids=[picking_id])

    # A returned action means Odoo wants a wizard answered. Map each wizard to the method
    # that means "yes, proceed as I asked" — for backorders that is "no backorder".
    WIZARD_METHODS = {
        "stock.backorder.confirmation": "process_cancel_backorder",
        "stock.immediate.transfer": "process",
        "mrp.consumption.warning": "action_confirm",
        "mrp.immediate.production": "process",
    }

    def _run_wizard(self, action: Any, active_model: str, active_ids: list[int]):
        """Complete a wizard action returned by a button, recursing while more appear."""
        for _ in range(4):  # a chain of two is normal; four is a runaway
            if not isinstance(action, dict) or not action.get("res_model"):
                return action
            model = action["res_model"]
            method = self.WIZARD_METHODS.get(model)
            if method is None:
                raise OdooError(
                    f"unhandled wizard {model}; inspect it with "
                    f"erp.fields({model!r}) and call it via erp.call(...)",
                    active_model, "wizard")
            context = dict(action.get("context") or {})
            context.setdefault("active_model", active_model)
            context.setdefault("active_ids", active_ids)
            context.setdefault("active_id", active_ids[0])
            wizard_id = action.get("res_id") or self.call(
                model, "create", [{}], {"context": context})
            action = self.call(model, method, [[wizard_id]], {"context": context})
        raise OdooError("wizard chain did not settle after 4 steps", active_model, "wizard")


def _strip_html(text: str) -> str:
    """Odoo stores Internal Notes as HTML; the agent wants the sentence."""
    import re

    plain = re.sub(r"<[^>]+>", " ", text or "")
    plain = plain.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(plain.split())


erp = Erp()
