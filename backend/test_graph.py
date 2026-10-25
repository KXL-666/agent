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
import storage  # noqa: E402


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
        self.original_db_path = storage.DB_PATH
        storage.DB_PATH = os.path.join(self.temp_root, "graph.db")
        storage.init()
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
        storage.DB_PATH = self.original_db_path
        shutil.rmtree(self.temp_root, ignore_errors=True)
        shutil.rmtree(self.temp_deliver, ignore_errors=True)

    async def test_code_plan_merges_duplicate_roles_and_keeps_distinct_scopes(self):
        tasks = graph.compact_code_tasks([
            {"title": "实现核心逻辑与 CLI", "role_hint": "后端", "requirement_ids": ["R1", "R2"],
             "acceptance_criteria": ["支持摄氏转华氏"]},
            {"title": "处理非法单位", "role_hint": "后端", "requirement_ids": ["R3"],
             "acceptance_criteria": ["非法单位非零退出"]},
            {"title": "编写测试", "role_hint": "测试", "requirement_ids": ["R4"],
             "acceptance_criteria": ["覆盖正反转换和错误输入"]},
            {"title": "编写 README", "role_hint": "写作", "requirement_ids": ["R5"],
             "acceptance_criteria": ["包含运行命令"]},
        ])
        self.assertEqual([task["role_hint"] for task in tasks], ["后端", "前端", "测试"])
        self.assertEqual(tasks[0]["requirement_ids"], ["R1", "R2", "R3"])
        self.assertEqual(tasks[0]["acceptance_criteria"], ["支持摄氏转华氏", "非法单位非零退出"])
        self.assertEqual(tasks[1]["title"], "实现命令行入口与使用说明")
        self.assertEqual(tasks[2]["title"], "编写自动化测试并验证错误路径")

    async def test_code_work_orders_backend_before_parallel_dependents(self):
        tasks = [
            {"id": "T1", "assignee_roster": graph.DEV_FE},
            {"id": "T2", "assignee_roster": graph.QA},
            {"id": "T3", "assignee_roster": graph.DEV_BE},
        ]
        phases = graph.code_task_phases(tasks)
        self.assertEqual([[task["id"] for task in phase] for phase in phases], [["T3"], ["T1", "T2"]])

    async def test_retrospective_never_claims_failed_tasks_were_completed(self):
        summary, markdown = graph.retrospective_content({
            "requirement": "生成命令行工具",
            "tasks": [{"id": "T1", "title": "实现转换逻辑", "assignee_roster": graph.DEV_BE, "status": "打回"}],
            "qa_report": {"status": "unchecked"},
            "lead_decision": {"status": "unchecked"},
        }, False, [{"kind": "final", "title": "交付待修正", "quality_status": "failed", "degraded": True}])
        self.assertIn("未通过", summary)
        self.assertIn("0/1", summary)
        self.assertIn("打回", markdown)
        self.assertIn("补齐或返工 T1", markdown)
        self.assertNotIn("任务均已完成", markdown)

    async def test_sprint_reaches_delivery(self):
        candidate_hashes_seen = {"qa": [], "lead": []}

        def observing_chat(system, user="", fallback=None, *args, **kwargs):
            return fake_chat_json(system, user, fallback, *args, **kwargs)

        def fake_verify(candidate, contract, digest, workflow_evidence=None):
            candidates = [a for a in artifacts.artifacts_store if a.get("kind") == "candidate"]
            latest = candidates[-1] if candidates else {}
            candidate_hashes_seen["qa"].append((
                digest, latest.get("regen", {}).get("sha256"),
                graph._sha256_file(latest.get("file_name")) if latest.get("file_name") else "",
            ))
            return {"status": "passed", "candidate_sha256": digest, "items": [
                {"requirement_id": c["requirement_id"], "status": "passed", "evidence": "模拟 QA 证据"}
                for c in contract.get("requirements", [])
            ], "reason": ""}

        def fake_lead(candidate, contract, qa_report, handoffs, digest, companions=None, workflow_evidence=None):
            candidate_hashes_seen["lead"].append((digest, qa_report["candidate_sha256"]))
            return {"status": "passed", "candidate_sha256": digest,
                    "qa_candidate_sha256": qa_report["candidate_sha256"], "issues": [],
                    "reason": "模拟独立复核"}

        with (
            patch.object(graph, "chat_json", side_effect=observing_chat),
            patch.object(graph.verification, "verify", side_effect=fake_verify),
            patch.object(graph.review_decision, "decide", side_effect=fake_lead),
            patch.object(forge, "forge_file", return_value=None),
            patch.object(forge, "render_docx_from_markdown", return_value=None),
            patch.object(forge, "forge_code", return_value=[]),
            patch.object(forge, "verify_generated_code", return_value=[]),
            patch.object(forge, "finalize_project", return_value=[]),
            patch.object(forge, "project_report", return_value={"status": "passed", "ok": True, "checks": [{"label": "模拟运行测试", "ok": True, "detail": "集成测试中替换执行器"}]}),
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
        self.assertEqual(result["requirement_contract"]["status"], "unchecked")
        self.assertEqual(result["requirement_contract"]["requirements"][0]["text"], "做一个命令行小工具")
        self.assertTrue(all(t["requirement_ids"] == ["R1"] for t in result["tasks"]))
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
        self.assertEqual(len(candidate_hashes_seen["qa"]), 1)
        self.assertEqual(len(candidate_hashes_seen["lead"]), 1)
        candidate = by_kind["candidate"][-1]
        self.assertEqual(candidate["regen"]["sha256"], graph._sha256_file(candidate["file_name"]))
        self.assertEqual(candidate_hashes_seen["qa"][0], (
            candidate["regen"]["sha256"], candidate["regen"]["sha256"], candidate["regen"]["sha256"]
        ))
        self.assertEqual(candidate_hashes_seen["lead"][0], (
            candidate["regen"]["sha256"], candidate["regen"]["sha256"]
        ))

        # 文件生成被 Mock 成失败：过程产物必须明确标记为兜底，不能假装成功
        self.assertTrue(by_kind["prd"][0]["degraded"])
        self.assertTrue(by_kind["prd"][0]["degraded_reason"])

        # 代码类最终交付必须挂真实 zip 文件
        final = by_kind["final"][0]
        self.assertEqual(final["quality_status"], "unchecked")
        self.assertEqual(graph._sha256_file(final["file_name"]), candidate["regen"]["sha256"])
        self.assertIsNotNone(final["file_name"])
        self.assertTrue(final["file_name"].endswith(".zip"))
        self.assertFalse(final["degraded"])
        self.assertTrue(
            os.path.exists(os.path.join(self.temp_deliver, final["file_name"]))
        )

    async def test_missing_contribution_blocks_candidate_creation(self):
        state = {
            "requirement": "生成接口联调通知",
            "deliver_kind": "docx",
            "tasks": [{"id": "T1", "title": "核对通知事实", "status": "待测试",
                       "assignee_roster": graph.DEV_BE, "requirement_ids": ["R1"]}],
            "requirement_contract": {"requirements": [{"requirement_id": "R1"}]},
            "uploads_text": "",
        }
        with patch.object(graph.storage, "get_sprint", return_value={"id": "TEST_SPRINT"}):
            result = await graph.candidate(state)
        self.assertNotIn("candidate_id", result)
        blocked = [item for item in artifacts.artifacts_store
                   if item.get("kind") == "candidate" and item.get("quality_status") == "failed"]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(blocked[0].get("degraded"))
        self.assertIn("贡献", blocked[0].get("degraded_reason", ""))

    def test_candidate_handoff_references_candidate_hash_and_contributions(self):
        state = {"requirement_contract": {"requirements": [{"requirement_id": "R1"}]},
                 "tasks": [{"contribution_ref": "contribution:T1"}]}
        with (patch.object(graph.storage, "get_sprint", return_value={"contract": {}}),
              patch.object(graph.handoffs, "offer") as offer):
            graph._offer_candidate_to_qa("S1", state, "A1", "abc123")
        args, kwargs = offer.call_args
        self.assertEqual(args[0], "S1")
        self.assertEqual(kwargs["to_roster"], 4)
        self.assertIn("artifact:A1", kwargs["output_refs"])
        self.assertIn("contribution:T1", kwargs["input_refs"])
        self.assertIn("abc123", kwargs["claims"][0])

    def test_short_notice_merges_duplicate_frontend_calls_and_keeps_clause_links(self):
        plan = [
            {"title": "撰写正文", "role_hint": "写作", "requirement_ids": ["R1", "R2"],
             "acceptance_criteria": ["正文完整"], "user_story": "通知正文"},
            {"title": "完成排版", "role_hint": "设计", "requirement_ids": ["R3"],
             "acceptance_criteria": ["不超过一页"], "user_story": "一页版式"},
            {"title": "核对事实", "role_hint": "后端", "requirement_ids": ["R4"],
             "acceptance_criteria": ["时间正确"]},
        ]
        result = graph.compact_short_document_tasks(
            plan, {"target_kind": "docx", "raw_requirement": "控制在一页，不要封面"}
        )
        self.assertEqual(len(result), 2)
        merged = next(item for item in result if item["role_hint"] == "前端")
        self.assertEqual(merged["requirement_ids"], ["R1", "R2", "R3"])
        self.assertEqual(merged["acceptance_criteria"], ["正文完整", "不超过一页"])

    def test_file_plan_adds_missing_fact_and_qa_roles(self):
        contract = {"target_kind": "docx", "requirements": [
            {"requirement_id": "R1"}, {"requirement_id": "R2"}]}
        planned = [{"title": "撰写通知正文", "role_hint": "前端",
                    "requirement_ids": ["R1", "R2"]}]
        result = graph.ensure_file_collaboration_tasks(planned, contract)
        self.assertEqual({item["role_hint"] for item in result}, {"后端", "前端", "测试"})
        self.assertEqual(len(result), 3)
        self.assertEqual(next(item for item in result if item["role_hint"] == "后端")["requirement_ids"],
                         ["R1", "R2"])
        self.assertEqual(graph.ensure_file_collaboration_tasks(result, contract), result)


if __name__ == "__main__":
    unittest.main()
