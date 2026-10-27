# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import anyio
import pytest
from flask import Flask
from mcp.types import CallToolResult, ImageContent
from werkzeug.serving import make_server

from serena.config.serena_config import LanguageBackend, SerenaConfig
from serena.mcp import SerenaFastMCPTool
from serena.mcp_bridge import BridgeFastMCPTool, SerenaMCPBridge
from serena.project import Project
from serena.project_server import ProjectServer, ProjectServerClient
from serena.shared_mcp_client import SharedMCPDaemonClient
from serena.task_executor import TaskExecutor
from serena.tools import QueryProjectTool, ToolRegistry
from serena.tools.raw_file_tools import ReadFileRawTool
from serena.util.tool_result import ToolResultCodec

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMXsAAAAASUVORK5CYII=")


@pytest.fixture
def raw_agent(tmp_path: Path) -> Any:
    project = Project.__new__(Project)
    project.project_root = str(tmp_path)
    project.serena_config = SerenaConfig(full_access_mode=False)
    (tmp_path / "image.png").write_bytes(PNG)
    agent = MagicMock()
    agent.serena_config = project.serena_config
    agent.serena_config.tool_timeout = 2
    agent.get_active_project.return_value = project
    agent.get_active_project_or_raise.return_value = project
    agent.get_context.return_value.tool_description_overrides = {}
    agent.get_active_tools.return_value.contains_tool_name.return_value = True
    agent.get_language_server_manager.return_value = None
    agent.issue_task = TaskExecutor("raw-file-test").issue_task
    return agent


def test_raw_tool_is_available_by_default() -> None:
    assert ToolRegistry().get_tool_class_by_name("read_file_raw") is ReadFileRawTool
    assert "read_file_raw" in ToolRegistry().get_tool_names_default_enabled()


@pytest.mark.parametrize("wrapper", [SerenaFastMCPTool, BridgeFastMCPTool])
@pytest.mark.parametrize("structured", [None, False, True])
def test_mcp_wrappers_return_native_image_content(raw_agent: Any, wrapper: Any, structured: bool | None) -> None:
    tool = ReadFileRawTool(raw_agent)
    wrapped = wrapper(tool, openai_tool_compatible=True, structured_output=structured)

    async def run() -> CallToolResult:
        return await wrapped.run({"relative_path": "image.png"}, convert_result=True)

    result = anyio.run(run)
    assert isinstance(result, CallToolResult)
    assert isinstance(result.content[1], ImageContent)
    assert base64.b64decode(result.content[1].data) == PNG
    assert wrapped.annotations is not None
    assert wrapped.annotations.readOnlyHint is True
    assert wrapped.annotations.destructiveHint is False
    summary = raw_agent.record_tool_usage.call_args.args[1]
    assert base64.b64encode(PNG).decode() not in summary


def test_bridge_proxy_retains_raw_tool_result_schema(raw_agent: Any) -> None:
    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge.project_root = raw_agent.get_active_project().project_root
    bridge.context = MagicMock(name="codex")
    bridge.session_id = "raw-session"
    bridge.client = MagicMock()
    bridge.client.call_tool.return_value = ReadFileRawTool(raw_agent).apply("image.png")
    proxy_type = bridge._proxy_tool_class(ReadFileRawTool)
    proxy = proxy_type(raw_agent)
    wrapped = BridgeFastMCPTool(proxy, openai_tool_compatible=True, structured_output=True)

    async def run() -> CallToolResult:
        return await wrapped.run({"relative_path": "image.png"}, convert_result=True)

    result = anyio.run(run)
    assert isinstance(result.content[1], ImageContent)
    assert base64.b64decode(result.content[1].data) == PNG
    assert wrapped.fn_metadata.output_schema is None


def test_missing_file_is_a_tool_error(raw_agent: Any) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    wrapped = SerenaFastMCPTool(ReadFileRawTool(raw_agent), openai_tool_compatible=True, structured_output=None)

    async def run() -> None:
        await wrapped.run({"relative_path": "missing.png"}, convert_result=True)

    with pytest.raises(ToolError, match="FileNotFoundError"):
        anyio.run(run)


