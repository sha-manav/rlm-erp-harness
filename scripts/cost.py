#!/usr/bin/env python3
"""Recompute tokens and cost per task from the raw provider records (pi's NDJSON, our
trajectory.jsonl), compare with the ingested totals and Harbor's accounting, and report
both configs on one price basis.

    python3 scripts/cost.py analysis/eval100_C_full.json analysis/eval100_A_pi.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEAD = {"api_error", "crash"}
TOLERANCE = 0.01   # 1%: anything above is a mismatch worth reporting

# USD per million tokens, OpenRouter endpoint list prices for z-ai/glm-5.1 . Used only for the informational cross-check (3).
PRICES = {
    "gmicloud":   {"input": 0.910, "output": 2.860, "cache_read": 0.169},
    "streamlake": {"input": 0.966, "output": 3.036, "cache_read": 0.179},
    "chutes":     {"input": 0.980, "output": 3.080, "cache_read": 0.098},
    "baidu":      {"input": 0.966, "output": 3.036, "cache_read": 0.179},   # same tier as streamlake
}
# One price table for both configs (models.yaml list prices). pi reports cost client-side at 1.30x this; ours is OpenRouter-billed.
COMMON = {"input": 0.966, "output": 3.036, "cache_read": 0.179}


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def merged_tasks(table_path: Path) -> tuple[list[Path], dict[str, str]]:
    """The run directory each merged task's result came from (same rule as merge_batches)."""
    table = json.loads(table_path.read_text())
    runs = [REPO_ROOT / r for r in table["runs"]]
    chosen: dict[str, str] = {}
    for run in runs:
        for path in sorted(run.glob("*/result.json")):
            r = _load(path)
            if not r or r.get("reward") is None or r.get("terminal_reason") in DEAD:
                continue
            chosen.setdefault(r["task_id"], str(run))
    return runs, {t: chosen[t] for t in table["tasks"] if t in chosen}


def trial_dir(run: Path, task_id: str) -> Path | None:
    """Harbor's trial directory for a task (names are truncated to a prefix + suffix)."""
    jobs = next(iter(run.glob("jobs/*/")), None)
    if not jobs:
        return None
    candidates = [d for d in jobs.glob("*__*/") if task_id.startswith(d.name.rsplit("__", 1)[0])]
    if not candidates:
        return None
    # A retried trial leaves several directories; take the one with a verifier reward.
    scored = [d for d in candidates if (d / "verifier" / "reward.json").exists()]
    return (scored or candidates)[-1]


def raw_pi(path: Path) -> dict:
    """Sum pi's message_end usages. Returns the common-schema totals plus the raw parts."""
    totals = {"input_excl_cache": 0, "cache_read": 0, "cache_write": 0, "output": 0,
              "reasoning": 0, "cost": 0.0, "calls": 0}
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant" or not message.get("usage"):
            continue
        u = message["usage"]
        totals["input_excl_cache"] += u.get("input", 0) or 0
        totals["cache_read"] += u.get("cacheRead", 0) or 0
        totals["cache_write"] += u.get("cacheWrite", 0) or 0
        totals["output"] += u.get("output", 0) or 0
        totals["reasoning"] += u.get("reasoning", 0) or 0
        totals["cost"] += ((u.get("cost") or {}).get("total", 0.0)) or 0.0
        totals["calls"] += 1
    return {
        "input": totals["input_excl_cache"] + totals["cache_read"],   # common schema: incl. cache
        "cached": totals["cache_read"],
        "output": totals["output"],
        "cost": totals["cost"],
        "calls": totals["calls"],
        "providers": {},
        "raw": totals,
    }


def raw_ours(path: Path) -> dict:
    totals = {"input": 0, "cached": 0, "output": 0, "cost": 0.0, "calls": 0}
    providers: dict[str, int] = {}
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("role") != "assistant" or not event.get("usage"):
            continue
        u = event["usage"]
        totals["input"] += u.get("input", 0) or 0          # prompt_tokens, incl. cached
        totals["cached"] += u.get("cached", 0) or 0
        totals["output"] += u.get("output", 0) or 0
        totals["cost"] += event.get("cost_usd", 0.0) or 0.0
        totals["calls"] += 1
        p = (event.get("provider") or "unknown").lower()
        providers[p] = providers.get(p, 0) + 1
    totals["providers"] = providers
    totals["raw"] = dict(totals)
    return totals


