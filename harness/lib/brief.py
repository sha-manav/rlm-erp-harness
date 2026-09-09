"""A short summary of the task environment (demand, stock, offers, BOMs, work centres),
injected into the first user message.
"""

from __future__ import annotations

from .fmt import Table

EXPORTS = ["brief"]

CHAR_BUDGET = 7900   # ~2,000 tokens at the measured 0.30 tokens/char; sections sum to 6,300


def _section(title: str, table: Table, rows: int, chars: int) -> str:
    """One section, cut by rows first and then by characters, never silently."""
    table.max_rows = rows
    body = str(table)
    if len(body) > chars:
        kept = body[:chars].rsplit("\n", 1)[0]
        body = f"{kept}\n… (section cut to budget; {len(table)} rows total — query `erp` for the rest)"
    return f"### {title}\n{body}"


# (rows, chars) per section. Vendor offers get the most: MOQ, price and lead time are
# where the stock harness's failures come from.
SECTIONS = {
    "products": (30, 1500),
    "suppliers": (30, 2000),
    "boms": (25, 1000),
    "sales": (12, 500),
    "purchases": (12, 500),
    "productions": (12, 400),
    "workcenters": (10, 1400),
}


def render(client=None, char_budget: int = CHAR_BUDGET) -> str:
    """Return the briefing text. Never raises: a section that fails says so and moves on."""
    if client is None:
        from .erp import erp as client

    parts: list[str] = []

    def add(title: str, fn, key: str) -> None:
        rows, chars = SECTIONS[key]
        try:
            table = fn()
            if len(table) == 0:
                parts.append(f"### {title}\n(none)")
            else:
                parts.append(_section(title, table, rows, chars))
        except Exception as exc:
            parts.append(f"### {title}\n(unavailable: {type(exc).__name__}: {str(exc)[:120]})")

    try:
        company = client.search_read("res.company", [], ["name"], limit=1)
        now = client.call("res.users", "read", [[client.uid]], {"fields": ["login_date"]})
        parts.append(
            f"Company: {company[0]['name'] if company else '?'}. "
            f"Server time (UTC): {(now[0].get('login_date') or '?') if now else '?'}. "
            "Dates in Odoo are UTC strings 'YYYY-MM-DD HH:MM:SS'.")
    except Exception:
        pass

    def products():
        rows = client.search_read(
            "product.product",
            [("type", "!=", "service"), ("sale_ok", "=", True)],
            ["default_code", "name", "qty_available", "list_price", "standard_price"],
            limit=60, order="default_code asc")
        return Table(
            [{"id": r["id"], "code": r["default_code"] or "", "name": r["name"],
              "on_hand": r["qty_available"], "list_price": r["list_price"],
              "cost": r["standard_price"]} for r in rows],
            ["id", "code", "name", "on_hand", "list_price", "cost"])

    add("Products (sellable, with stock on hand)", products, "products")
    add("Vendor offers (MOQ, price, lead time, and the vendor's own notes)",
        lambda: client.suppliers(), "suppliers")
    add("Bills of materials", lambda: client.boms(), "boms")
    add("Open sales orders", lambda: client.sales_orders(), "sales")
    add("Open purchase orders", lambda: client.purchase_orders(), "purchases")
    add("Manufacturing orders", lambda: client.productions(), "productions")
    add("Work centres (capacity)", lambda: client.workcenters(), "workcenters")

    text = "\n\n".join(parts)
    if len(text) > char_budget:   # belt and braces; the section budgets sum below this
        text = text[:char_budget].rsplit("\n", 1)[0] + "\n… (briefing cut to budget)"
    return text


brief = render
