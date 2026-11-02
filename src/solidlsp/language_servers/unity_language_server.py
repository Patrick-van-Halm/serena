# SPDX-License-Identifier: MIT
"""C# navigation with compilation diagnostics from the official Unity CLI."""

import os
import re
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from overrides import override

from serena.util.unity_cli import UnityCli, UnityCliResult
from solidlsp import ls_types
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.settings import SolidLSPSettings

from .csharp_language_server import CSharpLanguageServer


class UnityLanguageServer(CSharpLanguageServer):
    """
    Roslyn-backed C# operations with fresh Unity compiler diagnostics.

    Configure ``language_servers: [unity]`` and ``ls_specific_settings.unity`` with
    ``project_path`` (Unity root relative to the Serena root, default ``.``),
    ``unity_cli_path`` (default ``unity``), and ``timeout`` (positive seconds, default 120).
    C# settings are inherited, with Unity-specific overrides taking precedence.
    Explicit diagnostic requests refresh assets and compile in the running Editor;
    ordinary symbol operations and file notifications do not invoke the CLI.
    """

    _COMPILER_MESSAGE = re.compile(
        r"^(?P<file>.+)\((?P<line>\d+),(?P<column>\d+)\):\s*"
        r"(?P<severity>error|warning)\s+(?P<code>[^\s:]+):\s*(?P<message>.*)$",
        re.DOTALL,
    )

    @classmethod
    @override
    def diagnostics_modify_project(cls) -> bool:
        return True

    def __init__(self, config: LanguageServerConfig, repository_root_path: str, solidlsp_settings: SolidLSPSettings):
        # retain Roslyn configuration while allowing Unity-specific overrides
        custom = {
            **solidlsp_settings.get_ls_specific_settings(LanguageServerId.CSHARP).settings,
            **solidlsp_settings.get_ls_specific_settings(config.ls_id).settings,
        }
        settings_by_server = dict(solidlsp_settings.ls_specific_settings)
        if isinstance(config.ls_id, LanguageServerId):
            settings_by_server.pop(config.ls_id, None)
        settings_by_server[config.ls_id.get_key()] = custom
        settings = replace(solidlsp_settings, ls_specific_settings=settings_by_server)

        # select the child Unity root without changing Serena-relative file paths
        project_path = custom.get("project_path", ".")
        cli_path = custom.get("unity_cli_path", "unity")
        if not isinstance(project_path, str) or not project_path or not isinstance(cli_path, str) or not cli_path:
            raise ValueError("Unity project_path and unity_cli_path must be non-empty strings")
        self._unity_cli = UnityCli(repository_root_path, project_path, cli_path)
        unity_root = Path(self._unity_cli.project_root).resolve()
        if not unity_root.is_relative_to(Path(repository_root_path).resolve()):
            raise ValueError("Unity project_path must be within the Serena project")
        self._unity_timeout = custom.get("timeout", 120)
        if isinstance(self._unity_timeout, bool) or not isinstance(self._unity_timeout, int) or self._unity_timeout <= 0:
            raise ValueError("Unity timeout must be a positive whole number of seconds")
        if config.workspace_folders == ["."]:
            config = replace(config, workspace_folders=[self._unity_cli.project_root])
        super().__init__(config, repository_root_path, settings)

    @classmethod
    @override
    def ls_resources_dir(cls, solidlsp_settings: SolidLSPSettings, mkdir: bool = True) -> str:
        return CSharpLanguageServer.ls_resources_dir(solidlsp_settings, mkdir=mkdir)

    @override
    def _iter_solution_and_project_files(self) -> Iterable[str]:
        # Unity generates its IDE solution and projects beside Assets, outside Library and other caches
        return (str(path) for path in Path(self._unity_cli.project_root).iterdir() if path.is_file())

    @override
    def is_ignored_path(self, relative_path: str, ignore_unsupported_files: bool = True) -> bool:
        # exclude generated directories only at the Unity root, preserving names such as Assets/Library
        path = Path(os.path.abspath(os.path.join(self.repository_root_path, relative_path)))
        unity_root = Path(self._unity_cli.project_root)
        if path.is_relative_to(unity_root):
            parts = path.relative_to(unity_root).parts
            if parts and parts[0] in {"Library", "Temp", "Logs", "UserSettings"}:
                return True
        return super().is_ignored_path(relative_path, ignore_unsupported_files=ignore_unsupported_files)

    @override
    def request_text_document_diagnostics(
        self,
        relative_file_path: str,
        start_line: int = 0,
        end_line: int = -1,
        min_severity: int = 4,
    ) -> list[ls_types.Diagnostic]:
        """Refresh Unity compilation and return this file's diagnostics with the usual LSP filters."""
        # reject invalid requests before refreshing the Editor's state
        self._validate_text_document_diagnostics_request(relative_file_path, start_line, end_line, min_severity)
        requested_file = Path(self.repository_root_path, relative_file_path).resolve()
        if not requested_file.is_relative_to(Path(self._unity_cli.project_root).resolve()):
            raise ValueError(f"File is outside the configured Unity project: {relative_file_path}")
        if not requested_file.is_file():
            raise FileNotFoundError(f"Unity source file not found: {relative_file_path}")
        if self.config.read_only:
            raise SolidLSPException(
                "Unity diagnostics require recompilation, which is disabled for read-only projects. "
                "Use unity_get_status or unity_get_console_logs to inspect existing Editor state."
            )

        # rely on the shared process runner for deadline and cancellation handling
        result = self._unity_cli.run(["recompile", "--timeout", str(self._unity_timeout)], timeout=self._unity_timeout)
        diagnostics = self._compile_diagnostics(result)
        matching = [item for item in diagnostics if item["uri"] == requested_file.as_uri()]
        return self._filter_diagnostics(matching, start_line, end_line, min_severity)

    def _compile_diagnostics(self, result: UnityCliResult) -> list[ls_types.Diagnostic]:
        # require an understood compile verdict before accepting a clean result
        response = result.response
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("failed"), bool):
            self._raise_compile_failure(result, "missing compilation verdict")
        assert isinstance(data, dict)
        if result.command.return_code not in (0, 6):
            self._raise_compile_failure(result, "compilation outcome is unknown")

        # accept both structured CLI diagnostics and the Pipeline's original compiler lines
        diagnostics = []
        for key, severity in (("errors", ls_types.DiagnosticSeverity.Error), ("warnings", ls_types.DiagnosticSeverity.Warning)):
            entries = data.get(key)
            if not isinstance(entries, list):
                self._raise_compile_failure(result, f"invalid {key} payload")
            for entry in entries:
                diagnostics.append(self._parse_diagnostic(entry, severity))

        # compilationFailed can be stale; trust the current failed flag and CLI verdict
        failed = data["failed"] or not response["success"] or result.command.return_code != 0
        if failed and not any(item["severity"] == ls_types.DiagnosticSeverity.Error for item in diagnostics):
            self._raise_compile_failure(result, "compilation failed without located compiler errors")
        return diagnostics

    def _parse_diagnostic(self, entry: Any, severity: ls_types.DiagnosticSeverity) -> ls_types.Diagnostic:
        # parse original compiler text using its one-based source location
        if isinstance(entry, str):
            match = self._COMPILER_MESSAGE.fullmatch(entry.strip())
            if match is None:
                raise SolidLSPException(f"Unity returned an unlocated compiler diagnostic: {entry[:1000]}")
            entry = match.groupdict()
            entry["line"] = int(entry["line"])
            entry["column"] = int(entry["column"])
            severity = ls_types.DiagnosticSeverity.Error if entry["severity"] == "error" else ls_types.DiagnosticSeverity.Warning

        # do not silently discard diagnostics that cannot be assigned to a source file
        if not isinstance(entry, dict):
            raise SolidLSPException(f"Unity returned an invalid compiler diagnostic: {str(entry)[:1000]}")
        file = entry.get("file")
        line, column = entry.get("line"), entry.get("column")
        message = entry.get("message")
        if (
            not isinstance(file, str)
            or not file
            or type(line) is not int
            or line <= 0
            or type(column) is not int
            or column <= 0
            or not isinstance(message, str)
        ):
            raise SolidLSPException(f"Unity returned an unlocated compiler diagnostic: {str(entry)[:1000]}")

        # normalize Unity-relative paths and backslashes before matching Serena's outer-root paths
        path = Path(self._unity_cli.project_root, file.replace("\\", os.sep)).resolve()
        position: ls_types.Position = {"line": line - 1, "character": column - 1}
        diagnostic: ls_types.Diagnostic = {
            "uri": path.as_uri(),
            "range": {"start": position, "end": position.copy()},
            "severity": severity,
            "message": message,
            "source": "unity",
        }
        code = entry.get("code")
        if isinstance(code, str | int):
            diagnostic["code"] = code
        return diagnostic

    @staticmethod
    def _raise_compile_failure(result: UnityCliResult, reason: str) -> None:
        raise SolidLSPException(
            f"Unity diagnostics unavailable: {reason} (exit code {result.command.return_code}). {result.command.stdout.strip()[:2000]}"
        )
