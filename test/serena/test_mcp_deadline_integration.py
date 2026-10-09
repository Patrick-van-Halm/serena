# SPDX-License-Identifier: GPL-3.0-or-later

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import anyio
import pytest
import requests
from werkzeug.serving import make_server

from serena.config.serena_config import SerenaConfig
from serena.mcp import SerenaFastMCPTool
from serena.mcp_bridge import BridgeFastMCPTool, SerenaMCPBridge
from serena.project_server import MCPProjectRuntime, ProjectServer
from serena.shared_mcp_client import SharedMCPDaemonClient
from serena.task_executor import TaskExecutor
from serena.tools.tools_base import Tool, ToolMarkerCanEdit


class ControlledTool(Tool, ToolMarkerCanEdit):
    def apply(self, value: str, block: bool = False, delay: float = 0.0) -> str:
        """Record one observable effect after optional controlled blocking."""
        self.agent.entered.set()
        if block:
            assert self.agent.release.wait(5)
        time.sleep(delay)
        self.agent.effects.append(value)
        return value


class ControlledAgent:
    def __init__(self, root, config):
        self.serena_config = config
        self.project = SimpleNamespace(project_root=str(root), project_name="test")
        self.context = SimpleNamespace(name="codex", structured_tool_output=None, tool_description_overrides={})
        self.executor = TaskExecutor("HTTPDeadlineTest")
        self.entered = threading.Event()
        self.release = threading.Event()
        self.effects = []
        self.tool = ControlledTool(self)

    def get_active_project(self):
        return self.project

    def get_context(self):
        return self.context

    def activate_project_from_path_or_name(self, root):
        self.project = SimpleNamespace(project_root=root, project_name="test")

    def get_exposed_tool_instances(self):
        return [self.tool]

    def get_tool_by_name(self, name):
        if name != self.tool.get_name():
            raise ValueError(name)
        return self.tool

    def get_active_tools(self):
        return SimpleNamespace(contains_tool_name=lambda name: name == self.tool.get_name())

    def get_language_server_manager(self):
        return None

    def issue_task(self, *args, **kwargs):
        return self.executor.issue_task(*args, **kwargs)

    def get_current_tasks(self):
        return self.executor.get_current_tasks()

    def record_tool_usage(self, *args):
        pass

    def create_connection_prompt(self):
        return "test"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def http_runtime(tmp_path, monkeypatch):
    config = SimpleNamespace(tool_timeout=2, auth_secret="test-secret")
    config.with_headless_mode_overrides = lambda: config
    monkeypatch.setattr(SerenaConfig, "from_config_file", lambda: config)
    app = ProjectServer(port=0)
    agent = ControlledAgent(tmp_path, config)
    runtime = MCPProjectRuntime(agent=agent, project_root=str(tmp_path), context="codex", last_access=time.monotonic())
    monkeypatch.setattr(app, "_get_mcp_runtime", lambda root, context: runtime)
    server = make_server("127.0.0.1", 0, app._app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    headers = {"Authorization": "Bearer test-secret"}

    def call(value, request_id, **extra):
        payload = dict(
            project_root=str(tmp_path), context="codex", session_id="chat", tool_name=agent.tool.get_name(),
            arguments={"value": value}, request_id=request_id, deadline=time.time() + 2,
        )
        payload.update(extra)
        return requests.post(url + "/mcp/tool-call", headers=headers, json=payload, timeout=4)

    try:
        yield SimpleNamespace(app=app, agent=agent, runtime=runtime, server=server, url=url, headers=headers, call=call, config=config)
    finally:
        agent.release.set()
        agent.executor.execute_task(lambda: None, logged=False, timeout=3)
        app._mcp_runtime_stop_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_timeout_encloses_server_execution_allowance(http_runtime) -> None:
    fixture = http_runtime
    client = SharedMCPDaemonClient(fixture.config, port=fixture.server.server_port)
    # the old two-second configuration used a one-second HTTP timeout
    assert client.call_tool(fixture.runtime.project_root, "codex", "chat", fixture.agent.tool.get_name(), {"value": "ok", "delay": 1.2}) == "ok"
    assert fixture.agent.effects == ["ok"]


def test_expired_queued_http_edit_never_executes_later(http_runtime) -> None:
    fixture = http_runtime
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(fixture.call, "first", "first", arguments={"value": "first", "block": True})
        try:
            assert fixture.agent.entered.wait(1)
            response = fixture.call("expired", "expired", deadline=time.time() + 0.1)
            assert response.status_code >= 400
            assert "outcome=not_started" in response.text
        finally:
            fixture.agent.release.set()
        assert first.result(timeout=2).status_code == 200
    assert fixture.call("barrier", "barrier").status_code == 200
    assert fixture.agent.effects == ["first", "barrier"]


def test_running_cancellation_does_not_allow_a_second_edit_to_overlap(http_runtime) -> None:
    fixture = http_runtime
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(fixture.call, "first", "running", arguments={"value": "first", "block": True})
        try:
            assert fixture.agent.entered.wait(1)
            cancelled = requests.post(
                fixture.url + "/mcp/cancel-tool-call", headers=fixture.headers,
                json={"session_id": "chat", "request_id": "running"}, timeout=1,
            )
            assert cancelled.json()["outcome"] == "unknown"
            assert "outcome=unknown" in first.result(timeout=1).text
            rejected = fixture.call("overlap", "overlap")
            assert rejected.status_code == 429
            assert "not submitted" in rejected.text
        finally:
            fixture.agent.release.set()
    fixture.agent.executor.execute_task(lambda: None, logged=False, timeout=2)
    assert fixture.call("after", "after").status_code == 200
    assert fixture.agent.effects == ["first", "after"]


def test_duplicate_http_operation_is_not_replayed(http_runtime) -> None:
    fixture = http_runtime
    assert fixture.call("once", "same-id").status_code == 200
    repeated = fixture.call("twice", "same-id")
    assert repeated.status_code == 409
    assert fixture.agent.effects == ["once"]


def test_early_cancellation_and_authentication(http_runtime) -> None:
    fixture = http_runtime
    payload = {"session_id": "chat", "request_id": "early"}
    denied = requests.post(fixture.url + "/mcp/cancel-tool-call", json=payload, timeout=1)
    assert denied.status_code == 401
    assert fixture.call("authorized", "early").status_code == 200

    payload["request_id"] = "cancel-before-post"
    cancelled = requests.post(fixture.url + "/mcp/cancel-tool-call", headers=fixture.headers, json=payload, timeout=1)
    assert cancelled.json()["outcome"] == "not_started"
    rejected = fixture.call("must-not-run", "cancel-before-post")
    assert rejected.status_code == 408
    assert fixture.agent.effects == ["authorized"]


@pytest.mark.anyio
@pytest.mark.parametrize("wrapper", [SerenaFastMCPTool, BridgeFastMCPTool])
async def test_mcp_wrappers_allow_progress_while_a_tool_is_blocked(wrapper, tmp_path) -> None:
    agent = ControlledAgent(tmp_path, SimpleNamespace(tool_timeout=2))
    tool = wrapper(agent.tool, openai_tool_compatible=False, structured_output=None)
    results = []

    async def invoke():
        results.append(await tool.run({"value": "mcp", "block": True}))

    async with anyio.create_task_group() as group:
        group.start_soon(invoke)
        try:
            with anyio.fail_after(1):
                while not agent.entered.is_set():
                    await anyio.sleep(0.001)
            assert results == []
        finally:
            agent.release.set()
    assert results == ["mcp"]
    assert {"value", "block", "delay"} <= tool.parameters["properties"].keys()


@pytest.mark.parametrize("inactive", [True, False])
def test_http_call_repairs_project_binding_on_its_worker(http_runtime, inactive) -> None:
    fixture = http_runtime
    fixture.agent.project = None if inactive else SimpleNamespace(project_root="/wrong-worktree", project_name="test")
    assert fixture.call("bound", "binding-repair").status_code == 200
    assert fixture.agent.project.project_root == fixture.runtime.project_root
    assert fixture.agent.effects == ["bound"]


def test_shared_runtime_applies_backpressure_before_submission(http_runtime) -> None:
    fixture = http_runtime
    fixture.app.MCP_RUNTIME_MAX_CALLS = 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(fixture.call, "first", "capacity-first", arguments={"value": "first", "block": True})
        try:
            assert fixture.agent.entered.wait(1)
            rejected = fixture.call("rejected", "capacity-second")
            assert rejected.status_code == 429
            assert "not submitted" in rejected.text
        finally:
            fixture.agent.release.set()
        assert first.result(timeout=2).status_code == 200
    assert fixture.agent.effects == ["first"]


@pytest.mark.anyio
async def test_bridge_host_cancellation_prevents_a_queued_edit(http_runtime, monkeypatch) -> None:
    fixture = http_runtime
    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge.project_root = fixture.runtime.project_root
    bridge.context = fixture.agent.context
    bridge.session_id = "bridge-chat"
    bridge.client = SharedMCPDaemonClient(fixture.config, port=fixture.server.server_port)
    proxy = bridge._proxy_tool_class(ControlledTool)(fixture.agent)
    tool = BridgeFastMCPTool(proxy, False, None, tool_timeout=2, cancellation_handler=bridge)
    received = threading.Event()
    original = fixture.app._call_mcp_tool

    def track_request(req):
        if req.session_id == bridge.session_id:
            received.set()
        return original(req)

    monkeypatch.setattr(fixture.app, "_call_mcp_tool", track_request)

    async def invoke():
        await tool.run({"value": "cancelled"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(fixture.call, "first", "first", arguments={"value": "first", "block": True})
        try:
            assert fixture.agent.entered.wait(1)
            async with anyio.create_task_group() as group:
                group.start_soon(invoke)
                with anyio.fail_after(1):
                    while not received.is_set():
                        await anyio.sleep(0.001)
                group.cancel_scope.cancel()
        finally:
            fixture.agent.release.set()
        assert first.result(timeout=2).status_code == 200
    assert fixture.call("barrier", "barrier").status_code == 200
    assert fixture.agent.effects == ["first", "barrier"]


@pytest.mark.anyio
async def test_bridge_waits_for_server_timeout_outcome(http_runtime) -> None:
    fixture = http_runtime
    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge.project_root = fixture.runtime.project_root
    bridge.context = fixture.agent.context
    bridge.session_id = "short-deadline"
    bridge.client = SharedMCPDaemonClient(fixture.config, port=fixture.server.server_port)
    proxy = bridge._proxy_tool_class(ControlledTool)(fixture.agent)
    tool = BridgeFastMCPTool(proxy, False, None, tool_timeout=0.1, cancellation_handler=bridge)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(fixture.call, "first", "first", arguments={"value": "first", "block": True})
        try:
            assert fixture.agent.entered.wait(1)
            with pytest.raises(Exception, match="outcome=not_started"):
                await tool.run({"value": "expired"})
        finally:
            fixture.agent.release.set()
        assert first.result(timeout=2).status_code == 200
    assert fixture.call("barrier", "barrier").status_code == 200
    assert fixture.agent.effects == ["first", "barrier"]
