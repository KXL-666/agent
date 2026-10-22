"""Sprint 工作流集成测试（离线，不调用真实 LLM）

用 Mock 替换 LLM 与文件生成，验证节点串联、产物落库、兜底标记、代码包交付。

运行：cd backend && python -m unittest test_graph -v
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="office_graph_test_")
os.environ["OFFICE_DB_PATH"] = os.path.join(_TMP, "graph.db")

import artifacts  # noqa: E402
import events  # noqa: E402
import forge  # noqa: E402
import graph  # noqa: E402
import llm  # noqa: E402


def fake_chat_json(system, user="", fallback=None, *args, **kwargs):
    """按提示词特征返回结构合法的 JSON，模拟一次成功的模型调用"""
    if "拆解" in system:
        return {
            "tasks": [
                {"title": "设计命令行交互", "role_hint": "后端"},
                {"title": "实现核心逻辑", "role_hint": "前端"},
                {"title": "编写自测用例", "role_hint": "测试"},
            ]
        }
    if "最终交付物类型" in system:
        return {"type": "code"}
    if "测试工程师" in system:
        return {"verdict": "通过", "bug_task": None, "note": "自测通过"}
    if "评审意见" in system:
        return {"opinion": "结构清晰，建议补充异常处理"}
    if "最终评审结论" in system:
        return {"verdict": "通过", "note": "评审通过"}
    if "刚完成开发任务" in system:
        return {"note": "已完成实现并自测"}
    if "复盘" in system or "总结" in system:
        return {"summary": "本轮交付顺利"}
    return {"message": "好的"}


class GraphFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_root = tempfile.mkdtemp(prefix="office_code_")
        self.temp_deliver = tempfile.mkdtemp(prefix="office_deliver_")
        self.original = (forge.CODE_ROOT, forge.DELIVERABLES_DIR, forge.CODE_DIR)
        forge.CODE_ROOT = self.temp_root
        forge.DELIVERABLES_DIR = self.temp_deliver
        forge.begin_sprint("TEST_SPRINT")
        with open(os.path.join(forge.CODE_DIR, "main.py"), "w", encoding="utf-8") as f:
            f.write("print('hello')\n")

        events.sprint_meta.update(
            {"id": "TEST_SPRINT", "fast": True, "usage_baseline": 0.0, "stage": ""}
        )
        events.tasks_store.clear()
        artifacts.set_sprint("TEST_SPRINT")

    async def asyncTearDown(self):
        forge.CODE_ROOT, forge.DELIVERABLES_DIR, forge.CODE_DIR = self.original
        shutil.rmtree(self.temp_root, ignore_errors=True)
        shutil.rmtree(self.temp_deliver, ignore_errors=True)

    async def test_sprint_reaches_delivery(self):
        with (
            patch.object(graph, "chat_json", side_effect=fake_chat_json),
            patch.object(forge, "forge_file", return_value=None),
            patch.object(forge, "forge_code", return_value=[]),
            patch.object(forge, "verify_generated_code", return_value=[]),
            patch.object(forge, "finalize_project", return_value=[]),
            patch.object(forge, "extract_target_dir", return_value=None),
            patch.object(llm, "SCENE_PACE", 0),
        ):
            result = await graph.build_sprint_graph().ainvoke(
                {
                    "requirement": "做一个命令行小工具",
                    "tasks": [],
                    "rework_count": 0,
                    "review_note": "",
                    "deliver_kind": "",
                    "uploads_text": "",
                }
            )

        self.assertEqual(result["deliver_kind"], "code")
        self.assertTrue(all(t["status"] == "通过" for t in result["tasks"]))

        by_kind = {}
        for item in artifacts.artifacts_store:
            by_kind.setdefault(item["kind"], []).append(item)

        self.assertIn("prd", by_kind)
        self.assertIn("dev", by_kind)
        self.assertIn("test", by_kind)
        self.assertIn("review", by_kind)
        self.assertIn("summary", by_kind)
        self.assertIn("final", by_kind)

        # 文件生成被 Mock 成失败：过程产物必须明确标记为兜底，不能假装成功
        self.assertTrue(by_kind["prd"][0]["degraded"])
        self.assertTrue(by_kind["prd"][0]["degraded_reason"])

        # 代码类最终交付必须挂真实 zip 文件
        final = by_kind["final"][0]
        self.assertIsNotNone(final["file_name"])
        self.assertTrue(final["file_name"].endswith(".zip"))
        self.assertFalse(final["degraded"])
        self.assertTrue(
            os.path.exists(os.path.join(self.temp_deliver, final["file_name"]))
        )


if __name__ == "__main__":
    unittest.main()
