import time

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
    task1 = Task(0.25)
    task2 = Task(0.01)
    future1 = executor.issue_task(task1.run, name="task1")
    future2 = executor.issue_task(task2.run, name="task2")
    time.sleep(0.05)
    future1.cancel()

    with pytest.raises(Exception) as exc:
        future1.result()
    assert exc.value.__class__.__name__ == "CancelledError"

    time.sleep(0.05)
    assert not task2.did_run
    assert future2.result(timeout=1) is True
    assert task2.did_run


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
    first = Task(0.2)
    second = Task(0.01)
    executor.issue_task(first.run, "task1")
    executor.issue_task(second.run, "task2")
    time.sleep(0.03)
    task_infos = executor.get_current_tasks()
    task_infos2 = executor.get_current_tasks()

    assert len(task_infos) == 2
    assert "task1" in task_infos[0].name
    assert "task2" in task_infos[1].name
    assert task_infos2[0].task_id == task_infos[0].task_id

    task_infos[0].cancel()
    time.sleep(0.05)
    task_infos3 = executor.get_current_tasks()
    # The cancelled future remains the current underlying execution until its
    # thread exits, so it cannot become a detached concurrent task.
    assert len(task_infos3) == 2
    assert task_infos3[0].future.cancelled()

    time.sleep(0.25)
    assert second.did_run



def test_timed_out_task_does_not_overlap_following_task() -> None:
    executor = TaskExecutor("TimeoutSerialExecutor")
    state = {"first_started": False, "first_finished": False, "overlap": False}

    def first() -> None:
        state["first_started"] = True
        time.sleep(0.15)
        state["first_finished"] = True

    def second() -> None:
        state["overlap"] = state["first_started"] and not state["first_finished"]

    first_task = executor.issue_task(first, timeout=0.03)
    second_task = executor.issue_task(second, timeout=1)

    with pytest.raises(Exception):
        first_task.result(timeout=0.05)
    assert second_task.result(timeout=1) is None
    assert state["overlap"] is False
