"""Database snapshots (CREATE DATABASE ... TEMPLATE) and dry runs of a plan on a clone."""

from __future__ import annotations

import os
import re

from .fmt import Table

EXPORTS = ["state"]

# The models whose movement constitutes "what this task changed".
DIFF_MODELS = [
    ("sale_order", "sale.order"),
    ("sale_order_line", "sale.order.line"),
    ("purchase_order", "purchase.order"),
    ("purchase_order_line", "purchase.order.line"),
    ("mrp_production", "mrp.production"),
    ("stock_picking", "stock.picking"),
    ("stock_move", "stock.move"),
    ("account_move", "account.move"),
]


def _admin(dbname: str = "postgres"):
    import psycopg2

    connection = psycopg2.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ.get("PGUSER", "odoo"),
        password=os.environ.get("PGPASSWORD", "odoo"),
        dbname=dbname,
        connect_timeout=10,
    )
    connection.autocommit = True            # CREATE/DROP DATABASE cannot run in a transaction
    return connection


def _safe(name: str) -> str:
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,48}", name):
        raise ValueError(
            f"snapshot name {name!r} must be letters, digits and underscores, "
            "starting with a letter")
    return name


class State:
    def __init__(self, source: str | None = None):
        self.source = source or os.environ.get("ODOO_DB", "bench")

    def snapshot(self, name: str) -> str:
        """Clone the working database under `name`, replacing any existing clone."""
        _safe(name)
        connection = _admin()
        try:
            with connection.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
                # TEMPLATE requires no other session on the source; Odoo holds a pool open.
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()", (self.source,))
                cur.execute(f'CREATE DATABASE "{name}" TEMPLATE "{self.source}"')
        finally:
            connection.close()
        return name

    def drop(self, name: str) -> str:
        _safe(name)
        connection = _admin()
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            connection.close()
        return name

    def list(self) -> Table:
        connection = _admin()
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT datname FROM pg_database WHERE datistemplate = false "
                    "ORDER BY datname")
                rows = [{"database": r[0], "is_source": r[0] == self.source}
                        for r in cur.fetchall()]
        finally:
            connection.close()
        return Table(rows, ["database", "is_source"], "databases")

    def _counts(self, dbname: str) -> dict:
        connection = _admin(dbname)
        counts: dict[str, dict] = {}
        try:
            with connection.cursor() as cur:
                for table, model in DIFF_MODELS:
                    try:
                        cur.execute(f"SELECT count(*) FROM {table}")
                        total = cur.fetchone()[0]
                        cur.execute(
                            f"SELECT count(*) FROM {table} "
                            "WHERE write_date IS DISTINCT FROM create_date")
                        changed = cur.fetchone()[0]
                    except Exception:
                        connection.rollback()
                        continue
                    counts[model] = {"rows": total, "modified": changed}
                try:
                    cur.execute(
                        "SELECT coalesce(sum(amount_total), 0) FROM purchase_order "
                        "WHERE state IN ('purchase', 'done')")
                    counts["_cash_committed"] = {"rows": float(cur.fetchone()[0]), "modified": 0}
                    cur.execute("SELECT count(*) FROM account_move WHERE state = 'posted'")
                    counts["_invoices_posted"] = {"rows": cur.fetchone()[0], "modified": 0}
                except Exception:
                    connection.rollback()
        finally:
            connection.close()
        return counts

    def diff(self, a: str = "start", b: str | None = None) -> Table:
        """What changed between two databases, per model."""
        b = b or self.source
        left, right = self._counts(a), self._counts(b)
        rows = []
        for model in sorted(set(left) | set(right)):
            before = left.get(model, {"rows": 0, "modified": 0})
            after = right.get(model, {"rows": 0, "modified": 0})
            created = after["rows"] - before["rows"]
            changed = after["modified"] - before["modified"]
            if created or changed:
                rows.append({
                    "model": model.lstrip("_"),
                    "created": round(created, 2) if isinstance(created, float) else created,
                    "changed": changed,
                })
        title = f"diff {a} -> {b}"
        return Table(rows, ["model", "created", "changed"], title) if rows else Table(
            [], ["model", "created", "changed"], f"{title} (no differences)")


    def rehearse(self, plan_fn, name: str = "rehearsal"):
        """Run `plan_fn(client)` on a throwaway clone and return `check.all()` for it."""
        from .erp import erp
        from . import check as check_module
        from .fmt import Table

        self.snapshot(name)
        client = erp.on(name)
        main_writes_before = len(erp.write_log)
        try:
            plan_fn(client)
            table = check_module.all(client)
        finally:
            try:
                self.drop(name)
            except Exception:
                pass

        # A plan_fn that used the global erp wrote to the real database; report it as a hard failure.
        leaked = len(erp.write_log) - main_writes_before
        if leaked:
            rows = table.all() + [{
                "check": "rehearsal_isolation", "hard": "hard", "status": "FAIL",
                "evidence": (f"plan_fn made {leaked} write(s) to the MAIN database during the "
                             "rehearsal: it must use the `client` argument for every call, "
                             "not `erp`. Those writes are real; review them before continuing."),
            }]
            table = Table(rows, ["check", "hard", "status", "evidence"], "checks", max_rows=60)
        return table


state = State()
