# SPDX-License-Identifier: GPL-3.0-or-later

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from serena import mcp_bridge


@pytest.fixture
def daemon_process():
    """Real child with a recognizable daemon command line and guaranteed test cleanup."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "serena", "start-project-server"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.fixture
def startup_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, daemon_process):
    """Isolated daemon startup using a real child and a private cross-client lock."""
    monkeypatch.setattr(mcp_bridge, "SerenaPaths", lambda: SimpleNamespace(serena_user_home_dir=str(tmp_path)))
    monkeypatch.setattr(mcp_bridge, "_spawn_shared_daemon", lambda: daemon_process)
    return SimpleNamespace(tool_timeout=30, auth_secret="test-secret")


def test_startup_timeout_terminates_and_reaps_owned_child(monkeypatch, startup_environment, daemon_process) -> None:
    monkeypatch.setattr(mcp_bridge, "SharedMCPDaemonClient", Mock(side_effect=ConnectionError("not ready")))

    with pytest.raises(ConnectionError, match="did not become ready"):
        mcp_bridge.ensure_shared_daemon(startup_environment, startup_timeout=0.02)
    daemon_process.wait(timeout=1)


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("health check failed")])
def test_aborted_startup_cleans_up_and_preserves_original_error(monkeypatch, startup_environment, daemon_process, failure) -> None:
    monkeypatch.setattr(
        mcp_bridge,
        "SharedMCPDaemonClient",
        Mock(side_effect=[ConnectionError("not running"), ConnectionError("not running"), failure]),
    )

    with pytest.raises(type(failure)) as caught:
        mcp_bridge.ensure_shared_daemon(startup_environment, startup_timeout=1)
    assert caught.value is failure
    daemon_process.wait(timeout=1)


def test_incompatible_startup_does_not_leave_a_daemon_running(monkeypatch, startup_environment, daemon_process) -> None:
    calls = 0

    def connect(_config):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError("not running")
        raise mcp_bridge.IncompatibleSharedMCPDaemonError("wrong build", pid=daemon_process.pid)

    monkeypatch.setattr(mcp_bridge, "SharedMCPDaemonClient", connect)
    with pytest.raises(ConnectionError, match="wrong build"):
        mcp_bridge.ensure_shared_daemon(startup_environment, startup_timeout=0.02)
    daemon_process.wait(timeout=1)


def test_concurrent_clients_share_one_successful_startup(monkeypatch, startup_environment, daemon_process) -> None:
    client = object()
    starts = 0
    cold_clients = threading.Barrier(8)
    thread_state = threading.local()

    def connect(_config):
        # let all clients observe an unavailable daemon before any of them attempts startup
        if not getattr(thread_state, "checked", False):
            thread_state.checked = True
            cold_clients.wait(timeout=5)
            raise ConnectionError("not running")
        if starts == 0:
            raise ConnectionError("not running")
        return client

    def spawn():
        nonlocal starts
        time.sleep(0.05)
        starts += 1
        return daemon_process

    monkeypatch.setattr(mcp_bridge, "SharedMCPDaemonClient", connect)
    monkeypatch.setattr(mcp_bridge, "_spawn_shared_daemon", spawn)
    with ThreadPoolExecutor(max_workers=8) as clients:
        results = list(clients.map(lambda _: mcp_bridge.ensure_shared_daemon(startup_environment, startup_timeout=5), range(8)))

    assert all(result is client for result in results)
    assert starts == 1
    assert daemon_process.poll() is None


def test_existing_healthy_daemon_is_reused(monkeypatch, startup_environment, daemon_process) -> None:
    client = object()
    monkeypatch.setattr(mcp_bridge, "SharedMCPDaemonClient", lambda _config: client)
    assert mcp_bridge.ensure_shared_daemon(startup_environment) is client
    assert daemon_process.poll() is None
