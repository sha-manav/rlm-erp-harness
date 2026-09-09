#!/usr/bin/env python3
"""Convert Harbor job output (ours or pi's) into the common schema: per-trial result.json
and trajectory.jsonl under runs/<run>/<task>/.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _duration_s(block: dict | None) -> float | None:
    if not block or not block.get("started_at") or not block.get("finished_at"):
        return None
    started = datetime.fromisoformat(block["started_at"].replace("Z", "+00:00"))
    finished = datetime.fromisoformat(block["finished_at"].replace("Z", "+00:00"))
    return round((finished - started).total_seconds(), 2)


def read_reward(trial_dir: Path) -> tuple[float | None, bool | None, dict]:
    raw = _load_json(trial_dir / "verifier" / "reward.json")
    if isinstance(raw, dict):
        score = raw.get("overall_score")
        passed = raw.get("passed")
        return (float(score) if score is not None else None,
                bool(passed) if passed is not None else None,
                raw)
    text_path = trial_dir / "verifier" / "reward.txt"
    if text_path.exists():
        try:
            score = float(text_path.read_text().strip())
            return score, None, {"reward_txt": score}
        except ValueError:
            pass
    return None, None, {}


def parse_pi_trajectory(pi_txt: Path) -> tuple[list[dict], dict, int]:
    """pi NDJSON -> common-schema events, token totals, step count."""
    events: list[dict] = []
    totals = {"input": 0, "cached": 0, "output": 0, "cost_usd": 0.0}
    step = 0

    for line in pi_txt.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue

        kind = raw.get("type")
        if kind == "turn_start":
            step += 1
            continue
        if kind == "message_end":
            message = raw.get("message") or {}
            # Only assistant messages carry usage; tool results arrive via tool_execution_end.
            if message.get("role") == "toolResult":
                continue
            usage = message.get("usage") or {}
            cached = usage.get("cacheRead", 0) or 0
            uncached = usage.get("input", 0) or 0
            output = usage.get("output", 0) or 0
            totals["input"] += uncached + cached
            totals["cached"] += cached
            totals["output"] += output
            totals["cost_usd"] += (usage.get("cost") or {}).get("total", 0.0) or 0.0

            content = message.get("content")
            text_parts, tool_name, tool_args = [], None, None
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "toolCall":
                    tool_name = part.get("name")
                    tool_args = part.get("arguments") or part.get("input")
                elif part.get("type") in ("text", "thinking"):
                    text_parts.append(part.get("text") or "")
            events.append({
                "t": step,
                "agent_id": "root",
                "role": message.get("role", "assistant"),
                "content": "\n".join(p for p in text_parts if p),
                "tool": tool_name,
                "args": tool_args,
                "output": None,
                "usage": {"input": uncached + cached, "cached": cached, "output": output},
                "latency_s": None,
                "ts": raw.get("timestamp"),
            })
            continue
        if kind == "tool_execution_end":
            result = raw.get("result")
            if isinstance(result, dict):
                text = result.get("output") or result.get("content") or ""
            else:
                text = result or ""
            events.append({
                "t": step,
                "agent_id": "root",
                "role": "tool",
                "content": None,
                "tool": raw.get("toolName") or raw.get("name"),
                "args": None,
                "output": text if isinstance(text, str) else json.dumps(text)[:20000],
                "usage": None,
                "latency_s": None,
                "ts": raw.get("timestamp"),
            })
    return events, totals, step


def parse_our_trajectory(path: Path) -> tuple[list[dict], dict, int, int]:
    events: list[dict] = []
    totals = {"input": 0, "cached": 0, "output": 0, "cost_usd": 0.0}
    steps = 0
    sub_agents: set[str] = set()
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        events.append(event)
        usage = event.get("usage") or {}
        totals["input"] += usage.get("input", 0) or 0
        totals["cached"] += usage.get("cached", 0) or 0
        totals["output"] += usage.get("output", 0) or 0
        totals["cost_usd"] += event.get("cost_usd", 0.0) or 0.0
        if event.get("role") == "assistant" and (event.get("agent_id") or "root") == "root":
            steps = max(steps, event.get("t", 0) or 0)
        if str(event.get("agent_id", "")).startswith("sub-"):
            sub_agents.add(str(event["agent_id"]))
    return events, totals, steps, len(sub_agents)


def terminal_reason(trial_result: dict, events: list[dict], harness_result: dict | None) -> str:
    if harness_result and harness_result.get("terminal_reason"):
        return harness_result["terminal_reason"]
    exception = trial_result.get("exception_info") or {}
    if exception:
        name = (exception.get("exception_type") or "").lower()
        if "timeout" in name:
            return "timeout"
        return "crash"
    # pi exits on its own when the model stops calling tools.
    return "finish" if events else "crash"


def ingest_trial(run_dir: Path, trial_dir: Path, meta: dict) -> dict | None:
    trial_result = _load_json(trial_dir / "result.json")
    if trial_result is None:
        print(f"  ! {trial_dir.name}: no result.json", file=sys.stderr)
        return None

    task_id = trial_result.get("task_name") or trial_dir.name.rsplit("__", 1)[0]
    task_id = task_id.split("/")[-1]

    agent_dir = trial_dir / "agent"
    harness_result = _load_json(agent_dir / "result.json")
    delegations = 0
    if (agent_dir / "trajectory.jsonl").exists():
        events, totals, steps, delegations = parse_our_trajectory(agent_dir / "trajectory.jsonl")
    elif (agent_dir / "pi.txt").exists():
        events, totals, steps = parse_pi_trajectory(agent_dir / "pi.txt")
    else:
        events, totals, steps = [], {"input": 0, "cached": 0, "output": 0, "cost_usd": 0.0}, 0

    reward, passed, verifier_raw = read_reward(trial_dir)
    agent_context = trial_result.get("agent_result") or {}

    out_dir = run_dir / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "trajectory.jsonl").open("w") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")

    result = {
        "task_id": task_id,
        "config": meta.get("config", {}).get("id"),
        "model": meta.get("model", {}).get("model"),
        "model_key": meta.get("model_key"),
        "commit": meta.get("commit"),
        "pass": passed,
        "reward": reward,
        "steps": steps,
        "delegations": (harness_result or {}).get("delegations", delegations)
        if isinstance((harness_result or {}).get("delegations"), int) else delegations,
        "tokens": {
            "input": totals["input"],
            "cached": totals["cached"],
            "output": totals["output"],
        },
        "cost_usd": round(totals["cost_usd"], 6),
        "wallclock_s": _duration_s(trial_result.get("agent_execution")),
        "terminal_reason": terminal_reason(trial_result, events, harness_result),
        "lib_active": (harness_result or {}).get("lib_active", []),
        "agent_version": (trial_result.get("agent_info") or {}).get("version"),
        "verifier_raw": verifier_raw,
    }

    # Cross-check our own event accounting against Harbor's independent tally.
    for ours, theirs in (
        ("input", "n_input_tokens"),
        ("output", "n_output_tokens"),
        ("cached", "n_cache_tokens"),
    ):
        reported = agent_context.get(theirs)
        if reported and abs(reported - result["tokens"][ours]) > max(50, 0.02 * reported):
            result.setdefault("accounting_warnings", []).append(
                f"{ours}: events say {result['tokens'][ours]}, harbor says {reported}"
            )
    (out_dir / "result.json").write_text(json.dumps(result, indent=2))
    return result


def ingest_run(run_dir: Path) -> int:
    meta_path = run_dir / "meta.json"
    meta = _load_json(meta_path) or {}
    jobs_dir = run_dir / "jobs"
    if not jobs_dir.is_dir():
        print(f"{run_dir.name}: no jobs/ directory, skipping")
        return 0

    trial_dirs = sorted(
        p for job in jobs_dir.iterdir() if job.is_dir()
        for p in job.iterdir() if p.is_dir() and (p / "result.json").exists()
    )
    print(f"{run_dir.name}: {len(trial_dirs)} trials")

    reasons: dict[str, str] = {}
    ingested = 0
    for trial_dir in trial_dirs:
        result = ingest_trial(run_dir, trial_dir, meta)
        if result:
            reasons[result["task_id"]] = result["terminal_reason"]
            ingested += 1

    meta["terminal_reasons"] = reasons
    meta["ingested_at"] = datetime.now().astimezone().isoformat()
    meta_path.write_text(json.dumps(meta, indent=2))
    return ingested


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="*", help="run directories (default: all under runs/)")
    args = parser.parse_args()

    if args.runs:
        run_dirs = [Path(r) for r in args.runs]
    else:
        run_dirs = sorted(p for p in RUNS_DIR.iterdir() if (p / "meta.json").exists())

    if not run_dirs:
        print("no runs to ingest")
        return 0
    total = sum(ingest_run(run_dir) for run_dir in run_dirs)
    print(f"ingested {total} trials")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
