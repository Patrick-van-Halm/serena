# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared execution of the official Unity CLI against a selected Unity project."""

import json
import os.path
import shutil
from dataclasses import dataclass
from typing import Any

from serena.util.shell import ShellCommandResult, execute_shell_command


@dataclass
class UnityCliResult:
    """The original process result and its validated Unity CLI response envelope."""

    command: ShellCommandResult
    response: dict[str, Any]


class UnityCli:
    """Run Unity CLI commands without depending on the agent or its presentation layer."""

    project_root: str

    def __init__(self, repository_root: str, project_path: str = ".", unity_cli_path: str = "unity") -> None:
        self._repository_root = os.path.abspath(repository_root)
        self._unity_cli_path = unity_cli_path
        self.project_root = os.path.abspath(os.path.join(self._repository_root, project_path))

        # validate the Unity root without requiring the executable until a command is run
        if not os.path.isdir(os.path.join(self.project_root, "Assets")) or not os.path.isfile(
            os.path.join(self.project_root, "ProjectSettings", "ProjectVersion.txt")
        ):
            raise ValueError(
                f"Not a Unity project: {self.project_root}. Expected Assets/ and ProjectSettings/ProjectVersion.txt. "
                "For a nested Unity project, pass its directory in project_path."
            )

    def run(self, args: list[str], timeout: int = 120, process_timeout: float | None = None) -> UnityCliResult:
        """
        Run a command and validate its JSON response without discarding failure diagnostics.

        :param args: command arguments, including any command-specific ``--timeout`` option
        :param timeout: positive whole seconds for the command, with one extra second to collect its response
        :param process_timeout: optional tighter process deadline in seconds
        :return: original process output and the parsed response envelope, including nonzero exit results
        """
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError("timeout must be a positive whole number of seconds")

        # resolve an explicit executable before changing the child process's working directory
        cli_path = os.path.expanduser(self._unity_cli_path)
        if os.path.dirname(cli_path) and not os.path.isabs(cli_path):
            cli_path = os.path.join(self._repository_root, cli_path)
        executable = shutil.which(cli_path)
        if executable is None:
            raise FileNotFoundError(
                f"Unity CLI executable not found: {self._unity_cli_path!r}. Install the official Unity CLI and add it to PATH, "
                "or pass unity_cli_path. This is the 'unity' CLI, not the Unity Editor executable."
            )

        # target exactly this project and prevent prompts without involving shell parsing
        if args[0] == "test":
            command = [os.path.abspath(executable), "test", self.project_root, *args[1:]]
        else:
            command = [os.path.abspath(executable), *args, "--project-path", self.project_root]
        command.extend(["--format", "json", "--non-interactive"])
        command_timeout = float(timeout) + 1.0
        if process_timeout is not None:
            command_timeout = min(command_timeout, process_timeout)
        result = execute_shell_command(command, cwd=self.project_root, capture_stderr=True, timeout=command_timeout)

        # reject a missing CLI response rather than mistaking empty output for a clean compile
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            detail = (result.stderr or result.stdout).strip()[:500]
            raise ValueError(
                f"Unity CLI did not return valid JSON (exit code {result.return_code}). "
                f"Use a current Unity CLI with JSON output support. {detail}"
            ) from error
        if not isinstance(response, dict) or not isinstance(response.get("success"), bool):
            raise ValueError(
                f"Unity CLI returned an invalid result envelope (exit code {result.return_code}); expected a JSON success flag."
            )

        return UnityCliResult(command=result, response=response)
