"""Apply the reviewed SSE ownership correction to the isolated migration branch."""
from pathlib import Path


def edit(path: str, old: str, new: str, count: int = 1) -> None:
    target = Path(path)
    text = target.read_text()
    assert text.count(old) == count, (path, old, text.count(old))
    target.write_text(text.replace(old, new))


edit('src/serena/mcp.py',
     '            # release agent resources when the server lifetime ends, not on individual HTTP disconnects\n'
     '            log.info("MCP server shutting down")\n'
     '            self.agent.on_shutdown()',
     '            # legacy SSE enters this lifespan per connection; its CLI owner handles final shutdown\n'
     '            if self.transport != "sse":\n'
     '                log.info("MCP server shutting down")\n'
     '                self.agent.on_shutdown()')
edit('src/serena/mcp.py',
     '        For HTTP transports, the SDK enters the lifespan once at server startup.',
     '        Streamable HTTP enters once at server startup; legacy SSE enters once per connection.')
edit('src/serena/cli.py',
     '        if transport == "stdio":\n'
     '            server.run(transport="stdio")\n'
     '        else:\n'
     '            server.run(transport=transport, host=host, port=port)',
     '        try:\n'
     '            if transport == "stdio":\n'
     '                server.run(transport="stdio")\n'
     '            else:\n'
     '                server.run(transport=transport, host=host, port=port)\n'
     '        finally:\n'
     '            # the SDK enters legacy SSE lifespans per connection, so the process owner cleans up\n'
     '            if transport == "sse" and factory.agent is not None:\n'
     '                factory.agent.on_shutdown()')
edit('test/serena/test_mcp_sdk_v2.py',
     '        agent.on_shutdown.assert_called_once_with()',
     '        if transport == "sse":\n'
     '            agent.on_shutdown.assert_not_called()\n'
     '        else:\n'
     '            agent.on_shutdown.assert_called_once_with()')
edit('test/serena/test_mcp_sdk_v2.py',
     '    """All SDK server transports release their owned agent only when their lifespan exits."""',
     '    """SSE connection teardown preserves the agent; server-scoped lifespans release it."""')
print('Legacy SSE connection and process lifetimes are separated.')
