# Shared project daemon endpoints

The `optimizations` branch adds two **new** port options for the project server:

- `project_server_port` in the host-global `serena_config.yml`.
- `SERENA_PROJECT_SERVER_PORT` as a transient environment override.

The default remains **24225**. These options configure the project daemon, not the
MCP HTTP transport, dashboard, or JetBrains plugin ports. Project-local
`project.yml` settings do not override the daemon endpoint.

## Configure one endpoint for the listener and its clients

Edit `~/.serena/serena_config.yml`, or `$SERENA_HOME/serena_config.yml` when using a
custom Serena home:

```yaml
project_server_port: 25432
```

Both `SharedMCPDaemonClient` and `ProjectServerClient`, the standalone
`start-project-server` listener, and bridge-started daemons resolve this setting.
Alternatively, set the same override in the environment of every relevant client
and daemon process:

```bash
export SERENA_PROJECT_SERVER_PORT=25432
serena start-project-server
```

Resolution order, from highest to lowest priority:

1. An explicit `start-project-server --port` or HTTP client constructor `port`.
2. `SERENA_PROJECT_SERVER_PORT`.
3. `project_server_port` from the global configuration, defaulting to 24225.

Ports must be integers in `1..65535`. Empty, malformed, Boolean, fractional,
zero, and out-of-range values are rejected rather than silently ignored.
Environment overrides are not written back to the YAML file. Older configuration
files without the new setting retain the default port.

Changing **only** `start-project-server --port` still does not reconfigure another
process's clients. Use the global setting or matching environment overrides for
the whole installation. Existing client objects keep the endpoint they resolved
at construction. Restart the clients/bridges after changing the setting.

A bridge passes its resolved host and port explicitly to the child daemon and
uses its own Python interpreter, rather than selecting another Serena installation
from `PATH`. It propagates its configuration home to that child. Startup locks are
scoped to the canonical host and port within the Serena home; different ports do
not share a startup lock. `localhost` is canonicalized to `127.0.0.1`.

## Startup and recovery

An authenticated compatible daemon is reused. Automatic local startup is attempted
only when the connection is explicitly refused. Timeouts, proxy failures,
redirects, invalid heartbeat data, and other HTTP errors are not evidence that a
port is available.

An HTTP **401 or 403** reports an **endpoint/configuration mismatch**. Check the
resolved URL, `SERENA_HOME`, the global port setting, environment overrides, and
`auth_secret` in the client and daemon configurations. Do not disable
authentication to work around a mismatch. A rejection does not authorize another
daemon launch or termination of the existing listener.

An authenticated but incompatible daemon may be replaced only after checking
that its process belongs to the current user, has a recognized Serena project
server entrypoint, and actually listens on the selected endpoint. A reported PID
alone is insufficient. Legacy PID discovery is restricted to that endpoint and
fails closed when ownership cannot be established. Other ports and unrelated
processes are left untouched.

A failed bridge-started daemon has its stdout and stderr captured in a private
file under `$SERENA_HOME/logs/project-server-startup-<port>-*.log`. The failure
includes the endpoint, exit status when available, log path, and a bounded tail of
the actual startup output. The configured authentication secret is redacted from
the surfaced tail. Only the child started by that attempt is cleaned up on failure
or interruption; startup cleanup never discovers a different process to kill.

After installing this update, restart older bridges and the shared daemon together
when no tool calls are active. The shared protocol remains version 4, but the build
fingerprint includes the new endpoint and startup implementations. Moving to a new
port does not automatically stop a daemon on the old port.
