"""Measure cancellation drain and serial-queue delay using a private, short-lived shell child.

Run with the repository's installed dependencies. This is a component probe, not a
measurement of model TPS, MCP transport, Zed, or the user's original workload.
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from serena.task_executor import TaskExecutor
from serena.util.shell import execute_shell_command
from serena.util.tool_call import ToolCall


class ShellCancellationProbe:
    """Exercise the real shell utility and serial executor without touching a user project."""

    @staticmethod
    def run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="serena-latency-") as directory:
            # bound both the child and the call independently of the cancellation being tested
            ready = Path(directory) / "ready"
            script = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(1.5); print('child-completed')"
            argv = [sys.executable, "-c", script]
            command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
            executor = TaskExecutor("LatencyProbe")
            call = ToolCall(4.0)
            started = time.monotonic()
            worker = executor.issue_task(
                lambda: call.execute(lambda: execute_shell_command(command, cwd=directory, capture_stderr=True, timeout=3.0)),
                name="owned-shell",
                logged=False,
                timeout=5.0,
            )

            try:
                # synchronize with the child instead of assuming it launched after a fixed sleep
                ready_deadline = time.monotonic() + 2.0
                while not ready.exists():
                    if worker.is_done():
                        worker.result()
                        raise RuntimeError("Child exited without its readiness marker")
                    if time.monotonic() >= ready_deadline:
                        raise TimeoutError("Child did not become ready within the probe budget")
                    time.sleep(0.01)

                cancelled_at = time.monotonic()
                call.cancel()
                following = executor.issue_task(lambda: time.monotonic(), name="following-read", logged=False, timeout=5.0)
                time.sleep(0.2)
                held_after_200ms = not following.is_done()
                outcome = "returned"
                child_completed = False
                try:
                    result = worker.result(timeout=6.0)
                    child_completed = "child-completed" in result.stdout
                except Exception as error:
                    outcome = type(error).__name__
                following_started = following.result(timeout=6.0)
                return {
                    "probe": "owned_shell_cancellation_and_serial_queue",
                    "python": sys.version.split()[0],
                    "platform": sys.platform,
                    "child_sleep_s": 1.5,
                    "shell_timeout_s": 3.0,
                    "cancel_after_start_s": round(cancelled_at - started, 6),
                    "following_task_delay_after_cancel_s": round(following_started - cancelled_at, 6),
                    "following_task_still_waiting_after_200ms": held_after_200ms,
                    "shell_outcome": outcome,
                    "child_completed_after_cancel": child_completed,
                }
            finally:
                # retain the temporary directory until our bounded child has actually exited
                call.cancel()
                if not worker.wait_until_finished(timeout=8.0):
                    raise RuntimeError("Probe worker exceeded cleanup budget")


if __name__ == "__main__":
    print(json.dumps([ShellCancellationProbe.run() for _ in range(3)], indent=2))
