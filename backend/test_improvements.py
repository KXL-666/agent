"""修改建议的回归测试：使用隔离数据库与目录，不调用外部模型。"""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("OFFICE_DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

import httpx
import agile
import artifact_quality
import archive_layout
import artifacts
import chat_context
import events
import forge
import graph
import main
import storage


class ImprovementsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            patch.object(artifact_quality, "model_review", side_effect=lambda report, *args: report),
            patch.object(storage, "DB_PATH", str(root / "office.db")),
            patch.multiple(forge, DELIVERABLES_DIR=str(root / "deliverables"),
                           WORKSPACE_DIR=str(root / "workspace"), CODE_ROOT=str(root / "workspace/code"),
                           CODE_DIR=str(root / "workspace/code/test")),
        ]
        for p in self.patches:
            p.start()
        storage.init()
        Path(forge.DELIVERABLES_DIR).mkdir()
        self.sid = storage.create_sprint("开发图书借阅系统")
        events.sprint_meta.update(id=self.sid, requirement="开发图书借阅系统", fast=True)
        artifacts.set_sprint(self.sid)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_final_archive_and_process_separation(self):
        draft = forge._unique_name("成品.txt")
        Path(forge.output_path(draft)).write_text("完整正文", encoding="utf-8")
        with patch.object(artifacts, "emit"):
            process = artifacts.add_artifact("prd", "需求", 1, "正文", file_name=draft)
            final = artifacts.add_artifact("final", "成品", 6, "正文", file_name=draft)
        self.assertTrue(process["file_name"].startswith("_process/"))
        self.assertRegex(final["file_name"], r"^\d{4}-\d{2}-\d{2}/图书借阅系统/成品\.txt$")
        self.assertEqual(len(list(Path(forge.DELIVERABLES_DIR).rglob("*.txt"))), 1)
        self.assertEqual(Path(forge.safe_deliverable_path(final["file_name"])).read_text(encoding="utf-8"), "完整正文")
        self.assertIsNotNone(forge.safe_deliverable_path(draft))

    def test_archive_migration_is_repeatable_and_keeps_legacy_downloads(self):
        for name, kind in (("PRD.docx", "prd"), ("最终.docx", "final")):
            Path(forge.DELIVERABLES_DIR, name).write_bytes(name.encode())
            storage.add_artifact(self.sid, {"id": name, "kind": kind, "file_name": name, "created_at": 1729526400})
        old_code = Path(forge.DELIVERABLES_DIR, "code", self.sid, "main.py")
        old_code.parent.mkdir(parents=True)
        old_code.write_text("print(1)")
        self.assertEqual(archive_layout.migrate(), 2)
        self.assertEqual(archive_layout.migrate(), 0)
        self.assertTrue(storage.get_artifact("PRD.docx")["file_name"].startswith("_process/"))
        self.assertTrue(storage.get_artifact("最终.docx")["file_name"].startswith("2024-10-22/"))
        self.assertIsNotNone(forge.safe_deliverable_path("PRD.docx"))
        self.assertTrue(Path(forge.sprint_code_dir(self.sid), "main.py").exists())
        self.assertEqual(len([p for p in Path(forge.DELIVERABLES_DIR).rglob("*") if p.is_file()]), 1)

    def test_download_path_rejects_sibling_prefix_and_traversal(self):
        outside = Path(self.temp.name, "deliverables-other")
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        for name in ("../deliverables-other/secret.txt", "_process/../../deliverables-other/secret.txt", str(outside / "secret.txt")):
            self.assertIsNone(forge.safe_deliverable_path(name))

    def test_plan_fields_survive_database_roundtrip(self):
        fields = agile.plan_fields({"title": "借阅接口", "priority": "P0", "story_points": 5,
                                    "acceptance_criteria": ["库存为零时拒绝借阅"]}, "图书系统")
        storage.upsert_task(self.sid, {"id": "T1", "title": "借阅接口", "status": "待开发", **fields})
        restored = storage.list_tasks(self.sid)[0]
        for key in fields:
            self.assertEqual(restored[key], fields[key])
        invalid = agile.plan_fields({"story_points": -5, "priority": "invalid", "acceptance_criteria": "text"}, "需求")
        self.assertEqual(invalid["story_points"], 3)
        self.assertTrue(invalid["acceptance_criteria"])

    def test_quality_retry_cannot_destroy_first_file(self):
        def generate(kind, spec, path, roster, feedback=""):
            Path(path).write_bytes(b"damaged" if feedback else b"first usable file")
            return not feedback
        with patch.object(forge, "_forge_once", side_effect=generate), patch.object(forge, "self_check", return_value={"ok": False, "checks": [{"ok": False, "label": "正文", "detail": "不足"}]}):
            name = forge.forge_file("docx", "需求", "结果.docx")
        self.assertEqual(Path(forge.safe_deliverable_path(name)).read_bytes(), b"first usable file")

    async def test_clock_works_on_both_chat_endpoints_without_model(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            with patch.object(main, "chat_text", side_effect=AssertionError("不应调用模型")), patch.object(main, "stream_chat", side_effect=AssertionError("不应调用模型")):
                normal = await client.post("/api/chat", json={"roster": 1, "message": "现在几点了？"})
                stream = await client.post("/api/chat/stream", json={"roster": 2, "message": "今天星期几"})
        self.assertIn("北京时间", normal.json()["reply"])
        frames = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        self.assertIn("北京时间", frames[1]["delta"])
        self.assertFalse(frames[-1]["degraded"])
        self.assertIsNone(chat_context.clock_reply("现在几点可以开始部署？"))

    async def test_two_chat_streams_run_concurrently(self):
        barrier = threading.Barrier(2, timeout=3)
        def respond(system, history, roster, fallback, meta):
            barrier.wait()
            yield f"成员{roster}独立回复"
        with patch.object(main, "stream_chat", side_effect=respond):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                results = await asyncio.gather(*(client.post("/api/chat/stream", json={"roster": r, "message": "解释一下需求"}) for r in (1, 2)))
        for roster, result in enumerate(results, 1):
            self.assertIn(f"成员{roster}独立回复", result.text)

    async def test_rejected_review_triggers_rework_and_blocks_delivery(self):
        tasks = [{"id": "T1", "title": "核心功能", "status": "待评审", "assignee_roster": 2,
                  "requirement_ids": ["R1"]}]
        state = {"requirement": "需求", "tasks": tasks, "rework_count": 0, "deliver_kind": "code",
                 "candidate_id": "missing", "candidate_sha256": "hash",
                 "qa_report": {"status": "failed", "candidate_sha256": "hash",
                               "items": [{"requirement_id": "R1", "status": "failed"}]}}
        decision = {"status": "failed", "candidate_sha256": "hash", "issues": [{"requirement_id": "R1"}],
                    "reason": "验收条件未满足"}
        with patch.object(graph.review_decision, "decide", return_value=decision), patch.object(graph, "pause"), patch.object(graph, "speak"):
            result = await graph.review(state)
            self.assertEqual(graph.route_review({**state, **result}), "rework")
            self.assertEqual(tasks[0]["status"], "打回")
            await graph.publish({**state, **result})
        final = next(a for a in artifacts.artifacts_store if a["kind"] == "final")
        self.assertTrue(final["degraded"])
        self.assertIsNone(final["file_name"])

    async def test_testing_pass_waits_for_review_before_completion(self):
        tasks = [{"id": "T1", "title": "借阅接口", "status": "待测试", "assignee_roster": 2,
                  "requirement_ids": ["R1"]}]
        state = {"requirement": "图书系统", "tasks": tasks, "rework_count": 0,
                 "candidate_id": "missing", "candidate_sha256": "hash"}
        report = {"status": "passed", "candidate_sha256": "hash",
                  "items": [{"requirement_id": "R1", "status": "passed"}]}
        decision = {"status": "passed", "candidate_sha256": "hash", "reason": "复核通过", "issues": []}
        with patch.object(graph.verification, "verify", return_value=report), patch.object(graph.review_decision, "decide", return_value=decision), patch.object(graph, "pause"), patch.object(graph, "speak"):
            state.update(await graph.test(state))
            self.assertEqual(tasks[0]["status"], "待评审")
            state.update(await graph.review(state))
            self.assertEqual(tasks[0]["status"], "通过")

    async def test_persistent_defects_cannot_loop_forever(self):
        state = {"tasks": [{"id": "T1", "title": "缺陷", "status": "打回", "requirement_ids": ["R1"]}],
                 "rework_count": 0, "candidate_id": "missing", "candidate_sha256": "hash",
                 "qa_report": {"status": "failed", "candidate_sha256": "hash",
                               "items": [{"requirement_id": "R1", "status": "failed"}]}}
        decision = {"status": "failed", "candidate_sha256": "hash", "reason": "缺陷仍在",
                    "issues": [{"requirement_id": "R1"}]}
        with patch.object(graph.review_decision, "decide", return_value=decision), patch.object(graph, "pause"), patch.object(graph, "speak"):
            for _ in range(graph.MAX_REWORK + 1):
                state.update(await graph.review(state))
        self.assertGreater(state["rework_count"], graph.MAX_REWORK)
        self.assertEqual(graph.route_review(state), "approved")
        # approved 是图中的结束返工路由；publish 仍会阻止未通过任务发布。

    def test_chat_context_contains_actual_progress_and_does_not_limit_to_80_chars(self):
        context = chat_context.build_chat_system({"name": "测试员", "role": "测试工程师"}, 4,
                {"requirement": "图书系统", "stage": "测试验证"},
                [{"id": "T1", "title": "借阅接口", "status": "打回"}], [])
        self.assertIn('"status": "打回"', context)
        self.assertIn("北京时间", context)
        self.assertNotIn("不超过 80 字", context)

    def test_real_office_renderers_produce_readable_files(self):
        document = "# 系统设计说明\n\n" + "\n\n".join(
            f"## 第{i}章 设计与验收\n\n" + (f"本章{i}的借阅系统需要检查库存与用户权限，异常时返回明确原因，并记录操作结果。" * 8)
            + "\n\n" + (f"本章{i}的验收覆盖正常借阅、库存不足与重复请求，分别核对预期结果。" * 6)
            for i in range(1, 9))
        slides = {"title": "系统介绍", "subtitle": "迭代成果", "slides": [
            {"heading": f"第{i}页 方案与验收", "bullets": ["库存检查与权限验证共同保障借阅流程的一致性。" * 2] * 3, "note": "说明流程与异常条件"}
            for i in range(8)]}
        sheets = [{"name": "测试用例", "headers": ["编号", "场景", "结果"],
                   "rows": [[i, "库存不足时拒绝借阅", "待执行"] for i in range(1, 9)]}]
        with patch.object(forge, "_gen_docx", return_value=document), patch.object(forge, "_gen_pptx", return_value=slides), patch.object(forge, "_gen_xlsx", return_value=sheets):
            for kind in ("docx", "pptx", "xlsx"):
                name = forge.forge_file(kind, "系统说明", f"成品.{kind}")
                self.assertIsNotNone(name)
                self.assertTrue(forge.self_check(name, kind)["ok"])
                final = forge.publish_file(name, self.sid)
                self.assertIsNotNone(forge.safe_deliverable_path(final))


if __name__ == "__main__":
    unittest.main()
