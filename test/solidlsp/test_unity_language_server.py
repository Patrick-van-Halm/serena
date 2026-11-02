# SPDX-License-Identifier: GPL-3.0-or-later

"""Configured Unity diagnostics with fixed external Roslyn and CLI responses."""

import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from serena.config.serena_config import SerenaConfig, SerenaPaths
from serena.ls_manager import LanguageServerManagerInitialisationError
from serena.project import Project
from serena.util import unity_cli
from serena.util.shell import ShellCommandResult
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_exceptions import SolidLSPException


@dataclass(frozen=True)
class _ProjectOptions:
    read_only: bool = False
    cli_available: bool = True
    trusted: bool = True
    global_settings: bool = False


@dataclass
class _UnityWorkspace:
    root: Path
    unity_root: Path
    project: Project
    roslyn: Mock
    cli: Mock

    @property
    def server(self) -> SolidLanguageServer:
        return self.project.ensure_language_server_manager().get_language_server(self.relative_source)

    @property
    def source_path(self) -> Path:
        return self.unity_root / "Assets" / "Player.cs"

    @property
    def relative_source(self) -> str:
        return str(self.source_path.relative_to(self.root))

    def command_result(self, response: dict[str, Any], return_code: int = 0) -> ShellCommandResult:
        return ShellCommandResult(stdout=json.dumps(response), stderr="", return_code=return_code, cwd=str(self.unity_root))


