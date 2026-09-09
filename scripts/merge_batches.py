#!/usr/bin/env python3
"""Merge the batches of one config on one task set: first completed result per task,
api_error/crash excluded.

    python3 scripts/merge_batches.py --set eval100 --out analysis/eval100_C_full.json runs/C_full__big__eval100__*
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEAD = {"api_error", "crash"}


def task_ids(set_name: str) -> list[str]:
    path = REPO_ROOT / "configs" / f"{set_name}.txt"
    return [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]


def merge(run_dirs: list[Path], ids: list[str]) -> tuple[dict[str, dict], dict[str, list[str]]]:
    merged: dict[str, dict] = {}
    dead: dict[str, list[str]] = {}
    for run in run_dirs:
        for path in sorted(run.glob("*/result.json")):
            r = json.loads(path.read_text())
            tid = r.get("task_id")
            if tid not in ids or r.get("reward") is None:
                continue
            if r.get("terminal_reason") in DEAD:
                dead.setdefault(tid, []).append(run.name)
                continue
            merged.setdefault(tid, r)
    return merged, dead


def summarise(merged: dict[str, dict], total: int) -> dict:
    rows = list(merged.values())
    n = len(rows)
    if not n:
        return {"n": 0, "of": total, "passes": 0, "pass_rate": 0.0, "mean_reward": 0.0, "mean_steps": 0.0,
                "input_tokens_per_task": 0.0, "cache_pct": 0.0, "output_tokens_per_task": 0.0,
                "cost_per_task": 0.0, "terminal": {}}
    tin = sum(r["tokens"]["input"] for r in rows)
    cached = sum(r["tokens"]["cached"] for r in rows)
    terms: dict[str, int] = {}
    for r in rows:
        terms[r["terminal_reason"]] = terms.get(r["terminal_reason"], 0) + 1
    return {
        "n": n, "of": total, "passes": sum(1 for r in rows if r["pass"]),
        "pass_rate": 100 * sum(1 for r in rows if r["pass"]) / n,
        "mean_reward": sum(r["reward"] for r in rows) / n,
        "mean_steps": sum(r["steps"] for r in rows) / n,
        "input_tokens_per_task": tin / n, "cache_pct": 100 * cached / tin if tin else 0,
        "output_tokens_per_task": sum(r["tokens"].get("output", 0) for r in rows) / n,
        "cost_per_task": sum(r["cost_usd"] for r in rows) / n,
        "terminal": terms,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--set", required=True)
    parser.add_argument("--out", help="write the merged per-task table (JSON) here")
    args = parser.parse_args()
    ids = task_ids(args.set)
    merged, dead = merge([Path(r) for r in args.runs], ids)
    s = summarise(merged, len(ids))
    missing = [t for t in ids if t not in merged]
    print(f"{args.set}: {s['n']}/{s['of']} tasks with a valid result; pass {s['passes']}/{s['n']} "
          f"({s['pass_rate']:.1f}%), mean reward {s['mean_reward']:.1f}, steps {s['mean_steps']:.1f}, "
          f"input {s['input_tokens_per_task']/1e3:.0f}k/task (cache {s['cache_pct']:.0f}%), "
          f"output {s['output_tokens_per_task']/1e3:.1f}k, ${s['cost_per_task']:.3f}/task, terminal {s['terminal']}")
    if missing:
        print(f"missing ({len(missing)}): {', '.join(t[:30] for t in missing[:10])}{' …' if len(missing) > 10 else ''}")
    dead_only = [t for t in dead if t not in merged]
    if dead_only:
        print(f"dead only (excluded): {len(dead_only)}")
    if args.out:
        Path(args.out).write_text(json.dumps({"set": args.set, "runs": args.runs, "summary": s,
                                              "tasks": {t: {k: merged[t][k] for k in ("pass", "reward", "steps", "cost_usd", "terminal_reason")}
                                                        for t in merged}}, indent=1))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
