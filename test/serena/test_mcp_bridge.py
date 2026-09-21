import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from serena.config.context_mode import SerenaAgentContext
from serena.mcp_bridge import (
    BridgeFastMCPTool,
    SerenaMCPBridge,
    _BridgeAgent,
    _daemon_command,
    _terminate_stale_daemon,
)
from serena.tools import ToolRegistry


def test_daemon_command_targets_project_server() -> None:
    command = _daemon_command()
    assert "start-project-server" in command


def test_proxy_tool_forwards_to_shared_runtime() -> None:
    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge.project_root = "/repo"
    bridge.context = SerenaAgentContext.from_name("codex")
    bridge.session_id = "chat-1"
    bridge.client = MagicMock()
    bridge.client.call_tool.return_value = "remote-result"
    bridge._init_activity_tracking(unused_idle_timeout_seconds=120, idle_timeout_seconds=900)

    tool_class = ToolRegistry().get_tool_class_by_name("search_for_pattern")
    proxy_class = bridge._proxy_tool_class(tool_class)
    proxy = proxy_class(SimpleNamespace(get_context=lambda: bridge.context))

    result = proxy.apply_ex(substring_pattern="needle", relative_path="src")

    assert result == "remote-result"
    bridge.client.call_tool.assert_called_once_with(
        project_root="/repo",
        context="codex",
        session_id="chat-1",
        tool_name="search_for_pattern",
        arguments={"substring_pattern": "needle", "relative_path": "src"},
    )


def test_proxy_tool_preserves_original_tool_name_and_schema_source() -> None:
    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge.project_root = "/repo"
    bridge.context = SerenaAgentContext.from_name("codex")
    bridge.session_id = "chat-1"
    bridge.client = MagicMock()

    original = ToolRegistry().get_tool_class_by_name("find_symbol")
    proxy = bridge._proxy_tool_class(original)

    assert proxy.get_name_from_cls() == "find_symbol"
    assert proxy.get_apply_docstring_from_cls() == original.get_apply_docstring_from_cls()
    assert proxy.get_apply_fn_metadata_from_cls().arg_model.model_json_schema()["properties"].keys() == (
        original.get_apply_fn_metadata_from_cls().arg_model.model_json_schema()["properties"].keys()
    )



def test_bridge_fast_mcp_tool_preserves_search_schema() -> None:
    context = SerenaAgentContext.from_name("codex")
    original = ToolRegistry().get_tool_class_by_name("search_for_pattern")

    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge.project_root = "/repo"
    bridge.context = context
    bridge.session_id = "chat-1"
    bridge.client = MagicMock(return_value="ok")

    proxy_class = bridge._proxy_tool_class(original)
    proxy = proxy_class(_BridgeAgent(context))
    mcp_tool = BridgeFastMCPTool(proxy, openai_tool_compatible=True, structured_output=None)

    assert mcp_tool.name == "search_for_pattern"
    assert "substring_pattern" in mcp_tool.parameters["properties"]
    assert "relative_path" in mcp_tool.parameters["properties"]



def test_unused_bridge_expires_on_short_idle_timeout() -> None:
    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge._init_activity_tracking(unused_idle_timeout_seconds=120, idle_timeout_seconds=900)
    bridge._last_client_activity = 100.0

    assert bridge._is_idle_expired(now=219.9) is False
    assert bridge._is_idle_expired(now=220.0) is True


def test_used_bridge_gets_longer_idle_timeout() -> None:
    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge._init_activity_tracking(unused_idle_timeout_seconds=120, idle_timeout_seconds=900)
    bridge._last_client_activity = 100.0
    bridge._has_seen_tool_call = True

    assert bridge._is_idle_expired(now=999.9) is False
    assert bridge._is_idle_expired(now=1000.0) is True


def test_active_tool_call_never_expires_bridge() -> None:
    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge._init_activity_tracking(unused_idle_timeout_seconds=1, idle_timeout_seconds=1)
    bridge._last_client_activity = 0.0
    bridge._active_tool_calls = 1

    assert bridge._is_idle_expired(now=10_000.0) is False


def test_proxy_tool_marks_bridge_as_used() -> None:
    bridge = SerenaMCPBridge.__new__(SerenaMCPBridge)
    bridge.project_root = "/repo"
    bridge.context = SerenaAgentContext.from_name("codex")
    bridge.session_id = "chat-activity"
    bridge.client = MagicMock()
    bridge.client.call_tool.return_value = "ok"
    bridge._init_activity_tracking(unused_idle_timeout_seconds=120, idle_timeout_seconds=900)

    original = ToolRegistry().get_tool_class_by_name("search_for_pattern")
    proxy = bridge._proxy_tool_class(original)(SimpleNamespace(get_context=lambda: bridge.context))
    before = bridge._last_client_activity
    time.sleep(0.001)
    assert proxy.apply_ex(substring_pattern="needle", relative_path="src") == "ok"

    assert bridge._has_seen_tool_call is True
    assert bridge._active_tool_calls == 0
    assert bridge._last_client_activity > before



def test_stale_daemon_termination_refuses_unrelated_process(monkeypatch) -> None:
    import psutil
    import pytest

    process = MagicMock()
    process.cmdline.return_value = ["python", "unrelated.py"]
    monkeypatch.setattr(psutil, "Process", lambda pid: process)

    with pytest.raises(ConnectionError, match="Refusing to terminate"):
        _terminate_stale_daemon(12345)
