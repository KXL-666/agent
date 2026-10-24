"""Requests sharing one provider are paced to reduce RPM bursts."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch

import llm


class ProviderLimitTest(unittest.TestCase):
    def test_json_vision_review_falls_back_to_configured_vision_model_with_image(self):
        resolved = [
            ("https://glm.invalid/v4", "primary-test-key", "glm-4.7-flash"),
            ("https://deepseek.invalid/v1", "vision-test-key", "deepseek-flash"),
        ]
        meta = {}
        with (patch.object(llm, "_resolve", side_effect=resolved),
              patch.object(llm, "augment_system", side_effect=lambda system, *_args, **_kwargs: system),
              patch.object(llm, "_post", side_effect=[ValueError("image input unsupported"),
                                                       '{"verdict":"passed","note":"视觉复核完成","issues":[]}']) as request):
            result = llm.chat_json(
                "独立复核", "检查候选页面。", fallback={}, roster=5, meta=meta,
                images=["data:image/png;base64,AA=="], vision_fallback_roster=2,
            )
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(meta["model"], "deepseek-flash")
        self.assertEqual(meta["vision_fallback_roster"], 2)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[0], "https://deepseek.invalid/v1")
        self.assertEqual(request.call_args.args[3]["messages"][1]["content"][1]["type"], "image_url")

    def test_text_generation_can_skip_unneeded_skill_tool_roundtrips(self):
        with (patch.object(llm, "_resolve", return_value=("https://test.invalid/v1", "test-key", "cheap-model")),
              patch.object(llm, "_post", return_value="完成") as request):
            result = llm.chat_text(
                "仅根据给定需求撰写贡献内容。", [{"role": "user", "content": "一页通知"}],
                fallback="未完成", roster=2, tool_mode=False,
            )
        self.assertEqual(result, "完成")
        self.assertFalse(request.call_args.kwargs["tool_mode"])

    def test_text_fallback_skips_failed_provider_and_chooses_cheapest_configured_model(self):
        routes = {
            1: ("https://deepseek.invalid/v1", "deepseek-1", "deepseek-flash"),
            2: ("https://deepseek.invalid/v1", "deepseek-2", "deepseek-flash"),
            3: ("https://kimi.invalid/v1", "kimi-1", "kimi-k2.6"),
            4: ("https://kimi.invalid/v1", "kimi-2", "kimi-k2.6"),
            5: ("https://glm.invalid/v4", "glm-1", "glm-4.7-flash"),
            6: ("https://glm.invalid/v4", "glm-2", "glm-4.7-flash"),
        }
        meta = {}
        with (patch.object(llm, "_resolve", side_effect=lambda roster: routes.get(roster)),
              patch.object(llm, "_resolve_backup", return_value=None),
              patch.object(llm, "augment_system", side_effect=lambda system, *_args, **_kwargs: system),
              patch.object(llm, "_post", side_effect=[ConnectionError("provider unavailable"), "完成正文"]) as request):
            result = llm.chat_text(
                "你是文档主编。", [{"role": "user", "content": "根据团队内容汇编通知。"}],
                fallback="", roster=2, meta=meta, tool_mode=False, fallback_roster=1,
            )
        self.assertEqual(result, "完成正文")
        self.assertEqual(meta["model"], "glm-4.7-flash")
        self.assertEqual(meta["primary_model"], "deepseek-flash")
        self.assertEqual(meta["fallback_roster"], 5)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[0], "https://glm.invalid/v4")
        self.assertEqual(request.call_args.args[5], 5)
        self.assertEqual(request.call_args.kwargs["tool_mode"], False)

    def test_empty_primary_text_is_treated_as_failure_and_uses_cheapest_fallback(self):
        routes = {
            1: ("https://deepseek.invalid/v1", "deepseek-1", "deepseek-flash"),
            2: ("https://deepseek.invalid/v1", "deepseek-2", "deepseek-flash"),
            3: ("https://kimi.invalid/v1", "kimi-1", "kimi-k2.6"),
            4: ("https://kimi.invalid/v1", "kimi-2", "kimi-k2.6"),
            5: ("https://glm.invalid/v4", "glm-1", "glm-4.7-flash"),
            6: ("https://glm.invalid/v4", "glm-2", "glm-4.7-flash"),
        }
        meta = {}
        with (patch.object(llm, "_resolve", side_effect=lambda roster: routes.get(roster)),
              patch.object(llm, "_resolve_backup", return_value=None),
              patch.object(llm, "augment_system", side_effect=lambda system, *_args, **_kwargs: system),
              patch.object(llm, "_post", side_effect=["", "实际文件内容"]) as request):
            result = llm.chat_text(
                "你是后端工程师。", [{"role": "user", "content": "输出核心模块。"}],
                fallback="", roster=2, meta=meta,
            )
        self.assertEqual(result, "实际文件内容")
        self.assertEqual(meta["fallback_roster"], 5)
        self.assertEqual(request.call_count, 2)

    def test_json_fallback_selects_free_model_before_explicit_paid_fallback(self):
        routes = {
            1: ("https://deepseek.invalid/v1", "deepseek-1", "deepseek-flash"),
            2: ("https://deepseek.invalid/v1", "deepseek-2", "deepseek-flash"),
            3: ("https://kimi.invalid/v1", "kimi-1", "kimi-k2.6"),
            4: ("https://kimi.invalid/v1", "kimi-2", "kimi-k2.6"),
            5: ("https://glm.invalid/v4", "glm-1", "glm-4.7-flash"),
            6: ("https://glm.invalid/v4", "glm-2", "glm-4.7-flash"),
        }
        meta = {}
        with (patch.object(llm, "_resolve", side_effect=lambda roster: routes.get(roster)),
              patch.object(llm, "_resolve_backup", return_value=None),
              patch.object(llm, "augment_system", side_effect=lambda system, *_args, **_kwargs: system),
              patch.object(llm, "_post", side_effect=[ConnectionError("provider unavailable"), '{"ok":true}']) as request):
            result = llm.chat_json(
                "你是测试负责人。", "返回验收 JSON", fallback={}, roster=4, meta=meta,
                fallback_roster=1,
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(meta["model"], "glm-4.7-flash")
        self.assertEqual(meta["primary_model"], "kimi-k2.6")
        self.assertEqual(meta["fallback_roster"], 5)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[0], "https://glm.invalid/v4")
        self.assertEqual(request.call_args.args[5], 5)

    def test_requests_to_same_provider_do_not_overlap(self):
        state = {"active": 0, "maximum": 0}
        guard = threading.Lock()

        def fake_request(*args, **kwargs):
            with guard:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.02)
            with guard:
                state["active"] -= 1
            return "ok"

        with patch.object(llm, "_post_request", side_effect=fake_request):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda _: llm._post("https://serialize-test.invalid/v1", "unused", "cheap-model",
                                        {"messages": []}, 1), range(2)
                ))
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(state["maximum"], 1)


if __name__ == "__main__":
    unittest.main()
