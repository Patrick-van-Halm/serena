# Unity CLI

Serena integrates the official Unity CLI for compilation, Console logs, Editor status, and tests.
The operations work independently of Serena's language-server or JetBrains backend.

| Optional MCP tool | REPL method | Purpose |
| --- | --- | --- |
| `unity_recompile` | `s.unity.recompile` | Refresh assets and check script compilation. |
| `unity_get_console_logs` | `s.unity.get_console_logs` | Read existing Console entries by severity. |
| `unity_get_status` | `s.unity.get_status` | Inspect Editor readiness and Safe Mode diagnostics. |
| `unity_run_tests` | `s.unity.run_tests` | Run Edit Mode or Play Mode tests and write an NUnit report. |

## Setup

Install a current [official Unity CLI](https://docs.unity.com/en-us/unity-cli/use-unity-cli)
on the machine running Serena. `recompile` requires at least
[1.0.0-beta.11](https://docs.unity.com/en-us/unity-cli/release-notes).
For recompilation and Console queries, open the project in Unity 6 or later with the
[Unity Pipeline package](https://docs.unity.com/en-us/unity-cli/unity-pipeline/unity-pipeline-package)
installed and its server running. Status can diagnose an Editor without Pipeline; tests use a
separate batch Editor and do not require Pipeline.

[Activate a Serena project](040_workflow), then enable the operations in its or Serena's global
[configuration](050_configuration):

```yaml
# MCP tool interface
included_optional_tools:
  - unity_recompile
  - unity_get_console_logs
  - unity_get_status
  - unity_run_tests

# REPL interface: independent of MCP tool selection
included_apis:
  - unity
```

All four operations require an active Serena project and a Unity directory containing `Assets/`
and `ProjectSettings/ProjectVersion.txt`. Read-only projects allow status and Console queries;
recompilation and test execution are editing operations and are disabled.

## Unity inside a larger repository

The active Serena root can be an outer repository that is not itself a Unity project:

| Path | Role |
| --- | --- |
| `/workspace/repo/.serena/project.yml` | Serena configuration for the outer repository. |
| `/workspace/repo/games/MyGame/Assets/` | The nested Unity project's assets. |
| `/workspace/repo/games/MyGame/ProjectSettings/ProjectVersion.txt` | The nested Unity project's version file. |

Activate `/workspace/repo` and pass `project_path="games/MyGame"` to any Unity operation.
The child folder needs no separate Serena configuration. The default `project_path="."` expects
the active root itself to be a Unity project. Serena does not search for or choose among nested
projects; select the desired folder explicitly when there are several.

## Calling the tools

For example, call MCP tool `unity_recompile` with:

```json
{"project_path": "games/MyGame", "strict": false, "timeout": 120}
```

In the REPL, results expose the original Unity JSON through `stdout`:

```python
import json

result = s.unity.get_console_logs(project_path="games/MyGame", level="warn", tail=100)
{"return_code": result.return_code, "unity": json.loads(result.stdout), "stderr": result.stderr}
```

All operations accept these common options:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `project_path` | `"."` | Unity directory relative to the active Serena root, or an absolute path; project access rules apply. |
| `unity_cli_path` | `"unity"` | CLI executable on `PATH`, or an explicit executable path. Relative executable paths use the Serena root. |
| `timeout` | `120` for compilation/tests; `30` for status/logs | Positive integer seconds. Serena's enclosing tool deadline can interrupt sooner. |
| `max_answer_chars` | `-1` | Rendered response limit; `-1` uses Serena's configured default. |

`recompile` adds `strict=False`; set it to `True` to fail on warnings.

`get_console_logs` adds `level="error"` and `tail=100`. The
[Console log filters](https://github.com/Unity-Technologies/unity-agent-plugin/blob/main/skills/unity-cli/references/diagnostics-maintenance.md#logs---console-the-running-editors-console)
are `info` for all severities, `warn` for warnings and errors, and `error` for errors.
`tail=0` retrieves all entries still in the Console buffer. This is a finite read through
`unity logs --console`; it neither follows the log nor requests compilation.

`get_status` adds `wait=False`. With `wait=True`, it uses `unity status --until-ready` and waits
within the supplied timeout. A snapshot also has a Serena process deadline, but does not pass
the CLI's wait-only `--timeout` flag. Status errors such as `STATUS_SAFE_MODE` retain their
original details, including `safeMode.compileErrors` when Unity can recover them. See
[Unity's status and Safe Mode documentation](https://github.com/Unity-Technologies/unity-agent-plugin/blob/main/skills/unity-cli/references/integration-advanced.md#status--live-state-of-connected-editors).

## Running tests

`run_tests` adds `mode="EditMode"` (`"PlayMode"` is also supported), `test_filter=""`, and
`output_path="test-results.xml"`. An empty filter selects the mode's full suite.
**The selected project must be closed in the Editor:**
[Unity CLI test execution](https://github.com/Unity-Technologies/skills/blob/main/skills/unity-cli/references/build-run-test.md#test--run-editmodeplaymode-tests)
starts a batch Editor and refuses an already-open project. Serena does not close an existing
Editor or install a missing one automatically.

```python
result = s.unity.run_tests(
    project_path="games/MyGame",
    mode="EditMode",
    test_filter="MyGame.Tests.InventoryTests",
    output_path="reports/inventory-tests.xml",
)
{"return_code": result.return_code, "unity": json.loads(result.stdout)}
```

Report paths are relative to the selected Unity folder, so this example writes
`/workspace/repo/games/MyGame/reports/inventory-tests.xml`. Absolute paths are also accepted
under normal active-project access rules. Each run **overwrites its selected report path**,
including the default `test-results.xml`; use different paths to retain earlier reports.
The format is NUnit. Increase Serena's `tool_timeout` as well as the call's `timeout` when a
suite needs more time; the shorter deadline wins.

## Reading results

MCP responses contain `stdout`, `stderr`, `return_code`, and `cwd`. `stdout` preserves Unity's
JSON envelope, including unsuccessful results. REPL methods return `ShellCommandOutput` objects;
parse their full `stdout` before rendering to retain structured data. Rendered responses may be
shortened, and a shortened excerpt may not contain parseable Unity JSON.

| Operation | Relevant CLI exit codes |
| --- | --- |
| Recompile | `0`: compiled or already current; `6`: command/compilation failure, or strict warnings; `7`: unreachable Editor, compilation unknown. |
| Run tests | `0`: passed; `8`: tests failed; `6`: no test verdict; `14`: CLI test timeout. |
| Status | Inspect the envelope's error code; `6` can indicate Safe Mode/not-ready, and `12` can indicate no reachable Editor. |
| Console logs | `0` means the query succeeded, even when the returned entries contain errors. |

For [recompilation](https://github.com/Unity-Technologies/skills/blob/main/skills/unity-cli/references/build-run-test.md#recompile--compile-check-without-a-build),
use the CLI verdict: `compilationFailed` can remain true immediately after a compile fixed the errors.
Existing Console entries can be stale and do not prove current compilation success. An unavailable
connection is never an all-clear result; use `unity_get_status` to diagnose Safe Mode or readiness.
