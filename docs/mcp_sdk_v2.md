# MCP Python SDK 2 migration

Serena uses **MCP Python SDK 2.3.0**, pinned in `pyproject.toml` with a regenerated
`uv.lock`. This is a source migration, not a supported dependency-only upgrade
of older Serena checkouts.

## Updating an installation

Update Serena in every client installation and restart all MCP bridges and the
shared project daemon together while no calls are active. The shared daemon
protocol is **7**. Its build fingerprint includes the installed SDK version so a
bridge cannot silently reuse a daemon built against a different SDK.

The command-line entrypoints remain `start-mcp-server`, `start-mcp-bridge`,
`start-project-server`, and `restart`. Existing daemon port settings and
`SERENA_PROJECT_SERVER_PORT` keep their meanings. Restarting the daemon does not
upgrade an already-running bridge or refresh its advertised tools.

The SDK continues to accept legacy MCP clients while supporting modern protocol
negotiation. Serena's own shared HTTP daemon protocol is separate from the MCP
protocol negotiated with an IDE. This change does not require a client to use
modern MCP, and it is not proof of a fix for a host reporting that its context
server is no longer registered.

## Integration changes

- Use `MCPServer` and `mcp.server.mcpserver` in place of `FastMCP` and the removed
  `mcp.server.fastmcp` imports. Configure the advertised Serena version through
  the constructor rather than private low-level server attributes.
- Pass HTTP listen addresses to the transport runner, never the server constructor
  or stdio runner. Project `.env` files do not configure the MCP server.
- Use `mcp_types` and snake-case Python model fields. JSON serialization still uses
  protocol aliases; `read_file_raw` returns native image/resource blocks, not text
  containing their base64 representation.
- Preserve Serena's existing bounded worker offloading, deadlines, cancellation,
  endpoint authentication, ownership verification, and no-replay behavior.
- Derive legacy conversation identity from the underlying SDK connection rather
  than its new request-scoped session proxy. The pinned SDK compatibility seam is
  isolated in `MCPConnectionIdentity`; weak references avoid retaining closed
  connections. Shared bridges still supply their explicit conversation IDs.

## Server lifetime

Stdio and Streamable HTTP use a server-scoped lifespan for owned agent cleanup.
Legacy SSE invokes that lifespan for each connection: disconnecting an SSE client
must not shut down the shared agent. The CLI releases that agent when its SSE
server runner exits. Applications embedding the SSE app directly own its final
agent cleanup, as opposed to individual client disconnects.

## Regression coverage

`test/serena/test_mcp_sdk_v2.py` uses the installed SDK, actual Serena CLI
subprocesses, and real stdio/HTTP connections. It covers legacy and modern
negotiation; missing-file failures followed by concurrent successful calls; raw
image byte fidelity; shell/file tools; connection-scoped identity; cancellation;
explicit HTTP ports; ignored project `.env` settings; and reconnection through
both Streamable HTTP and legacy SSE.

The MCP SDK validation workflow runs the migration and existing daemon, recovery,
endpoint, and multimodal regressions on Linux with Python 3.11 and 3.13. It also
runs the repository's full source/test type checks. The workflow is read-only and
does not modify branches or dependencies.

These tests are not native Zed UI or WSL networking tests, nor a run of every
language-server integration suite.
