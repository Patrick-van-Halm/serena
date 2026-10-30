# SPDX-License-Identifier: GPL-3.0-or-later
"""
The implementation of shell command execution.
"""

import json
import os.path
from typing import TYPE_CHECKING

from serena.tools import ExecuteShellCommandTool
from serena.util.shell import ShellCommandResult, execute_shell_command

from ..facade import FacadeApi, facade_method
from ..representable import Renderer, RepresentableViaRenderer

if TYPE_CHECKING:
    from serena.agent import SerenaAgent


class ShellCommandOutput(RepresentableViaRenderer):
    """
    The outcome of a shell command: `stdout`, `stderr` (None if not captured), `return_code` and `cwd`.
    """

    def __init__(self, result: ShellCommandResult, renderer: "ShellCommandOutputRenderer"):
        """
        :param result: the result of the command execution
        :param renderer: the renderer to use for representing the output
        """
        super().__init__(renderer)
        self.result_ = result

    @property
    def stdout(self) -> str:
        return self.result_.stdout

    @property
    def stderr(self) -> str | None:
        return self.result_.stderr

    @property
    def return_code(self) -> int:
        return self.result_.return_code

    @property
    def cwd(self) -> str:
        return self.result_.cwd


class ShellCommandOutputRenderer(Renderer[ShellCommandOutput]):
    """Preserve exit status and useful excerpts when complete shell output will not fit."""

    @staticmethod
    def _excerpt(text: str | None, retained: int) -> str | None:
        if text is None or len(text) <= retained:
            return text
        if retained == 0:
            return ""
        head = retained // 3
        tail = retained - head
        return text[:head] + "\n... output omitted ...\n" + text[-tail:]

    def _preview(self, result: ShellCommandResult) -> str:
        # fit serialized JSON rather than estimating unescaped string length
        limit = self._get_max_answer_chars()
        stdout_count = len(result.stdout)
        stderr_count = len(result.stderr or "")

        def encode(retained: int) -> str:
            stdout_budget = min(stdout_count, retained // 2)
            stderr_budget = min(stderr_count, retained - stdout_budget)
            stdout_budget = min(stdout_count, retained - stderr_budget)
            payload = result.model_dump()
            payload.update(
                stdout=self._excerpt(result.stdout, stdout_budget),
                stderr=self._excerpt(result.stderr, stderr_budget),
                output_truncated=True,
                stdout_original_chars=stdout_count,
                stderr_original_chars=stderr_count,
            )
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        low, high = 0, max(0, min(limit, stdout_count + stderr_count))
        best = encode(0)
        while low <= high:
            retained = (low + high) // 2
            candidate = encode(retained)
            if len(candidate) <= limit:
                best = candidate
                low = retained + 1
            else:
                high = retained - 1
        return best

    def render(self, obj: ShellCommandOutput) -> str:
        return self._limit_length(obj.result_.model_dump_json(), [lambda: self._preview(obj.result_)])


class ShellApi(FacadeApi):
    def __init__(self, agent: "SerenaAgent") -> None:
        super().__init__(agent, name="shell", description="execution of shell commands")

    @facade_method(can_edit=True, corresponding_tool=ExecuteShellCommandTool)
    def execute_shell_command(
        self, command: str, cwd: str | None = None, capture_stderr: bool = True, max_answer_chars: int = -1
    ) -> ShellCommandOutput:
        """
        Executes a shell command and returns its output. If there is a memory about suggested commands, read that first.
        Never execute unsafe shell commands!
        IMPORTANT: Do not use this to start
          * long-running processes (e.g. servers) that are not intended to terminate quickly,
          * processes that require user interaction.

        :param command: the shell command to execute
        :param cwd: the working directory to execute the command in (absolute, or relative to the project root).
            If None, the project root will be used.
        :param capture_stderr: whether to capture and return stderr output
        :return: the output (object with properties stdout, stderr, return_code and cwd)
        """
        project_root = self._get_project().project_root
        if cwd is None:
            _cwd = project_root
        elif os.path.isabs(cwd):
            _cwd = cwd
        else:
            _cwd = os.path.join(project_root, cwd)
            if not os.path.isdir(_cwd):
                raise FileNotFoundError(
                    f"Specified a relative working directory ({cwd}), but the resulting path is not a directory: {_cwd}"
                )

        configured_timeout = getattr(self._agent.serena_config, "tool_timeout", None)
        shell_timeout = None
        if isinstance(configured_timeout, int | float):
            # Leave the outer tool executor a small margin to receive the exception after
            # this function has terminated the child process tree.
            shell_timeout = max(float(configured_timeout) - 1.0, 0.1)
        result = execute_shell_command(command, cwd=_cwd, capture_stderr=capture_stderr, timeout=shell_timeout)
        return ShellCommandOutput(result, ShellCommandOutputRenderer(self._agent, max_answer_chars))
