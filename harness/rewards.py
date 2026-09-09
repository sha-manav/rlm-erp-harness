"""Read the verifier's reward.json: overall score, pass flag, failed rule names."""

from __future__ import annotations

from typing import Any

# Nested blocks that hold lists of per-rule detail rather than scalars.
_SKIP_SUBKEYS = frozenset({"by_dimension", "rules_detail"})


def flatten_rewards(raw: Any, prefix: str = "") -> dict[str, float | int]:
    """Flatten nested reward blocks into scalar ``key_subkey`` entries."""
    flat: dict[str, float | int] = {}
    if not isinstance(raw, dict):
        return flat
    for key, value in raw.items():
        name = f"{prefix}{key}"
        if isinstance(value, bool):
            flat[name] = int(value)
        elif isinstance(value, (int, float)):
            flat[name] = value
        elif isinstance(value, dict) and key not in _SKIP_SUBKEYS:
            flat.update(flatten_rewards(value, prefix=f"{name}_"))
    return flat


def failed_rule_names(raw: dict, limit: int | None = None) -> list[str]:
    """The rules the verifier marked FAIL, as ``dimension: expression`` strings."""
    by_dimension = ((raw.get("rules") or {}).get("by_dimension")) or {}
    names: list[str] = []
    for dimension, rules in by_dimension.items():
        for rule in rules if isinstance(rules, list) else []:
            # A rule counts as failed only when the verifier marks it FAIL and applicable.
            if rule.get("status") == "FAIL" and rule.get("applicable") is not False:
                names.append(f"{dimension}: {rule.get('expr') or rule.get('rule')}")
    return names[:limit] if limit else names
