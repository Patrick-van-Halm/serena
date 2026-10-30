"""Analyze observed tool durations without mistaking unobserved time for model inference."""

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any


@dataclass(frozen=True)
class ObservedCall:
    """One reported call; missing timing remains unknown rather than becoming zero."""

    session: str
    tool: str
    start_s: float | None
    duration_s: float | None
    outcome: str = "recorded"

    def __post_init__(self) -> None:
        if not isinstance(self.session, str) or not self.session:
            raise ValueError("session must be a non-empty string")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError("tool must be a non-empty string")
        for name in ("start_s", "duration_s"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0
            ):
                raise ValueError(f"{name} must be finite, non-negative seconds or null")
        if self.start_s is not None and self.duration_s is not None and not math.isfinite(self.start_s + self.duration_s):
            raise ValueError("interval endpoint must be finite")


class LatencyReport:
    """Separate recorded duration totals from interval coverage and missing observations."""

    def __init__(self, calls: list[ObservedCall]) -> None:
        self._calls = tuple(calls)

    @staticmethod
    def _union_duration(intervals: list[tuple[float, float]]) -> float:
        # merge intersecting intervals instead of counting parallel work twice
        total = 0.0
        end: float | None = None
        for start, stop in sorted(intervals):
            total += stop - start if end is None else max(0.0, stop - max(start, end))
            end = stop if end is None else max(end, stop)
        return total

    @classmethod
    def _session_summary(cls, calls: list[ObservedCall]) -> dict[str, Any]:
        # preserve untimed calls separately from measured executions
        durations = [call.duration_s for call in calls if call.duration_s is not None]
        intervals = [
            (call.start_s, call.start_s + call.duration_s)
            for call in calls
            if call.start_s is not None and call.duration_s is not None
        ]
        by_tool: dict[str, list[float]] = defaultdict(list)
        for call in calls:
            if call.duration_s is not None:
                by_tool[call.tool].append(call.duration_s)

        # an execution envelope is not the user's request-to-final-result duration
        start = min((span[0] for span in intervals), default=None)
        end = max((span[1] for span in intervals), default=None)
        window = end - start if start is not None and end is not None else None
        covered = cls._union_duration(intervals) if intervals else None
        uncovered = max(0.0, window - covered) if window is not None and covered is not None else None

        return {
            "reported_calls": len(calls),
            "timed_calls": len(durations),
            "calls_without_duration": len(calls) - len(durations),
            "calls_without_complete_interval": len(calls) - len(intervals),
            "duration_sum_s": round(sum(durations), 6) if durations else None,
            "duration_median_s": median(durations) if durations else None,
            "subsecond_calls": sum(duration < 1 for duration in durations),
            "observed_start_s": start,
            "observed_end_s": end,
            "observed_window_s": round(window, 6) if window is not None else None,
            "covered_execution_s": round(covered, 6) if covered is not None else None,
            "unattributed_window_s": round(uncovered, 6) if uncovered is not None else None,
            "by_tool": {
                name: {"timed_calls": len(values), "duration_sum_s": round(sum(values), 6)}
                for name, values in sorted(by_tool.items())
            },
            "untimed_outcomes": [
                {"tool": call.tool, "outcome": call.outcome}
                for call in calls
                if call.start_s is None or call.duration_s is None
            ],
        }

    def summarize(self) -> dict[str, Any]:
        # do not combine distinct sessions into a fictitious continuous agent request
        sessions: dict[str, list[ObservedCall]] = defaultdict(list)
        for call in self._calls:
            sessions[call.session].append(call)
        return {
            "sessions": {name: self._session_summary(calls) for name, calls in sorted(sessions.items())},
            "model_tokens_per_second": None,
            "user_request_to_final_result_s": None,
            "limitations": [
                "Coverage assumes start_s denotes execution start on one consistent clock per session.",
                "Timestamp precision and duration scope come from the input; overlap can be a rounding artifact.",
                "Unattributed time may include model work, approvals, queueing, missing calls, or other activity.",
                "Tool execution can continue after the caller has already timed out or cancelled.",
                "No provider token counts, token timestamps, user submission, or final-result timestamp are inferred.",
            ],
        }


def main() -> None:
    """Read an observation JSON file and emit a reproducible, execution-only report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.observations.read_text(encoding="utf-8"))
        calls = [ObservedCall(**record) for record in data["calls"]]
        report = LatencyReport(calls).summarize()
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