@pytest.mark.parametrize("route", ["bridge", "legacy-mcp", "query"])
def test_http_clients_preserve_multimodal_results(raw_agent: Any, route: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # run real project-server routes with a lightweight project/tool fixture
    server = ProjectServer.__new__(ProjectServer)
    server._agent = raw_agent
    server._app = Flask(__name__)
    native = ReadFileRawTool(raw_agent).apply("image.png")
    monkeypatch.setattr(server, "_call_mcp_tool", lambda req: native)
    monkeypatch.setattr(server, "_query_project", lambda req: native)
    server._setup_routes()
    http = make_server("127.0.0.1", 0, server._app, threaded=True)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        if route == "bridge":
            client = SharedMCPDaemonClient(raw_agent.serena_config, port=http.server_port)
            result = client.call_tool("/project", "codex", "session", "read_file_raw", {"relative_path": "image.png"})
        else:
            legacy_client = ProjectServerClient(raw_agent.serena_config, port=http.server_port)
            if route == "query":
                result = legacy_client.query_project("/project", "read_file_raw", '{"relative_path":"image.png"}')
            else:
                result = legacy_client.call_mcp_tool("/project", "codex", "session", "read_file_raw", {"relative_path": "image.png"})
        assert isinstance(result, CallToolResult)
        assert isinstance(result.content[1], ImageContent)
        assert base64.b64decode(result.content[1].data) == PNG
        assert ToolResultCodec.summary(result) == ToolResultCodec.summary(native)
    finally:
        http.shutdown()
        thread.join(timeout=5)
        http.server_close()


@pytest.mark.parametrize("structured", [None, False, True])
@pytest.mark.parametrize("raw", [False, True])
def test_cross_project_forwarding_preserves_text_and_native_content(raw_agent: Any, structured: bool | None, raw: bool) -> None:
    from contextlib import nullcontext

    project = raw_agent.get_active_project()
    native = ReadFileRawTool(raw_agent).apply("image.png")
    original = native if raw else "existing text result"
    forwarded = MagicMock()
    forwarded.is_readonly.return_value = True
    forwarded.get_apply_fn.return_value.return_value = original
    raw_agent.get_tool_by_name.return_value = forwarded
    raw_agent.get_language_backend.return_value = LanguageBackend.JETBRAINS
    raw_agent.active_project_context.return_value = nullcontext(project)
    registered = MagicMock()
    registered.get_project_instance.return_value = project
    raw_agent.serena_config.get_registered_project = MagicMock(return_value=registered)
    query = QueryProjectTool(raw_agent)
    assert query.apply("other-project", "read_file_raw" if raw else "read_file", "{}") is original
    wrapped = SerenaFastMCPTool(query, openai_tool_compatible=True, structured_output=structured)

    async def run() -> Any:
        return await wrapped.run({"project_name": "other-project", "tool_name": "read_file_raw", "tool_params_json": "{}"}, convert_result=True)

    result = anyio.run(run)
    if raw:
        assert isinstance(result, CallToolResult)
        assert isinstance(result.content[1], ImageContent)
        assert base64.b64decode(result.content[1].data) == PNG
    else:
        assert result[0].text == original


@pytest.mark.parametrize("blocked", ["inactive", "no-project"])
def test_raw_tool_preserves_agent_access_checks(raw_agent: Any, blocked: str) -> None:
    from serena.tools import ToolCallError

    if blocked == "inactive":
        raw_agent.get_active_tools.return_value.contains_tool_name.return_value = False
        expected = "is not active"
    else:
        raw_agent.get_active_project.return_value = None
        expected = "No active project"
    with pytest.raises(ToolCallError, match=expected):
        ReadFileRawTool(raw_agent).apply_ex(relative_path="image.png", catch_exceptions=False)
