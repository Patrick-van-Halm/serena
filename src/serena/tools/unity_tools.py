# SPDX-License-Identifier: GPL-3.0-or-later
"""Tools for compilation and Console diagnostics through the official Unity CLI."""

from typing import TYPE_CHECKING, Literal, cast

from serena.tools import Tool, ToolMarkerCanEdit, ToolMarkerOptional

if TYPE_CHECKING:
    from serena.repl.api.unity_api import UnityApi


class UnityApiMixin:
    """Delegate Unity operations to the shared facade API."""

    def _api(self) -> "UnityApi":
        from serena.repl.api.unity_api import UnityApi

        tool = cast(Tool, cast(object, self))
        return UnityApi(tool.agent)


class UnityRecompileTool(Tool, ToolMarkerCanEdit, ToolMarkerOptional, UnityApiMixin):
    """Recompile a running Unity project's scripts and return compiler diagnostics."""

    def apply(
        self,
        project_path: str = ".",
        strict: bool = False,
        timeout: int = 120,
        unity_cli_path: str = "unity",
        max_answer_chars: int = -1,
    ) -> str:
        """
        Refresh assets and recompile scripts through the official Unity CLI.
        Requires an active Serena project and a running Unity Editor with the Pipeline package.
        Returns Unity's JSON response in stdout, plus stderr, return_code and cwd.
        Exit 0 means compilation succeeded or was unnecessary; 6 means command/compilation failure;
        7 means the Editor was unreachable and the compile outcome is unknown.
        Does not install packages or start an Editor.

        :param project_path: Unity project directory, relative to the active Serena project root or absolute.
            Defaults to the project root; normal project path access rules apply.
        :param strict: whether compiler warnings should also fail the compile check
        :param timeout: positive whole seconds to wait; Serena's tool timeout may interrupt sooner
        :param unity_cli_path: Unity CLI command on PATH or executable path; relative paths use the Serena project root
        :param max_answer_chars: maximum response length; -1 uses the configured default.
            Shortened output is marked and retains the exit status when it fits. Do not recompile solely to retrieve longer output.
        :return: the CLI's stdout, stderr, return_code and cwd as JSON
        """
        return self._api().recompile(project_path, strict, timeout, unity_cli_path, max_answer_chars).represent()


class UnityGetConsoleLogsTool(Tool, ToolMarkerOptional, UnityApiMixin):
    """Read recent Unity Console errors, warnings or logs without clearing them or recompiling."""

    def apply(
        self,
        project_path: str = ".",
        level: Literal["info", "warn", "error"] = "error",
        tail: int = 100,
        timeout: int = 30,
        unity_cli_path: str = "unity",
        max_answer_chars: int = -1,
    ) -> str:
        """
        Read Console entries through the official Unity CLI without clearing them or recompiling.
        Requires an active Serena project and a running Unity Editor with the Pipeline package.
        Returns Unity's JSON response in stdout, plus stderr, return_code and cwd.
        Exit 0 means the query succeeded, not that the Console is free of errors: inspect its returned entries.
        Unreachable Editors and Safe Mode are not clean projects; use unity_get_status for Safe Mode diagnostics.

        :param project_path: Unity project directory, relative to the active Serena project root or absolute.
            Defaults to the project root; normal project path access rules apply.
        :param level: error for errors, warn for warnings and errors, or info for all Console levels
        :param tail: number of recent entries to request; 0 requests the available buffer
        :param timeout: positive whole seconds to wait; Serena's tool timeout may interrupt sooner
        :param unity_cli_path: Unity CLI command on PATH or executable path; relative paths use the Serena project root
        :param max_answer_chars: maximum response length; -1 uses the configured default.
            Oversized output is shortened to marked excerpts while retaining the exit status when it fits.
        :return: the CLI's stdout, stderr, return_code and cwd as JSON
        """
        return self._api().get_console_logs(project_path, level, tail, timeout, unity_cli_path, max_answer_chars).represent()


class UnityGetStatusTool(Tool, ToolMarkerOptional, UnityApiMixin):
    """Inspect Unity Editor readiness and Safe Mode compiler diagnostics for the selected project."""

    def apply(
        self,
        project_path: str = ".",
        wait: bool = False,
        timeout: int = 30,
        unity_cli_path: str = "unity",
        max_answer_chars: int = -1,
    ) -> str:
        """
        Read Unity Editor status, including connection problems and Safe Mode compiler errors.
        Requires an active Serena project and the official Unity CLI. Does not start an Editor or install Pipeline.
        Nonzero exit codes and structured status errors are preserved; an unreachable Editor is not a clean compile.

        :param project_path: Unity directory relative to the Serena project root or absolute; normal project access rules apply
        :param wait: whether to wait for the selected Editor to become ready instead of taking a status snapshot
        :param timeout: positive whole seconds to wait; Serena's tool timeout may interrupt sooner
        :param unity_cli_path: CLI command on PATH or executable path; relative paths use the Serena project root
        :param max_answer_chars: response length limit; -1 uses the configured default
        :return: the CLI's JSON response in stdout, plus stderr, return_code and cwd
        """
        return self._api().get_status(project_path, wait, timeout, unity_cli_path, max_answer_chars).represent()


class UnityRunTestsTool(Tool, ToolMarkerCanEdit, ToolMarkerOptional, UnityApiMixin):
    """Run Unity Edit Mode or Play Mode tests in a batch Editor and write an NUnit report."""

    def apply(
        self,
        project_path: str = ".",
        mode: Literal["EditMode", "PlayMode"] = "EditMode",
        test_filter: str = "",
        output_path: str = "test-results.xml",
        timeout: int = 120,
        unity_cli_path: str = "unity",
        max_answer_chars: int = -1,
    ) -> str:
        """
        Run Unity tests through the official CLI, replacing the NUnit report at output_path.
        Requires an active Serena project and an installed Unity Editor; Pipeline is not needed.
        The selected Unity project must be closed in the Editor. The CLI refuses an already-open project and does not close it.
        Starts a batch Editor and runs project code. Exit 0 means passed, 8 means tests failed, 6 means no verdict,
        and 14 means timeout. Inspect the returned JSON for details.

        :param project_path: Unity directory relative to the Serena project root or absolute; normal project access rules apply
        :param mode: EditMode or PlayMode tests
        :param test_filter: Unity test name filter; empty runs all tests in the selected mode
        :param output_path: NUnit report relative to the Unity directory, or absolute; normal project access rules apply
        :param timeout: positive whole seconds to wait; increase Serena's tool timeout for longer runs
        :param unity_cli_path: CLI command on PATH or executable path; relative paths use the Serena project root
        :param max_answer_chars: response length limit; -1 uses the configured default
        :return: the CLI's JSON response in stdout, plus stderr, return_code and cwd
        """
        return self._api().run_tests(project_path, mode, test_filter, output_path, timeout, unity_cli_path, max_answer_chars).represent()
