import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.serving import make_server

from serena.config.serena_config import SerenaConfig
from serena.project_server import (
    MCPBridgeCloseRequest,
    MCPBridgeSessionRequest,
    MCPProjectRuntime,
    MCPRuntimeInfoRequest,
    MCPToolCallRequest,
    ProjectServer,
    ProjectServerClient,
    QueryProjectRequest,
)


@pytest.fixture
def project_server() -> ProjectServer:
    server = ProjectServer.__new__(ProjectServer)
    server._agent = MagicMock()
    server._serena_config = server._agent.serena_config
    server._loaded_projects_by_root = {}
    server._project_load_locks_by_root = {}
    server._active_project_lock = threading.Lock()
    server._loaded_projects_lock = threading.Lock()
    return server


@pytest.fixture
def authenticated_server(project_server: ProjectServer, monkeypatch: pytest.MonkeyPatch) -> ProjectServer:
    # expose the real HTTP routes with a query handler that needs no language servers
    project_server._agent.serena_config.auth_secret = "test-shared-secret"
    project_server._app = Flask(__name__)
    monkeypatch.setattr(project_server, "_query_project", lambda req: req.project_name)
    project_server._setup_routes()
    return project_server


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-secret", "test-shared-secret", "Bearer café"])
@pytest.mark.parametrize(
    "path",
    ["/heartbeat", "/query_project", "/mcp/runtime-info", "/mcp/tool-call", "/mcp/bridge-heartbeat", "/mcp/bridge-close"],
)
def test_project_server_rejects_invalid_credentials(authenticated_server: ProjectServer, authorization: str | None, path: str) -> None:
    # unauthorized requests are rejected even before query payload validation
    headers = {} if authorization is None else {"Authorization": authorization}
    with authenticated_server._app.test_client() as client:
        response = client.open(path, method="GET" if path == "/heartbeat" else "POST", headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize("use_wrong_password", [True, False])
def test_project_server_client_authenticates_requests(authenticated_server: ProjectServer, use_wrong_password: bool) -> None:
    # run the authenticated endpoints on an ephemeral local port
    http_server = make_server("127.0.0.1", 0, authenticated_server._app)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:

        def check_client():
            serena_config = SerenaConfig()
            serena_config.auth_secret = "wrong-secret" if use_wrong_password else authenticated_server.get_auth_secret()
            client = ProjectServerClient(serena_config, port=http_server.server_port)
            assert client.query_project("other", "find_symbol", "{}") == "other"

        # construction authenticates the heartbeat, raising a Connection error if using the wrong password
        if use_wrong_password:
            with pytest.raises(expected_exception=ConnectionError, match="401"):
                check_client()
        else:
            check_client()

    finally:
        http_server.shutdown()
        thread.join(timeout=5)
        http_server.server_close()


def test_cached_project_lookup_is_not_blocked_by_unrelated_cold_load(project_server: ProjectServer) -> None:
    cached_root = Path("/cached")
    cold_root = Path("/cold")
    cached_project = MagicMock()
    cold_project = MagicMock()
    cold_load_started = threading.Event()
    allow_cold_load_to_finish = threading.Event()

    cached_registration = MagicMock(project_root=cached_root)
    cold_registration = MagicMock(project_root=cold_root)

    def block_cold_load() -> None:
        cold_load_started.set()
        assert allow_cold_load_to_finish.wait(timeout=5)

    cold_registration.get_project_instance.return_value = cold_project
    cold_project.create_language_server_manager.side_effect = block_cold_load
    project_server._loaded_projects_by_root[str(cached_root)] = cached_project
    serena_config = cast(Any, project_server._agent.serena_config)
    serena_config.get_registered_project.side_effect = {
        "cached": cached_registration,
        "cold": cold_registration,
    }.get

    with ThreadPoolExecutor(max_workers=2) as executor:
        cold_future = executor.submit(project_server._get_project, "cold")
        assert cold_load_started.wait(timeout=1)

        cached_future = executor.submit(project_server._get_project, "cached")
        try:
            assert cached_future.result(timeout=1) is cached_project
        finally:
            allow_cold_load_to_finish.set()

        assert cold_future.result(timeout=1) is cold_project


def test_cold_loads_for_different_projects_can_run_concurrently(project_server: ProjectServer) -> None:
    first_root = Path("/first")
    second_root = Path("/second")
    first_project = MagicMock()
    second_project = MagicMock()
    first_load_started = threading.Event()
    allow_first_load_to_finish = threading.Event()

    first_registration = MagicMock(project_root=first_root)
    second_registration = MagicMock(project_root=second_root)

    def block_first_load() -> None:
        first_load_started.set()
        assert allow_first_load_to_finish.wait(timeout=5)

    first_registration.get_project_instance.return_value = first_project
    first_project.create_language_server_manager.side_effect = block_first_load
    second_registration.get_project_instance.return_value = second_project
    serena_config = cast(Any, project_server._agent.serena_config)
    serena_config.get_registered_project.side_effect = {
        "first": first_registration,
        "second": second_registration,
    }.get

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(project_server._get_project, "first")
        assert first_load_started.wait(timeout=1)

        second_future = executor.submit(project_server._get_project, "second")
        try:
            assert second_future.result(timeout=1) is second_project
        finally:
            allow_first_load_to_finish.set()

        assert first_future.result(timeout=1) is first_project


def test_concurrent_lookups_load_each_project_only_once(project_server: ProjectServer) -> None:
    project_root = Path("/project")
    project = MagicMock()
    load_started = threading.Event()
    second_lookup_started = threading.Event()
    allow_load_to_finish = threading.Event()
    registration = MagicMock(project_root=project_root)
    lookup_count = 0
    lookup_count_lock = threading.Lock()

    def block_load() -> None:
        load_started.set()
        assert allow_load_to_finish.wait(timeout=5)

    registration.get_project_instance.return_value = project
    project.create_language_server_manager.side_effect = block_load
    serena_config = cast(Any, project_server._agent.serena_config)

    def get_registration(project_name: str) -> MagicMock:
        nonlocal lookup_count
        assert project_name == "project"
        with lookup_count_lock:
            lookup_count += 1
            if lookup_count == 2:
                second_lookup_started.set()
        return registration

    serena_config.get_registered_project.side_effect = get_registration

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(project_server._get_project, "project")
        assert load_started.wait(timeout=1)
        second_future = executor.submit(project_server._get_project, "project")
        assert second_lookup_started.wait(timeout=1)
        allow_load_to_finish.set()

        assert first_future.result(timeout=1) is project
        assert second_future.result(timeout=1) is project

    registration.get_project_instance.assert_called_once_with(serena_config)
    project.create_language_server_manager.assert_called_once_with()


def test_concurrent_queries_serialize_active_project_context(project_server: ProjectServer) -> None:
    first_query_started = threading.Event()
    second_lookup_finished = threading.Event()
    allow_first_query_to_finish = threading.Event()
    state_lock = threading.Lock()
    state: dict[str, str | None] = {"active_project": None}

    def get_project(project_name: str) -> str:
        if project_name == "second":
            second_lookup_finished.set()
        return project_name

    @contextmanager
    def active_project_context(project: str) -> Iterator[None]:
        with state_lock:
            assert state["active_project"] is None
            state["active_project"] = project
        try:
            yield
        finally:
            with state_lock:
                state["active_project"] = None

    def apply_tool(hold: bool = False) -> str:
        if hold:
            first_query_started.set()
            assert allow_first_query_to_finish.wait(timeout=5)
        with state_lock:
            active_project = state["active_project"]
        assert active_project is not None
        return active_project

    tool = MagicMock()
    tool.is_readonly.return_value = True
    tool.apply_ex.side_effect = apply_tool
    server = cast(Any, project_server)
    server._get_project = MagicMock(side_effect=get_project)
    agent = cast(Any, project_server._agent)
    agent.active_project_context.side_effect = active_project_context
    agent.get_tool_by_name.return_value = tool
    first_request = QueryProjectRequest(project_name="first", tool_name="read_file", tool_params_json='{"hold": true}')
    second_request = QueryProjectRequest(project_name="second", tool_name="read_file", tool_params_json="{}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(project_server._query_project, first_request)
        assert first_query_started.wait(timeout=1)
        second_future = executor.submit(project_server._query_project, second_request)
        assert second_lookup_finished.wait(timeout=1)

        try:
            with pytest.raises(FutureTimeoutError):
                second_future.result(timeout=0.1)
        finally:
            allow_first_query_to_finish.set()

        assert first_future.result(timeout=1) == "first"
        assert second_future.result(timeout=1) == "second"



def test_shared_mcp_runtime_info_reuses_project_agent(project_server: ProjectServer, monkeypatch: pytest.MonkeyPatch) -> None:
    server = cast(Any, project_server)
    server._mcp_runtimes = {}
    server._mcp_runtime_load_locks = {}
    server._mcp_runtimes_lock = threading.Lock()

    agent = MagicMock()
    project = MagicMock(project_root="/project")
    agent.get_active_project.return_value = project
    tool = MagicMock()
    tool.get_name.return_value = "find_symbol"
    agent.get_exposed_tool_instances.return_value = [tool]
    agent.create_connection_prompt.return_value = "instructions"
    agent.get_context.return_value.structured_tool_output = None

    monkeypatch.setattr(server, "_mcp_runtime_key", lambda root, context: ("/project", context))
    runtime = MCPProjectRuntime(agent=agent, project_root="/project", context="codex", last_access=0.0)
    server._mcp_runtimes[("/project", "codex")] = runtime

    first = server._get_mcp_runtime_info(MCPRuntimeInfoRequest(project_root="/project", context="codex"))
    second = server._get_mcp_runtime_info(MCPRuntimeInfoRequest(project_root="/project", context="codex"))

    assert first.project_root == second.project_root == "/project"
    assert first.tool_names == ["find_symbol"]
    assert first.instructions == "instructions"
    assert server._mcp_runtimes[("/project", "codex")].agent is agent


def test_shared_mcp_tool_call_forwards_bridge_session_id(project_server: ProjectServer, monkeypatch: pytest.MonkeyPatch) -> None:
    server = cast(Any, project_server)
    server._mcp_runtimes = {}
    server._mcp_runtime_load_locks = {}
    server._mcp_runtimes_lock = threading.Lock()

    tool = MagicMock()
    tool.get_name.return_value = "serena_repl"
    tool.apply_ex.return_value = "ok"
    agent = MagicMock()
    agent.get_exposed_tool_instances.return_value = [tool]
    agent.get_tool_by_name.return_value = tool
    runtime = MCPProjectRuntime(agent=agent, project_root="/project", context="codex", last_access=0.0)
    monkeypatch.setattr(server, "_get_mcp_runtime", lambda root, context: runtime)

    result = server._call_mcp_tool(
        MCPToolCallRequest(
            project_root="/project",
            context="codex",
            session_id="chat-a",
            tool_name="serena_repl",
            arguments={"code": "1 + 1"},
        )
    )

    assert result == "ok"
    tool.apply_ex.assert_called_once_with(
        catch_exceptions=False,
        session_id_override="chat-a",
        code="1 + 1",
    )
    assert runtime.active_calls == 0



def test_shared_mcp_open_bridge_blocks_idle_eviction(project_server: ProjectServer, monkeypatch: pytest.MonkeyPatch) -> None:
    server = cast(Any, project_server)
    agent = MagicMock()
    agent.get_current_tasks.return_value = []
    runtime = MCPProjectRuntime(
        agent=agent,
        project_root="/project",
        context="codex",
        last_access=0.0,
        bridge_sessions={"chat-1": time.monotonic()},
    )
    server._mcp_runtimes = {("/project", "codex"): runtime}
    server._mcp_runtime_load_locks = {}
    server._mcp_runtimes_lock = threading.Lock()
    server.MCP_RUNTIME_IDLE_SECONDS = 0
    server.MCP_RUNTIME_MAX_PROJECTS = 0

    server._evict_mcp_runtimes()
    assert ("/project", "codex") in server._mcp_runtimes

    monkeypatch.setattr(server, "_mcp_runtime_key", lambda root, context: ("/project", context))
    server._close_mcp_bridge(MCPBridgeCloseRequest(project_root="/project", context="codex", session_id="chat-1"))
    server._evict_mcp_runtimes()

    assert ("/project", "codex") not in server._mcp_runtimes
    agent.on_shutdown.assert_called_once_with()


def test_shared_mcp_background_task_blocks_eviction(project_server: ProjectServer) -> None:
    server = cast(Any, project_server)
    agent = MagicMock()
    agent.get_current_tasks.return_value = [MagicMock()]
    runtime = MCPProjectRuntime(agent=agent, project_root="/project", context="codex", last_access=0.0)
    server._mcp_runtimes = {("/project", "codex"): runtime}
    server._mcp_runtime_load_locks = {}
    server._mcp_runtimes_lock = threading.Lock()
    server.MCP_RUNTIME_IDLE_SECONDS = 0
    server.MCP_RUNTIME_MAX_PROJECTS = 0

    server._evict_mcp_runtimes()

    assert ("/project", "codex") in server._mcp_runtimes
    agent.on_shutdown.assert_not_called()



def test_shared_mcp_stale_bridge_lease_does_not_pin_runtime(project_server: ProjectServer) -> None:
    server = cast(Any, project_server)
    agent = MagicMock()
    agent.get_current_tasks.return_value = []
    runtime = MCPProjectRuntime(
        agent=agent,
        project_root="/project",
        context="codex",
        last_access=0.0,
        bridge_sessions={"dead-chat": 0.0},
    )
    server._mcp_runtimes = {("/project", "codex"): runtime}
    server._mcp_runtime_load_locks = {}
    server._mcp_runtimes_lock = threading.Lock()
    server.MCP_RUNTIME_IDLE_SECONDS = 0
    server.MCP_RUNTIME_MAX_PROJECTS = 0
    server.MCP_BRIDGE_LEASE_SECONDS = 0

    server._evict_mcp_runtimes()

    assert ("/project", "codex") not in server._mcp_runtimes
    agent.on_shutdown.assert_called_once_with()


def test_shared_mcp_heartbeat_renews_bridge_lease(project_server: ProjectServer, monkeypatch: pytest.MonkeyPatch) -> None:
    server = cast(Any, project_server)
    runtime = MCPProjectRuntime(agent=MagicMock(), project_root="/project", context="codex", last_access=0.0)
    server._mcp_runtimes = {("/project", "codex"): runtime}
    server._mcp_runtimes_lock = threading.Lock()
    monkeypatch.setattr(server, "_mcp_runtime_key", lambda root, context: ("/project", context))

    server._touch_mcp_bridge(MCPBridgeSessionRequest(project_root="/project", context="codex", session_id="chat-1"))

    assert "chat-1" in runtime.bridge_sessions
    assert runtime.bridge_sessions["chat-1"] > 0



def test_project_server_heartbeat_exposes_shared_runtime_identity(project_server: ProjectServer) -> None:
    project_server._agent.serena_config.auth_secret = "heartbeat-secret"
    project_server._app = Flask(__name__)
    project_server._setup_routes()

    with project_server._app.test_client() as client:
        response = client.get("/heartbeat", headers={"Authorization": "Bearer heartbeat-secret"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "alive"
    assert isinstance(payload["shared_mcp_protocol_version"], int)
    assert isinstance(payload["shared_mcp_build_id"], str)
    assert payload["pid"] > 0
