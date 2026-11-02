import threading
import time
from concurrent.futures import CancelledError

import pytest

from serena.task_executor import TaskExecutor


@pytest.fixture
def executor():
    """
    Fixture for a basic SerenaAgent without a project
    """
    return TaskExecutor("TestExecutor")


class Task:
    def __init__(self, delay: float, exception: bool = False):
        self.delay = delay
        self.exception = exception
        self.did_run = False

    def run(self):
        self.did_run = True
        time.sleep(self.delay)
        if self.exception:
            raise ValueError("Task failed")
        return True


def test_task_executor_sequence(executor):
    """
    Tests that a sequence of tasks is executed correctly
    """
    future1 = executor.issue_task(Task(1).run, name="task1")
    future2 = executor.issue_task(Task(1).run, name="task2")
    assert future1.result() is True
    assert future2.result() is True


def test_task_executor_exception(executor):
    """
    Tests that tasks that raise exceptions are handled correctly, i.e. that
      * the exception is propagated,
      * subsequent tasks are still executed.
    """
    future1 = executor.issue_task(Task(1, exception=True).run, name="task1")
    future2 = executor.issue_task(Task(1).run, name="task2")
    have_exception = False
    try:
        assert future1.result()
    except Exception as e:
        assert isinstance(e, ValueError)
        have_exception = True
    assert have_exception
    assert future2.result() is True


def test_task_executor_cancel_current(executor):
    """
    Cancelling the public future must not allow a still-running task to overlap
    the next task; the executor remains linear until the underlying thread exits.
    """
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    second_started = threading.Event()

    def first():
        started.set()
        assert release.wait(2)
        finished.set()

    def second():
        second_started.set()
        assert finished.is_set()
        return True

    future1 = executor.issue_task(first, name="task1")
    future2 = executor.issue_task(second, name="task2")
    try:
        assert started.wait(1)
        future1.cancel()
        with pytest.raises(CancelledError):
            future1.result(timeout=1)
        assert not second_started.wait(0.05)
    finally:
        release.set()
    assert future2.result(timeout=1) is True
    assert second_started.is_set()


def test_task_executor_cancel_future(executor):
    """A queued task cancelled before dispatch is never run."""
    task1 = Task(0.2)
    task2 = Task(0.01)
    future1 = executor.issue_task(task1.run, name="task1")
    future2 = executor.issue_task(task2.run, name="task2")
    time.sleep(0.05)
    future2.cancel()
    assert future1.result(timeout=1) is True
    with pytest.raises(Exception) as exc:
        future2.result()
    assert exc.value.__class__.__name__ == "CancelledError"
    assert not task2.did_run


def test_task_executor_cancellation_via_task_info(executor):
    """Task-info cancellation keeps unfinished work visible and the queue serial."""
    first_started = threading.Event()
    first_finished = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()

    def first():
        first_started.set()
        assert release_first.wait(2)
        first_finished.set()

    def second():
        second_started.set()
        assert first_finished.is_set()
        assert release_second.wait(2)
        return True

    first_task = executor.issue_task(first, "task1")
    second_task = executor.issue_task(second, "task2")
    try:
        assert first_started.wait(1)
        task_infos = executor.get_current_tasks()
        task_infos2 = executor.get_current_tasks()

        assert len(task_infos) == 2
        assert "task1" in task_infos[0].name
        assert "task2" in task_infos[1].name
        assert task_infos[0].is_running
        assert not task_infos[1].is_running
        task_ids = [task_info.task_id for task_info in task_infos]
        assert [task_info.task_id for task_info in task_infos2] == task_ids

        task_infos[0].cancel()
        with pytest.raises(CancelledError):
            first_task.result(timeout=1)
        assert not second_started.wait(0.05)
        task_infos3 = executor.get_current_tasks()
        # Cancellation cannot hide a still-running body or dispatch its successor.
        assert [task_info.task_id for task_info in task_infos3] == task_ids
        assert task_infos3[0].is_running
        assert task_infos3[0].future.cancelled()
        assert not task_infos3[1].is_running

        release_first.set()
        assert second_started.wait(1)
        task_infos4 = executor.get_current_tasks()
        assert len(task_infos4) == 1
        assert task_infos4[0].task_id == task_ids[1]
        assert task_infos4[0].is_running
    finally:
        release_first.set()
        release_second.set()
    assert second_task.result(timeout=1) is True


def test_timed_out_task_does_not_overlap_following_task() -> None:
    executor = TaskExecutor("TimeoutSerialExecutor")
    first_started = threading.Event()
    first_finished = threading.Event()
    release = threading.Event()
    second_started = threading.Event()

    def first() -> None:
        first_started.set()
        assert release.wait(2)
        first_finished.set()

    def second() -> None:
        second_started.set()
        assert first_finished.is_set()

    first_task = executor.issue_task(first, timeout=0.03)
    second_task = executor.issue_task(second, timeout=1)

    try:
        assert first_started.wait(1)
        with pytest.raises(CancelledError):
            first_task.result(timeout=1)
        assert not second_started.is_set()
    finally:
        release.set()
    assert second_task.result(timeout=1) is None
    assert second_started.is_set()
