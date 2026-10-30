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
when the connection is explicitly refused. On WSL only, a heartbeat timeout may
also permit a startup attempt when an exclusive bind to the selected loopback
address and port succeeds after rechecking health under the endpoint startup lock;
it does not use address or port reuse and never authorizes terminating a listener.
If the port is occupied/reserved or cannot be inspected by binding, startup fails
without stopping anything. A bind race with a separately launched service is still
handled by the child's actual bind and authenticated readiness checks.

This check establishes availability only in the current WSL network namespace,
not the state of Windows-side services. It does not change firewall rules or
networking modes. Non-WSL and non-loopback timeouts, proxy failures, redirects,
invalid heartbeat data, and other HTTP errors remain terminal. A started child's
readiness checks may retry timeouts only within the original startup deadline;
this does not authorize replacing another process.

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
when no tool calls are active. The shared protocol is version 7; the build
fingerprint includes the endpoint, startup, recovery, and installed SDK identity.
Moving to a new port does not automatically stop a daemon on the old port.

## Restart from the terminal

`serena restart` restarts the shared project daemon selected by the global port
setting or environment override. It starts a daemon if none is listening.
The command prints the selected endpoint and asks for confirmation before stopping
anything, because it interrupts active tool calls and discards in-memory sessions.
It does not update the Serena package, pull Git changes, or restart client-owned
MCP bridge processes.

```bash
serena restart
serena restart --yes
serena restart --port 25432 --startup-timeout 45 --yes
```

`--host` defaults to `127.0.0.1`; only loopback endpoints may be restarted.
`--port` overrides the environment/global port without persisting the override.
`--startup-timeout` is a finite positive time allowance (default 30 seconds) for
lock acquisition and readiness; process cleanup has separate bounded waits.
`--yes` (or `-y`) skips confirmation, not authentication or ownership checks.
The endpoint-scoped startup lock covers shutdown and startup, including competing
bridge startups. Success is printed only after authenticated readiness succeeds.
Failures exit nonzero and include the existing startup diagnostics where available.

An authentication mismatch, ambiguous listener identity, or unresponsive heartbeat
is not permission to kill a process. These failures are reported for operator
inspection rather than bypassed. A WSL timeout with an exclusively bindable local
socket can start an absent daemon, but never authorize termination. Other ports
are not restarted.

## Last-resort agent recovery through the bridge

The shared MCP bridge exposes an additional, parameter-free `restart_daemon` tool.
It uses a separate bounded control worker, outside the ordinary MCP call pool and
the daemon's tool executor. It restarts the daemon while leaving the requesting
bridge's stdio connection alive. This tool is for confirmed blocked retrievals,
not routine cleanup, ordinary load, one failed call, or normal long-running work.
Do not use the terminal restart command or process-killing commands as an agent
fallback when the guarded tool refuses recovery.

Recovery is authorized only when all of the following hold:

- An authenticated compatible daemon identifies its actual caller-owned retrieval
  worker as still running at least **30 seconds past its original deadline**.
  The worker must be a recognized retrieval operation as well as read-only.
- Two daemon-side observations at least **two seconds apart** identify that same
  request. If the worker finishes, changes, or belongs to another bridge, recovery
  is refused. A client timeout or network error is never sufficient evidence.
- There are no queued tasks, pending requests, active legacy queries, runtime loads,
  or executing work in another project. Edits, shell commands, REPL executions,
  and unknown tool types are never eligible for agent recovery.
- The current user's process identity, Serena entrypoint, and listening socket
  match the selected endpoint before the coordinator terminates anything.

The final observation atomically claims a short recovery lease and closes normal
HTTP request admission, so a new edit cannot begin between authorization and
shutdown. Heartbeat, cancellation, and recovery control remain reachable. The
lease is released after a failed shutdown where possible and expires after
60 seconds if the coordinator dies. No force argument or adjustable evidence
threshold is exposed to the agent.

These checks establish a persistently overdue worker holding the serial executor;
they do not prove that arbitrary code could never finish later. Missing evidence,
a completely unresponsive control endpoint, or an unfinished modifying operation
therefore requires operator intervention instead of an automatic restart.

Successful recovery resets all daemon in-memory sessions, including idle ones;
persisted project files and memories are not deleted. Project services load again
on the next call. Earlier operations are not replayed. Updated bridges can
reconnect, but an older installed bridge cannot be upgraded by restarting the daemon;
restart the client integrations after updating Serena to expose the new tool.
