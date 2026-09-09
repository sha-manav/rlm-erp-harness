"""Synchronous exec/upload over a task container (Harbor's async environment or plain
docker), and the client for the in-container kernel.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_TIMEOUT = 120


@dataclass(frozen=True)
class ExecResult:
    rc: int
    stdout: str
    stderr: str

    def __iter__(self):
        """Unpack as ``rc, stdout, stderr``."""
        return iter((self.rc, self.stdout, self.stderr))

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def out(self) -> str:
        """stdout, or stderr when the command failed silently on stdout."""
        if self.stdout.strip():
            return self.stdout
        return self.stderr


class Container(Protocol):
    def exec(
        self,
        cmd: str,
        timeout: int | None = DEFAULT_TIMEOUT,
        stdin: str | bytes | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
    ) -> ExecResult: ...

    def put_file(self, path: str, data: bytes | str) -> None: ...

    def put_dir(self, local_dir: str | Path, remote_dir: str) -> None: ...

    def read_file(self, path: str) -> bytes: ...


def _as_bytes(data: bytes | str) -> bytes:
    return data.encode() if isinstance(data, str) else data


class DockerContainer:
    """Drives a container by name with the local docker CLI."""

    def __init__(self, name: str, default_user: str | None = None):
        self.name = name
        self.default_user = default_user

    def _base(self, user: str | None, interactive: bool = False) -> list[str]:
        cmd = ["docker", "exec"]
        if interactive:
            cmd.append("-i")
        effective_user = user or self.default_user
        if effective_user:
            cmd += ["-u", effective_user]
        return cmd

    def exec(
        self,
        cmd: str,
        timeout: int | None = DEFAULT_TIMEOUT,
        stdin: str | bytes | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
    ) -> ExecResult:
        argv = self._base(user, interactive=stdin is not None)
        for key, value in (env or {}).items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.name, "bash", "-lc", cmd]
        try:
            proc = subprocess.run(
                argv,
                input=_as_bytes(stdin) if stdin is not None else None,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                124,
                (exc.stdout or b"").decode(errors="replace"),
                f"timeout after {timeout}s",
            )
        return ExecResult(
            proc.returncode,
            proc.stdout.decode(errors="replace"),
            proc.stderr.decode(errors="replace"),
        )

    def put_file(self, path: str, data: bytes | str) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(_as_bytes(data))
            handle.flush()
            self.exec(f"mkdir -p {shlex.quote(str(Path(path).parent))}")
            subprocess.run(
                ["docker", "cp", handle.name, f"{self.name}:{path}"],
                check=True,
                capture_output=True,
            )

    def put_dir(self, local_dir: str | Path, remote_dir: str) -> None:
        self.exec(f"mkdir -p {shlex.quote(remote_dir)}")
        # `docker cp src/. dst` copies the *contents* of src, matching upload_dir().
        subprocess.run(
            ["docker", "cp", f"{Path(local_dir)}/.", f"{self.name}:{remote_dir}"],
            check=True,
            capture_output=True,
        )

    def read_file(self, path: str) -> bytes:
        proc = subprocess.run(
            ["docker", "exec", self.name, "cat", path], capture_output=True
        )
        if proc.returncode != 0:
            raise FileNotFoundError(f"{path}: {proc.stderr.decode(errors='replace')[:200]}")
        return proc.stdout


class HarborContainer:
    """Sync facade over Harbor's async ``BaseEnvironment``."""

    def __init__(self, environment: Any, loop: asyncio.AbstractEventLoop):
        self.environment = environment
        self.loop = loop

    def _await(self, coro, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)

    def exec(
        self,
        cmd: str,
        timeout: int | None = DEFAULT_TIMEOUT,
        stdin: str | bytes | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
    ) -> ExecResult:
        if stdin is not None:
            # BaseEnvironment.exec has no stdin channel; stage the payload as a file
            # and redirect, which behaves identically for our uses (kernel requests).
            staged = "/tmp/.harness_stdin"
            self.put_file(staged, stdin)
            cmd = f"{cmd} < {staged}"
        # Give the host-side wait a little slack over the container-side timeout so a
        # command that honours its own deadline reports its own error, not ours.
        result = self._await(
            self.environment.exec(
                command=cmd, env=env, timeout_sec=timeout, user=user
            ),
            timeout=None if timeout is None else timeout + 30,
        )
        return ExecResult(
            result.return_code, result.stdout or "", result.stderr or ""
        )

    def put_file(self, path: str, data: bytes | str) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(_as_bytes(data))
            local = handle.name
        try:
            self.exec(f"mkdir -p {shlex.quote(str(Path(path).parent))}")
            self._await(
                self.environment.upload_file(source_path=local, target_path=path)
            )
        finally:
            Path(local).unlink(missing_ok=True)

    def put_dir(self, local_dir: str | Path, remote_dir: str) -> None:
        self._await(
            self.environment.upload_dir(
                source_dir=str(local_dir), target_dir=remote_dir
            )
        )

    def read_file(self, path: str) -> bytes:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            local = handle.name
        try:
            self._await(
                self.environment.download_file(source_path=path, target_path=local)
            )
            return Path(local).read_bytes()
        finally:
            Path(local).unlink(missing_ok=True)


