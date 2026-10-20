# SPDX-License-Identifier: GPL-3.0-or-later

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from serena.project_server import MCPProjectRuntime, ProjectServer
from serena.shared_mcp_recovery import DaemonRecovery, DaemonRecoveryRefusedError, RecoveryScope
from serena.util.tool_call import ToolCall


@pytest.fixture
def stalled_server(monkeypatch):
    # represent a real expired ToolCall and the executor's published running-task view
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr("serena.util.tool_call.time", SimpleNamespace(monotonic=lambda: clock.now))
    call = ToolCall(1)
    call.mark_dispatched()
    clock.now += 32
    agent = Mock()
    agent.get_current_tasks.return_value = [SimpleNamespace(is_running=True)]
    runtime = MCPProjectRuntime(
        agent=agent, project_root="/repo", context="codex", last_access=0,
        active_calls=1, current_call=call, current_tool_name="find_symbol",
        current_tool_read_only=True, current_session_id="chat",
    )
    server = ProjectServer.__new__(ProjectServer)
    server._agent = None
    server._mcp_runtimes_lock = threading.Lock()
    server._mcp_runtime_load_locks = {}
    server._mcp_runtimes = {("/repo", "codex"): runtime}
    server._mcp_recovery = DaemonRecovery(server)
    return server, runtime, RecoveryScope("/repo", "codex", "chat")


def test_server_observes_stranded_worker_without_entering_its_binding_lock(stalled_server):
    server, runtime, scope = stalled_server
    ready, finish = threading.Event(), threading.Event()

    def hold_binding():
        with runtime.binding_lock:
            ready.set()
            assert finish.wait(3)

    worker = threading.Thread(target=hold_binding)
    worker.start()
    assert ready.wait(1)
    try:
        evidence = server._mcp_recovery.observe(scope)
        assert evidence.request_id == runtime.current_call.request_id
        assert evidence.overdue_seconds == 31
    finally:
        finish.set()
        worker.join(3)


@pytest.mark.parametrize("state", ["waiting-request", "queued-task", "other-project", "legacy", "cold-load", "other-session"])
def test_server_refuses_to_recover_when_blockage_is_not_isolated(stalled_server, state):
    server, runtime, scope = stalled_server
    if state == "waiting-request":
        runtime.active_calls += 1
    elif state == "queued-task":
        runtime.agent.get_current_tasks.return_value.append(SimpleNamespace(is_running=False))
    elif state == "other-project":
        agent = Mock()
        agent.get_current_tasks.return_value = [SimpleNamespace(is_running=True)]
        server._mcp_runtimes[("/other", "codex")] = MCPProjectRuntime(agent=agent, project_root="/other", context="codex", last_access=0)
    elif state == "legacy":
        server._agent = Mock()
        server._agent.get_current_tasks.return_value = [SimpleNamespace(is_running=True)]
    elif state == "cold-load":
        server._mcp_runtime_load_locks[("/other", "codex")] = threading.Lock()
    else:
        runtime.current_session_id = "other"
    with pytest.raises(DaemonRecoveryRefusedError):
        server._mcp_recovery.observe(scope)


def test_server_refuses_unknown_execution_metadata(stalled_server):
    server, runtime, scope = stalled_server
    runtime.current_tool_name = None
    with pytest.raises(DaemonRecoveryRefusedError):
        server._mcp_recovery.observe(scope)


@pytest.fixture
def recovery_routes(stalled_server, monkeypatch):
    from flask import Flask

    server, runtime, scope = stalled_server
    server._serena_config = SimpleNamespace(auth_secret="recovery-route-secret")
    server._app = Flask(__name__)
    server._setup_routes()
    monkeypatch.setattr("serena.project_server.shared_mcp_build_id", lambda: "test-build")
    clock = SimpleNamespace(now=200.0)
    monkeypatch.setattr("serena.shared_mcp_recovery.time", SimpleNamespace(monotonic=lambda: clock.now))
    headers = {"Authorization": "Bearer recovery-route-secret"}
    payload = {"project_root": scope.project_root, "context": scope.context, "session_id": scope.session_id}
    return server, runtime, payload, headers, clock


def test_recovery_control_route_requires_authentication(recovery_routes):
    server, _, payload, _, _ = recovery_routes
    with server._app.test_client() as client:
        response = client.post("/mcp/recovery", json={**payload, "action": "observe"})
    assert response.status_code == 401


@pytest.mark.parametrize("path", [
    "/query_project", "/mcp/runtime-info", "/mcp/bridge-heartbeat", "/mcp/bridge-close",
    "/mcp/tool-call", "/call_facade_method",
])
def test_claimed_recovery_excludes_all_new_work_but_preserves_control_access(recovery_routes, path):
    server, runtime, payload, headers, clock = recovery_routes
    with server._app.test_client() as client:
        observed = client.post("/mcp/recovery", json={**payload, "action": "observe"}, headers=headers)
        assert observed.status_code == 200
        request_id = observed.get_json()["request_id"]
        assert request_id == runtime.current_call.request_id
        clock.now += 2.0
        claimed = client.post(
            "/mcp/recovery", json={**payload, "action": "claim", "request_id": request_id}, headers=headers,
        )
        assert claimed.status_code == 200
        response = client.post(path, json={}, headers=headers)
        assert response.status_code == 503
        assert "not admitted" in response.get_data(as_text=True)
        assert client.get("/heartbeat", headers=headers).status_code == 200
        released = client.post(
            "/mcp/recovery",
            json={**payload, "action": "release", "token": claimed.get_json()["token"]},
            headers=headers,
        )
        assert released.status_code == 200
        server._query_project = lambda req: "admission restored"
        response = client.post(
            "/query_project",
            json={"project_name": "repo", "tool_name": "read_file", "tool_params_json": "{}"},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.get_data(as_text=True) == "admission restored"
