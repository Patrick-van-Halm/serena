"""Regression tests for the condition-based task dispatcher."""
# SPDX-License-Identifier: GPL-3.0-or-later

import threading

from serena.task_executor import TaskExecutor


def test_spurious_wakeup_does_not_dispatch_or_lose_next_task(monkeypatch):
    executor = TaskExecutor("wakeup-regression")
    condition = executor._task_executor_condition
    entered_wait = threading.Event()
    original_wait = condition.wait

    def observe_wait(timeout=None):
        entered_wait.set()
        return original_wait(timeout)

    monkeypatch.setattr(condition, "wait", observe_wait)

    # Wake an existing wait or race harmlessly with the first wait.
    with condition:
        condition.notify_all()
    assert entered_wait.wait(3)
    assert executor.get_current_tasks() == []

    # An empty notification must return to waiting, not dispatch a sentinel.
    with condition:
        entered_wait.clear()
        condition.notify_all()
    assert entered_wait.wait(3)
    assert executor.execute_task(lambda: 42, logged=False, timeout=3) == 42


def test_new_work_wakes_a_confirmed_waiter(monkeypatch):
    executor = TaskExecutor("wake-new-work")
    condition = executor._task_executor_condition
    entered_wait = threading.Event()
    original_wait = condition.wait

    def observe_wait(timeout=None):
        entered_wait.set()
        return original_wait(timeout)

    monkeypatch.setattr(condition, "wait", observe_wait)
    with condition:
        condition.notify_all()
    assert entered_wait.wait(3)

    # issue_task acquires the same lock that wait() releases atomically, so the
    # notification cannot be lost between checking the queue and going to sleep.
    assert executor.execute_task(lambda: "awake", logged=False, timeout=3) == "awake"
