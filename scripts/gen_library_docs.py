#!/usr/bin/env python3
"""Generate harness/prompts/library_docs.md from the library's signatures and docstrings
(`make docs`), so the prompt cannot describe a function that does not exist.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "harness" / "lib"
OUT_PATH = REPO_ROOT / "harness" / "prompts" / "library_docs.md"

# Ordered so the document reads as a workflow, not as an alphabetical dump.
MODULE_ORDER = ["erp", "check", "plan", "finish", "db", "state", "delegate", "fmt"]

SKIP_PREFIX = "_"


def first_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node) or ""
    for line in doc.splitlines():
        if line.strip():
            return line.strip()
    return ""


def signature(node: ast.FunctionDef) -> str:
    args = []
    positional = node.args.posonlyargs + node.args.args
    defaults = list(node.args.defaults)
    pad = len(positional) - len(defaults)
    for index, arg in enumerate(positional):
        if arg.arg in ("self", "cls"):
            continue
        if index >= pad:
            try:
                default = ast.unparse(defaults[index - pad])
            except Exception:
                default = "..."
            args.append(f"{arg.arg}={default}")
        else:
            args.append(arg.arg)
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        args.append(f"{arg.arg}={ast.unparse(default)}" if default else arg.arg)
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")
    return f"{node.name}({', '.join(args)})"


def exports_of(tree: ast.Module) -> list[str] | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXPORTS" for t in node.targets):
            try:
                return list(ast.literal_eval(node.value))
            except (ValueError, SyntaxError):
                return None
    return None


def render_module(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    name = path.stem
    exports = exports_of(tree)
    lines = [f"## `{name}`", ""]
    summary = first_line(tree)
    if summary:
        lines += [summary, ""]
    # Document only names the kernel binds; an export can shadow the module name (finish, erp, state, db, plan).
    shadowed = bool(exports) and name in exports
    if exports:
        lines += [f"Bound in your kernel: {', '.join(f'`{e}`' for e in exports)}"
                  + ("" if shadowed else f" (and the module itself as `{name}`)") + ".", ""]

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and shadowed and node.name not in exports:
            continue
        if isinstance(node, ast.ClassDef) and shadowed and node.name not in exports:
            continue
        if isinstance(node, ast.ClassDef) and not node.name.startswith(SKIP_PREFIX):
            doc = first_line(node)
            lines.append(f"**class `{node.name}`** — {doc}" if doc else f"**class `{node.name}`**")
            methods = [
                child for child in node.body
                if isinstance(child, ast.FunctionDef)
                and (not child.name.startswith(SKIP_PREFIX) or child.name == "__str__")
            ]
            for method in methods:
                doc = first_line(method)
                lines.append(f"- `{signature(method)}` — {doc}" if doc else f"- `{signature(method)}`")
            lines.append("")
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith(SKIP_PREFIX):
            doc = first_line(node)
            lines.append(f"- `{signature(node)}` — {doc}" if doc else f"- `{signature(node)}`")

    if lines[-1] != "":
        lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lib-dir", default=str(LIB_DIR))
    parser.add_argument("--out", default=str(OUT_PATH))
    parser.add_argument("--check", action="store_true",
                        help="fail if the generated file is out of date")
    args = parser.parse_args()

    lib_dir = Path(args.lib_dir)
    present = {p.stem: p for p in sorted(lib_dir.glob("*.py")) if p.stem != "__init__"}
    ordered = [present[n] for n in MODULE_ORDER if n in present]
    ordered += [present[n] for n in sorted(present) if n not in MODULE_ORDER]

    lines = [
        "# Preloaded library",
        "",
        "Every name below is already bound in your Python kernel — do not import anything to",
        "use it, and do not reimplement it. Generated from the source, so it cannot describe a",
        "function that does not exist.",
        "",
    ]
    for path in ordered:
        lines += render_module(path)

    text = "\n".join(lines).rstrip() + "\n"
    out_path = Path(args.out)

    if args.check:
        current = out_path.read_text() if out_path.exists() else ""
        if current != text:
            print(f"{out_path} is out of date — run `make docs`", file=sys.stderr)
            return 1
        print(f"{out_path} is up to date")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    print(f"wrote {out_path} ({len(text.splitlines())} lines, ~{len(text) // 4} tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
