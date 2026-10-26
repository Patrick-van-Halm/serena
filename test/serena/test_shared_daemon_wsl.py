# SPDX-License-Identifier: GPL-3.0-or-later

import errno
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

import psutil
import pytest
import requests
from filelock import FileLock

from serena.project_server_endpoint import (
    ProjectServerAuthenticationError,
    ProjectServerEndpoint,
    ProjectServerEndpointError,
    ProjectServerTimeoutError,
)
from serena.shared_mcp_daemon import ProjectServerStartupError, SharedMCPDaemon
from serena.shared_mcp_recovery import RecoveryScope
from test.serena.test_shared_daemon_startup import DaemonEnvironment
from test.serena.test_shared_daemon_startup import daemon_environment as daemon_environment
from test.serena.test_shared_daemon_startup import unused_port


@pytest.fixture
def wsl_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # simulate only the WSL kernel identity and its timeout on a locally refused connection
    if sys.platform != "linux":
        pytest.skip("WSL bind checks require Linux socket semantics")
    monkeypatch.setattr("serena.shared_mcp_daemon.platform.release", lambda: "6.6.87.2-microsoft-standard-WSL2")
    real_get = requests.get

    def get(*args, **kwargs):
        try:
            return real_get(*args, **kwargs)
        except requests.ConnectionError as error:
            if not ProjectServerEndpoint._is_connection_refused(error):
                raise
        raise requests.ConnectTimeout("simulated WSL loopback timeout")

    monkeypatch.setattr(requests, "get", get)


@pytest.mark.parametrize("timeout_type", [requests.ConnectTimeout, requests.ReadTimeout])
def test_heartbeat_preserves_timeout_as_indeterminate(monkeypatch, timeout_type) -> None:
    def get(*args, **kwargs):
        raise timeout_type("probe timed out")

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(ProjectServerTimeoutError, match="health check timed out"):
        ProjectServerEndpoint().heartbeat("secret")


@pytest.mark.parametrize("operation", ["ensure_running", "restart"])
def test_wsl_timeout_starts_absent_daemon(daemon_environment: DaemonEnvironment, wsl_network, operation: str) -> None:
    config = daemon_environment.config(unused_port())
    client = getattr(SharedMCPDaemon(config), operation)(5)
    assert client.pid is not None
    assert daemon_environment.records() == [
        {"pid": client.pid, "port": config.project_server_port, "home": str(daemon_environment.home)}
    ]
    assert SharedMCPDaemon(config).ensure_running(5).pid == client.pid


def test_wsl_read_timeout_can_start_only_after_free_bind(daemon_environment, wsl_network, monkeypatch) -> None:
    real_get = requests.get

    def get(*args, **kwargs):
        try:
            return real_get(*args, **kwargs)
        except requests.ConnectTimeout as error:
            raise requests.ReadTimeout("WSL forwarding accepted but no application answered") from error

    monkeypatch.setattr(requests, "get", get)
    config = daemon_environment.config(unused_port())
    client = SharedMCPDaemon(config).ensure_running(5)
    assert client.pid == daemon_environment.records()[0]["pid"]
    assert len(daemon_environment.records()) == 1


def test_wsl_concurrent_clients_share_one_startup(daemon_environment, wsl_network) -> None:
    config = daemon_environment.config(unused_port())
    with ThreadPoolExecutor(max_workers=6) as pool:
        clients = list(pool.map(lambda _: SharedMCPDaemon(config).ensure_running(10), range(6)))
    assert len({client.pid for client in clients}) == 1
    assert len(daemon_environment.records()) == 1


def test_wsl_environment_port_reaches_child(daemon_environment, wsl_network, monkeypatch) -> None:
    config = daemon_environment.config(unused_port())
    override = unused_port()
    monkeypatch.setenv("SERENA_PROJECT_SERVER_PORT", str(override))
    client = SharedMCPDaemon(config).ensure_running(5)
    assert client.pid is not None
    assert daemon_environment.records()[0]["port"] == override


@pytest.mark.parametrize("timeout_type", [requests.ConnectTimeout, requests.ReadTimeout])
@pytest.mark.parametrize("operation", ["ensure_running", "restart"])
def test_wsl_timeout_preserves_existing_listener(daemon_environment, wsl_network, monkeypatch, timeout_type, operation) -> None:
    port = unused_port()
    listener = daemon_environment.launch(port, unrelated=True)

    def get(*args, **kwargs):
        raise timeout_type("selected endpoint did not answer")

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(ProjectServerEndpointError, match="could not be exclusively bound"):
        getattr(SharedMCPDaemon(daemon_environment.config(port)), operation)(1)
    assert listener.poll() is None
    assert len(daemon_environment.records()) == 1


