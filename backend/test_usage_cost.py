"""Cost estimates must accumulate at the model used for each call."""

import copy
import unittest
from unittest.mock import patch

import llm


class UsageCostTest(unittest.TestCase):
    def setUp(self):
        self.previous = copy.deepcopy(llm._usage)
        llm._usage.clear()

    def tearDown(self):
        llm._usage.clear()
        llm._usage.update(self.previous)

    def test_switching_to_zero_cost_fallback_does_not_reprice_old_tokens(self):
        with patch.object(llm, "_save_usage"):
            llm._record_usage(5, "kimi-k2.6", {"prompt_tokens": 1000, "completion_tokens": 500})
            before_fallback = llm.usage_total_cost()
            llm._record_usage(5, "glm-4.7-flash", {"prompt_tokens": 2000, "completion_tokens": 1000})
            after_fallback = llm.usage_total_cost()
            report = llm.usage_report()
        self.assertGreater(before_fallback, 0)
        self.assertEqual(after_fallback, before_fallback)
        self.assertEqual(report["members"][0]["model"], "glm-4.7-flash")
        self.assertEqual(report["members"][0]["cost"], round(before_fallback, 4))

    def test_legacy_usage_is_migrated_before_new_model_usage_is_added(self):
        llm._usage["2"] = {"model": "kimi-k2.6", "calls": 1,
                            "prompt_tokens": 1000, "completion_tokens": 1000}
        old_estimate = llm.estimate_cost("kimi-k2.6", 1000, 1000)
        with patch.object(llm, "_save_usage"):
            llm._record_usage(2, "deepseek-flash", {"prompt_tokens": 1000, "completion_tokens": 1000})
        expected = old_estimate + llm.estimate_cost("deepseek-flash", 1000, 1000)
        self.assertAlmostEqual(llm.usage_total_cost(), expected, places=8)


if __name__ == "__main__":
    unittest.main()
