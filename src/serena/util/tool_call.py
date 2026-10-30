# SPDX-License-Identifier: GPL-3.0-or-later

"""End-to-end tool-call deadlines, cooperative cancellation, and bounded async dispatch."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError, Future
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from queue import Empty, Queue
from typing import ClassVar, Protocol, TypeVar
from uuid import uuid4

import anyio
from anyio.lowlevel import RunVar
from anyio.to_thread import run_sync

log = logging.getLogger(__name__)
T = TypeVar("T")


class ToolCallInterrupted(TimeoutError):
    """A call whose deadline expired or whose caller requested cancellation."""


class ToolCallBusyError(RuntimeError):
    """An operation rejected before submission because execution capacity is exhausted."""


class ToolCallLock(Protocol):
    """Lock operations used by deadline-aware project guards."""

    def acquire(self, *, timeout: float) -> bool: ...

    def release(self) -> None: ...


class ToolCall:
    """One deadline and cancellation state shared by a call's waiters and worker.

    Cancellation prevents pending work from starting. Running work is only cancelled
    cooperatively; its outcome must be checked before an edit is retried.
    """

    RESPONSE_GRACE_SECONDS = 5.0

    _POLL_SECONDS = 0.05

    def __init__(self, timeout: float, request_id: str | None = None) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Tool-call timeout must be finite and non-negative")
        self.request_id = request_id or uuid4().hex
        self._deadline = time.monotonic() + timeout
        self._lock = threading.Lock()
        self._cancelled = False
        self._started = False
        self._finished = False
        self._succeeded = False

    @property
    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    @property
    def overdue_seconds(self) -> float:
        """Elapsed time beyond the immutable deadline, independently of cancellation."""
        return max(0.0, time.monotonic() - self._deadline)

    @property
    def interrupted(self) -> bool:
        with self._lock:
            return self._cancelled or self.remaining == 0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._started and not self._finished

    @property
    def outcome(self) -> str:
        with self._lock:
            if not self._started:
                return "not_started"
            if self._succeeded:
                return "succeeded"
            return "unknown"

    def cancel(self) -> None:
        """Prevent pending execution and signal running work to stop at its next checkpoint."""
        with self._lock:
            self._cancelled = True

    def _check_locked(self) -> None:
        if self._cancelled or self.remaining == 0:
            reason = "deadline exceeded" if self.remaining == 0 else "cancelled"
            outcome = "not_started" if not self._started else "unknown"
            detail = "The operation did not start." if not self._started else "The outcome is unknown; verify changes before retrying."
            raise ToolCallInterrupted(f"Tool call {self.request_id}: {reason}; outcome={outcome}. {detail}")

    def check(self) -> None:
        """Raise when this call may no longer start or continue cooperative work."""
        with self._lock:
            self._check_locked()

    def mark_dispatched(self) -> None:
        """Record possible remote execution before sending a non-retriable request."""
        with self._lock:
            self._check_locked()
            self._started = True

    def execute(self, function: Callable[[], T], guard: AbstractContextManager[None] | None = None) -> T:
        """Run admitted work once, retaining its guard until the actual worker exits."""
        # serialize admission with cancellation before taking ownership of completion
        with self._lock:
            self._check_locked()
            if self._started or self._finished:
                raise RuntimeError(f"Tool call {self.request_id} was already dispatched")
            self._started = True

        try:
            # expose the call to cooperative downstream operations without inheriting its guard
            with ToolCallContext(self).activate():
                with guard if guard is not None else nullcontext():
                    self.check()
                    result = function()
            with self._lock:
                self._succeeded = True
            return result
        finally:
            with self._lock:
                self._finished = True

    def wait(self, future: Future[T]) -> T:
        """Wait within the original deadline, cancelling queued work on interruption."""
        try:
            while True:
                self.check()
                try:
                    return future.result(timeout=min(self._POLL_SECONDS, self.remaining))
                except TimeoutError:
                    # propagate a task's own TimeoutError instead of treating it as a polling timeout
                    if future.done():
                        return future.result()
                except CancelledError:
                    self.cancel()
                    self.check()
        except ToolCallInterrupted:
            self.cancel()
            future.cancel()
            raise

    def wait_queue(self, queue: Queue[T], timeout: float | None = None) -> T:
        """Wait for a downstream response within both its timeout and this call's deadline."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self.check()
            remaining = self.remaining
            if deadline is not None:
                remaining = min(remaining, max(0.0, deadline - time.monotonic()))
            try:
                return queue.get(timeout=min(self._POLL_SECONDS, remaining))
            except Empty:
                if deadline is not None and time.monotonic() >= deadline:
                    raise

    def acquire(self, lock: ToolCallLock) -> None:
        """Acquire a lock without waiting past cancellation or the call's deadline."""
        while True:
            self.check()
            if lock.acquire(timeout=min(self._POLL_SECONDS, self.remaining)):
                try:
                    self.check()
                except BaseException:
                    lock.release()
                    raise
                return


