"""Live tests use the dev container from scripts/devbox.sh and skip when it is not running."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEV_CONTAINER = os.environ.get("ERP_DEV_CONTAINER", "erpdev")


def _container_is_ready(name: str) -> bool:
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True
    )
    if running.returncode != 0 or running.stdout.strip() != b"true":
        return False
    marker = subprocess.run(
        ["docker", "exec", name, "test", "-f", "/tmp/saas_setup_complete"],
        capture_output=True,
    )
    return marker.returncode == 0


@pytest.fixture(scope="session")
def dev_container_name() -> str:
    if not _container_is_ready(DEV_CONTAINER):
        pytest.skip(
            f"dev container '{DEV_CONTAINER}' is not running with its scenario loaded "
            "— start it with `make devbox`"
        )
    return DEV_CONTAINER


@pytest.fixture(scope="session")
def container(dev_container_name: str):
    from harness.container import DockerContainer

    return DockerContainer(dev_container_name)