def price_estimate(raw: dict, providers: dict[str, int], config: str) -> float | None:
    """Tokens x list price of the upstream that served most calls (pi: unknown, assume GMICloud)."""
    if config == "A_pi":
        upstream = "gmicloud"      # account-restricted to gmicloud + chutes, GMICloud 9 in 10
    else:
        upstream = max(providers, key=providers.get) if providers else None
    prices = PRICES.get(upstream or "")
    if not prices:
        return None
    uncached = raw["input"] - raw["cached"]
    return (uncached * prices["input"] + raw["cached"] * prices["cache_read"]
            + raw["output"] * prices["output"]) / 1e6


def common_cost(raw: dict) -> float:
    uncached = raw["input"] - raw["cached"]
    return (uncached * COMMON["input"] + raw["cached"] * COMMON["cache_read"]
            + raw["output"] * COMMON["output"]) / 1e6


def rel(a: float, b: float) -> float:
    return abs(a - b) / b if b else (0.0 if not a else float("inf"))


def audit(table_path: Path) -> dict:
    runs, chosen = merged_tasks(table_path)
    config = None
    rows = []
    for task_id, run_name in chosen.items():
        run = Path(run_name)
        ingested = _load(run / task_id / "result.json")
        config = config or ingested.get("config")
        trial = trial_dir(run, task_id)
        if trial is None:
            rows.append({"task": task_id, "error": "no trial dir"})
            continue
        agent = trial / "agent"
        if (agent / "trajectory.jsonl").exists():
            raw = raw_ours(agent / "trajectory.jsonl")
            source = "trajectory.jsonl"
        elif (agent / "pi.txt").exists():
            raw = raw_pi(agent / "pi.txt")
            source = "pi.txt"
        else:
            rows.append({"task": task_id, "error": "no raw trajectory"})
            continue
        harbor = (_load(trial / "result.json") or {}).get("agent_result") or {}
        est = price_estimate(raw, raw.get("providers", {}), ingested.get("config"))
        row = {
            "task": task_id, "run": run.name, "source": source, "calls": raw["calls"],
            "terminal": ingested.get("terminal_reason"),
            "raw": {"input": raw["input"], "cached": raw["cached"], "output": raw["output"], "cost": raw["cost"]},
            "ingested": {"input": ingested["tokens"]["input"], "cached": ingested["tokens"]["cached"],
                         "output": ingested["tokens"]["output"], "cost": ingested["cost_usd"]},
            "harbor": {"input": harbor.get("n_input_tokens"), "cached": harbor.get("n_cache_tokens"),
                       "output": harbor.get("n_output_tokens"), "cost": harbor.get("cost_usd")},
            "price_estimate": est,
            "common_cost": common_cost(raw),
            "providers": raw.get("providers", {}),
            "pi_raw": raw.get("raw") if source == "pi.txt" else None,
        }
        row["mismatch"] = {}
        for key in ("input", "cached", "output", "cost"):
            d = rel(row["raw"][key], row["ingested"][key])
            if d > TOLERANCE:
                row["mismatch"][f"ingested.{key}"] = d
            if row["harbor"][key] is not None:
                d = rel(row["raw"][key], row["harbor"][key])
                if d > TOLERANCE:
                    row["mismatch"][f"harbor.{key}"] = d
        if est is not None:
            row["price_delta"] = (est - raw["cost"]) / raw["cost"] if raw["cost"] else None
        rows.append(row)
    return {"config": config, "table": str(table_path), "n": len(rows), "rows": rows}


