# Unity

Set `language_servers: [unity]` to combine Roslyn's C# symbols, navigation, and editing with
compilation diagnostics from the official Unity CLI. Serena's ordinary diagnostic calls then
refresh the selected Unity project and return compiler errors and warnings in the usual format.
Optional CLI tools also expose compilation, Console logs, Editor status, and tests directly.

## Setup

Install a current [official Unity CLI](https://docs.unity.com/en-us/unity-cli/use-unity-cli)
on the machine running Serena. `recompile` requires at least
[1.0.0-beta.11](https://docs.unity.com/en-us/unity-cli/release-notes).
For recompilation and Console queries, open the project in Unity 6 or later with the
[Unity Pipeline package](https://docs.unity.com/en-us/unity-cli/unity-pipeline/unity-pipeline-package)
installed and its server running. Status can diagnose an Editor without Pipeline; tests use a
separate batch Editor and do not require Pipeline.

The language-server integration also has the normal [C# Roslyn prerequisites](050_configuration#c-roslyn-language-server).
Generate the Unity project's solution and C# project files in the Editor so Roslyn can resolve
its references. Serena does not start the Unity Editor or install Unity or Pipeline automatically.

## Language server configuration

[Activate a Serena project](040_workflow) and select the opt-in backend in `.serena/project.yml`:

```yaml
language_servers: [unity]
ls_specific_settings:
  unity:
    project_path: "."
    unity_cli_path: "unity"
    timeout: 120
```

Use `unity` as the C# backend for the project. Both it and `csharp` match `.cs` files, so placing
`csharp` first would route those files to ordinary Roslyn diagnostics. Unity is not selected by
automatic language detection. Other languages can still have their own entries.

| Setting under `ls_specific_settings.unity` | Default | Meaning |
| --- | --- | --- |
| `project_path` | `"."` | Unity root relative to the active Serena repository. It must contain `Assets/` and `ProjectSettings/ProjectVersion.txt`. |
| `unity_cli_path` | `"unity"` | Official CLI executable on `PATH`, or an executable path; relative paths use the Serena repository root. |
| `timeout` | `120` | Positive integer seconds for recompilation, bounded by Serena's enclosing tool deadline. |

Roslyn settings from `ls_specific_settings.csharp` carry over, with `unity` settings taking
precedence. Both backends share the Roslyn installation. As with other
[language-server settings](050_configuration#language-server-specific-settings), project settings
are applied only to trusted projects; settings can also be supplied globally.

No optional Unity tools or APIs need to be enabled for ordinary diagnostics. Call
`get_diagnostics_for_files` (or `s.lsp.get_diagnostics_for_file` in the REPL) as usual. Each explicit
diagnostic request invokes `unity recompile` for a fresh result, filters it to the requested file
and line range, and groups diagnostics by C# symbol. The optional `get_diagnostics_for_symbols` tool uses the same
compiler diagnostics. Ordinary symbol queries and edits do not invoke Unity CLI recompilation.
An unavailable Editor, malformed output, or a failed compile without usable diagnostics produces
an error instead of an empty all-clear result. Unity retrieves warnings separately from the Editor
Console; if that query fails, a successful compilation does not establish that every warning was
checked. Use `unity_recompile` to inspect the complete CLI response, including its stderr advisories.

Diagnostic refresh can write Unity-generated and imported project state, so it is disabled when
the Serena project has `read_only: true` and in read-only queries of external projects.
C# symbol queries remain available. Use the optional
`unity_get_status` or `unity_get_console_logs` operations to inspect existing Editor information
in a read-only project.

## Unity inside a larger repository

The active Serena root can be an outer repository that is not itself a Unity project:

| Path | Role |
| --- | --- |
| `/workspace/repo/.serena/project.yml` | Serena configuration for the outer repository. |
| `/workspace/repo/games/MyGame/Assets/` | The nested Unity project's assets. |
| `/workspace/repo/games/MyGame/ProjectSettings/ProjectVersion.txt` | The nested Unity project's version file. |

Activate `/workspace/repo` and set `ls_specific_settings.unity.project_path: games/MyGame`.
The child folder needs no separate Serena configuration. Paths passed to Serena's standard
symbol and diagnostic tools stay relative to the outer repository. For example, call MCP tool
`get_diagnostics_for_files` with:

```json
{"files": [{"relative_path": "games/MyGame/Assets/Scripts/Player.cs", "min_severity": 2}]}
```

The optional CLI operations below select their Unity folder through their own
`project_path="games/MyGame"` argument. The default `project_path="."` expects the active root
itself to be a Unity project. Serena does not search for or choose among nested projects; select
the desired folder explicitly when there are several.

## Optional CLI operations

These operations supplement the language-server integration and also work with other language
servers or the JetBrains backend:

| Optional MCP tool | REPL method | Purpose |
| --- | --- | --- |
| `unity_recompile` | `s.unity.recompile` | Refresh assets and check script compilation. |
| `unity_get_console_logs` | `s.unity.get_console_logs` | Read existing Console entries by severity. |
| `unity_get_status` | `s.unity.get_status` | Inspect Editor readiness and Safe Mode diagnostics. |
| `unity_run_tests` | `s.unity.run_tests` | Run Edit Mode or Play Mode tests and write an NUnit report. |

Enable them in the project or global [configuration](050_configuration):

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

All four operations require an active Serena project and a valid Unity directory. Read-only
projects allow status and Console queries; the explicit recompilation and test tools are editing
operations and are disabled.

### Calling the optional tools

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

## Reading CLI results

Optional MCP tool responses contain `stdout`, `stderr`, `return_code`, and `cwd`. `stdout` preserves Unity's
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
