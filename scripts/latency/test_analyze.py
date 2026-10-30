"""Observable guarantees for the offline latency report."""

import json
import unittest
from pathlib import Path

from analyze import LatencyReport, ObservedCall


class LatencyReportTest(unittest.TestCase):
    def test_overlap_is_not_double_counted(self) -> None:
        calls = [ObservedCall("a", "read", 0, 10), ObservedCall("a", "read", 2, 3), ObservedCall("a", "read", 9, 4)]
        summary = LatencyReport(calls).summarize()["sessions"]["a"]
        self.assertEqual(summary["duration_sum_s"], 17)
        self.assertEqual(summary["covered_execution_s"], 13)
        self.assertEqual(summary["unattributed_window_s"], 0)

    def test_missing_calls_and_separate_sessions_remain_distinct(self) -> None:
        calls = [
            ObservedCall("a", "shell", 0, 1),
            ObservedCall("a", "read", 5, 1),
            ObservedCall("a", "search", None, None, "not_started"),
            ObservedCall("b", "read", 100, 2),
        ]
        report = LatencyReport(calls).summarize()
        summary = report["sessions"]["a"]
        self.assertEqual(summary["unattributed_window_s"], 4)
        self.assertEqual(summary["calls_without_duration"], 1)
        self.assertEqual(summary["untimed_outcomes"], [{"tool": "search", "outcome": "not_started"}])
        self.assertEqual(report["sessions"]["b"]["observed_window_s"], 2)
        self.assertIsNone(report["model_tokens_per_second"])
        self.assertIsNone(report["user_request_to_final_result_s"])

    def test_unknown_and_zero_are_different(self) -> None:
        calls = [ObservedCall("unknown", "read", None, None), ObservedCall("zero", "read", 0, 0)]
        summary = LatencyReport(calls).summarize()["sessions"]
        self.assertIsNone(summary["unknown"]["duration_sum_s"])
        self.assertIsNone(summary["unknown"]["observed_window_s"])
        self.assertEqual(summary["zero"]["duration_sum_s"], 0)
        self.assertEqual(summary["zero"]["observed_window_s"], 0)

    def test_invalid_times_are_rejected(self) -> None:
        for value in (True, -1, float("inf"), float("nan"), "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ObservedCall("a", "read", value, 1)

    def test_supplied_observations_reproduce_report(self) -> None:
        root = Path(__file__).resolve().parents[2]
        data = json.loads((root / "docs/performance/request_latency_observations.json").read_text(encoding="utf-8"))
        report = LatencyReport([ObservedCall(**row) for row in data["calls"]]).summarize()["sessions"]
        self.assertEqual(report["primary"]["timed_calls"], 27)
        self.assertEqual(report["primary"]["calls_without_duration"], 3)
        self.assertAlmostEqual(report["primary"]["duration_sum_s"], 719.256)
        self.assertAlmostEqual(report["primary"]["unattributed_window_s"], 836.234)
        self.assertEqual(report["verification"]["timed_calls"], 18)
        self.assertAlmostEqual(report["verification"]["duration_sum_s"], 36.143)
        self.assertAlmostEqual(report["verification"]["unattributed_window_s"], 101.291)


if __name__ == "__main__":
    unittest.main()
