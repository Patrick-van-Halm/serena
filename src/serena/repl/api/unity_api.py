# SPDX-License-Identifier: GPL-3.0-or-later
"""Project-scoped Unity CLI compilation and Console queries."""

import os.path
from typing import TYPE_CHECKING, Literal

from serena.tools import UnityGetConsoleLogsTool, UnityGetStatusTool, UnityRecompileTool, UnityRunTestsTool
from serena.util.unity_cli import UnityCli

from ..facade import FacadeApi, facade_method
from .shell_api import ShellCommandOutput, ShellCommandOutputRenderer

if TYPE_CHECKING:
    from serena.agent import SerenaAgent


class UnityApi(FacadeApi):
    """Unity Editor operations implemented by the official Unity CLI."""

    def __init__(self, agent: "SerenaAgent") -> None:
        super().__init__(agent, name="unity", description="Unity Editor diagnostics, compilation and tests")

    def _run_cli(
        self,
        args: list[str],
        project_path: str,
        timeout: int,
        unity_cli_path: str,
        max_answer_chars: int,
        output_path: str | None = None,
    ) -> ShellCommandOutput:
        # require a selected Unity project before looking up or running an executable
        project = self._get_project()
        project.validate_relative_path(project_path)
        cli = UnityCli(project.project_root, project_path, unity_cli_path)

        # resolve test reports relative to the Unity root while enforcing the outer project's access policy
        if output_path is not None:
            report_path = os.path.abspath(os.path.join(cli.project_root, output_path))
            project.validate_relative_path(report_path)
            if not output_path or os.path.isdir(report_path):
                raise ValueError("output_path must name a test report file")
            args = [*args, "--output", report_path, "--report-format", "nunit"]

        # retain the CLI's failure codes and diagnostics verbatim, including nonzero-exit responses
        process_timeout = max(float(self._agent.serena_config.tool_timeout) - 1.0, 0.1)
        result = cli.run(args, timeout=timeout, process_timeout=process_timeout)
        return ShellCommandOutput(result.command, ShellCommandOutputRenderer(self._agent, max_answer_chars))

    @facade_method(can_edit=True, corresponding_tool=UnityRecompileTool)
    def recompile(
        self,
        project_path: str = ".",
        strict: bool = False,
        timeout: int = 120,
        unity_cli_path: str = "unity",
        max_answer_chars: int = -1,
    ) -> ShellCommandOutput:
        """
        Refresh assets and recompile scripts in the selected project's running Unity Editor.
        Requires the official Unity CLI and the Editor's Pipeline package. Does not start an Editor or install packages.
        ``stdout`` contains the CLI's JSON envelope, including compiler diagnostics; ``return_code`` is 0 on success,
        6 on command/compilation failure, or 7 if the Editor is unreachable and compilation is unknown.
        A stale ``compilationFailed`` field alone does not establish failure; preserve the CLI's verdict.

        :param project_path: Unity root relative to the active Serena project or absolute; normal project access rules apply
        :param strict: whether warnings should also fail the compile check
        :param timeout: positive whole seconds to wait; Serena's tool timeout may interrupt sooner
        :param unity_cli_path: CLI command on PATH or executable path; relative paths use the Serena project root
        :param max_answer_chars: response length limit; -1 uses the configured default.
            Full stdout remains available on the returned object even when its representation is shortened.
        :return: CLI output and exit status; decode ``stdout`` with ``json.loads`` for structured diagnostics
        """
        args = ["recompile", "--timeout", str(timeout)]
        if strict:
            args.append("--strict")
        return self._run_cli(args, project_path, timeout, unity_cli_path, max_answer_chars)

    @facade_method(corresponding_tool=UnityGetConsoleLogsTool)
    def get_console_logs(
        self,
        project_path: str = ".",
        level: Literal["info", "warn", "error"] = "error",
        tail: int = 100,
        timeout: int = 30,
        unity_cli_path: str = "unity",
        max_answer_chars: int = -1,
    ) -> ShellCommandOutput:
        """
        Read recent Unity Console entries without clearing them or requesting compilation.
        Requires the official Unity CLI and a running Editor with the Pipeline package.
        Exit 0 means the query succeeded, even if its returned Console entries contain errors.
        An unreachable Editor, including Safe Mode, does not establish that the project is error-free.
        Use ``get_status`` to inspect Safe Mode diagnostics.

        :param project_path: Unity root relative to the active Serena project or absolute; normal project access rules apply
        :param level: ``error`` for errors, ``warn`` for warnings and errors, or ``info`` for all levels
        :param tail: number of recent entries to request; 0 requests the available buffer
        :param timeout: positive whole seconds to wait; Serena's tool timeout may interrupt sooner
        :param unity_cli_path: CLI command on PATH or executable path; relative paths use the Serena project root
        :param max_answer_chars: response length limit; -1 uses the configured default.
            Full stdout remains available on the returned object even when its representation is shortened.
        :return: CLI output and exit status; decode ``stdout`` with ``json.loads`` for structured Console entries
        """
        if level not in ("info", "warn", "error"):
            raise ValueError("level must be info, warn or error")
        if isinstance(tail, bool) or not isinstance(tail, int) or tail < 0:
            raise ValueError("tail must be a non-negative whole number")
        args = ["logs", "--console", "--level", level, "--tail", str(tail)]
        return self._run_cli(args, project_path, timeout, unity_cli_path, max_answer_chars)

    @facade_method(corresponding_tool=UnityGetStatusTool)
    def get_status(
        self,
        project_path: str = ".",
        wait: bool = False,
        timeout: int = 30,
        unity_cli_path: str = "unity",
        max_answer_chars: int = -1,
    ) -> ShellCommandOutput:
        """
        Inspect the selected Editor's readiness, connection problems and Safe Mode compiler diagnostics.
        Does not start an Editor or require Pipeline to report Safe Mode. Nonzero exit codes and structured
        status errors are retained; an unavailable Editor does not establish a clean compilation.

        :param project_path: Unity root relative to the active Serena project or absolute; normal project access rules apply
        :param wait: whether to wait until the selected Editor is ready instead of taking one status snapshot
        :param timeout: positive whole seconds to wait; Serena's tool timeout may interrupt sooner
        :param unity_cli_path: CLI command on PATH or executable path; relative paths use the Serena project root
        :param max_answer_chars: response length limit; -1 uses the configured default
        :return: CLI output and exit status; decode ``stdout`` with ``json.loads`` for structured Editor status
        """
        args = ["status"]
        if wait:
            args.extend(["--until-ready", "--timeout", str(timeout)])
        return self._run_cli(args, project_path, timeout, unity_cli_path, max_answer_chars)

    @facade_method(can_edit=True, corresponding_tool=UnityRunTestsTool)
    def run_tests(
        self,
        project_path: str = ".",
        mode: Literal["EditMode", "PlayMode"] = "EditMode",
        test_filter: str = "",
        output_path: str = "test-results.xml",
        timeout: int = 120,
        unity_cli_path: str = "unity",
        max_answer_chars: int = -1,
    ) -> ShellCommandOutput:
        """
        Run Unity tests in a batch Editor and write an NUnit report, replacing an existing report at that path.
        The Unity project must be closed in the Editor; the CLI refuses an already-open project and does not close it.
        Pipeline is not required. Runs project code, writes project state and starts a batch Editor without installing one.
        Exit codes distinguish passed tests (0), failed tests (8), no verdict (6), and timeout (14).

        :param project_path: Unity root relative to the active Serena project or absolute; normal project access rules apply
        :param mode: whether to run Edit Mode or Play Mode tests
        :param test_filter: Unity test name filter; empty runs all tests in the selected mode
        :param output_path: NUnit report path relative to the Unity root, or absolute; normal project access rules apply
        :param timeout: positive whole seconds to wait; increase Serena's tool timeout for longer runs
        :param unity_cli_path: CLI command on PATH or executable path; relative paths use the Serena project root
        :param max_answer_chars: response length limit; -1 uses the configured default
        :return: CLI output and exit status; decode ``stdout`` with ``json.loads`` for the test result
        """
        if mode not in ("EditMode", "PlayMode"):
            raise ValueError("mode must be EditMode or PlayMode")
        args = ["test", "--mode", mode, "--timeout", str(timeout)]
        if test_filter:
            args.extend(["--filter", test_filter])
        return self._run_cli(args, project_path, timeout, unity_cli_path, max_answer_chars, output_path=output_path)
