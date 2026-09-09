"""The container abstraction round-trips against a live task container."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_exec_returns_stdout_and_rc(container):
    result = container.exec("echo hello; exit 3")
    assert result.rc == 3
    assert result.stdout.strip() == "hello"
    assert not result.ok


def test_exec_unpacks_as_rc_stdout_stderr(container):
    rc, stdout, stderr = container.exec("echo out; echo err >&2")
    assert rc == 0
    assert stdout.strip() == "out"
    assert stderr.strip() == "err"


def test_put_file_then_exec(container):
    container.put_file("/harness/_t/probe.py", "print('probe', 6 * 7)\n")
    result = container.exec("python3 /harness/_t/probe.py")
    assert result.rc == 0
    assert result.stdout.strip() == "probe 42"


def test_put_file_creates_missing_parents(container):
    container.exec("rm -rf /harness/_t/deep")
    container.put_file("/harness/_t/deep/nested/file.txt", b"nested\n")
    assert container.read_file("/harness/_t/deep/nested/file.txt") == b"nested\n"


def test_stdin_round_trip(container):
    result = container.exec("cat", stdin="payload\n")
    assert result.stdout == "payload\n"


def test_large_output_survives_intact(container):
    # 10k characters must round-trip unmangled.
    result = container.exec("python3 -c \"print('x' * 10000)\"")
    assert len(result.stdout.strip()) == 10000


def test_timeout_reports_rather_than_hangs(container):
    result = container.exec("sleep 30", timeout=2)
    assert result.rc == 124
    assert "timeout" in result.stderr


def test_read_file_missing_raises(container):
    with pytest.raises(FileNotFoundError):
        container.read_file("/harness/_t/definitely-absent")


def test_env_is_passed_through(container):
    result = container.exec("echo $ERP_PROBE", env={"ERP_PROBE": "set-by-test"})
    assert result.stdout.strip() == "set-by-test"