def render(results: list[dict]) -> str:
    out = ["# Efficiency audit", "",
           "Per-task tokens and cost recomputed from the raw provider records and compared with the",
           "ingested `result.json` totals and Harbor's `agent_result`. Tolerance 1%.", ""]
    for res in results:
        rows = [r for r in res["rows"] if "error" not in r]
        errors = [r for r in res["rows"] if "error" in r]
        n = len(rows)
        cfg = res["config"]
        tot = lambda src, key: sum(r[src][key] for r in rows)
        mism = [r for r in rows if r["mismatch"]]
        terms: dict[str, int] = {}
        for r in rows:
            terms[r["terminal"]] = terms.get(r["terminal"], 0) + 1
        out += [f"## {cfg} — {res['table']}", "",
                f"tasks audited: {n} (errors: {len(errors)}); model calls: {sum(r['calls'] for r in rows):,}", "",
                "| source | input tokens (incl. cache) | cache reads | output | cost USD |",
                "|---|---:|---:|---:|---:|"]
        for src, label in (("raw", "raw provider records"), ("ingested", "ingested result.json"), ("harbor", "Harbor agent_result")):
            if src == "harbor" and any(r["harbor"]["input"] is None for r in rows):
                out.append(f"| {label} | (absent on some trials) | | | |")
                continue
            out.append(f"| {label} | {tot(src, 'input'):,} | {tot(src, 'cached'):,} | {tot(src, 'output'):,} | {tot(src, 'cost'):.4f} |")
        cache_pct = 100 * tot("raw", "cached") / tot("raw", "input") if tot("raw", "input") else 0
        common = sum(r["common_cost"] for r in rows)
        out += ["", f"per task: input {tot('raw','input')/n:,.0f} (cache {cache_pct:.1f}%), output {tot('raw','output')/n:,.0f}, "
                f"reported cost ${tot('raw','cost')/n:.4f}, **common-basis cost ${common/n:.4f}** "
                f"(tokens × models.yaml prices {COMMON['input']}/{COMMON['cache_read']}/{COMMON['output']} $/Mtok "
                f"uncached/cached/output; reported ÷ common = {tot('raw','cost')/common:.3f})", ""]
        out.append(f"mismatches above 1%: **{len(mism)}** of {n}")
        for r in mism[:20]:
            out.append(f"- {r['task']}: " + ", ".join(f"{k} {100*v:.1f}%" for k, v in r["mismatch"].items()))
        for r in errors:
            out.append(f"- {r['task']}: {r['error']}")
        ests = [r["price_delta"] for r in rows if r.get("price_delta") is not None]
        if ests:
            mean_delta = sum(ests) / len(ests)
            out += ["", f"price-table cross-check (tokens × list price of the serving upstream vs reported cost): "
                    f"mean {100*mean_delta:+.1f}%, range [{100*min(ests):+.1f}%, {100*max(ests):+.1f}%] over {len(ests)} tasks"]
        if cfg == "A_pi":
            raw_in = sum(r["pi_raw"]["input_excl_cache"] for r in rows)
            raw_cr = sum(r["pi_raw"]["cache_read"] for r in rows)
            raw_cw = sum(r["pi_raw"]["cache_write"] for r in rows)
            raw_rs = sum(r["pi_raw"]["reasoning"] for r in rows)
            out += ["", "cache accounting (pi NDJSON): `input` excludes cache reads. "
                    f"Σinput {raw_in:,} + ΣcacheRead {raw_cr:,} = {raw_in + raw_cr:,} = ingested tokens.input; "
                    f"ΣcacheWrite {raw_cw:,}; Σreasoning {raw_rs:,} (inside output)"]
        else:
            provs: dict[str, int] = {}
            for r in rows:
                for p, c in r["providers"].items():
                    provs[p] = provs.get(p, 0) + c
            out += ["", "cache accounting (our loop): `usage.input` is OpenRouter `prompt_tokens`, which includes "
                    "`cached_tokens`; ingested tokens.input = Σprompt_tokens, tokens.cached = Σcached_tokens.",
                    f"serving upstreams over all calls: {provs}"]
        out += ["", f"terminal reasons over the merged set: {terms}"]
        caps = [r["task"] for r in rows if r["terminal"] == "token_cap"]
        if caps:
            out.append(f"token_cap trials ({len(caps)}): " + ", ".join(caps))
        out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tables", nargs="+", help="merged per-task tables from merge_batches.py")
    parser.add_argument("--out", help="write the markdown report here")
    parser.add_argument("--json", help="write the per-task audit rows here")
    args = parser.parse_args()
    results = [audit(Path(t)) for t in args.tables]
    text = render(results)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
