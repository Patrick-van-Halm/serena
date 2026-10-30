"""One-time, assertion-checked migration on upgrade/mcp-sdk-2.3.0."""
from pathlib import Path
import re


def edit(path: str, old: str, new: str, count: int = 1) -> None:
    target = Path(path)
    text = target.read_text()
    if text.count(old) != count:
        raise RuntimeError(f'{path}: expected {count} occurrences of {old!r}, got {text.count(old)}')
    target.write_text(text.replace(old, new))


edit('pyproject.toml', '"mcp==1.28.1"', '"mcp==2.3.0"')

# change Python imports and attribute/keyword names, never JSON wire keys
for root in ('src/serena', 'test/serena'):
    for path in Path(root).rglob('*.py'):
        text = original = path.read_text()
        text = text.replace('mcp.server.fastmcp', 'mcp.server.mcpserver')
        text = text.replace('from mcp.types import ', 'from mcp_types import ')
        for old, new in {
            'readOnlyHint': 'read_only_hint', 'destructiveHint': 'destructive_hint',
            'idempotentHint': 'idempotent_hint', 'openWorldHint': 'open_world_hint',
            'mimeType': 'mime_type', 'isError': 'is_error', 'structuredContent': 'structured_content',
        }.items():
            text = re.sub(r'(?<=\.)' + old + r'\b', new, text)
            text = re.sub(r'\b' + old + r'(?=\s*=)', new, text)
        if text != original:
            path.write_text(text)

for name in ('mcp.py', 'mcp_bridge.py'):
    path = 'src/serena/' + name
    edit(path, 'from mcp.server.mcpserver.server import Context, FastMCP, Settings',
         'from mcp.server.mcpserver import Context, MCPServer')
    edit(path, 'from pydantic_settings import SettingsConfigDict\n', '')
    target = Path(path)
    text = re.sub(r'\bFastMCP\b', 'MCPServer', target.read_text())
    text = text.replace('return await super().run(arguments, context, convert_result)',
                        'return await super().run(arguments, context or Context(), convert_result)')
    target.write_text(text)

edit('src/serena/mcp.py', 'from mcp.server.mcpserver import server\n', '')
edit('src/serena/mcp.py', 'from mcp.server.session import ServerSessionT\nfrom mcp.shared.context import LifespanContextT, RequestT',
     'from mcp.server.context import LifespanContextT, RequestT')
edit('src/serena/mcp.py', 'Context[ServerSessionT, LifespanContextT, RequestT]', 'Context[LifespanContextT, RequestT]')
path = Path('src/serena/mcp.py')
text = path.read_text()
start = text.index('\ndef configure_logging(')
end = text.index('\n\n@dataclass', start)
text = text[:start] + text[end:]
start = text.index('        # Override model_config')
end = text.index('        instructions = self._get_initial_instructions()', start)
text = text[:start] + text[end:]
start = text.index('        # MCPServer currently falls back')
end = text.index('        return mcp', start)
text = text[:start] + text[end:]
text = text.replace('            host=host,\n            port=port,\n', '            version=__version__,\n')
text = text.replace('        host: str = "127.0.0.1",\n        port: int = 8000,\n', '')
text = text.replace('        :param host: The host to bind to\n        :param port: The port to bind to\n', '')
text = text.replace('Create an MCP server with process-isolated SerenaAgent to prevent asyncio contamination.',
                    'Create an MCP server with Serena tool dispatch offloaded from its event loop.')
text = text.replace('        For other transports, this is called once per connection!',
                    '        For HTTP transports, the SDK enters the lifespan once at server startup.')
path.write_text(text)

edit('src/serena/cli.py', '            host=host,\n            port=port,\n            mode_selection_def=', '            mode_selection_def=')
edit('src/serena/cli.py', '        server.run(transport=transport)', '        server.run(transport=transport, host=host, port=port)')

edit('src/serena/mcp_bridge.py', '        Settings.model_config = SettingsConfigDict(env_prefix="FASTMCP_")\n', '')
edit('src/serena/mcp_bridge.py', '            instructions=runtime_info.instructions + binding_instructions,',
     '            instructions=runtime_info.instructions + binding_instructions,\n            version=__version__,')
edit('src/serena/mcp_bridge.py', '        self.server._mcp_server.version = __version__\n', '')

edit('src/serena/tools/tools_base.py', 'from mcp import Implementation\n', '')
edit('src/serena/tools/tools_base.py', 'from serena.util.inspection import iter_subclasses',
     'from serena.util.inspection import iter_subclasses\nfrom serena.util.mcp_context import MCPConnectionIdentity')
edit('src/serena/tools/tools_base.py', 'session_id = "%x" % id(mcp_ctx.session)',
     'session_id = MCPConnectionIdentity.session_id(mcp_ctx)')
edit('src/serena/tools/tools_base.py', 'client_info = cast(Implementation, client_params.clientInfo)',
     'client_info = client_params.client_info')
edit('src/serena/tools/tools_base.py', 'Any, Protocol, TypeVar, cast', 'Any, Protocol, TypeVar')

edit('src/serena/shared_mcp_protocol.py', 'SHARED_MCP_PROTOCOL_VERSION = 6', 'SHARED_MCP_PROTOCOL_VERSION = 7')
edit('src/serena/shared_mcp_protocol.py', '    "serena/util/tool_result.py",',
     '    "serena/util/tool_result.py",\n    "serena/util/mcp_context.py",')
edit('src/serena/shared_mcp_protocol.py', 'from functools import lru_cache',
     'from functools import lru_cache\nfrom importlib.metadata import version')
edit('src/serena/shared_mcp_protocol.py', '    digest = hashlib.sha256()',
     '    digest = hashlib.sha256()\n    digest.update(f"mcp={version(\'mcp\')}\\0".encode("utf-8"))')

# update tests to exercise awaited SDK calls and the public server identity
edit('test/serena/test_mcp.py', 'import pytest', 'import anyio\nimport pytest')
edit('test/serena/test_mcp.py', '        self.serena_config = None', '        self.serena_config = SerenaConfig()')
edit('test/serena/test_mcp.py', '    initialization_options = mcp_server._mcp_server.create_initialization_options()\n\n    assert initialization_options.server_name == "Serena"\n    assert initialization_options.server_version == __version__',
     '    assert mcp_server.name == "Serena"\n    assert mcp_server.version == __version__')
edit('test/serena/test_mcp.py', '    result = mcp_tool.fn(name="Alice", age=30)',
     '    async def invoke() -> str:\n        return await mcp_tool.run({"name": "Alice", "age": 30}, Context())\n\n    result = anyio.run(invoke)')

print('MCP SDK v2 source migration applied.')
