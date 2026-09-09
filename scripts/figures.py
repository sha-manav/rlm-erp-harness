#!/usr/bin/env python3
"""Figures for the write-up: fig1_pareto and fig2_harness_groups. Costs are on the
common price basis (tokens x one list-price table), stated on each figure.
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "analysis"

DEV40 = {
    "A_pi": "runs/A_pi__big__dev40__20260902T002458Z",
    "B_bash": "runs/B_bash__big__dev40__20260902T022100Z",
    "C_full v0": "runs/C_full__big__dev40__20260902T021402Z",
    "C_full v1": "runs/C_full__big__dev40__20260902T182723Z",
}
EVAL = {  # merged tables; B_bash is the eval30 slice when present
    "A_pi": "analysis/eval100_A_pi.json",
    "C_full v1": "analysis/eval100_C_full.json",
    "B_bash": "analysis/eval30_B_bash.json",
}
COLOURS = {"A_pi": "#4c72b0", "B_bash": "#999999", "C_full v0": "#dd8452", "C_full v1": "#55a868"}

# Costs are plotted on one price basis (tokens x list price); see scripts/cost.py.
COMMON = {"input": 0.966, "cache_read": 0.179, "output": 3.036}
BASIS = "cost basis: tokens × one list price (0.966 / 0.179 / 3.036 $/Mtok uncached / cached / output), not the reported cost"


def common_cost(s: dict) -> float:
    return ((s["input"] - s["cached"]) * COMMON["input"] + s["cached"] * COMMON["cache_read"]
            + s["output"] * COMMON["output"]) / 1e6


def from_run(run_dir: str) -> dict | None:
    rows = [json.loads(p.read_text()) for p in (REPO_ROOT / run_dir).glob("*/result.json")]
    rows = [r for r in rows if r.get("reward") is not None]
    if not rows:
        return None
    n = len(rows)
    tin = sum(r["tokens"]["input"] for r in rows)
    return {"n": n, "pass": 100 * sum(1 for r in rows if r["pass"]) / n,
            "reward": sum(r["reward"] for r in rows) / n,
            "cost": sum(r["cost_usd"] for r in rows) / n,
            "input": tin / n, "cached": sum(r["tokens"]["cached"] for r in rows) / n,
            "output": sum(r["tokens"].get("output", 0) for r in rows) / n}


def from_merged(path: str) -> dict | None:
    p = REPO_ROOT / path
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    s = d["summary"]
    if not s.get("n"):
        return None
    return {"n": s["n"], "pass": s["pass_rate"], "reward": s["mean_reward"], "cost": s["cost_per_task"],
            "input": s["input_tokens_per_task"], "cached": s["input_tokens_per_task"] * s["cache_pct"] / 100,
            "output": s["output_tokens_per_task"]}


def main() -> int:
    dev = {k: from_run(v) for k, v in DEV40.items()}
    ev = {k: from_merged(v) for k, v in EVAL.items()}
    dev = {k: v for k, v in dev.items() if v}
    ev = {k: v for k, v in ev.items() if v}

    # fig1: Pareto
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    for name, s in dev.items():
        ax.scatter(common_cost(s), s["pass"], color=COLOURS[name], marker="o", s=70, alpha=0.55)
        ax.annotate(f"{name} (dev40)", (common_cost(s), s["pass"]), xytext=(5, -10), textcoords="offset points", fontsize=8, alpha=0.8)
    for name, s in ev.items():
        label = f"{name} (eval100)" if s["n"] >= 100 else f"{name} (eval30 slice)"
        ax.scatter(common_cost(s), s["pass"], color=COLOURS[name], marker="*", s=200, edgecolor="black", linewidth=0.6)
        ax.annotate(label, (common_cost(s), s["pass"]), xytext=(6, 4), textcoords="offset points", fontsize=8, fontweight="bold")
    ax.set_xlabel("cost per task (USD, common price basis)")
    ax.set_ylabel("pass@1 (%)")
    ax.set_title("Quality vs cost — same model (GLM-5.1 fp8), harness varies")
    ax.grid(alpha=0.3)
    ax.set_ylim(0, 100)
    fig.text(0.01, 0.005, BASIS, fontsize=6.5, alpha=0.8)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(OUT / "fig1_pareto.png", dpi=160)

    # fig2: harness groups
    names = ["A_pi", "B_bash", "C_full v0", "C_full v1"]
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    width = 0.38
    xs = range(len(names))
    dv = [dev.get(n, {}).get("pass", 0) for n in names]
    evv = [ev.get(n, {}).get("pass", 0) for n in names]
    ax.bar([x - width / 2 for x in xs], dv, width, label="dev40", color=[COLOURS[n] for n in names], alpha=0.5)
    ax.bar([x + width / 2 for x in xs], evv, width, label="eval (100, or 30-task slice for B_bash)",
           color=[COLOURS[n] for n in names], edgecolor="black", linewidth=0.6)
    for x, (a, b) in enumerate(zip(dv, evv)):
        if a: ax.text(x - width / 2, a + 1, f"{a:.0f}", ha="center", fontsize=8)
        if b: ax.text(x + width / 2, b + 1, f"{b:.0f}", ha="center", fontsize=8, fontweight="bold")
    ax.set_xticks(list(xs)); ax.set_xticklabels(names)
    ax.set_ylabel("pass@1 (%)"); ax.set_ylim(0, 100); ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.3)
    ax.set_title("Harness groups (no fine-tuning; same model and provider pin)")
    fig.tight_layout()
    fig.savefig(OUT / "fig2_harness_groups.png", dpi=160)

    print("wrote", OUT / "fig1_pareto.png", OUT / "fig2_harness_groups.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
