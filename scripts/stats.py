#!/usr/bin/env python3
"""Paired bootstrap CI and exact McNemar for two configs on the same tasks.

    python3 scripts/stats.py analysis/eval100_C_full.json analysis/eval100_A_pi.json [--subset configs/eval30.txt]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path


def load(path: str) -> dict[str, dict]:
    return json.loads(Path(path).read_text())["tasks"]


def bootstrap(pairs: list[tuple[float, float]], n: int = 10000, seed: int = 0) -> tuple[float, float, float]:
    rng = random.Random(seed)
    diffs = [a - b for a, b in pairs]
    point = sum(diffs) / len(diffs)
    samples = []
    for _ in range(n):
        s = [diffs[rng.randrange(len(diffs))] for _ in diffs]
        samples.append(sum(s) / len(s))
    samples.sort()
    return point, samples[int(0.025 * n)], samples[int(0.975 * n) - 1]


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value on discordant counts b (A only) and c (B only)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate")
    parser.add_argument("baseline")
    parser.add_argument("--subset", help="task list to restrict to (e.g. configs/eval30.txt)")
    args = parser.parse_args()
    cand, base = load(args.candidate), load(args.baseline)
    ids = sorted(set(cand) & set(base))
    if args.subset:
        keep = {l.strip() for l in Path(args.subset).read_text().splitlines() if l.strip() and not l.startswith("#")}
        ids = [t for t in ids if t in keep]
    n = len(ids)
    cp = [1.0 if cand[t]["pass"] else 0.0 for t in ids]
    bp = [1.0 if base[t]["pass"] else 0.0 for t in ids]
    cr = [cand[t]["reward"] for t in ids]
    br = [base[t]["reward"] for t in ids]
    only_c = sum(1 for a, b in zip(cp, bp) if a and not b)
    only_b = sum(1 for a, b in zip(cp, bp) if b and not a)
    d_pass, lo_pass, hi_pass = bootstrap(list(zip(cp, bp)))
    d_rew, lo_rew, hi_rew = bootstrap(list(zip(cr, br)))
    name_c = Path(args.candidate).stem.split("_", 1)[-1]
    name_b = Path(args.baseline).stem.split("_", 1)[-1]
    print(f"paired tasks: {n}")
    print(f"pass@1   {name_c} {100*sum(cp)/n:.1f}%  {name_b} {100*sum(bp)/n:.1f}%  "
          f"diff {100*d_pass:+.1f} pts  95% CI [{100*lo_pass:+.1f}, {100*hi_pass:+.1f}]  "
          f"discordant {only_c} vs {only_b}  McNemar p={mcnemar_exact(only_c, only_b):.2e}")
    print(f"reward   {name_c} {sum(cr)/n:.1f}  {name_b} {sum(br)/n:.1f}  "
          f"diff {d_rew:+.1f}  95% CI [{lo_rew:+.1f}, {hi_rew:+.1f}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
