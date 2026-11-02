# SPDX-License-Identifier: GPL-3.0-or-later

"""Project-scoped Unity CLI diagnostics without a running Editor in the test suite."""

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.agent import SerenaAgent, ToolSet
from serena.config.serena_config import ApiInclusionDefinition, ProjectConfig, SerenaConfig
from serena.project import Project
from serena.repl.api import unity_api
from serena.repl.api.unity_api import UnityApi
from serena.repl.facade import ApiScope
from serena.util.shell import ShellCommandResult


def _create_unity_project(path: Path) -> None:
    (path / "Assets").mkdir(parents=True)
    (path / "ProjectSettings").mkdir()
    (path / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.47f1\n", encoding="utf-8")


@pytest.fixture
def api(tmp_path: Path) -> UnityApi:
    root = tmp_path / "workspace"
    root.mkdir()
    _create_unity_project(root / "Game & tools")
    config = SerenaConfig(gui_log_window=False, web_dashboard=False)
    project = Project(
        project_root=str(root),
        project_config=ProjectConfig(project_name="unity-test", language_servers=[]),
        serena_config=config,
    )
    agent = MagicMock()
    agent.serena_config = config
    agent.get_active_project_or_raise.return_value = project
    return UnityApi(agent)


@pytest.fixture
def cli(api: UnityApi, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    # supply fixed process output at the external CLI boundary
    result = ShellCommandResult(
        stdout='{"success":true,"command":"recompile","data":{"failed":false},"errors":[],"warnings":[]}',
        stderr="",
        return_code=0,
        cwd=str(Path(api._get_project().project_root) / "Game & tools"),
    )
    command = MagicMock(return_value=result)
    monkeypatch.setattr(unity_api, "execute_shell_command", command)
    monkeypatch.setattr(shutil, "which", lambda executable: sys.executable)
    return command


@pytest.mark.parametrize("operation", ["recompile", "get_console_logs", "get_status", "run_tests"])
def test_unity_subfolder_works_inside_a_non_unity_serena_project(api: UnityApi, cli: MagicMock, operation: str) -> None:
    root = Path(api._get_project().project_root)
    nested = root / "Game & tools"
    assert not (root / "Assets").exists()
    assert not (nested / ".serena").exists()
    command_name = {"recompile": "recompile", "get_console_logs": "logs", "get_status": "status", "run_tests": "test"}[operation]
    response = {"success": True, "command": command_name, "data": {}, "errors": [], "warnings": []}
    cli.return_value = ShellCommandResult(stdout=json.dumps(response), stderr="", return_code=0, cwd=str(nested))
    options = {}
    if operation == "recompile":
        options = {"strict": True}
    elif operation == "get_console_logs":
        options = {"level": "warn", "tail": 0}

    output = getattr(api, operation)(project_path=nested.name, timeout=45, unity_cli_path=sys.executable, **options)

    command = cli.call_args.args[0]
    assert isinstance(command, list)
    assert command[0] == sys.executable
    assert command_name in command
    if operation == "run_tests":
        assert command[command.index("test") + 1] == str(nested)
        assert "--project-path" not in command
        assert command[command.index("--mode") + 1] == "EditMode"
        assert command[command.index("--output") + 1] == str(nested / "test-results.xml")
    else:
        assert command[command.index("--project-path") + 1] == str(nested)
    if operation in ("recompile", "run_tests"):
        assert command[command.index("--timeout") + 1] == "45"
    else:
        assert "--timeout" not in command
    if operation == "recompile":
        assert "--strict" in command
    elif operation == "get_console_logs":
        assert "--console" in command
        assert command[command.index("--level") + 1] == "warn"
        assert command[command.index("--tail") + 1] == "0"
        assert "--follow" not in command
    assert command[command.index("--format") + 1] == "json"
    assert "--non-interactive" in command
    assert cli.call_args.kwargs["cwd"] == str(nested)
    assert output.return_code == 0
    assert json.loads(output.stdout) == response
    assert not (nested / ".serena").exists()


@pytest.mark.parametrize(
    ("return_code", "response"),
    [
        (
            6,
            {
                "success": False,
                "command": "recompile",
                "data": {
                    "failed": True,
                    "errors": [{"file": "Assets/Player.cs", "line": 8, "column": 12, "code": "CS1002", "message": "; expected"}],
                },
                "errors": [{"code": "RECOMPILE_FAILED", "message": "Compilation reported errors"}],
                "warnings": [],
            },
        ),
        (
            7,
            {
                "success": False,
                "command": "recompile",
                "data": None,
                "errors": [{"code": "EDITOR_UNREACHABLE", "message": "No Editor responded"}],
                "warnings": [],
            },
        ),
        (
            0,
            {
                "success": True,
                "command": "recompile",
                "data": {"failed": False, "compilationFailed": True},
                "errors": [],
                "warnings": [],
            },
        ),
    ],
    ids=["compiler-diagnostics", "unavailable-editor", "fixed-errors-with-stale-flag"],
)
def test_recompile_keeps_the_cli_verdict_and_diagnostics(api: UnityApi, cli: MagicMock, return_code: int, response: dict) -> None:
    cli.return_value = ShellCommandResult(
        stdout=json.dumps(response),
        stderr="CLI diagnostic detail",
        return_code=return_code,
        cwd=str(Path(api._get_project().project_root) / "Game & tools"),
    )

    output = api.recompile(project_path="Game & tools")
    rendered = json.loads(output.represent())

    assert output.return_code == return_code
    assert json.loads(rendered["stdout"]) == response
    assert rendered["return_code"] == return_code
    assert rendered["stderr"] == "CLI diagnostic detail"


def test_read_only_tools_allow_diagnostics_but_refuse_compile_and_test_side_effects(api: UnityApi, cli: MagicMock) -> None:
    response = {
        "success": True,
        "command": "logs",
        "data": {"entries": [{"message": "NullReferenceException: Player.Update", "type": "Error"}]},
        "errors": [],
        "warnings": [],
    }
    cli.return_value.stdout = json.dumps(response)
    scope = ApiScope()
    scope.process(ApiInclusionDefinition(included_apis=["unity"]))
    scope.exclude_editing()
    facades = SerenaAgent.create_default_facade_list(api._agent, scope)
    facade = next(facade for facade in facades if facade.name == "unity")

    # preserve diagnostic reads while refusing the operation that refreshes project state
    with pytest.raises(AttributeError):
        facade.recompile()
    with pytest.raises(AttributeError):
        facade.run_tests()
    selection = ToolSet({"unity_recompile", "unity_get_console_logs", "unity_get_status", "unity_run_tests"}).without_editing_tools()
    assert selection.get_tool_names() == {"unity_get_console_logs", "unity_get_status"}
    output = facade.get_console_logs(project_path="Game & tools")

    command = cli.call_args.args[0]
    assert "logs" in command and "--console" in command
    assert command[command.index("--level") + 1] == "error"
    assert command[command.index("--project-path") + 1] == str(Path(api._get_project().project_root) / "Game & tools")
    assert output.return_code == 0
    assert json.loads(output.stdout) == response

    cli.return_value.stdout = '{"success":true,"command":"status","data":{"ready":true},"errors":[],"warnings":[]}'
    assert facade.get_status(project_path="Game & tools").return_code == 0


@pytest.mark.parametrize("wait", [False, True], ids=["snapshot", "until-ready"])
def test_status_preserves_safe_mode_diagnostics_and_uses_supported_wait_options(api: UnityApi, cli: MagicMock, wait: bool) -> None:
    response = {
        "success": False,
        "command": "status",
        "data": {
            "items": [
                {
                    "safeMode": {
                        "detected": True,
                        "compileErrors": [{"file": "Assets/Player.cs", "line": 8, "message": "; expected"}],
                    }
                }
            ]
        },
        "errors": [{"code": "STATUS_SAFE_MODE", "message": "Editor is in Safe Mode"}],
        "warnings": [],
    }
    cli.return_value.stdout = json.dumps(response)
    cli.return_value.return_code = 6

    output = api.get_status(project_path="Game & tools", wait=wait, timeout=17)

    command = cli.call_args.args[0]
    assert "status" in command
    if wait:
        assert "--until-ready" in command
        assert command[command.index("--timeout") + 1] == "17"
    else:
        assert "--until-ready" not in command and "--timeout" not in command
    assert output.return_code == 6
    assert json.loads(output.stdout) == response


def test_test_run_targets_the_child_and_resolves_report_paths_against_it(api: UnityApi, cli: MagicMock) -> None:
    root = Path(api._get_project().project_root)
    nested = root / "Game & tools"
    report = root / "Reports" / "play-mode.xml"
    response = {
        "success": False,
        "command": "test",
        "data": {"failed": 1, "reportPath": str(report)},
        "errors": [{"code": "TESTS_FAILED", "message": "One test failed"}],
        "warnings": [],
    }
    cli.return_value.stdout = json.dumps(response)
    cli.return_value.return_code = 8

    output = api.run_tests(
        project_path=nested.name,
        mode="PlayMode",
        test_filter="Gameplay.Inventory & Combat",
        output_path="../Reports/play-mode.xml",
    )

    command = cli.call_args.args[0]
    assert command[command.index("test") + 1] == str(nested)
    assert command[command.index("--mode") + 1] == "PlayMode"
    assert command[command.index("--filter") + 1] == "Gameplay.Inventory & Combat"
    assert command[command.index("--output") + 1] == str(report)
    assert command[command.index("--report-format") + 1] == "nunit"
    assert output.return_code == 8
    assert json.loads(output.stdout) == response


def test_report_output_cannot_escape_the_active_project_without_full_access(api: UnityApi, cli: MagicMock, tmp_path: Path) -> None:
    report = tmp_path / "outside.xml"

    with pytest.raises(ValueError, match="outside the project root"):
        api.run_tests(project_path="Game & tools", output_path=str(report))
    cli.assert_not_called()

    api._get_project().serena_config.full_access_mode = True
    api.run_tests(project_path="Game & tools", output_path=str(report))
    command = cli.call_args.args[0]
    assert command[command.index("--output") + 1] == str(report)


def test_project_access_policy_applies_to_unity_recompilation(api: UnityApi, cli: MagicMock, tmp_path: Path) -> None:
    outside = tmp_path / "Other game"
    _create_unity_project(outside)

    with pytest.raises(ValueError, match="outside the project root"):
        api.recompile(project_path=str(outside))
    cli.assert_not_called()

    api._get_project().serena_config.full_access_mode = True
    api.recompile(project_path=str(outside))
    command = cli.call_args.args[0]
    assert command[command.index("--project-path") + 1] == str(outside)


@pytest.mark.parametrize("unavailable", ["active-project", "unity-project", "cli"])
def test_missing_prerequisites_fail_before_starting_a_process(
    api: UnityApi, cli: MagicMock, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    if unavailable == "active-project":
        monkeypatch.setattr(api._agent, "get_active_project_or_raise", MagicMock(side_effect=ValueError("No active project")))
        error_type, message = ValueError, "active project"
    elif unavailable == "unity-project":
        (Path(api._get_project().project_root) / "Game & tools" / "ProjectSettings" / "ProjectVersion.txt").unlink()
        error_type, message = ValueError, "Unity"
    else:
        monkeypatch.setattr(shutil, "which", lambda executable: None)
        error_type, message = FileNotFoundError, "Unity"

    with pytest.raises(error_type, match=message):
        api.get_console_logs(project_path="Game & tools")
    cli.assert_not_called()


@pytest.mark.parametrize("timeout", [0, 0.5, True])
def test_invalid_timeouts_cannot_start_unbounded_cli_work(api: UnityApi, cli: MagicMock, timeout: int) -> None:
    with pytest.raises(ValueError, match="timeout"):
        api.recompile(project_path="Game & tools", timeout=timeout)
    cli.assert_not_called()


@pytest.mark.parametrize(
    ("operation", "options"),
    [
        ("get_console_logs", {"level": "debug"}),
        ("get_console_logs", {"tail": -1}),
        ("run_tests", {"mode": "unknown"}),
    ],
)
def test_invalid_query_and_test_options_do_not_start_a_process(api: UnityApi, cli: MagicMock, operation: str, options: dict) -> None:
    with pytest.raises(ValueError):
        getattr(api, operation)(project_path="Game & tools", **options)
    cli.assert_not_called()


@pytest.mark.parametrize("stdout", ["", "not JSON", '{"success":"yes"}'])
def test_invalid_cli_output_is_never_reported_as_a_success(api: UnityApi, cli: MagicMock, stdout: str) -> None:
    cli.return_value.stdout = stdout
    with pytest.raises(ValueError, match="JSON"):
        api.recompile(project_path="Game & tools")
