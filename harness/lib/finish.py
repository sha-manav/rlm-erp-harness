"""The finish tool: runs the checks and refuses (up to three times) while a hard check
fails, nothing has been written, or a required audit delegation has not run.
"""

from __future__ import annotations

import os
from pathlib import Path

EXPORTS = ["finish", "FINISH_SENTINEL"]

FINISH_SENTINEL = "__ERP_HARNESS_" "FINISHED__"   # split so a printed source never contains it
MAX_REFUSALS = 3
SUMMARY_PATH = os.environ.get("ERP_SUMMARY_PATH", "/output/summary.md")

_state = {"refusals": 0, "finished": False, "baseline": set(), "require_delegation": False}


def reset() -> None:
    """Start a fresh episode (used by tests and by the agent at task start)."""
    _state["refusals"] = 0
    _state["finished"] = False
    _state["baseline"] = set()
    _state["require_delegation"] = False


def set_baseline(failing_ids) -> list[str]:
    """Record checks that were already failing before the agent acted."""
    _state["baseline"] = set(failing_ids)
    return sorted(_state["baseline"])


def record_baseline(client=None) -> list[str]:
    """Run the invariants now and remember which already fail. Called at task start."""
    try:
        from . import check as check_module
    except ImportError:
        return set_baseline([])
    table = check_module.invariants(client)
    return set_baseline(r["check"] for r in table.all() if r["status"] == "FAIL")


def refusals() -> int:
    return _state["refusals"]


def _write_summary(summary: str, checks_text: str) -> str:
    path = Path(SUMMARY_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{summary.strip()}\n\n## Checks at finish\n\n```\n{checks_text}\n```\n")
        return str(path)
    except OSError as exc:
        # Never lose a finished episode over a missing directory.
        return f"(could not write {path}: {exc})"


def require_delegation(flag: bool = True) -> None:
    """Make `finish` refuse until at least one sub-agent report exists (config C_full_audit)."""
    _state["require_delegation"] = bool(flag)


def _delegations_run() -> int:
    try:
        from . import delegate as delegate_module
        return len(delegate_module.reports)
    except Exception:
        return 0


def _main_write_count() -> int:
    try:
        from .erp import erp
        return len(erp.write_log)
    except Exception:
        return -1


def finish(summary: str = "", client=None, gate: bool = True) -> str:
    """End the episode, if the hard checks agree."""
    # `check` is ablatable (C_minus_dry ships without it), so it is imported here rather
    # than at module load: no checks simply means no gate, not a broken finish tool.
    try:
        from . import check as check_module
    except ImportError:
        check_module = None

    if check_module is None:
        checks_text = "(no checks in this configuration)"
        failing, preexisting = [], []
    else:
        table = check_module.all(client)
        checks_text = str(table)
        hard_fails = [r for r in table.all() if r["status"] == "FAIL" and r["hard"] == "hard"]
        # Pre-existing failures are labelled but still block: repair tasks exist to fix them.
        for row in hard_fails:
            if row["check"] in _state["baseline"]:
                row["evidence"] = "(already failing at task start) " + row["evidence"]
        failing = hard_fails

    # Refuse once when nothing was written to main; a second call with an explanation goes through.
    if gate and not _state.get("empty_refused") and _main_write_count() == 0:
        _state["empty_refused"] = True
        _state["refusals"] += 1
        return (
            f"finish refused (attempt {_state['refusals']}/{MAX_REFUSALS}): no changes have "
            "been written to the main database (erp.write_log is empty). A rehearsal on a "
            "clone does not count — run the plan against `erp` itself. If this task genuinely "
            "requires no change, say so in the summary and call finish again."
        )

    # C_full_audit: an audit delegation must have run; refused like a failing hard check.
    if (gate and _state.get("require_delegation") and _delegations_run() == 0
            and _state["refusals"] < MAX_REFUSALS - 1):
        _state["refusals"] += 1
        return (
            f"finish refused (attempt {_state['refusals']}/{MAX_REFUSALS}): no audit has run. "
            "Delegate one independent audit of the main database against the instruction "
            "first — delegate(\"Audit the main database against these requirements: …\") — "
            "act on its findings, then call finish again."
        )

    if gate and failing and _state["refusals"] < MAX_REFUSALS - 1:
        _state["refusals"] += 1
        lines = "\n".join(f"  {row['check']}: {row['evidence']}" for row in failing)
        return (
            f"finish refused (attempt {_state['refusals']}/{MAX_REFUSALS}): "
            f"fix or downgrade.\n{len(failing)} hard check(s) failing:\n{lines}\n\n"
            "Fix them and call finish again, or explain in the summary why a check does "
            "not apply to this task."
        )

    _state["finished"] = True
    written = _write_summary(summary or "(no summary given)", checks_text)
    note = ""
    if failing:
        note = (
            f"\nProceeding with {len(failing)} hard check(s) still failing after "
            f"{_state['refusals']} refusal(s):\n"
            + "\n".join(f"  {row['check']}: {row['evidence']}" for row in failing)
        )
    return f"{FINISH_SENTINEL}\nsummary written to {written}{note}"


# `finish.reset()` / `finish.record_baseline()` are what a reader of the module docs
# reaches for; they cost a pass-3 trial its ending. Both spellings work.
finish.reset = reset                    # type: ignore[attr-defined]
finish.record_baseline = record_baseline  # type: ignore[attr-defined]
finish.set_baseline = set_baseline      # type: ignore[attr-defined]
