"""Sub-agents (container side): the read-only erp proxy, the namespace lockdown, and the
delegate() kernel function that hands a task to the host.
"""

from __future__ import annotations

import json

EXPORTS = ["delegate", "ErpReadOnly"]

DELEGATE_SENTINEL = "__ERP_HARNESS_" "DELEGATE__"   # split so a printed source never contains it
DEFAULT_MAX_STEPS = 40
MAX_REPORT_CHARS = 1600        # ~400 tokens: a report, not a transcript

# `erp` methods a sub-agent may call. Everything not listed raises PermissionError,
# so a new write helper added to erp.py is refused by default rather than let through.
READ_METHODS = frozenset({
    "search_read", "get", "fields", "count", "call", "on", "today",
    "sales_orders", "so_lines", "stock", "boms", "suppliers", "purchase_orders",
    "po_lines", "productions", "pickings", "workcenters", "workcenter_options",
    "invoices", "feasible_vendors", "cheapest_buy", "earliest_build", "bom_for",
    "vendor_price", "origin_capacity", "payment_term_id",
})
READ_ATTRIBUTES = frozenset({"db", "url", "user", "uid"})
# Raw `execute_kw` methods that read. `write_log` on the real client treats the same set
# as reads, plus the ones here that only search or describe.
RAW_READ_METHODS = frozenset({
    "read", "search", "search_read", "search_count", "fields_get", "name_search",
    "read_group", "name_get", "default_get", "check_access_rights",
})


class ErpReadOnly:
    """A read-only `Erp`: reads and planning helpers pass through, writes raise
    PermissionError.
    """

    def __init__(self, client):
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str):
        client = object.__getattribute__(self, "_client")
        if name in READ_ATTRIBUTES:
            return getattr(client, name)
        if name == "write_log":
            return list(client.write_log)
        if name == "on":
            return lambda db: ErpReadOnly(client.on(db))
        if name == "call":
            return self._call
        if name in READ_METHODS:
            return getattr(client, name)
        if name.startswith("_"):
            raise AttributeError(name)
        if hasattr(client, name):
            raise PermissionError(
                f"sub-agents are read-only: erp.{name} would change the database. "
                "Report what should be done; the root agent does it.")
        raise AttributeError(f"erp has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        raise PermissionError("sub-agents are read-only: the erp client cannot be modified")

    def _call(self, model: str, method: str, args=(), kwargs=None):
        if method not in RAW_READ_METHODS:
            raise PermissionError(
                f"sub-agents are read-only: erp.call({model!r}, {method!r}) is not a read. "
                "Report what should be done; the root agent does it.")
        client = object.__getattribute__(self, "_client")
        return client.call(model, method, args, kwargs)

    def __repr__(self) -> str:
        client = object.__getattribute__(self, "_client")
        return f"ErpReadOnly({client.db})"


class _ReadOnlyModule:
    """A module facade with a few names refused — `check` without `register`/`clear`."""

    def __init__(self, module, denied: tuple[str, ...]):
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_denied", denied)

    def __getattr__(self, name: str):
        if name in object.__getattribute__(self, "_denied"):
            raise PermissionError(
                f"sub-agents cannot call {name}: the root agent owns the rule set.")
        return getattr(object.__getattribute__(self, "_module"), name)

    def __setattr__(self, name: str, value) -> None:
        raise PermissionError("sub-agents cannot modify the library")

    def __repr__(self) -> str:
        return f"<read-only {object.__getattribute__(self, '_module').__name__}>"


# Executed by the host in a fresh kernel namespace before a sub-agent's first step.
# It rebinds what the namespace preload put there; nothing else is imported.
SUB_NAMESPACE_SETUP = """
from lib.delegate import ErpReadOnly as _ErpReadOnly, _ReadOnlyModule as _ROM
erp = _ErpReadOnly(erp)
for _name in ("state", "finish", "Erp", "delegate", "lib", "FINISH_SENTINEL"):
    globals().pop(_name, None)
if "check" in globals():
    check = _ROM(check, ("register", "clear"))
if "plan" in globals():
    from lib.plan import Plan as _Plan
    plan = _Plan()
del _ErpReadOnly, _ROM, _name
"""


# -- the root agent's side ----------------------------------------------------
reports: dict[int, str] = {}
_pending: list[dict] = []
_counter = 0


def delegate(task: str, max_steps: int = DEFAULT_MAX_STEPS) -> str:
    """Hand a self-contained question to a fresh read-only sub-agent; its ~400-token report
    appears in this tool result and in `delegate.reports[n]`.
    """
    global _counter
    _counter += 1
    n = _counter
    request = {"n": n, "task": str(task), "max_steps": int(max_steps or DEFAULT_MAX_STEPS)}
    _pending.append(request)
    # The host replaces this line with the sub-agent's report in the same tool result.
    print(f"{DELEGATE_SENTINEL}{json.dumps(request)}")
    return f"(delegation #{n} requested; its report follows in this tool result)"


delegate.reports = reports   # type: ignore[attr-defined]


def _deliver(n: int, report: str) -> None:
    """Called by the host once sub-agent `n` has answered."""
    reports[int(n)] = report
    _pending[:] = [r for r in _pending if r["n"] != int(n)]


def _take_pending() -> list[dict]:
    taken = list(_pending)
    _pending.clear()
    return taken
