"""Projectless CLI startup preserves the selected filesystem root and usable MCP tools."""

import json
import logging
from pathlib import Path

import anyio
import pytest
from click import Command
from click.testing import CliRunner
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent

from serena.cli import TopLevelCommands, find_project_root
from serena.config.serena_config import SerenaConfig, SerenaPaths


@pytest.mark.parametrize("command", [TopLevelCommands.start_mcp_server, TopLevelCommands.start_mcp_bridge], ids=["server", "bridge"])
@pytest.mark.parametrize("selection", ["cwd", "explicit", "git-root", "malformed"])
def test_unconfigured_directory_serves_file_and_shell_tools(
    command: Command, selection: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # select a directory without a Serena configuration through each supported startup route
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("projectless read\n", encoding="utf-8")
    config_path = workspace / ".serena" / "project.yml"
    invalid_config = "language_servers: [python\n"
    if selection == "malformed":
        config_path.parent.mkdir()
        config_path.write_text(invalid_config, encoding="utf-8")
    args = ["--context", "agent"]
    if selection in {"explicit", "malformed"}:
        monkeypatch.chdir(tmp_path)
        args.extend(["--project", str(workspace)])
    else:
        cwd = workspace
        if selection == "git-root":
            (workspace / ".git").mkdir()
            cwd = workspace / "nested"
            cwd.mkdir()
        monkeypatch.chdir(cwd)
        args.append("--project-from-cwd")

    # keep discovery inside the scenario even when the runner places temporary files in a checkout
    monkeypatch.setattr("serena.cli.find_project_root", lambda: find_project_root(root=tmp_path))
    config = SerenaConfig().with_headless_mode_overrides()
    monkeypatch.setattr(SerenaConfig, "from_config_file", classmethod(lambda cls: config))
    monkeypatch.setattr(SerenaPaths(), "serena_user_home_dir", str(tmp_path / "serena-home"))

    def text_of(result: CallToolResult) -> str:
        return "\n".join(block.text for block in result.content if isinstance(block, TextContent))

    read_results: list[str] = []

    # replace only the blocking transport loop; exercise the real server lifespan and tool dispatch
    def exercise_server(server: MCPServer, **kwargs) -> None:
        async def exercise() -> None:
            assert server.settings.lifespan is not None
            async with server.settings.lifespan(server):
                read = await server.call_tool("read_file", {"relative_path": "note.txt"})
                assert isinstance(read, CallToolResult)
                read_results.append(text_of(read))
                shell = await server.call_tool("execute_shell_command", {"command": "echo projectless-shell"})
                assert isinstance(shell, CallToolResult)
                output = json.loads(text_of(shell))
                assert output["return_code"] == 0
                assert "projectless-shell" in output["stdout"]
                assert Path(output["cwd"]).samefile(workspace)

        anyio.run(exercise)

    monkeypatch.setattr(MCPServer, "run", exercise_server)
    handlers = set(logging.getLogger().handlers)
    try:
        result = CliRunner().invoke(command, args)
        assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
        assert read_results == ["projectless read\n"]
        if selection == "malformed":
            assert config_path.read_text(encoding="utf-8") == invalid_config
        else:
            assert not (workspace / ".serena").exists()
    finally:
        for handler in set(logging.getLogger().handlers) - handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()
