"""The persistent kernel, against a live dev container."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def kernel(container):
    from harness.container import Kernel

    kern = Kernel(container, port=8799, lib_modules=["fmt"])
    kern.start()
    yield kern
    kern.stop()


def test_state_persists_across_calls(kernel):
    assert kernel.run("shopping = ['bolts', 'nuts']")["ok"]
    result = kernel.run("print(len(shopping), shopping[0])")
    assert result["ok"]
    assert result["stdout"].strip() == "2 bolts"


def test_stdout_and_stderr_are_captured_separately(kernel):
    result = kernel.run("import sys; print('to out'); print('to err', file=sys.stderr)")
    assert result["ok"]
    assert result["stdout"].strip() == "to out"
    assert result["stderr"].strip() == "to err"


def test_error_reports_the_agents_traceback_not_the_kernels(kernel):
    result = kernel.run("def f():\n    return 1 / 0\nf()")
    assert result["ok"] is False
    assert "ZeroDivisionError" in result["stderr"]
    # The kernel's own frames must not leak into what the model reads.
    assert "kernel_server.py" not in result["stderr"]


def test_large_output_survives_intact(kernel):
    result = kernel.run("print('x' * 10000)")
    assert result["ok"]
    assert len(result["stdout"].strip()) == 10000


def test_timeout_recovers_and_says_state_was_lost(kernel):
    assert kernel.run("keeper = 'before timeout'", ns="tmo")["ok"]
    result = kernel.run("import time; time.sleep(30)", timeout=2, ns="tmo")
    assert result["ok"] is False
    assert result["restarted"] is True
    assert "kernel restarted" in result["stderr"]

    # The namespace really was rebuilt, and it still works afterwards.
    after = kernel.run("print(keeper)", ns="tmo")
    assert after["ok"] is False
    assert "NameError" in after["stderr"]
    assert kernel.run("print(2 + 2)", ns="tmo")["stdout"].strip() == "4"


def test_namespaces_are_isolated(kernel):
    kernel.run("secret = 'root only'", ns="root")
    result = kernel.run("print(secret)", ns="sub-1")
    assert result["ok"] is False
    assert "NameError" in result["stderr"]


def test_library_is_preloaded(kernel):
    status = kernel.lib_status()
    assert "fmt" in status["loaded"], status
    assert not status["failed"], status
    result = kernel.run("print(str(Table([{'a': 1, 'b': 2}])))")
    assert result["ok"], result["stderr"]
    assert "a" in result["stdout"] and "1" in result["stdout"]


def test_only_requested_lib_modules_are_installed(kernel, container):
    """The kernel fixture asks for `fmt` only, so nothing else may arrive except plumbing."""
    listing = sorted(f for f in container.exec("ls /harness/lib").stdout.split()
                     if f.endswith(".py"))
    assert listing == ["__init__.py", "finish.py", "fmt.py"], listing


def test_reset_clears_the_namespace(kernel):
    kernel.run("throwaway = 1", ns="resettable")
    kernel.reset(ns="resettable")
    result = kernel.run("print(throwaway)", ns="resettable")
    assert result["ok"] is False
    assert "NameError" in result["stderr"]


def test_plumbing_ships_even_when_the_config_omits_it(container):
    """A config's `lib` list names primitives under test, not the plumbing they need."""
    from harness.container import Kernel

    kern = Kernel(container, port=8801, lib_modules=["erp", "check", "plan"])
    try:
        kern.install()
        listing = container.exec("ls /harness/lib").stdout.split()
        assert "fmt.py" in listing, "fmt is required by erp.py"
        assert "finish.py" in listing, "finish is how an episode ends"
        assert "erp.py" in listing and "check.py" in listing and "plan.py" in listing
    finally:
        container.exec("rm -rf /harness/lib")


def test_a_stale_export_name_does_not_lose_the_module(container, tmp_path):
    """One bad EXPORTS entry cost the whole `check` module on the first C_full run."""
    import shutil

    from harness.container import Kernel

    # A throwaway source tree, so this goes through the real install path rather than
    # racing it: start() clears the remote directory before copying.
    source = tmp_path / "harness"
    (source / "lib").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "harness" / "kernel_server.py", source / "kernel_server.py")
    for name in ("__init__.py", "fmt.py", "finish.py"):
        shutil.copy(REPO_ROOT / "harness" / "lib" / name, source / "lib" / name)
    (source / "lib" / "probe_mod.py").write_text(
        "EXPORTS = ['Present', 'AbsentSymbol']\nclass Present: pass\n")

    kern = Kernel(container, port=8802, lib_modules=None, source_dir=source)
    try:
        kern.start()
        status = kern.lib_status()
        assert "probe_mod" in status["loaded"], status
        assert "probe_mod:exports" in status["failed"]
        assert "AbsentSymbol" in status["failed"]["probe_mod:exports"]
        assert kern.run("print(Present.__name__)")["stdout"].strip() == "Present"
    finally:
        kern.stop()
        container.exec("rm -rf /harness/lib")


def test_a_large_request_travels_inline(kernel):
    """Requests travel base64 in the command line now — no file staging to fail."""
    big = "x = '" + ("a" * 60_000) + "'\nprint(len(x))"
    reply = kernel.run(big)
    assert reply["ok"], reply["stderr"]
    assert reply["stdout"].strip() == "60000"


def test_transport_failure_is_reported_not_raised(container):
    """A kernel that is not there yields a labelled reply, after retries, never an exception."""
    from harness.container import Kernel

    kern = Kernel(container, port=8899, lib_modules=[])   # nothing listening on 8899
    kern.TRANSPORT_ATTEMPTS = 1
    reply = kern.run("print(1)", timeout=10)
    assert reply["ok"] is False
    assert reply.get("transport_failure") is True
    assert "kernel" in reply["stderr"]
