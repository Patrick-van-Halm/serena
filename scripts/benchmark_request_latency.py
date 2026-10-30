"""Compare cancellation-to-next-result latency against the fixed investigation baseline."""

import json
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

from serena.task_executor import TaskExecutor
from serena.util.shell import execute_shell_command
from serena.util.tool_call import ToolCall, ToolCallInterrupted

BASELINE = "db388954b3b72d83a3411fd3d5d86b4caf883c5a"


class CancellationBenchmark:
    """Measure the serial executor, not model generation speed or synthetic HTTP delay."""

    @staticmethod
    def _load_baseline() -> ModuleType:
        source = subprocess.check_output(["git", "show", f"{BASELINE}:src/serena/util/shell.py"], text=True)
        module = ModuleType("serena_latency_baseline_shell")
        sys.modules[module.__name__] = module
        exec(compile(source, "baseline_shell.py", "exec"), module.__dict__)
        return module

    @staticmethod
    def _measure(function, marker: Path) -> float:
        source = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(1.2)"
        command = shlex.join([sys.executable, "-c", source])
        executor = TaskExecutor("latency-benchmark")
        call = ToolCall(10)
        task = executor.issue_task(lambda: call.execute(lambda: function(command, timeout=3)), logged=False)
        deadline = time.monotonic() + 5
        while not marker.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Benchmark subprocess did not start")
            time.sleep(.005)
        start = time.monotonic()
        call.cancel()
        following = executor.issue_task(time.monotonic, logged=False)
        elapsed = following.result(timeout=5) - start
        try:
            task.result(timeout=5)
        except ToolCallInterrupted:
            pass
        return elapsed

    @classmethod
    def run(cls) -> dict:
        baseline = cls._load_baseline()
        results = {}
        with tempfile.TemporaryDirectory() as directory:
            for name, function in (("baseline", baseline.execute_shell_command), ("patched", execute_shell_command)):
                samples = [cls._measure(function, Path(directory) / f"{name}-{index}") for index in range(5)]
                results[name] = {
                    "samples_ms": [round(value * 1000, 3) for value in samples],
                    "median_ms": round(statistics.median(samples) * 1000, 3),
                    "max_ms": round(max(samples) * 1000, 3),
                }
        return {"baseline_commit": BASELINE, "metric": "cancel_to_next_executor_result", "runs_per_version": 5, "results": results}


if __name__ == "__main__":
    print(json.dumps(CancellationBenchmark.run(), indent=2))