@dataclass(frozen=True)
class ToolCallContext:
    """Call state and optional project guard carried to the actual executor task."""

    call: ToolCall
    guard: AbstractContextManager[None] | None = None

    _current: ClassVar[ContextVar["ToolCallContext | None"]] = ContextVar("serena_tool_call", default=None)

    @classmethod
    def current(cls) -> "ToolCallContext | None":
        return cls._current.get()

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = self._current.set(self)
        try:
            yield
        finally:
            self._current.reset(token)

    def execute(self, function: Callable[[], T]) -> T:
        return self.call.execute(function, self.guard)


class ToolCallCancellationHandler(Protocol):
    """Transport-specific delivery of cancellation for a dispatched tool call."""

    def cancel_tool_call(self, call: ToolCall) -> None: ...


class AsyncToolCallRunner:
    """Bounded blocking-call adapter that keeps MCP request handling responsive.

    The thread-side semaphore stays held even after async cancellation abandons the
    waiter. An AnyIO limiter alone releases its slot on cancellation and would not
    bound the number of still-running blocking calls.
    """

    _slots = threading.BoundedSemaphore(16)
    _cancellation_slots = threading.BoundedSemaphore(4)
    _cancellation_limiter: RunVar[anyio.CapacityLimiter] = RunVar("serena_cancellation_limiter")

    @classmethod
    async def run(
        cls,
        function: Callable[[], T],
        timeout: float,  # noqa: ASYNC109 - shared remote execution deadline, also enforced by fail_after below
        cancellation_handler: ToolCallCancellationHandler | None = None,
        *,
        response_grace: float = 0.0,
    ) -> T:
        if not math.isfinite(response_grace) or response_grace < 0:
            raise ValueError("Response grace must be finite and non-negative")
        call = ToolCall(timeout)

        def invoke() -> T:
            if not cls._slots.acquire(blocking=False):
                raise ToolCallBusyError("Serena is busy; the operation was not submitted. Reduce concurrent tool calls.")
            try:
                call.check()
                with ToolCallContext(call).activate():
                    return function()
            finally:
                cls._slots.release()

        try:
            # include worker-pool admission in the same deadline as remote queueing and execution
            with anyio.fail_after(call.remaining + response_grace):
                return await run_sync(invoke, abandon_on_cancel=True)
        except anyio.get_cancelled_exc_class():
            call.cancel()
            if cancellation_handler is not None and call.outcome != "not_started":
                await cls._cancel_remote(cancellation_handler, call)
            raise
        except TimeoutError:
            if not call.interrupted:
                raise
            call.cancel()
            if cancellation_handler is not None and call.outcome != "not_started":
                await cls._cancel_remote(cancellation_handler, call)
            call.check()
            raise

    @classmethod
    async def _cancel_remote(cls, handler: ToolCallCancellationHandler, call: ToolCall) -> None:
        def notify() -> None:
            if not cls._cancellation_slots.acquire(blocking=False):
                log.warning("Cancellation delivery capacity exhausted for tool call %s; outcome unknown", call.request_id)
                return
            try:
                handler.cancel_tool_call(call)
            except Exception:
                log.warning("Could not deliver cancellation for tool call %s; outcome unknown", call.request_id, exc_info=True)
            finally:
                cls._cancellation_slots.release()

        # share a separate per-event-loop limiter so cancellation bursts queue instead of being dropped
        try:
            limiter = cls._cancellation_limiter.get()
        except LookupError:
            limiter = anyio.CapacityLimiter(4)
            cls._cancellation_limiter.set(limiter)
        with anyio.move_on_after(2.0, shield=True) as scope:
            await run_sync(notify, abandon_on_cancel=True, limiter=limiter)
        if scope.cancel_called:
            log.warning("Cancellation delivery timed out for tool call %s; outcome unknown", call.request_id)
