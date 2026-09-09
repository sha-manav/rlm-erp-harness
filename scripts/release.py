#!/usr/bin/env python3
"""Package run directories as release artefacts: one .tar.zst per run with trajectory,
result and reward per task, meta.json, and a manifest with sha256 per file.

    python3 scripts/release.py --out dist/<tag> runs/<run> ...
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def trial_dir(run: Path, task_id: str) -> Path | None:
    jobs = next(iter(run.glob("jobs/*/")), None)
    if not jobs:
        return None
    candidates = [d for d in jobs.glob("*__*/") if task_id.startswith(d.name.rsplit("__", 1)[0])]
    scored = [d for d in candidates if (d / "verifier" / "reward.json").exists()]
    return (scored or candidates or [None])[-1]


def package(run: Path, out_dir: Path) -> dict:
    meta_path = run / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    rows = []
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        def add(name: str, data: bytes) -> str:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
            return sha256(data)

        for result_path in sorted(run.glob("*/result.json")):
            task_dir = result_path.parent
            task_id = task_dir.name
            result = json.loads(result_path.read_text())
            if result.get("reward") is None:
                continue
            files = {"result.json": result_path.read_bytes()}
            traj = task_dir / "trajectory.jsonl"
            if traj.exists():
                files["trajectory.jsonl"] = traj.read_bytes()
            trial = trial_dir(run, task_id)
            if trial and (trial / "verifier" / "reward.json").exists():
                files["reward.json"] = (trial / "verifier" / "reward.json").read_bytes()
            row = {"task_id": task_id, "config": result.get("config"), "model": result.get("model"),
                   "commit": result.get("commit"), "pass": result.get("pass"), "reward": result.get("reward"),
                   "terminal_reason": result.get("terminal_reason"), "sha256": {}}
            for name, data in files.items():
                row["sha256"][name] = add(f"{run.name}/{task_id}/{name}", data)
            rows.append(row)
        manifest = {"run_id": run.name, "commit": meta.get("commit"), "tag": meta.get("tag"),
                    "config": (meta.get("config") or {}).get("id"), "model": (meta.get("model") or {}).get("model"),
                    "set": meta.get("set_alias") or meta.get("set"), "n_tasks": len(rows), "tasks": rows}
        if meta_path.exists():
            add(f"{run.name}/meta.json", meta_path.read_bytes())
        add(f"{run.name}/manifest.json", json.dumps(manifest, indent=1).encode())

    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir / f"{run.name}.tar"
    tar_path.write_bytes(buffer.getvalue())
    zst_path = out_dir / f"{run.name}.tar.zst"
    subprocess.run(["zstd", "-19", "-q", "-f", str(tar_path), "-o", str(zst_path)], check=True)
    tar_path.unlink()
    digest = sha256(zst_path.read_bytes())
    print(f"{zst_path.name}: {len(rows)} tasks, {zst_path.stat().st_size / 1e6:.1f} MB, sha256 {digest[:12]}…")
    return {"file": zst_path.name, "sha256": digest, "bytes": zst_path.stat().st_size,
            "run_id": run.name, "config": manifest["config"], "model": manifest["model"],
            "set": manifest["set"], "commit": manifest["commit"], "n_tasks": len(rows)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out)
    entries = [package(Path(r), out_dir) for r in args.runs]
    (out_dir / "release_manifest.json").write_text(json.dumps(entries, indent=1))
    (out_dir / "SHA256SUMS").write_text("".join(f"{e['sha256']}  {e['file']}\n" for e in entries))
    print(f"wrote {out_dir}/release_manifest.json and SHA256SUMS ({len(entries)} archives)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