@pytest.fixture
def unity_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[_UnityWorkspace]:
    # configure the Unity child from the surrounding non-Unity Serena project
    options = getattr(request, "param", _ProjectOptions())
    root = tmp_path / "workspace"
    unity_root = root / "Game & tools"
    (unity_root / "Assets").mkdir(parents=True)
    (unity_root / "ProjectSettings").mkdir()
    (unity_root / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.47f1\n", encoding="utf-8")
    (unity_root / "Assets" / "Player.cs").write_text(
        "public class Player\n{\n    public void Update()\n    {\n        int score = 0;\n"
        "\n\n        score++\n\n        int unused;\n    }\n}\n",
        encoding="utf-8",
    )
    (unity_root / "Assets" / "Other.cs").write_text("public class Other {}\n", encoding="utf-8")
    (root / "Outer.cs").write_text("public class Outer {}\n", encoding="utf-8")
    (root / "Unrelated.sln").touch()
    (root / "Unrelated.csproj").touch()
    (unity_root / "Game.sln").touch()
    (unity_root / "Assembly-CSharp.csproj").touch()
    (unity_root / "Library" / "PackageCache").mkdir(parents=True)
    (unity_root / "Library" / "PackageCache" / "Cached.csproj").touch()
    unity_settings = {
        "project_path": f"./{unity_root.name}",
        "unity_cli_path": sys.executable if options.cli_available else str(tmp_path / "missing-unity-cli"),
        "timeout": 17,
    }
    project_settings = unity_settings
    if options.global_settings:
        project_settings = {
            "project_path": "./UntrustedProject",
            "unity_cli_path": str(tmp_path / "untrusted-unity-cli"),
            "timeout": 1,
        }
    (root / ".serena").mkdir()
    (root / ".serena" / "project.yml").write_text(
        json.dumps(
            {
                "project_name": "unity-backend-test",
                "language_servers": ["unity"],
                "read_only": options.read_only,
                "ls_specific_settings": {"unity": project_settings},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(SerenaPaths(), "serena_user_home_dir", str(tmp_path / "serena-home"))
    serena_config = SerenaConfig.from_config_file()
    serena_config.language_server_idle_timeout_seconds = 0
    if options.trusted:
        serena_config.trusted_project_path_patterns = [str(root)]
    if options.global_settings:
        serena_config.ls_specific_settings = {"unity": unity_settings}
    project = Project.load(root, serena_config=serena_config)

    # supply only the external server's readiness and document-symbol responses
    roslyn = Mock()
    handlers: dict[str, Callable[[dict], None]] = {}

    def on_notification(method: str, handler: Callable[[dict], None]) -> None:
        handlers[method] = handler

    roslyn.on_notification.side_effect = on_notification
    roslyn.notify.initialized.side_effect = lambda _: handlers["workspace/projectInitializationComplete"]({})
    roslyn.send.initialize.return_value = {
        "capabilities": {
            "textDocumentSync": 2,
            "definitionProvider": True,
            "referencesProvider": True,
            "documentSymbolProvider": True,
        }
    }
    roslyn.send.document_symbol.return_value = [
        {
            "name": "Player",
            "kind": 5,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 11, "character": 1}},
            "selectionRange": {"start": {"line": 0, "character": 13}, "end": {"line": 0, "character": 19}},
        }
    ]
    roslyn.is_running.return_value = True
    monkeypatch.setattr(SolidLanguageServer, "_create_language_server_interface", Mock(return_value=roslyn))
    cli = Mock()
    monkeypatch.setattr(unity_cli, "execute_shell_command", cli)

    # exercise normal project startup, including trust filtering and global settings
    workspace = _UnityWorkspace(root, unity_root, project, roslyn, cli)
    cli.return_value = workspace.command_result({"success": True, "data": {"failed": False, "errors": [], "warnings": []}})
    try:
        yield workspace
    finally:
        project.shutdown()


def test_nested_unity_navigation_opens_only_its_generated_projects_without_recompiling(unity_workspace: _UnityWorkspace) -> None:
    workspace = unity_workspace
    server = workspace.server
    notifications = workspace.roslyn.notify.send_notification.call_args_list
    assert [notification.args[1] for notification in notifications if notification.args[0] == "solution/open"] == [
        {"solution": (workspace.unity_root / "Game.sln").as_uri()}
    ]
    assert [notification.args[1] for notification in notifications if notification.args[0] == "project/open"] == [
        {"projects": [(workspace.unity_root / "Assembly-CSharp.csproj").as_uri()]}
    ]
    workspace_folders = workspace.roslyn.send.initialize.call_args.args[0]["workspaceFolders"]
    assert [folder["uri"] for folder in workspace_folders] == [workspace.unity_root.as_uri()]

    symbols = list(server.request_document_symbols(workspace.relative_source).iter_symbols())
    assert [(symbol["name"], symbol["location"]["relativePath"]) for symbol in symbols] == [("Player", workspace.relative_source)]
    with workspace.server.open_file(workspace.relative_source) as source:
        workspace.server.insert_text_at_position(workspace.relative_source, 0, 0, "// edited\n")
        assert source.contents.startswith("// edited\npublic class Player")
    workspace.cli.assert_not_called()


@pytest.mark.parametrize("unity_workspace", [_ProjectOptions(trusted=False)], indirect=True)
def test_untrusted_nested_unity_reports_ignored_project_settings(unity_workspace: _UnityWorkspace) -> None:
    workspace = unity_workspace

    with pytest.raises(LanguageServerManagerInitialisationError) as error:
        workspace.project.ensure_language_server_manager()

    message = str(error.value)
    assert f"Not a Unity project: {workspace.root}." in message
    assert "ls_specific_settings" in message
    assert "not trusted" in message
    assert "trusted_project_path_patterns" in message
    assert str(workspace.project.serena_config.config_file_path) in message
    assert "trusted_project_path_patterns" in workspace.project.get_language_server_manager_status()
    workspace.roslyn.send.initialize.assert_not_called()
    workspace.cli.assert_not_called()


@pytest.mark.parametrize("unity_workspace", [_ProjectOptions(trusted=False, global_settings=True)], indirect=True)
def test_untrusted_nested_unity_uses_global_settings_instead_of_project_overrides(unity_workspace: _UnityWorkspace) -> None:
    workspace = unity_workspace

    assert workspace.server.request_text_document_diagnostics(workspace.relative_source) == []

    command = workspace.cli.call_args.args[0]
    assert command[0] == sys.executable
    assert command[command.index("--project-path") + 1] == str(workspace.unity_root)
    assert command[command.index("--timeout") + 1] == "17"
    assert workspace.cli.call_args.kwargs["cwd"] == str(workspace.unity_root)


def test_unity_cache_exclusions_keep_user_code_in_same_named_asset_folders(unity_workspace: _UnityWorkspace) -> None:
    workspace = unity_workspace
    user_code = workspace.unity_root / "Assets" / "Library" / "Helper.cs"
    user_code.parent.mkdir()
    user_code.write_text("public class Helper {}\n", encoding="utf-8")
    generated_code = workspace.unity_root / "Library" / "Generated.cs"
    generated_code.write_text("public class Generated {}\n", encoding="utf-8")

    assert not workspace.server.is_ignored_path(str(user_code.relative_to(workspace.root)))
    assert workspace.server.is_ignored_path(str(generated_code.relative_to(workspace.root)))


def test_nested_compile_diagnostics_normalize_locations_and_apply_file_range_and_severity_filters(
    unity_workspace: _UnityWorkspace,
) -> None:
    workspace = unity_workspace
    workspace.cli.return_value = workspace.command_result(
        {
            "success": False,
            "data": {
                "failed": True,
                "errors": [
                    {"file": "Assets\\Player.cs", "line": 8, "column": 12, "code": "CS1002", "message": "; expected"},
                    {
                        "file": str(workspace.source_path),
                        "line": 4,
                        "column": 2,
                        "code": "CS1513",
                        "message": "} expected",
                    },
                    {"file": "Assets/Other.cs", "line": 1, "column": 1, "code": "CS1002", "message": "; expected"},
                ],
                "warnings": ["Assets/Player.cs(10,5): warning CS0168: The variable 'unused' is declared but never used"],
            },
        },
        return_code=6,
    )

    diagnostics = workspace.server.request_text_document_diagnostics(workspace.relative_source)

    assert sorted((str(item.get("code")), item.get("severity"), item["range"]["start"], item["message"]) for item in diagnostics) == [
        ("CS0168", 2, {"line": 9, "character": 4}, "The variable 'unused' is declared but never used"),
        ("CS1002", 1, {"line": 7, "character": 11}, "; expected"),
        ("CS1513", 1, {"line": 3, "character": 1}, "} expected"),
    ]
    filtered = workspace.server.request_text_document_diagnostics(workspace.relative_source, start_line=7, end_line=9, min_severity=1)
    assert filtered == [item for item in diagnostics if item.get("code") == "CS1002"]
    command = workspace.cli.call_args.args[0]
    assert isinstance(command, list)
    assert command[1] == "recompile"
    assert command[command.index("--project-path") + 1] == str(workspace.unity_root)
    assert command[command.index("--timeout") + 1] == "17"
    assert workspace.cli.call_args.kwargs["cwd"] == str(workspace.unity_root)


def test_explicit_diagnostics_recompile_after_a_fix_and_accept_a_stale_compilation_failed_flag(unity_workspace: _UnityWorkspace) -> None:
    workspace = unity_workspace
    workspace.cli.side_effect = [
        workspace.command_result(
            {"success": False, "data": {"failed": True, "errors": ["Assets/Player.cs(8,12): error CS1002: ; expected"], "warnings": []}},
            return_code=6,
        ),
        workspace.command_result({"success": True, "data": {"failed": False, "compilationFailed": True, "errors": [], "warnings": []}}),
    ]
    assert [item.get("code") for item in workspace.server.request_text_document_diagnostics(workspace.relative_source)] == ["CS1002"]

    workspace.source_path.write_text(workspace.source_path.read_text(encoding="utf-8").replace("score++", "score++;"), encoding="utf-8")

    assert workspace.server.request_text_document_diagnostics(workspace.relative_source) == []


@pytest.mark.parametrize(
    ("response", "return_code", "error_match"),
    [
        pytest.param(
            {"success": False, "data": None, "errors": [{"message": "Editor is not running"}]},
            7,
            "exit code 7",
            id="editor-unreachable",
        ),
        pytest.param(
            {"success": False, "data": {"failed": True, "errors": [], "warnings": []}},
            6,
            "without.*errors",
            id="failure-without-errors",
        ),
        pytest.param(
            {"success": False, "data": {"failed": True, "errors": [{"message": "; expected", "line": 8, "column": 12}], "warnings": []}},
            6,
            "unlocated",
            id="missing-source-location",
        ),
        pytest.param(
            {
                "success": False,
                "data": {"failed": True, "errors": ["error CS0006: Metadata file 'Missing.dll' could not be found"], "warnings": []},
            },
            6,
            "unlocated",
            id="unlocated-compiler-message",
        ),
        pytest.param(
            {"success": True, "data": {"failed": False, "errors": {}, "warnings": []}},
            0,
            "errors",
            id="malformed-diagnostics-array",
        ),
        pytest.param(
            {"success": True, "data": {"failed": False}},
            0,
            "Unity diagnostics unavailable",
            id="missing-diagnostics-arrays",
        ),
    ],
)
def test_unknown_or_unlocated_compilation_failures_are_never_reported_as_clean(
    unity_workspace: _UnityWorkspace, response: dict[str, Any], return_code: int, error_match: str
) -> None:
    workspace = unity_workspace
    workspace.cli.return_value = workspace.command_result(response, return_code)

    with pytest.raises(SolidLSPException, match=error_match):
        workspace.server.request_text_document_diagnostics(workspace.relative_source)


@pytest.mark.parametrize("unity_workspace", [_ProjectOptions(read_only=True)], indirect=True)
def test_project_read_only_setting_blocks_unity_recompilation(unity_workspace: _UnityWorkspace) -> None:
    workspace = unity_workspace

    with pytest.raises(SolidLSPException, match="read.only"):
        workspace.server.request_text_document_diagnostics(workspace.relative_source)

    workspace.cli.assert_not_called()


@pytest.mark.parametrize("unity_workspace", [_ProjectOptions(cli_available=False)], indirect=True)
def test_missing_unity_cli_allows_roslyn_navigation_but_fails_explicit_diagnostics(unity_workspace: _UnityWorkspace) -> None:
    workspace = unity_workspace
    assert [symbol["name"] for symbol in workspace.server.request_document_symbols(workspace.relative_source).iter_symbols()] == ["Player"]

    with pytest.raises(FileNotFoundError, match="Unity CLI executable not found"):
        workspace.server.request_text_document_diagnostics(workspace.relative_source)

    workspace.cli.assert_not_called()


@pytest.mark.parametrize(
    ("source", "exception", "error_match"),
    [
        pytest.param("Outer.cs", ValueError, "outside.*Unity project", id="outside-unity"),
        pytest.param("Game & tools/Assets/Missing.cs", FileNotFoundError, "source file not found", id="missing-file"),
    ],
)
def test_invalid_source_requests_do_not_trigger_unity_recompilation(
    unity_workspace: _UnityWorkspace, source: str, exception: type[Exception], error_match: str
) -> None:
    workspace = unity_workspace

    with pytest.raises(exception, match=error_match):
        workspace.server.request_text_document_diagnostics(source)

    workspace.cli.assert_not_called()
