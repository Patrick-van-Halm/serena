# SPDX-License-Identifier: GPL-3.0-or-later

import gc
import threading
import weakref
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import anyio
import pytest

from serena.shared_mcp_calls import DuplicateToolCallError, SharedMCPCalls
from serena.task_executor import TaskExecutor
from serena.util.tool_call import AsyncToolCallRunner, ToolCall, ToolCallBusyError, ToolCallContext, ToolCallInterrupted


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("cancel", [False, True])
def test_expired_or_cancelled_queued_work_never_starts(cancel: bool) -> None:
    executor = TaskExecutor("DeadlineTest")
    entered = threading.Event()
    release = threading.Event()
    effects: list[str] = []

    def blocker() -> None:
        entered.set()
        assert release.wait(3)

    first = executor.issue_task(blocker, logged=False)
    assert entered.wait(2)
    call = ToolCall(2 if cancel else 0.05)
    pending = executor.issue_task(lambda: call.execute(lambda: effects.append("edit")), logged=False)
    try:
        if cancel:
            call.cancel()
        with pytest.raises(ToolCallInterrupted, match="outcome=not_started"):
            call.wait(pending.future)
    finally:
        release.set()
        first.result(timeout=2)
    executor.execute_task(lambda: effects.append("barrier"), logged=False, timeout=2)
    assert effects == ["barrier"]


def test_cancelled_running_work_keeps_its_execution_guard_until_it_finishes() -> None:
    acquired = threading.Event()
    released = threading.Event()
    finish = threading.Event()
    call = ToolCall(2)

    @contextmanager
    def guard():
        acquired.set()
        try:
            yield
        finally:
            released.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(call.execute, lambda: finish.wait(2), guard())
        try:
            assert acquired.wait(1)
            call.cancel()
            with pytest.raises(ToolCallInterrupted, match="outcome=unknown"):
                call.wait(worker)
            assert not released.is_set()
        finally:
            finish.set()
        assert worker.result(timeout=1)
    assert released.is_set()


@pytest.mark.anyio
async def test_blocking_tool_leaves_event_loop_responsive() -> None:
    entered = threading.Event()
    release = threading.Event()
    results = []

    def operation():
        entered.set()
        assert release.wait(2)
        return "done"

    async def invoke():
        results.append(await AsyncToolCallRunner.run(operation, 3))

    async with anyio.create_task_group() as group:
        group.start_soon(invoke)
        try:
            with anyio.fail_after(1):
                while not entered.is_set():
                    await anyio.sleep(0.001)
            # this coroutine must run while the blocking operation is still waiting
            assert results == []
        finally:
            release.set()
    assert results == ["done"]


@pytest.mark.anyio
async def test_host_cancellation_is_delivered_while_worker_is_blocked() -> None:
    entered = threading.Event()
    release = threading.Event()
    delivered = threading.Event()
    received: list[str] = []

    class Canceller:
        def cancel_tool_call(self, call: ToolCall) -> None:
            received.append(call.request_id)
            delivered.set()

    def operation():
        context = ToolCallContext.current()
        assert context is not None
        context.call.mark_dispatched()
        received.append(context.call.request_id)
        entered.set()
        assert release.wait(3)

    async def invoke():
        await AsyncToolCallRunner.run(operation, 4, Canceller())

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            with anyio.fail_after(1):
                while not entered.is_set():
                    await anyio.sleep(0.001)
            group.cancel_scope.cancel()
        assert delivered.is_set()
        assert len(received) == 2 and received[0] == received[1]
    finally:
        release.set()


@pytest.mark.anyio
async def test_async_deadline_reports_an_explicit_outcome() -> None:
    release = threading.Event()
    try:
        with pytest.raises(ToolCallInterrupted, match="deadline exceeded"):
            await AsyncToolCallRunner.run(lambda: release.wait(2), 0.05)
    finally:
        release.set()


