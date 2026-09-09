#!/usr/bin/env python3
"""Patch every task environment for current uv (PEP 668): add --break-system-packages."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

OLD = 'RUN uv pip install --system --no-cache \\\n    "odoo-client-lib==2.0.0"'
NEW = 'RUN uv pip install --system --break-system-packages --no-cache \\\n    "odoo-client-lib==2.0.0"'


def patch_dockerfile(path: Path) -> str:
    text = path.read_text()
    if NEW in text:
        return "already-patched"
    if OLD not in text:
        return "unexpected-content"
    path.write_text(text.replace(OLD, NEW))
    return "patched"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks-dir", default="vendor/erp-bench/tasks")
    args = ap.parse_args()

    tasks_dir = Path(args.tasks_dir)
    if not tasks_dir.is_dir():
        print(f"no such tasks dir: {tasks_dir}", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for dockerfile in sorted(tasks_dir.glob("*/environment/Dockerfile")):
        status = patch_dockerfile(dockerfile)
        counts[status] = counts.get(status, 0) + 1

    for status, n in sorted(counts.items()):
        print(f"{status}: {n}")
    return 1 if counts.get("unexpected-content") else 0


if __name__ == "__main__":
    raise SystemExit(main())
