#!/usr/bin/env python3
"""Collect every ingested trial into analysis/results.csv and print a per-config summary.

    python3 scripts/aggregate.py [--runs runs] [--out analysis/results.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.discover import read_task_meta  # noqa: E402

COLUMNS = [
    "task_id", "pattern", "difficulty", "config", "model", "model_key",
    "pass", "reward", "steps", "delegations",
    "tokens_in", "tokens_cached", "tokens_out",
    "cost_usd", "wallclock_s", "terminal_reason", "commit", "run_id",
]


def task_metadata(tasks_dir: Path) -> dict[str, dict[str, str]]:
    meta: dict[str, dict[str, str]] = {}
    for task_dir in sorted(tasks_dir.iterdir()):
        if (task_dir / "task.toml").exists():
            meta[task_dir.name] = read_task_meta(task_dir)
    return meta


def rows_for_run(run_dir: Path, meta: dict[str, dict[str, str]]) -> list[dict]:
    rows = []
    for result_path in sorted(run_dir.glob("*/result.json")):
        result = json.loads(result_path.read_text())
        if result.get("reward") is None:
            # A trial with no verifier reward never completed (killed mid-flight); it is
            # not a measurement, and counting it inflates n and deflates every mean.
            continue
        task_id = result["task_id"]
        task_meta = meta.get(task_id, {})
        tokens = result.get("tokens") or {}
        rows.append({
            "task_id": task_id,
            "pattern": task_meta.get("task_pattern", ""),
            "difficulty": task_meta.get("difficulty", ""),
            "config": result.get("config"),
            "model": result.get("model"),
            "model_key": result.get("model_key"),
            "pass": result.get("pass"),
            "reward": result.get("reward"),
            "steps": result.get("steps"),
            "delegations": result.get("delegations"),
            "tokens_in": tokens.get("input"),
            "tokens_cached": tokens.get("cached"),
            "tokens_out": tokens.get("output"),
            "cost_usd": result.get("cost_usd"),
            "wallclock_s": result.get("wallclock_s"),
            "terminal_reason": result.get("terminal_reason"),
            "commit": result.get("commit"),
            "run_id": run_dir.name,
        })
    return rows


def summarise(rows: list[dict]) -> None:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["config"], row["model_key"], row["run_id"].split("__")[2] if row["run_id"].count("__") >= 2 else "?")].append(row)

    print(f"\n{'config':<16}{'model':<7}{'set':<10}{'n':>4}{'pass@1':>9}{'reward':>9}"
          f"{'steps':>7}{'$/task':>9}  terminal reasons")
    for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        group = groups[key]
        n = len(group)
        passes = [r["pass"] for r in group if r["pass"] is not None]
        rewards = [r["reward"] for r in group if r["reward"] is not None]
        steps = [r["steps"] for r in group if r["steps"]]
        costs = [r["cost_usd"] for r in group if r["cost_usd"] is not None]
        reasons = defaultdict(int)
        for row in group:
            reasons[row["terminal_reason"]] += 1
        pass_rate = f"{100 * sum(passes) / len(passes):.1f}" if passes else "-"
        mean_reward = f"{sum(rewards) / len(rewards):.1f}" if rewards else "-"
        mean_steps = f"{sum(steps) / len(steps):.1f}" if steps else "-"
        mean_cost = f"{sum(costs) / len(costs):.3f}" if costs else "-"
        mix = " ".join(f"{k}:{v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
        print(f"{str(key[0]):<16}{str(key[1]):<7}{str(key[2]):<10}{n:>4}{pass_rate:>9}{mean_reward:>9}"
              f"{mean_steps:>7}{mean_cost:>9}  {mix}")

        api_errors = reasons.get("api_error", 0) + reasons.get("crash", 0)
        if n and api_errors / n > 0.05:
            print(f"  !! {api_errors}/{n} trials ended in api_error/crash (>5%): "
                  "fix the infrastructure and rerun the whole config, never single tasks")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", default=str(REPO_ROOT / "runs"))
    parser.add_argument("--tasks-dir", default=str(REPO_ROOT / "vendor/erp-bench/tasks"))
    parser.add_argument("--out", default=str(REPO_ROOT / "analysis/results.csv"))
    args = parser.parse_args()

    meta = task_metadata(Path(args.tasks_dir))
    runs_dir = Path(args.runs)
    rows: list[dict] = []
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        # Runs marked invalid (e.g. an out-of-credit provider failure) are excluded:
        # their numbers measure the outage, not the harness.
        run_meta = run_dir / "meta.json"
        if run_meta.exists() and (json.loads(run_meta.read_text()).get("invalid")):
            print(f"skipping {run_dir.name}: marked invalid in meta.json")
            continue
        rows.extend(rows_for_run(run_dir, meta))

    if not rows:
        print("no ingested trials found — run scripts/ingest_harbor.py first")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out_path} ({len(rows)} rows)")
    summarise(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
