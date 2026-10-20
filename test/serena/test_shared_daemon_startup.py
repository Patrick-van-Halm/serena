# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest
import requests
from filelock import FileLock

from serena.project_server_endpoint import (
    ProjectServerAuthenticationError,
    ProjectServerEndpoint,
    ProjectServerEndpointError,
    ProjectServerUnavailableError,
)
from serena.shared_mcp_client import SharedMCPDaemonClient
from serena.shared_mcp_daemon import ProjectServerStartupError, SharedMCPDaemon
from serena.shared_mcp_protocol import SHARED_MCP_PROTOCOL_VERSION
from serena.shared_mcp_recovery import DaemonRecoveryRefusedError, RecoveryScope


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class DaemonEnvironment:
    home: Path
    starts: Path

    def config(self, port: int) -> SimpleNamespace:
        return SimpleNamespace(
            project_server_port=port,
            tool_timeout=5.0,
            auth_secret="startup-test-secret",
            config_file_path=str(self.home / "serena_config.yml"),
        )

    def records(self) -> list[dict]:
        if not self.starts.exists():
            return []
        return [json.loads(line) for line in self.starts.read_text().splitlines()]

    def launch(
        self,
        port: int,
        *,
        mode: str = "ready",
        build: str = "test-build",
        pid: int | None = None,
        unrelated: bool = False,
    ) -> subprocess.Popen:
        # run a real listener independently of the startup coordinator
        environment = os.environ.copy()
        environment["DAEMON_TEST_MODE"] = mode
        environment["DAEMON_TEST_BUILD"] = build
        if pid is not None:
            environment["DAEMON_TEST_PID"] = str(pid)
        code = "from serena.cli import top_level; top_level()"
        if unrelated:
            code = "from serena.cli import top_level as serve; serve()"
        process = subprocess.Popen(
            [sys.executable, "-c", code, "start-project-server", "--host", "127.0.0.1", "--port", str(port), "--log-level", "WARNING"],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"Synthetic daemon exited: {process.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return process
            except OSError:
                time.sleep(0.02)
        process.kill()
        process.wait()
        pytest.fail("Synthetic listener did not start")


@pytest.fixture
def daemon_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # substitute only the daemon application; exercise real HTTP, file locks, and child processes
    module_root = tmp_path / "application"
    package = module_root / "serena"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    shutil.copyfile(Path(__file__).parents[1] / "resources" / "project_server" / "cli.py", package / "cli.py")
    shutil.copyfile(Path(__file__).parents[2] / "src" / "serena" / "shared_mcp_recovery.py", package / "shared_mcp_recovery.py")
    home = tmp_path / "home"
    home.mkdir()
    starts = tmp_path / "starts.jsonl"
    monkeypatch.setenv("PYTHONPATH", str(module_root))
    monkeypatch.setenv("DAEMON_TEST_STARTS", str(starts))
    monkeypatch.setenv("DAEMON_TEST_SECRET", "startup-test-secret")
    monkeypatch.setenv("DAEMON_TEST_BUILD", "test-build")
    monkeypatch.setenv("DAEMON_TEST_PROTOCOL", str(SHARED_MCP_PROTOCOL_VERSION))
    monkeypatch.delenv("SERENA_PROJECT_SERVER_PORT", raising=False)
    monkeypatch.setattr("serena.shared_mcp_client.shared_mcp_build_id", lambda: "test-build")
    fixture = DaemonEnvironment(home, starts)
    try:
        yield fixture
    finally:
        for record in fixture.records():
            try:
                process = psutil.Process(record["pid"])
                process.kill()
                process.wait(timeout=3)
            except psutil.NoSuchProcess:
                pass


def test_concurrent_clients_start_one_daemon_and_reuse_it(daemon_environment) -> None:
    config = daemon_environment.config(unused_port())
    with ThreadPoolExecutor(max_workers=8) as pool:
        clients = list(pool.map(lambda _: SharedMCPDaemon(config).ensure_running(10), range(8)))
    assert len({client.pid for client in clients}) == 1
    assert SharedMCPDaemon(config).ensure_running().pid == clients[0].pid
    assert daemon_environment.records() == [
        {"pid": clients[0].pid, "port": config.project_server_port, "home": str(daemon_environment.home)}
    ]


def test_environment_port_is_used_by_spawn_and_client(daemon_environment, monkeypatch) -> None:
    config = daemon_environment.config(unused_port())
    override = unused_port()
    monkeypatch.setenv("SERENA_PROJECT_SERVER_PORT", str(override))
    client = SharedMCPDaemon(config).ensure_running(5)
    assert SharedMCPDaemonClient(config).pid == client.pid
    assert daemon_environment.records()[0]["port"] == override
    assert config.project_server_port != override


def test_endpoint_lock_blocks_same_endpoint_but_not_other_ports(daemon_environment) -> None:
    first, second = unused_port(), unused_port()
    endpoint = ProjectServerEndpoint(host="localhost", port=first)
    with FileLock(str(endpoint.startup_lock_path(daemon_environment.home))):
        with pytest.raises(ProjectServerStartupError, match="startup lock"):
            SharedMCPDaemon(daemon_environment.config(first)).ensure_running(0.1)
        client = SharedMCPDaemon(daemon_environment.config(second)).ensure_running(5)
        assert client.pid is not None
    assert [row["port"] for row in daemon_environment.records()] == [second]


@pytest.mark.parametrize("mode", ["401", "403", "404", "500", "302", "invalid"])
def test_reachable_mismatch_never_launches_or_replaces(daemon_environment, mode) -> None:
    port = unused_port()
    process = daemon_environment.launch(port, mode=mode)
    error = ProjectServerAuthenticationError if mode in {"401", "403"} else ProjectServerEndpointError
    with pytest.raises(error, match=f"127.0.0.1:{port}"):
        SharedMCPDaemon(daemon_environment.config(port)).ensure_running(2)
    assert process.poll() is None
    assert len(daemon_environment.records()) == 1


def test_authentication_mismatch_names_settings_without_leaking_secret(daemon_environment) -> None:
    port = unused_port()
    process = daemon_environment.launch(port)
    config = daemon_environment.config(port)
    config.auth_secret = "wrong-secret"
    with pytest.raises(ProjectServerAuthenticationError) as caught:
        SharedMCPDaemon(config).ensure_running()
    assert "SERENA_HOME" in str(caught.value)
    assert "SERENA_PROJECT_SERVER_PORT" in str(caught.value)
    assert config.auth_secret not in str(caught.value)
    assert process.poll() is None
    assert len(daemon_environment.records()) == 1


@pytest.mark.parametrize("mode", ["ready", "legacy"])
def test_replaces_only_owned_stale_listener_on_selected_port(daemon_environment, mode) -> None:
    selected, other = unused_port(), unused_port()
    stale = daemon_environment.launch(selected, build="old-build", mode=mode)
    independent = daemon_environment.launch(other, build="old-build")
    client = SharedMCPDaemon(daemon_environment.config(selected)).ensure_running(10)
    assert client.pid != stale.pid
    assert stale.poll() is not None
    assert independent.poll() is None
    assert len(daemon_environment.records()) == 3


def test_spoofed_pid_cannot_kill_serena_on_another_port(daemon_environment) -> None:
    selected, other = unused_port(), unused_port()
    independent = daemon_environment.launch(other)
    spoof = daemon_environment.launch(selected, build="old-build", pid=independent.pid)
    with pytest.raises(ProjectServerEndpointError, match="not the listener"):
        SharedMCPDaemon(daemon_environment.config(selected)).ensure_running()
    assert independent.poll() is None and spoof.poll() is None
    assert len(daemon_environment.records()) == 2


def test_command_substrings_do_not_establish_process_ownership(daemon_environment) -> None:
    port = unused_port()
    process = daemon_environment.launch(port, build="old-build", unrelated=True)
    with pytest.raises(ProjectServerEndpointError, match="not a Serena project server"):
        SharedMCPDaemon(daemon_environment.config(port)).ensure_running()
    assert process.poll() is None
    assert len(daemon_environment.records()) == 1


def test_startup_failure_surfaces_actual_stderr_and_reaps_child(daemon_environment, monkeypatch) -> None:
    monkeypatch.setenv("DAEMON_TEST_MODE", "fail")
    config = daemon_environment.config(unused_port())
    with pytest.raises(ProjectServerStartupError) as caught:
        SharedMCPDaemon(config).ensure_running(5)
    message = str(caught.value)
    assert "Actual startup failure: test bind/configuration error" in message
    assert "23" in message and "Startup log:" in message
    assert config.auth_secret not in message
    assert "<redacted>" in message
    assert not psutil.pid_exists(daemon_environment.records()[0]["pid"])
    logs = list((daemon_environment.home / "logs").glob("project-server-startup-*.log"))
    assert len(logs) == 1 and logs[0].stat().st_size > 65536
    if os.name == "posix":
        assert logs[0].stat().st_mode & 0o077 == 0


def test_startup_timeout_reaps_only_owned_child(daemon_environment, monkeypatch) -> None:
    independent = daemon_environment.launch(unused_port())
    monkeypatch.setenv("DAEMON_TEST_MODE", "hang")
    with pytest.raises(ProjectServerStartupError, match="deadline"):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(2.0)
    assert independent.poll() is None
    assert not psutil.pid_exists(daemon_environment.records()[-1]["pid"])


def test_newly_started_incompatible_daemon_is_cleaned_up(daemon_environment, monkeypatch) -> None:
    monkeypatch.setenv("DAEMON_TEST_BUILD", "wrong-build")
    with pytest.raises(ProjectServerStartupError, match="wrong-build"):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(5)
    assert len(daemon_environment.records()) == 1
    assert not psutil.pid_exists(daemon_environment.records()[0]["pid"])


def test_interruption_preserves_original_error_and_cleans_child(daemon_environment, monkeypatch) -> None:
    real_heartbeat = ProjectServerEndpoint.heartbeat
    interrupt = KeyboardInterrupt()

    def heartbeat(endpoint, secret, timeout=5):
        if daemon_environment.records():
            raise interrupt
        return real_heartbeat(endpoint, secret, timeout)

    monkeypatch.setattr(ProjectServerEndpoint, "heartbeat", heartbeat)
    with pytest.raises(KeyboardInterrupt) as caught:
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(5)
    assert caught.value is interrupt
    assert not psutil.pid_exists(daemon_environment.records()[0]["pid"])


@pytest.mark.parametrize("error", [requests.ReadTimeout("busy"), requests.ConnectionError("connection reset")])
def test_indeterminate_health_failure_is_not_treated_as_absence(daemon_environment, monkeypatch, error) -> None:
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(ProjectServerEndpointError):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(1)
    assert daemon_environment.records() == []


def test_refused_connection_is_explicitly_classified() -> None:
    with pytest.raises(ProjectServerUnavailableError):
        ProjectServerEndpoint(port=unused_port()).heartbeat("secret", timeout=0.5)


def test_recheck_authentication_failure_does_not_spawn(daemon_environment, monkeypatch) -> None:
    checks = 0

    def heartbeat(endpoint, secret, timeout=5):
        nonlocal checks
        checks += 1
        if checks == 1:
            raise ProjectServerUnavailableError("not listening yet")
        raise ProjectServerAuthenticationError(f"endpoint/configuration mismatch at {endpoint.base_url}")

    monkeypatch.setattr(ProjectServerEndpoint, "heartbeat", heartbeat)
    with pytest.raises(ProjectServerAuthenticationError, match="mismatch"):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(2)
    assert daemon_environment.records() == []


def test_new_child_authentication_failure_does_not_restart(daemon_environment, monkeypatch) -> None:
    monkeypatch.setenv("DAEMON_TEST_MODE", "403")
    with pytest.raises(ProjectServerStartupError, match="authentication rejected.*403"):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(5)
    assert len(daemon_environment.records()) == 1
    assert not psutil.pid_exists(daemon_environment.records()[0]["pid"])


def test_proxy_refusal_is_not_endpoint_refusal(daemon_environment, monkeypatch) -> None:
    import errno

    def fail(*args, **kwargs):
        raise requests.exceptions.ProxyError("proxy failed", ConnectionRefusedError(errno.ECONNREFUSED, "proxy refused"))

    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(ProjectServerEndpointError, match="proxy failed"):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(1)
    assert daemon_environment.records() == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX zombie and signal semantics")
def test_stale_child_of_another_parent_can_be_replaced_without_killing_parent(daemon_environment) -> None:
    # keep a separate parent alive after its child exits, as detached bridge launches can do
    port = unused_port()
    child_command = [sys.executable, "-c", "from serena.cli import top_level; top_level()", "start-project-server",
                     "--host", "127.0.0.1", "--port", str(port), "--log-level", "WARNING"]
    holder_code = (
        "import subprocess,signal,time,sys; "
        f"child=subprocess.Popen({child_command!r}); "
        "signal.signal(signal.SIGTERM, lambda *_: (child.wait(),sys.exit(0))); time.sleep(60)"
    )
    environment = os.environ.copy()
    environment["DAEMON_TEST_BUILD"] = "old-build"
    holder = subprocess.Popen([sys.executable, "-c", holder_code], env=environment,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    old_pid = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            records = daemon_environment.records()
            if records:
                old_pid = records[0]["pid"]
            try:
                ProjectServerEndpoint(port=port).heartbeat("startup-test-secret", timeout=0.1)
                break
            except ProjectServerUnavailableError:
                time.sleep(0.02)
        else:
            pytest.fail("Listener did not start")
        client = SharedMCPDaemon(daemon_environment.config(port)).ensure_running(15)
        assert client.pid != old_pid
        assert holder.poll() is None
    finally:
        if old_pid is not None:
            try:
                psutil.Process(old_pid).terminate()
            except psutil.NoSuchProcess:
                pass
        holder.terminate()
        holder.wait(timeout=5)


@pytest.mark.parametrize("denial", ["foreign-user", "access-denied"])
def test_unverifiable_ownership_leaves_listener_untouched(daemon_environment, monkeypatch, denial) -> None:
    port = unused_port()
    listener = daemon_environment.launch(port, build="old-build")
    original_process = psutil.Process

    def inspect_process(pid=None):
        process = original_process(pid)
        if pid != listener.pid:
            return process
        if denial == "access-denied":
            raise psutil.AccessDenied(pid)
        inspected = MagicMock(wraps=process)
        inspected.username.return_value = "different-user"
        if os.name == "posix":
            inspected.uids.return_value = SimpleNamespace(real=os.getuid() + 1)
        return inspected

    with monkeypatch.context() as patch:
        patch.setattr(psutil, "Process", inspect_process)
        with pytest.raises(ProjectServerEndpointError):
            SharedMCPDaemon(daemon_environment.config(port)).ensure_running(5)
    assert listener.poll() is None
    assert len(daemon_environment.records()) == 1


@pytest.mark.parametrize("old_build", ["test-build", "old-build"])
def test_restart_replaces_selected_daemon_but_not_another_port(daemon_environment, old_build):
    port = unused_port()
    old = daemon_environment.launch(port, build=old_build)
    other = daemon_environment.launch(unused_port())
    client = SharedMCPDaemon(daemon_environment.config(port)).restart(15)
    assert client.pid != old.pid
    assert old.wait(timeout=2) is not None
    assert other.poll() is None
    assert daemon_environment.records()[-1]["port"] == port


def test_restart_starts_a_missing_daemon(daemon_environment):
    client = SharedMCPDaemon(daemon_environment.config(unused_port())).restart(5)
    assert client.pid is not None
    assert len(daemon_environment.records()) == 1


@pytest.mark.parametrize("mode", ["401", "403", "invalid"])
def test_restart_never_replaces_an_unauthenticated_or_unverifiable_listener(daemon_environment, mode):
    port = unused_port()
    old = daemon_environment.launch(port, mode=mode)
    with pytest.raises(ProjectServerEndpointError):
        SharedMCPDaemon(daemon_environment.config(port)).restart(5)
    assert old.poll() is None
    assert len(daemon_environment.records()) == 1


def test_restart_lock_timeout_does_not_stop_listener(daemon_environment):
    port = unused_port()
    old = daemon_environment.launch(port)
    endpoint = ProjectServerEndpoint(port=port)
    with FileLock(str(endpoint.startup_lock_path(daemon_environment.home))):
        with pytest.raises(ProjectServerStartupError, match="startup lock"):
            SharedMCPDaemon(daemon_environment.config(port)).restart(0.1)
    assert old.poll() is None


def test_restart_reports_replacement_failure_and_reaps_child(daemon_environment, monkeypatch):
    port = unused_port()
    old = daemon_environment.launch(port)
    monkeypatch.setenv("DAEMON_TEST_MODE", "fail")
    with pytest.raises(ProjectServerStartupError, match="Actual startup failure"):
        SharedMCPDaemon(daemon_environment.config(port)).restart(15)
    old.wait(timeout=2)
    assert not psutil.pid_exists(daemon_environment.records()[-1]["pid"])


def test_agent_recovery_restarts_after_two_positive_server_observations(daemon_environment):
    port = unused_port()
    old = daemon_environment.launch(port, mode="blocked")
    client = SharedMCPDaemon(daemon_environment.config(port)).recover_blocked(RecoveryScope("/repo", "codex", "chat"), 15)
    assert client.pid != old.pid
    old.wait(timeout=2)
    assert len(daemon_environment.records()) == 2


@pytest.mark.parametrize("mode", ["ready", "recovers", "edit"])
def test_agent_recovery_preserves_healthy_recovered_or_editing_daemons(daemon_environment, mode):
    port = unused_port()
    old = daemon_environment.launch(port, mode=mode)
    with pytest.raises(DaemonRecoveryRefusedError):
        SharedMCPDaemon(daemon_environment.config(port)).recover_blocked(RecoveryScope("/repo", "codex", "chat"), 15)
    assert old.poll() is None
    assert len(daemon_environment.records()) == 1


def test_agent_recovery_does_not_start_an_absent_daemon(daemon_environment):
    with pytest.raises(ProjectServerUnavailableError):
        SharedMCPDaemon(daemon_environment.config(unused_port())).recover_blocked(RecoveryScope("/repo", "codex", "chat"), 5)
    assert daemon_environment.records() == []


def test_agent_recovery_cannot_restart_another_sessions_stalled_call(daemon_environment):
    port = unused_port()
    old = daemon_environment.launch(port, mode="blocked")
    with pytest.raises(DaemonRecoveryRefusedError):
        SharedMCPDaemon(daemon_environment.config(port)).recover_blocked(RecoveryScope("/repo", "codex", "other"), 5)
    assert old.poll() is None
