# SPDX-License-Identifier: GPL-3.0-or-later

import os
import subprocess
import time

from pydantic import BaseModel

from serena.util.tool_call import ToolCall, ToolCallContext
from solidlsp.util.subprocess_util import subprocess_kwargs, terminate_process_tree_with_kill_fallback


class ShellCommandResult(BaseModel):
    stdout: str
    return_code: int
    cwd: str
    stderr: str | None = None


class _ShellProcessWaiter:
    """Wait for owned shell processes within the caller's remaining deadline."""

    _POLL_SECONDS = 0.1

    def __init__(self, timeout: float | None) -> None:
        context = ToolCallContext.current()
        self._call: ToolCall | None = context.call if context is not None else None
        self._timeout = timeout
        self._deadline = None if timeout is None else time.monotonic() + timeout

    def check(self) -> None:
        """Reject cancelled or expired work before spawning or continuing a process."""
        if self._call is not None:
            self._call.check()
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise TimeoutError(f"Shell command timed out after {self._timeout} seconds")

    def wait(self, process: subprocess.Popen[str]) -> tuple[str, str | None]:
        """Drain output while periodically observing cooperative cancellation."""
        while True:
            self.check()
            remaining = None if self._deadline is None else max(0.0, self._deadline - time.monotonic())
            if self._call is not None:
                remaining = min(self._POLL_SECONDS, self._call.remaining, remaining if remaining is not None else float("inf"))
            try:
                return process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                # communicate may be resumed after a timeout without losing captured output
                self.check()


def execute_shell_command(
    command: str,
    cwd: str | None = None,
    capture_stderr: bool = False,
    timeout: float | None = None,
) -> ShellCommandResult:
    """
    Execute a shell command and return the output.

    :param command: the command to execute
    :param cwd: the working directory; None uses the current working directory
    :param capture_stderr: whether to capture standard error
    :param timeout: maximum execution time; an active tool deadline or cancellation may interrupt sooner
    :return: the command output and exit status
    """
    # reject expired work before creating any child process
    waiter = _ShellProcessWaiter(timeout)
    waiter.check()
    if cwd is None:
        cwd = os.getcwd()

    # isolate the owned process group for descendant-aware cleanup
    popen_kwargs = subprocess_kwargs()
    start_new_session = os.name == "posix"
    if start_new_session:
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt":
        popen_kwargs["creationflags"] = popen_kwargs.get("creationflags", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    process = subprocess.Popen(
        command,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture_stderr else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        **popen_kwargs,
    )

    try:
        stdout, stderr = waiter.wait(process)
    except BaseException:
        # finish cleanup before releasing the serial executor's protection
        terminate_process_tree_with_kill_fallback(
            process,
            terminate_timeout=1.0,
            process_name="Shell command",
            process_group_id=process.pid if start_new_session else None,
        )
        try:
            process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        raise

    return ShellCommandResult(stdout=stdout, stderr=stderr, return_code=process.returncode, cwd=cwd)


def subprocess_check_output(
    args: list[str], encoding: str = "utf-8", strip: bool = True, timeout: float | None = None, cwd: str | None = None
) -> str:
    output = subprocess.check_output(
        args, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout, env=os.environ.copy(), cwd=cwd, **subprocess_kwargs()
    ).decode(encoding)
    if strip:
        output = output.strip()
    return output
