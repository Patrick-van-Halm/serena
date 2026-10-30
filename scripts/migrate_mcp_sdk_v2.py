"""Apply reviewed follow-up fixes to the SDK migration branch."""
from pathlib import Path


def edit(path: str, old: str, new: str, count: int = 1) -> None:
    target = Path(path)
    text = target.read_text()
    assert text.count(old) == count, (path, old, text.count(old))
    target.write_text(text.replace(old, new))


edit('src/serena/cli.py', '        server.run(transport=transport, host=host, port=port)',
     '        if transport == "stdio":\n            server.run(transport="stdio")\n        else:\n            server.run(transport=transport, host=host, port=port)')
edit('src/serena/mcp.py', 'from mcp.server.context import LifespanContextT, RequestT\n', '')
edit('src/serena/mcp.py', 'Context[LifespanContextT, RequestT] | None', 'Context | None')
for path in ('src/serena/mcp_bridge.py', 'src/serena/util/tool_call.py'):
    target = Path(path)
    text = target.read_text()
    assert 'import anyio\n' in text
    assert 'anyio.to_thread.run_sync' in text
    target.write_text(text.replace('import anyio\n', 'import anyio\nfrom anyio.to_thread import run_sync\n').replace('anyio.to_thread.run_sync', 'run_sync'))
edit('src/serena/agno.py', 'from serena.util.exception import show_fatal_exception_safe',
     'from serena.util.exception import show_fatal_exception_safe\nfrom serena.util.tool_result import ToolResult')
edit('src/serena/agno.py', 'def entrypoint(**kwargs: Any) -> str:', 'def entrypoint(**kwargs: Any) -> ToolResult:')
edit('test/serena/test_raw_file_transport.py', 'assert result[0].text == original', 'assert result.content[0].text == original')
edit('src/serena/util/tool_call.py', '        timeout: float,\n        cancellation_handler:',
     '        timeout: float,  # noqa: ASYNC109 - shared remote execution deadline, also enforced by fail_after below\n        cancellation_handler:')
edit('test/serena/test_mcp_sdk_v2.py', 'import anyio\n', 'import anyio\nfrom anyio.to_thread import run_sync\n')
edit('test/serena/test_mcp_sdk_v2.py', '                    while not started.is_set():\n                        await anyio.sleep(0.01)',
     '                    assert await run_sync(started.wait, 5.0)')
edit('test/serena/test_mcp_sdk_v2.py', '                    while not stopped.is_set():\n                        await anyio.sleep(0.01)',
     '                    assert await run_sync(stopped.wait, 5.0)')
print('SDK follow-up fixes applied.')
