#!/usr/bin/env python3
"""Persistent Python kernel that runs inside the task container: one namespace per agent,
preloaded with harness/lib. JSON request per line; TCP server plus a --client relay.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import socket
import socketserver
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 120
LIB_PATH = "/harness"
FINISH_SENTINEL = "__ERP_HARNESS_FINISHED__"


class Namespace:
    """One persistent `exec` globals dict, rebuilt from scratch after a timeout."""

    def __init__(self, name: str):
        self.name = name
        self.globals: dict[str, Any] = {}
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.globals = {"__name__": "__kernel__", "__builtins__": __builtins__}
        if LIB_PATH not in sys.path:
            sys.path.insert(0, LIB_PATH)
        # Preload the library. A missing or broken module must not take the kernel down:
        # the config decides which modules exist, and result.json records lib_active.
        loaded, failed = [], {}
        try:
            import lib  # noqa: PLC0415  (the container-side package under /harness)

            for module_name in getattr(lib, "__all__", []):
                try:
                    module = __import__(f"lib.{module_name}", fromlist=[module_name])
                    self.globals[module_name] = module
                    # Let `import check` in agent code resolve to the same module object.
                    sys.modules.setdefault(module_name, module)
                    missing = []
                    for symbol in getattr(module, "EXPORTS", []):
                        if hasattr(module, symbol):
                            self.globals[symbol] = getattr(module, symbol)
                        else:
                            missing.append(symbol)
                    loaded.append(module_name)
                    if missing:
                        # A stale EXPORTS entry must not cost the whole module: the agent
                        # can still use it via `module.thing`.
                        failed[f"{module_name}:exports"] = f"missing symbols {missing}"
                except Exception as exc:
                    failed[module_name] = f"{type(exc).__name__}: {exc}"
            self.globals["lib"] = lib
        except Exception as exc:
            failed["lib"] = f"{type(exc).__name__}: {exc}"
        self.globals["_lib_loaded"] = loaded
        self.globals["_lib_failed"] = failed


class Kernel:
    def __init__(self):
        self._namespaces: dict[str, Namespace] = {}
        self._lock = threading.Lock()

    def namespace(self, name: str) -> Namespace:
        with self._lock:
            if name not in self._namespaces:
                self._namespaces[name] = Namespace(name)
            return self._namespaces[name]

    def run(self, code: str, timeout: float = DEFAULT_TIMEOUT, ns: str = "root") -> dict:
        namespace = self.namespace(ns)
        stdout, stderr = io.StringIO(), io.StringIO()
        result: dict[str, Any] = {}
        started = time.time()

        def target() -> None:
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exec(compile(code, "<agent>", "exec"), namespace.globals)
                result["ok"] = True
            except BaseException as exc:
                result["ok"] = False
                # Show the agent its own traceback, not the kernel's frames.
                frames = traceback.format_exception(type(exc), exc, exc.__traceback__)
                stderr.write("".join(frames[:1] + frames[2:]))

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout)

        if worker.is_alive():
            namespace.reset()
            return {
                "ok": False,
                "stdout": stdout.getvalue(),
                "stderr": (
                    f"TimeoutError: code exceeded {timeout}s. "
                    "kernel restarted; in-memory state lost"
                ),
                "elapsed": round(time.time() - started, 3),
                "ns": ns,
                "restarted": True,
            }

        return {
            "ok": result.get("ok", False),
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "elapsed": round(time.time() - started, 3),
            "ns": ns,
            "restarted": False,
        }

    def handle(self, request: dict) -> dict:
        action = request.get("action", "run")
        if action == "ping":
            return {"id": request.get("id"), "ok": True, "pong": True}
        if action == "reset":
            self.namespace(request.get("ns", "root")).reset()
            return {"id": request.get("id"), "ok": True, "reset": True}
        if action == "lib":
            namespace = self.namespace(request.get("ns", "root"))
            return {
                "id": request.get("id"),
                "ok": True,
                "loaded": namespace.globals.get("_lib_loaded", []),
                "failed": namespace.globals.get("_lib_failed", {}),
            }
        response = self.run(
            request.get("code", ""),
            float(request.get("timeout") or DEFAULT_TIMEOUT),
            request.get("ns", "root"),
        )
        response["id"] = request.get("id")
        return response


KERNEL = Kernel()


class RequestHandler(socketserver.StreamRequestHandler):
    # A single request may carry a large program; a reply may carry large output.
    timeout = 3600

    def handle(self) -> None:
        for raw in self.rfile:
            raw = raw.strip()
            if not raw:
                continue
            try:
                request = json.loads(raw)
            except ValueError as exc:
                reply = {"ok": False, "stdout": "", "stderr": f"bad request JSON: {exc}"}
            else:
                reply = KERNEL.handle(request)
            self.wfile.write((json.dumps(reply) + "\n").encode())
            self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    address_family = socket.AF_INET


def serve(port: int) -> None:
    Path("/harness").mkdir(parents=True, exist_ok=True)
    with Server(("127.0.0.1", port), RequestHandler) as server:
        print(f"kernel listening on 127.0.0.1:{port}", flush=True)
        server.serve_forever()


def stdio() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except ValueError as exc:
            reply = {"ok": False, "stdout": "", "stderr": f"bad request JSON: {exc}"}
        else:
            reply = KERNEL.handle(request)
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


def client(port: int) -> int:
    """Relay one JSON request from stdin to the kernel and print the reply."""
    request = sys.stdin.read().strip() or "{}"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
            connection.sendall(request.encode().rstrip() + b"\n")
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                buffer += chunk
    except OSError as exc:
        print(json.dumps({"ok": False, "stdout": "", "stderr": f"kernel unreachable: {exc}"}))
        return 1
    sys.stdout.write(buffer.decode(errors="replace"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--stdio", action="store_true", help="read requests from stdin")
    parser.add_argument("--client", action="store_true", help="relay one request from stdin")
    args = parser.parse_args()
    if args.client:
        return client(args.port)
    if args.stdio:
        stdio()
    else:
        serve(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
