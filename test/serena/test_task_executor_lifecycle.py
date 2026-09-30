# SPDX-License-Identifier: GPL-3.0-or-later

import gc
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from serena.task_executor import TaskExecutor


class ExecutorOwner:
    """Owner with the same bound-callback reference cycle as a Serena agent."""

    def __init__(self) -> None:
        self.completed = threading.Event()
        self.executor = TaskExecutor("LifecycleTestExecutor", self.on_completion)

    def on_completion(self) -> None:
        self.completed.set()


@pytest.mark.parametrize("run_task", [False, True])
def test_unused_executor_owner_can_be_collected(run_task: bool) -> None:
    """An unused or drained executor must allow an evicted runtime owner to be reclaimed."""
    owner = ExecutorOwner()
    if run_task:
        assert owner.executor.execute_task(lambda: 42, timeout=2) == 42
        assert owner.completed.wait(timeout=2)

    # release the last external reference, leaving only the executor/callback cycle
    reference = weakref.ref(owner)
    del owner
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        gc.collect()
        if reference() is None:
            break
        time.sleep(0.01)
    assert reference() is None


def test_executor_preserves_fifo_across_idle_periods() -> None:
    executor = TaskExecutor("BurstyTestExecutor")
    observed: list[int] = []

    # alternate queued bursts with idle periods to exercise dispatcher retirement and reuse
    for burst in range(10):
        tasks = [
            executor.issue_task(lambda value=value: observed.append(value), logged=False)
            for value in range(burst * 5, burst * 5 + 5)
        ]
        for task in tasks:
            task.result(timeout=2)
        time.sleep(0.005)
    assert observed == list(range(50))


def test_concurrent_submitters_execute_every_task_once_without_overlap() -> None:
    executor = TaskExecutor("ConcurrentTestExecutor")
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    observed: list[int] = []

    def work(value: int) -> int:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.001)
        with state_lock:
            observed.append(value)
            active -= 1
        return value

    def submit(value: int) -> int:
        return executor.execute_task(lambda: work(value), logged=False, timeout=5)

    with ThreadPoolExecutor(max_workers=8) as submitters:
        results = list(submitters.map(submit, range(100)))
    assert results == list(range(100))
    assert sorted(observed) == list(range(100))
    assert max_active == 1


def test_completion_callback_can_schedule_followup_work() -> None:
    completed = threading.Event()
    observed: list[int] = []

    def on_completion() -> None:
        if observed == [1]:
            executor.issue_task(lambda: observed.append(2), logged=False)
        else:
            completed.set()

    executor = TaskExecutor("CallbackTestExecutor", on_completion)
    executor.issue_task(lambda: observed.append(1), logged=False).result(timeout=2)
    assert completed.wait(timeout=2)
    assert observed == [1, 2]
