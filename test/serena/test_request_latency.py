"""Behavioral coverage of shell cancellation, bounded results, and request timing."""

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import psutil
import pytest
import requests
from anyio.to_thread import run_sync
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from serena.repl.api.shell_api import ShellCommandOutput, ShellCommandOutputRenderer
from serena.task_executor import TaskExecutor
from serena.util.shell import ShellCommandResult, execute_shell_command
from serena.util.tool_call import AsyncToolCallRunner, ToolCall, ToolCallContext, ToolCallInterrupted

from .test_mcp_sdk_v2 import BOOTSTRAP, isolated_project as isolated_project, text_of


def _command(source: str) -> str:
    return shlex.join([sys.executable, "-c", source])


def _await_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Child did not signal readiness")
        time.sleep(0.01)


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell quoting and process-group regression")
def test_cancelled_shell_releases_serial_executor_and_stops_only_owned_tree(tmp_path: Path) -> None:
    marker = tmp_path / "started.json"
    source = (
        "import json,os,subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid])); time.sleep(30)"
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    executor = TaskExecutor("latency-test")
    call = ToolCall(20)
    task = executor.issue_task(lambda: call.execute(lambda: execute_shell_command(_command(source), timeout=20)), logged=False)
    try:
        _await_file(marker)
        call.cancel()
        next_task = executor.issue_task(lambda: "queue available", logged=False)
        with pytest.raises(ToolCallInterrupted):
            task.result(timeout=4)
        assert next_task.result(timeout=4) == "queue available"
        for pid in json.loads(marker.read_text()):
            assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        assert unrelated.poll() is None
    finally:
        call.cancel()
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell quoting")
def test_shell_inherits_remaining_deadline() -> None:
    call = ToolCall(0.5)
    time.sleep(0.25)
    started = time.monotonic()
    with pytest.raises(ToolCallInterrupted), ToolCallContext(call).activate():
        execute_shell_command(_command("import time; time.sleep(10)"), timeout=10)
    assert time.monotonic() - started < 3


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell quoting")
def test_cancelled_shell_never_starts(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    call = ToolCall(10)
    call.cancel()
    with pytest.raises(ToolCallInterrupted), ToolCallContext(call).activate():
        execute_shell_command(_command(f"from pathlib import Path; Path({str(marker)!r}).touch()"))
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell quoting")
def test_polled_shell_preserves_all_output_and_exit_status() -> None:
    source = "import sys,time; print('before',flush=True); time.sleep(.3); print('after'); print('error',file=sys.stderr); sys.exit(7)"
    with ToolCallContext(ToolCall(5)).activate():
        result = execute_shell_command(_command(source), capture_stderr=True)
    assert result.stdout == "before\nafter\n" and result.stderr == "error\n" and result.return_code == 7


@pytest.mark.parametrize("limit", [300, 500, 1000])
@pytest.mark.parametrize("stderr", [None, "warning\n" * 1000 + "FINAL ERROR\n"])
def test_oversized_shell_output_retains_status_and_final_summary(limit: int, stderr: str | None) -> None:
    result = ShellCommandResult(stdout="progress\n" * 1000 + "33 tests passed\n", stderr=stderr, return_code=124, cwd="/project")
    agent = SimpleNamespace(serena_config=SimpleNamespace(default_max_tool_answer_chars=limit))
    rendered = ShellCommandOutput(result, ShellCommandOutputRenderer(agent)).represent()
    payload = json.loads(rendered)
    assert len(rendered) <= limit
    assert payload["return_code"] == 124 and payload["cwd"] == "/project" and payload["output_truncated"] is True
    assert payload["stdout_original_chars"] == len(result.stdout)
    assert payload["stdout"].endswith("33 tests passed\n")
    if stderr is not None:
        assert payload["stderr"].endswith("FINAL ERROR\n")
    else:
        assert payload["stderr"] is None
    assert result.stdout.endswith("33 tests passed\n") and len(result.stdout) > limit


def test_small_shell_output_is_unchanged() -> None:
    result = ShellCommandResult(stdout="ok", return_code=0, cwd="/project")
    agent = SimpleNamespace(serena_config=SimpleNamespace(default_max_tool_answer_chars=1000))
    assert ShellCommandOutput(result, ShellCommandOutputRenderer(agent)).represent() == result.model_dump_json()


def test_timing_records_distinguish_waiting_execution_and_response_without_payloads(monkeypatch, caplog) -> None:
    monkeypatch.setenv("SERENA_TRACE_TOOL_LATENCY", "1")
    guard = threading.Lock()
    guard.acquire()
    caplog.set_level(logging.INFO, logger="serena.util.tool_call")

    async def exercise() -> None:
        async def invoke() -> None:
            def operation() -> str:
                call = ToolCallContext.current().call
                call.trace("tool_received", tool_name="test_read", session_id="test-session")
                return call.execute(lambda: "private-output", guard)
            assert await AsyncToolCallRunner.run(operation, timeout=5) == "private-output"
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(invoke)
            await anyio.sleep(0.15)
            guard.release()

    anyio.run(exercise)
    records = [json.loads(r.getMessage().split("Tool latency: ", 1)[1]) for r in caplog.records if r.getMessage().startswith("Tool latency: ")]
    by_event = {record["event"]: record for record in records}
    assert {"mcp_received", "worker_admitted", "tool_received", "executor_dequeued", "execution_started", "execution_finished", "mcp_finished"} <= by_event.keys()
    assert by_event["execution_started"]["elapsed_ms"] > by_event["executor_dequeued"]["elapsed_ms"]
    assert len({record["request_id"] for record in records}) == 1
    assert "private-output" not in json.dumps(records)


@pytest.mark.parametrize("bridge", [False, True], ids=["direct", "bridge"])
@pytest.mark.skipif(os.name != "posix", reason="POSIX shell quoting")
def test_stdio_cancellation_allows_next_read(isolated_project, bridge: bool) -> None:
    fixture = isolated_project
    marker = fixture.project / "command-started"
    daemon = None
    daemon_log = (fixture.home / "latency-daemon.log").open("w+", encoding="utf-8")
    try:
        if bridge:
            daemon = subprocess.Popen([sys.executable, "-c", BOOTSTRAP, "start-project-server"], env=fixture.environment,
                                      stdin=subprocess.DEVNULL, stdout=daemon_log, stderr=daemon_log)
            deadline = time.monotonic() + 20
            while True:
                try:
                    response = requests.get(f"http://127.0.0.1:{fixture.port}/heartbeat", headers={"Authorization": "Bearer sdk-integration-test-secret"}, timeout=.2)
                    if response.ok:
                        break
                except requests.RequestException:
                    pass
                if daemon.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("Owned test daemon failed to start")
                time.sleep(.05)
        command = "start-mcp-bridge" if bridge else "start-mcp-server"
        params = StdioServerParameters(command=sys.executable, args=["-c", BOOTSTRAP, command, "--project", str(fixture.project), "--context", "codex"], env=fixture.environment)
        source = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(10)"

        async def exercise() -> None:
            with anyio.fail_after(30):
                async with Client(params, mode="legacy", read_timeout_seconds=20) as client:
                    scope = anyio.CancelScope()
                    async def slow() -> None:
                        with scope:
                            await client.call_tool("execute_shell_command", {"command": _command(source)})
                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(slow)
                        await run_sync(_await_file, marker)
                        scope.cancel()
                    with anyio.fail_after(4):
                        result = await client.call_tool("read_file", {"relative_path": "hello.txt"})
                    assert not result.is_error and "SDK migration works" in text_of(result)
        anyio.run(exercise)
    finally:
        if daemon is not None:
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=5)
        daemon_log.close()
