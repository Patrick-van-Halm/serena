# SPDX-License-Identifier: GPL-3.0-or-later

import os
import subprocess

from pydantic import BaseModel

from solidlsp.util.subprocess_util import subprocess_kwargs, terminate_process_tree_with_kill_fallback


class ShellCommandResult(BaseModel):
    stdout: str
    return_code: int
    cwd: str
    stderr: str | None = None


def execute_shell_command(
    command: str,
    cwd: str | None = None,
    capture_stderr: bool = False,
    timeout: float | None = None,
) -> ShellCommandResult:
    """
    Execute a shell command and return the output.

    :param command: The command to execute.
    :param cwd: The working directory to execute the command in. If None, the current working directory will be used.
    :param capture_stderr: Whether to capture the stderr output.
    :param timeout: Maximum execution time in seconds. Timed-out process trees are terminated.
    :return: The output of the command.
    """
    if cwd is None:
        cwd = os.getcwd()

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
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        terminate_process_tree_with_kill_fallback(
            process,
            terminate_timeout=1.0,
            process_name="Shell command",
            process_group_id=process.pid if start_new_session else None,
        )
        # Drain closed pipes so large partial output buffers are promptly released.
        try:
            process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        raise TimeoutError(f"Shell command timed out after {timeout} seconds") from e

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