def test_wsl_reserved_non_listening_socket_prevents_startup(daemon_environment, wsl_network) -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        with pytest.raises(ProjectServerEndpointError, match="could not be exclusively bound"):
            SharedMCPDaemon(daemon_environment.config(port)).ensure_running(1)
        assert daemon_environment.records() == []


@pytest.mark.parametrize("mode", ["401", "403", "404", "500", "302", "invalid"])
def test_wsl_reachable_errors_still_refuse_startup(daemon_environment, wsl_network, mode) -> None:
    port = unused_port()
    listener = daemon_environment.launch(port, mode=mode)
    with pytest.raises(ProjectServerEndpointError):
        SharedMCPDaemon(daemon_environment.config(port)).ensure_running(2)
    assert listener.poll() is None
    assert len(daemon_environment.records()) == 1


def test_wsl_timeout_rechecks_authentication_under_lock(daemon_environment, wsl_network, monkeypatch) -> None:
    calls = 0

    def get(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.ConnectTimeout("first probe timed out")
        response = requests.Response()
        response.status_code = 401
        response._content = b""
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(ProjectServerAuthenticationError):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(1)
    assert daemon_environment.records() == []


def test_wsl_endpoint_lock_still_prevents_duplicate_startup(daemon_environment, wsl_network) -> None:
    config = daemon_environment.config(unused_port())
    endpoint = ProjectServerEndpoint.from_config(config)
    with FileLock(str(endpoint.startup_lock_path(daemon_environment.home))):
        with pytest.raises(ProjectServerStartupError, match="startup lock"):
            SharedMCPDaemon(config).ensure_running(0.1)
    assert daemon_environment.records() == []


def test_wsl_timeout_does_not_authorize_agent_recovery(daemon_environment, wsl_network) -> None:
    with pytest.raises(ProjectServerTimeoutError):
        SharedMCPDaemon(daemon_environment.config(unused_port())).recover_blocked(RecoveryScope("/repo", "codex", "chat"), 1)
    assert daemon_environment.records() == []


def test_non_wsl_timeout_remains_terminal(daemon_environment, wsl_network, monkeypatch) -> None:
    monkeypatch.setattr("serena.shared_mcp_daemon.platform.release", lambda: "6.6.87-generic")
    with pytest.raises(ProjectServerTimeoutError):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(1)
    assert daemon_environment.records() == []


def test_wsl_remote_timeout_remains_terminal(daemon_environment, wsl_network, monkeypatch) -> None:
    def get(*args, **kwargs):
        raise requests.ConnectTimeout("remote timeout")

    monkeypatch.setattr(requests, "get", get)
    endpoint = ProjectServerEndpoint("192.0.2.1", unused_port())
    with pytest.raises(ProjectServerTimeoutError):
        SharedMCPDaemon(daemon_environment.config(endpoint.port), endpoint).ensure_running(1)
    assert daemon_environment.records() == []


def test_wsl_proxy_error_is_not_a_startup_signal(daemon_environment, wsl_network, monkeypatch) -> None:
    def get(*args, **kwargs):
        raise requests.exceptions.ProxyError("proxy timed out", requests.ConnectTimeout("proxy"))

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(ProjectServerEndpointError, match="proxy timed out"):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(1)
    assert daemon_environment.records() == []


def test_wsl_bind_denial_never_launches(daemon_environment, wsl_network, monkeypatch) -> None:
    config = daemon_environment.config(unused_port())

    def get(*args, **kwargs):
        raise requests.ConnectTimeout("timeout")

    def socket_denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "bind inspection not permitted")

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(socket, "socket", socket_denied)
    with pytest.raises(ProjectServerEndpointError, match="could not be exclusively bound"):
        SharedMCPDaemon(config).ensure_running(1)
    assert daemon_environment.records() == []


def test_wsl_unready_child_is_cleaned_up_at_deadline(daemon_environment, wsl_network, monkeypatch) -> None:
    monkeypatch.setenv("DAEMON_TEST_MODE", "hang")
    with pytest.raises(ProjectServerStartupError, match="deadline"):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(2.0)
    records = daemon_environment.records()
    assert len(records) == 1
    assert not psutil.pid_exists(records[0]["pid"])


def test_wsl_child_error_is_not_hidden_by_timeout(daemon_environment, wsl_network, monkeypatch) -> None:
    monkeypatch.setenv("DAEMON_TEST_MODE", "fail")
    with pytest.raises(ProjectServerStartupError, match="Actual startup failure: test bind/configuration error"):
        SharedMCPDaemon(daemon_environment.config(unused_port())).ensure_running(5)
    records = daemon_environment.records()
    assert len(records) == 1
    assert not psutil.pid_exists(records[0]["pid"])