def test_task_exception_is_not_confused_with_wait_timeout() -> None:
    call = ToolCall(1)

    def fail():
        raise TimeoutError("downstream timeout")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call.execute, fail)
        with pytest.raises(TimeoutError, match="downstream timeout"):
            call.wait(future)


def test_cancellation_before_dispatch_prevents_execution_and_is_session_scoped() -> None:
    calls = SharedMCPCalls(1)
    assert calls.cancel("session-a", "request") == "not_started"
    with pytest.raises(ToolCallInterrupted, match="not_started"):
        calls.register("session-a", "request", time.time() + 1)
    call = calls.register("session-b", "request", time.time() + 1)
    assert call.execute(lambda: "other-session") == "other-session"


def test_duplicate_operation_does_not_replay_an_edit() -> None:
    calls = SharedMCPCalls(1)
    effects: list[str] = []
    call = calls.register("chat", "request", time.time() + 1)
    call.execute(lambda: effects.append("edit"))
    with pytest.raises(DuplicateToolCallError, match="Not replaying"):
        calls.register("chat", "request", time.time() + 1)
    assert effects == ["edit"]


def test_past_wire_deadline_is_rejected_before_admission() -> None:
    with pytest.raises(ToolCallInterrupted, match="not_started"):
        SharedMCPCalls(1).register("chat", "request", time.time() - 1)


def test_call_capacity_is_bounded_without_discarding_cancellation_records() -> None:
    calls = SharedMCPCalls(1, capacity=1)
    call = calls.register("chat", "first", time.time() + 1)
    with pytest.raises(ToolCallBusyError, match="not submitted"):
        calls.register("chat", "second", time.time() + 1)
    calls.cancel("chat", "first")
    with pytest.raises(ToolCallInterrupted, match="not_started"):
        call.execute(lambda: None)


def test_duplicate_execution_does_not_release_the_running_call() -> None:
    call = ToolCall(2)
    entered = threading.Event()
    release = threading.Event()

    def first():
        entered.set()
        assert release.wait(2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(call.execute, first)
        try:
            assert entered.wait(1)
            with pytest.raises(RuntimeError, match="already dispatched"):
                call.execute(lambda: None)
            assert call.running
        finally:
            release.set()
        worker.result(timeout=1)
    assert not call.running


def test_cancelled_queued_task_releases_captured_state_while_worker_is_still_blocked() -> None:
    class Payload:
        pass

    executor = TaskExecutor("CancelledQueueCleanup")
    entered = threading.Event()
    release = threading.Event()

    def first():
        entered.set()
        assert release.wait(3)

    blocker = executor.issue_task(first, logged=False)
    assert entered.wait(1)
    payload = Payload()
    reference = weakref.ref(payload)
    pending = executor.issue_task(lambda captured=payload: captured, logged=False)
    del payload
    try:
        pending.cancel()
        gc.collect()
        assert reference() is None
    finally:
        release.set()
        blocker.result(timeout=1)
    assert executor.execute_task(lambda: "recovered", logged=False, timeout=1) == "recovered"


@pytest.mark.anyio
async def test_cancellation_burst_is_delivered_without_dropping_requests() -> None:
    release = threading.Event()
    lock = threading.Lock()
    started: set[str] = set()
    delivered: set[str] = set()

    class Canceller:
        def cancel_tool_call(self, call: ToolCall) -> None:
            # keep delivery busy long enough for concurrent cancellations to contend
            time.sleep(0.02)
            with lock:
                delivered.add(call.request_id)

    def operation():
        context = ToolCallContext.current()
        assert context is not None
        context.call.mark_dispatched()
        with lock:
            started.add(context.call.request_id)
        assert release.wait(3)

    async def invoke():
        await AsyncToolCallRunner.run(operation, 4, Canceller())

    try:
        async with anyio.create_task_group() as group:
            for _ in range(8):
                group.start_soon(invoke)
            with anyio.fail_after(1):
                while True:
                    with lock:
                        if len(started) == 8:
                            break
                    await anyio.sleep(0.001)
            group.cancel_scope.cancel()
        assert delivered == started
    finally:
        release.set()
