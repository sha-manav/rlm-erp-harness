"""The library preloaded into every kernel namespace. __all__ is whatever modules were
shipped for this config; each module's EXPORTS are bound directly in the namespace.
"""

from __future__ import annotations

from pathlib import Path

# Load order matters: later modules import earlier ones (check needs erp, state needs db).
_PREFERRED_ORDER = ["fmt", "erp", "db", "state", "plan", "check", "finish", "delegate"]


def _discover() -> list[str]:
    present = {
        path.stem
        for path in Path(__file__).parent.glob("*.py")
        if path.stem != "__init__"
    }
    ordered = [name for name in _PREFERRED_ORDER if name in present]
    ordered += sorted(present - set(ordered))
    return ordered


__all__ = _discover()
