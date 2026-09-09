#!/usr/bin/env python3
"""Measure facts about the task set (patterns, counts, reward semantics) and read task.toml."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

METADATA_KEYS = ("task_pattern", "difficulty", "objective_kind", "scenario_number", "seed")


def read_task_meta(task_dir: Path) -> dict[str, str]:
    """Parse the handful of `key = value` metadata lines we care about."""
    text = (task_dir / "task.toml").read_text()
    meta: dict[str, str] = {}
    for key in METADATA_KEYS:
        match = re.search(rf'^{key}\s*=\s*"?([^"\n]*)"?\s*$', text, re.M)
        if match:
            meta[key] = match.group(1).strip()
    return meta


def survey_tasks(tasks_dir: Path) -> dict:
    task_dirs = sorted(p for p in tasks_dir.iterdir() if (p / "task.toml").exists())
    patterns: Counter[str] = Counter()
    difficulties: Counter[str] = Counter()
    objectives: Counter[str] = Counter()
    missing_pattern: list[str] = []
    for task_dir in task_dirs:
        meta = read_task_meta(task_dir)
        pattern = meta.get("task_pattern")
        if pattern:
            patterns[pattern] += 1
        else:
            missing_pattern.append(task_dir.name)
        difficulties[meta.get("difficulty", "?")] += 1
        objectives[meta.get("objective_kind", "?")] += 1
    return {
        "n_tasks": len(task_dirs),
        "n_patterns": len(patterns),
        "patterns": dict(patterns.most_common()),
        "difficulty": dict(difficulties.most_common()),
        "objective_kind": dict(objectives.most_common()),
        "missing_task_pattern": missing_pattern,
    }


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"<{type(exc).__name__}>"
    return (proc.stdout or proc.stderr).decode(errors="replace").strip().splitlines()[0:1][0] if (
        proc.stdout or proc.stderr
    ) else ""


def survey_tooling() -> dict:
    out = {
        "harbor": _run(["harbor", "--version"]) if shutil.which("harbor") else None,
        "uv": _run(["uv", "--version"]) if shutil.which("uv") else None,
        "docker": _run(["docker", "--version"]) if shutil.which("docker") else None,
        "python": sys.version.split()[0],
    }
    if shutil.which("docker"):
        info = subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}} {{.NCPU}}"],
            capture_output=True,
        )
        if info.returncode == 0:
            mem, _, cpus = info.stdout.decode().strip().partition(" ")
            try:
                out["docker_vm_memory_gib"] = round(int(mem) / 1024**3, 2)
                out["docker_vm_cpus"] = int(cpus)
            except ValueError:
                pass
    return out


PROBE = r"""
import json, subprocess, sys, xmlrpc.client
report = {"python": sys.version.split()[0]}
for mod in ("psycopg2", "odoolib", "pandas", "ortools", "pulp"):
    try:
        __import__(mod); report[mod] = True
    except ImportError:
        report[mod] = False
for binary in ("psql", "curl", "jq", "git"):
    report[binary] = subprocess.run(["which", binary], capture_output=True).returncode == 0
try:
    key = open("/etc/odoo/api_key").read().strip()
    common = xmlrpc.client.ServerProxy("http://127.0.0.1:8069/xmlrpc/2/common")
    report["odoo_version"] = common.version()["server_version"]
    uid = common.authenticate("bench", "admin", key, {})
    report["odoo_uid"] = uid
    models = xmlrpc.client.ServerProxy("http://127.0.0.1:8069/xmlrpc/2/object")
    for model in ("sale.order", "purchase.order", "product.product", "res.partner",
                  "mrp.production", "mrp.bom", "product.supplierinfo"):
        report[f"count:{model}"] = models.execute_kw(
            "bench", uid, key, model, "search_count", [[]]
        )
except Exception as exc:
    report["odoo_error"] = f"{type(exc).__name__}: {exc}"[:200]
try:
    import psycopg2
    conn = psycopg2.connect(host="127.0.0.1", user="odoo", password="odoo", dbname="bench")
    cur = conn.cursor()
    cur.execute("select current_user, version()")
    report["pg"] = cur.fetchone()[0]
    cur.execute("select rolcreatedb, rolsuper from pg_roles where rolname = current_user")
    report["pg_createdb"], report["pg_superuser"] = cur.fetchone()
except Exception as exc:
    report["pg_error"] = f"{type(exc).__name__}: {exc}"[:200]
print(json.dumps(report, indent=1, sort_keys=True))
"""


def survey_container(name: str) -> dict:
    from harness.container import DockerContainer

    container = DockerContainer(name)
    result = container.exec("python3 -", stdin=PROBE, timeout=120)
    if result.rc != 0:
        return {"error": result.stderr[-500:]}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": result.stdout[-500:]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks-dir", default=str(REPO_ROOT / "vendor/erp-bench/tasks"))
    parser.add_argument(
        "--live",
        metavar="CONTAINER",
        nargs="?",
        const="erpdev",
        help="also probe a running dev container (default name: erpdev)",
    )
    args = parser.parse_args()

    report: dict[str, object] = {"tooling": survey_tooling()}
    tasks_dir = Path(args.tasks_dir)
    if tasks_dir.is_dir():
        report["tasks"] = survey_tasks(tasks_dir)
    else:
        report["tasks"] = {"error": f"no such directory: {tasks_dir}"}
    if args.live:
        report["container"] = survey_container(args.live)

    print(json.dumps(report, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
