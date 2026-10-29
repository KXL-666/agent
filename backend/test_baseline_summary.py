"""Benchmark summaries must not confuse completion with acceptance or missing costs with zero."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("baseline_summary", Path(__file__).resolve().parents[1] / "scripts/summarize-quality-baseline.py")
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


class BaselineSummaryTest(unittest.TestCase):
    def test_unverified_and_missing_costs_are_explicit(self):
        runs = [dict(sprint_id="one", case="W1", execution_style="quick", sprint_status="completed", input_sha256="same",
                     duration_s=10, stored_usage={"estimated_cost_yuan": 0.1},
                     artifacts=[{"kind": "final", "quality_status": "passed", "file_sha256": "abc"}]),
                dict(sprint_id="two", case="W1", execution_style="collaborative", sprint_status="completed", input_sha256="same",
                     artifacts=[{"kind": "final", "quality_status": "unchecked"}])]
        result = summary.summarize(runs)
        self.assertEqual(result["comparable_cases"], ["W1"])
        self.assertEqual(result["groups"]["quick"]["pass_rate"], 1)
        self.assertEqual(result["groups"]["collaborative"]["pass_rate"], 0)
        self.assertIsNone(result["groups"]["collaborative"]["mean_estimated_cost_yuan"])
        self.assertEqual(result["groups"]["collaborative"]["missing_cost_runs"], 1)

    def test_changed_inputs_and_duplicate_runs_cannot_support_comparison(self):
        runs = [dict(sprint_id="one", case="W1", input_sha256="a", execution_style="quick"),
                dict(sprint_id="two", case="W1", input_sha256="b", execution_style="collaborative")]
        self.assertEqual(summary.summarize(runs)["comparable_cases"], [])
        with self.assertRaises(ValueError):
            summary.summarize([runs[0], runs[0]])

    def test_passed_label_requires_actual_file_hash_and_completed_sprint(self):
        run = dict(sprint_status="completed", artifacts=[{"kind": "final", "quality_status": "passed"}])
        self.assertEqual(summary.outcome(run), "unchecked")
        self.assertEqual(summary.outcome({**run, "timed_out": True}), "failed")
