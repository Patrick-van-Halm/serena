"""Apply the final reviewed transport and test adjustments on the migration branch."""
from pathlib import Path


def edit(path: str, old: str, new: str, count: int = 1) -> None:
    target = Path(path)
    text = target.read_text()
    assert text.count(old) == count, (path, old, text.count(old))
    target.write_text(text.replace(old, new))


edit('test/serena/test_mcp.py', '        mcp_ctx: Context | None = None,\n        **kwargs,',
     '        mcp_ctx: Context | None = None,\n        session_id_override: str | None = None,\n        **kwargs,')
edit('test/serena/test_project_server_port_integration.py', 'def log_message(self, *_args):',
     'def log_message(self, format: str, *args) -> None:')
edit('test/serena/test_mcp_sdk_v2.py', '        def apply_ex(self, **kwargs) -> str:',
     '        def apply_ex(self, log_call: bool = True, catch_exceptions: bool = True,\n'
     '                     mcp_ctx: Context | None = None, session_id_override: str | None = None, **kwargs) -> str:')
edit('test/serena/test_mcp_sdk_v2.py', 'from serena.constants import SERENA_CONFIG_TEMPLATE_FILE',
     'from serena.constants import SERENA_CONFIG_TEMPLATE_FILE, SERENAS_OWN_CONTEXT_YAMLS_DIR')
edit('test/serena/test_mcp_sdk_v2.py', '    home.mkdir()\n',
     '    home.mkdir()\n'
     '    # enable filesystem operations only in this test home, preserving production context policy\n'
     '    context = yaml.safe_load(Path(SERENAS_OWN_CONTEXT_YAMLS_DIR, "codex.yml").read_text(encoding="utf-8"))\n'
     '    context["excluded_tools"] = []\n'
     '    (home / "contexts").mkdir()\n'
     '    (home / "contexts" / "codex.yml").write_text(yaml.safe_dump(context), encoding="utf-8")\n')
for indent in (20, 16):
    edit('test/serena/test_mcp_sdk_v2.py', '\n' + ' ' * indent + 'await client.session.send_ping()',
         '\n' + ' ' * indent + 'if mode == "legacy":\n' + ' ' * (indent + 4) + 'await client.session.send_ping()')
edit('src/serena/mcp.py',
     '            # Shut down the server if we are running in stdio mode.\n'
     '            # For other transports, we do nothing; the singleton agent instance remains active.\n'
     '            if self.transport == "stdio":\n'
     '                log.info("MCP server shutting down")\n'
     '                if self.agent is not None:\n'
     '                    self.agent.on_shutdown()\n'
     '            else:\n'
     '                log.info("Client disconnected")',
     '            # release agent resources when the server lifetime ends, not on individual HTTP disconnects\n'
     '            log.info("MCP server shutting down")\n'
     '            self.agent.on_shutdown()')

path = Path('test/serena/test_mcp_sdk_v2.py')
path.write_text(path.read_text() + r'''

@pytest.mark.parametrize("transport,mode", [("streamable-http", "legacy"), ("streamable-http", "auto"), ("sse", "legacy")])
def test_http_cli_binds_requested_endpoint_and_survives_client_disconnect(isolated_project, transport: str, mode: str) -> None:
    """Serve real HTTP clients on the explicit port without reading project .env settings."""
    from mcp.client.sse import sse_client

    fixture = isolated_project
    (fixture.project / ".env").write_text("FASTMCP_HOST=invalid.example\nFASTMCP_PORT=1\nLOG_LEVEL=INVALID\n", encoding="utf-8")
    with (fixture.home / "http-test.log").open("w+", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", BOOTSTRAP, "start-mcp-server", "--project", str(fixture.project),
             "--context", "codex", "--transport", transport, "--host", "127.0.0.1", "--port", str(fixture.port)],
            env=fixture.environment, cwd=fixture.project, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", fixture.port), timeout=0.2):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        output.seek(0)
                        pytest.fail(f"HTTP server did not start: {output.read()}")
                    time.sleep(0.05)

            async def exercise() -> None:
                with anyio.fail_after(30):
                    # connect twice: closing one HTTP client must not shut down the shared server agent
                    for _ in range(2):
                        origin = f"http://127.0.0.1:{fixture.port}"
                        target = sse_client(origin + "/sse") if transport == "sse" else origin + "/mcp"
                        async with Client(target, mode=mode, read_timeout_seconds=10) as client:
                            assert client.server_info.version == __version__
                            assert "read_file" in {tool.name for tool in (await client.list_tools()).tools}
                            missing = await client.call_tool("read_file", {"relative_path": "missing-file.txt"})
                            assert missing.is_error
                            good = await client.call_tool("read_file", {"relative_path": "hello.txt"})
                            assert not good.is_error and "SDK migration works" in text_of(good)

            anyio.run(exercise)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("transport", ["stdio", "sse", "streamable-http"])
def test_agent_resources_released_at_server_lifetime_end(transport: str) -> None:
    """All SDK server transports release their owned agent only when their lifespan exits."""
    from unittest.mock import Mock

    from serena.mcp import SerenaMCPFactory

    agent = Mock()
    agent.get_context.return_value = SerenaAgentContext.from_name("codex")
    agent.get_exposed_tool_instances.return_value = []
    factory = SerenaMCPFactory(transport=transport, context="codex")
    factory.agent = agent
    server = MCPServer("lifetime-test")

    async def exercise() -> None:
        async with factory.server_lifespan(server):
            agent.on_shutdown.assert_not_called()
        agent.on_shutdown.assert_called_once_with()

    anyio.run(exercise)
''')
print('Transport fixtures and SDK server lifetime updated.')
