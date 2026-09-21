from types import SimpleNamespace
from unittest.mock import MagicMock

from serena.config.context_mode import SerenaAgentContext
from serena.mcp_bridge import BridgeFastMCPTool, SerenaMCPBridge, _BridgeAgent, _daemon_command
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
    bridge.client.call_mcp_tool.return_value = "remote-result"

    tool_class = ToolRegistry().get_tool_class_by_name("search_for_pattern")
    proxy_class = bridge._proxy_tool_class(tool_class)
    proxy = proxy_class(SimpleNamespace(get_context=lambda: bridge.context))

    result = proxy.apply_ex(substring_pattern="needle", relative_path="src")

    assert result == "remote-result"
    bridge.client.call_mcp_tool.assert_called_once_with(
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