class KernelError(RuntimeError):
    """The kernel could not be reached or did not start."""


class Kernel:
    """Host-side client for `harness/kernel_server.py` running in the container."""

    REMOTE_DIR = "/harness"
    REMOTE_SERVER = f"{REMOTE_DIR}/kernel_server.py"

    # Always shipped: fmt (tables, paging) and finish are infrastructure, not ablatable primitives.
    ALWAYS = ("fmt", "finish")

    def __init__(
        self,
        container: Container,
        port: int = 8765,
        lib_modules: list[str] | None = None,
        source_dir: Path | None = None,
    ):
        self.container = container
        self.port = port
        self.lib_modules = lib_modules
        self.source_dir = Path(source_dir or Path(__file__).parent)
        self.started = False

    # -- lifecycle ------------------------------------------------------------
    def install(self) -> None:
        """Copy the kernel and the configured subset of lib/ into the container."""
        self.container.exec(f"rm -rf {self.REMOTE_DIR}/lib")
        self.container.exec(f"mkdir -p {self.REMOTE_DIR}/lib")
        self.container.put_file(
            self.REMOTE_SERVER, (self.source_dir / "kernel_server.py").read_bytes()
        )
        lib_dir = self.source_dir / "lib"
        wanted = self.lib_modules
        for path in sorted(lib_dir.glob("*.py")):
            # __init__ always ships; it discovers whichever modules arrived.
            if (path.stem != "__init__" and path.stem not in self.ALWAYS
                    and wanted is not None and path.stem not in wanted):
                continue
            self.container.put_file(f"{self.REMOTE_DIR}/lib/{path.name}", path.read_bytes())

    def start(self, env: dict[str, str] | None = None, timeout: int = 30) -> None:
        self.install()
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
        self.container.exec(
            f"{exports} nohup python3 {self.REMOTE_SERVER} --port {self.port} "
            f"> {self.REMOTE_DIR}/kernel.log 2>&1 & echo started"
        )
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            reply = self._request({"action": "ping"}, timeout=15)
            if reply.get("pong"):
                self.started = True
                return
            last = reply.get("stderr", "")
            time.sleep(0.5)
        log = self.container.exec(f"cat {self.REMOTE_DIR}/kernel.log").stdout
        raise KernelError(f"kernel did not start within {timeout}s: {last}\n{log[-800:]}")

    def stop(self) -> None:
        self.container.exec(f"pkill -f 'kernel_server.py --port {self.port}' || true")
        self.started = False

    # -- calls ----------------------------------------------------------------
    TRANSPORT_ATTEMPTS = 3

    def _request(self, payload: dict, timeout: int = 180) -> dict:
        """Send one request to the kernel and return its reply."""
        import base64

        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        cmd = (f"echo {encoded} | base64 -d | "
               f"python3 {self.REMOTE_SERVER} --client --port {self.port}")
        last = ""
        for attempt in range(1, self.TRANSPORT_ATTEMPTS + 1):
            try:
                result = self.container.exec(cmd, timeout=timeout)
            except Exception as exc:
                last = f"exec raised {type(exc).__name__}: {exc}"
                result = None
            if result is not None:
                text = result.stdout.strip()
                if text:
                    try:
                        reply = json.loads(text.splitlines()[-1])
                    except ValueError:
                        last = f"unparsable kernel reply: {text[:300]}"
                    else:
                        # The client relay reports its own connection failure as a reply;
                        # that is still transport, and still worth a retry.
                        if str(reply.get("stderr", "")).startswith("kernel unreachable"):
                            last = reply["stderr"]
                        else:
                            return reply
                else:
                    last = result.stderr.strip() or "no reply"
            if attempt < self.TRANSPORT_ATTEMPTS:
                time.sleep(2 * attempt)
        return {"ok": False, "stdout": "", "stderr": f"kernel transport failed: {last}",
                "transport_failure": True}

    def run(self, code: str, timeout: int = 120, ns: str = "root") -> dict:
        """Execute `code` in the persistent namespace `ns`."""
        return self._request(
            {"action": "run", "code": code, "timeout": timeout, "ns": ns},
            timeout=timeout + 60,
        )

    def reset(self, ns: str = "root") -> dict:
        return self._request({"action": "reset", "ns": ns})

    def lib_status(self, ns: str = "root") -> dict:
        return self._request({"action": "lib", "ns": ns})
