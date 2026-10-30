"""Installed-SDK transport tests, including real Serena stdio subprocesses."""

import base64
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
import requests
import yaml
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ImageContent, TextContent

from serena import __version__
from serena.config.context_mode import SerenaAgentContext
from serena.config.serena_config import SerenaConfig
from serena.constants import SERENA_CONFIG_TEMPLATE_FILE
from serena.mcp import SerenaFastMCPTool
from serena.tools import Tool, ToolCallError
from serena.util.mcp_context import MCPConnectionIdentity
from serena.util.tool_call import ToolCallContext, ToolCallInterrupted

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")
BOOTSTRAP = "from serena.cli import top_level; top_level()"


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


@pytest.fixture
def isolated_project(tmp_path: Path):
    """Project and global configuration private to each subprocess integration test."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".serena").mkdir()
    (project / ".serena" / "project.yml").write_text("project_name: sdk-test\nlanguage_servers: []\n", encoding="utf-8")
    (project / "hello.txt").write_text("SDK migration works\n", encoding="utf-8")
    (project / "image.png").write_bytes(PNG)
    home = tmp_path / "home"
    home.mkdir()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = yaml.safe_load(Path(SERENA_CONFIG_TEMPLATE_FILE).read_text(encoding="utf-8"))
    config.update(
        projects=[str(project)], auth_secret="sdk-integration-test-secret", project_server_port=port,
        web_dashboard=False, gui_log_window=False, tool_timeout=15,
        trusted_project_path_patterns=[str(project)],
    )
    (home / "serena_config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    environment = dict(os.environ, SERENA_HOME=str(home), SERENA_PROJECT_SERVER_PORT=str(port), PYTHONUNBUFFERED="1")
    return SimpleNamespace(project=project, home=home, port=port, environment=environment)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("bridge", [False, True], ids=["server", "bridge"])
def test_stdio_survives_errors_parallel_calls_and_native_images(isolated_project, bridge: bool, mode: str) -> None:
    """Run the real CLI and SDK; a missing file must not remove the context server."""
    fixture = isolated_project
    daemon = None
    daemon_log = (fixture.home / "daemon-test.log").open("w+", encoding="utf-8")
    try:
        if bridge:
            daemon = subprocess.Popen(
                [sys.executable, "-c", BOOTSTRAP, "start-project-server"],
                env=fixture.environment, stdin=subprocess.DEVNULL, stdout=daemon_log, stderr=daemon_log,
            )
            deadline = time.monotonic() + 30
            while True:
                try:
                    response = requests.get(
                        f"http://127.0.0.1:{fixture.port}/heartbeat",
                        headers={"Authorization": "Bearer sdk-integration-test-secret"}, timeout=0.3,
                    )
                    if response.ok:
                        break
                except requests.RequestException:
                    pass
                if daemon.poll() is not None or time.monotonic() >= deadline:
                    daemon_log.seek(0)
                    pytest.fail(f"Daemon did not start: {daemon_log.read()}")
                time.sleep(0.05)

        command = "start-mcp-bridge" if bridge else "start-mcp-server"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", BOOTSTRAP, command, "--project", str(fixture.project), "--context", "codex"],
            env=fixture.environment,
        )

        async def exercise() -> None:
            with anyio.fail_after(45):
                async with Client(parameters, mode=mode, read_timeout_seconds=20) as client:
                    assert client.server_info.name == "Serena"
                    assert client.server_info.version == __version__
                    names = {tool.name for tool in (await client.list_tools()).tools}
                    assert {"read_file", "read_file_raw", "list_dir", "execute_shell_command"} <= names
                    failed = await client.call_tool("read_file", {"relative_path": "missing-file.txt"})
                    assert failed.is_error and "FileNotFoundError" in text_of(failed)
                    results = []

                    async def read_one(index: int) -> None:
                        path = "missing-file.txt" if index % 3 == 0 else "hello.txt"
                        result = await client.call_tool("read_file", {"relative_path": path})
                        results.append(result)

                    async with anyio.create_task_group() as group:
                        for index in range(12):
                            group.start_soon(read_one, index)
                    assert sum(bool(result.is_error) for result in results) == 4
                    assert all("SDK migration works" in text_of(result) for result in results if not result.is_error)
                    image_result = await client.call_tool("read_file_raw", {"relative_path": "image.png"})
                    assert not image_result.is_error
                    image = next(block for block in image_result.content if isinstance(block, ImageContent))
                    assert image.mime_type == "image/png" and base64.b64decode(image.data) == PNG
                    listing = await client.call_tool("list_dir", {"relative_path": ".", "recursive": False})
                    assert not listing.is_error and "hello.txt" in text_of(listing)
                    shell = await client.call_tool("execute_shell_command", {"command": "echo sdk-shell-ok"})
                    assert not shell.is_error and "sdk-shell-ok" in text_of(shell)
                    await client.session.send_ping()
                    assert "read_file_raw" in {tool.name for tool in (await client.list_tools()).tools}

        anyio.run(exercise)
    finally:
        if daemon is not None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=5)
        daemon_log.close()


def test_connection_identity_is_stable_and_isolated() -> None:
    """The SDK creates request-scoped proxies; legacy conversations remain connection-scoped."""
    server = MCPServer("identity-test")

    @server.tool()
    def identity(ctx: Context) -> str:
        return MCPConnectionIdentity.session_id(ctx)

    async def exercise() -> None:
        async with Client(server, mode="legacy") as first, Client(server, mode="legacy") as second:
            first_id = text_of(await first.call_tool("identity"))
            assert first_id == text_of(await first.call_tool("identity"))
            assert first_id != text_of(await second.call_tool("identity"))

    anyio.run(exercise)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_serena_wrapper_cancellation_keeps_sdk_connection_usable(mode: str) -> None:
    """Cancellation reaches Serena's worker deadline without closing the MCP connection."""
    started = threading.Event()
    stopped = threading.Event()
    agent = SimpleNamespace(
        serena_config=SerenaConfig(tool_timeout=10),
        get_context=lambda: SerenaAgentContext.from_name("codex"),
    )

    class WaitingTool(Tool):
        def apply(self) -> str:
            """Wait until this operation is cancelled."""
            return "unused"

        def apply_ex(self, **kwargs) -> str:
            call = ToolCallContext.current().call
            started.set()
            try:
                while True:
                    call.check()
                    time.sleep(0.01)
            except ToolCallInterrupted as error:
                raise ToolCallError(str(error)) from error
            finally:
                stopped.set()

    server = MCPServer("cancellation-test", tools=[SerenaFastMCPTool(WaitingTool(agent), False, False)])

    async def exercise() -> None:
        with anyio.fail_after(10):
            async with Client(server, mode=mode) as client:
                scope = anyio.CancelScope()

                async def wait() -> None:
                    with scope:
                        await client.call_tool("waiting")

                async with anyio.create_task_group() as group:
                    group.start_soon(wait)
                    while not started.is_set():
                        await anyio.sleep(0.01)
                    scope.cancel()
                    while not stopped.is_set():
                        await anyio.sleep(0.01)
                await client.session.send_ping()
                assert "waiting" in {tool.name for tool in (await client.list_tools()).tools}

    anyio.run(exercise)
