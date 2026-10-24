"""Offline checks for source preservation, missing PO output, and migration."""

import json
import os
import sqlite3
import tempfile
import unittest

import requirement_contract as contracts
import storage
from agile import plan_fields


W1 = ("生成一份给研发与测试同事的 Word 通知，说明 11 月 4 日 14:00 至 16:00 进行接口联调，"
      "地点为线上会议室 A。前端在 11 月 3 日 18:00 前提交调用清单，后端在联调开始前准备测试环境，"
      "测试同事记录阻塞问题并在当日 20:00 前汇总。控制在一页，不要封面。")
C1 = ("生成可运行的 Python 项目，实现命令行温度换算：输入“25 C”输出“77.0 F”，"
      "输入“77 F”输出“25.0 C”。无效格式与不支持的单位返回清楚错误，并以非零状态退出。"
      "提供 README 和有实际断言的自动化测试。")


class ContractTest(unittest.TestCase):
    def test_code_requirements_receive_executable_acceptance_methods(self):
        contract = contracts.build_contract(C1, deliver_kind="code", model_contract={
            "target_kind": "code", "audience": "终端用户", "purpose": "温度换算",
            "requirements": [], "missing_info": [], "allowed_assumptions": [],
        })
        self.assertEqual(contract["status"], "ready", contract["unchecked_reasons"])
        self.assertTrue(all(clause["acceptance_method"] for clause in contract["requirements"]))

    def test_fixed_samples_keep_every_explicit_constraint(self):
        for raw, expected in (
            (W1, ("11 月 4 日 14:00 至 16:00", "11 月 3 日 18:00", "20:00", "一页", "不要封面")),
            (C1, ("25 C", "77 F", "无效格式", "不支持的单位", "非零状态", "README", "自动化测试")),
        ):
            with self.subTest(raw=raw[:15]):
                contract = contracts.build_contract(raw, deliver_kind="docx")
                joined = " ".join(c["text"] for c in contract["requirements"])
                for phrase in expected:
                    self.assertIn(phrase, joined)
                self.assertEqual(contract["status"], "unchecked")
                self.assertEqual(contract["raw_requirement"], raw)
                self.assertEqual(
                    [c["requirement_id"] for c in contract["requirements"]],
                    [f"R{i}" for i in range(1, len(contract["requirements"]) + 1)],
                )

    def test_model_coverage_and_task_links(self):
        raw = "交付 Word 通知，一页，不要封面。"
        fragments = contracts._fragments(raw)
        model = {"target_kind": "docx", "audience": "同事", "purpose": "通知",
                 "missing_info": [], "allowed_assumptions": [], "requirements": [
            {"source": "user", "source_quote": fragment, "acceptance_method": f"检查文件包含：{fragment}", "priority": "P0"}
            for fragment in fragments
        ]}
        contract = contracts.build_contract(raw, model_contract=model, deliver_kind="docx")
        self.assertEqual(contract["status"], "ready")
        task = plan_fields({"title": "撰写通知", "requirement_ids": ["R2", "R999"]}, raw, contract)
        self.assertEqual(task["requirement_ids"], ["R2"])
        self.assertIn("一页", task["acceptance_criteria"][0])
        model["requirements"].pop(0)
        self.assertEqual(contracts.build_contract(raw, model_contract=model)["status"], "unchecked")

    def test_explicit_page_and_cover_constraints_get_mechanical_acceptance(self):
        raw = W1 + " 请由团队协作完成。"
        requirements = []
        for fragment in contracts._fragments(raw):
            requirements.append({"source": "user", "source_quote": fragment,
                                 "acceptance_method": "" if fragment in ("控制在一页", "不要封面") else f"逐字核对：{fragment}",
                                 "priority": "P0"})
        model = {"target_kind": "docx", "audience": "同事", "purpose": "通知",
                 "missing_info": [], "allowed_assumptions": ["请由团队协作完成"], "requirements": requirements}
        contract = contracts.build_contract(raw, model_contract=model, deliver_kind="docx")
        self.assertEqual(contract["status"], "ready")
        self.assertEqual(contract["allowed_assumptions"], [])
        self.assertTrue(contract["assumption_warnings"])
        self.assertIn("真实页数", contract["requirements"][-3]["acceptance_method"])
        self.assertIn("独立封面页", contract["requirements"][-2]["acceptance_method"])

    def test_collaboration_requirement_gets_a_traceable_workflow_acceptance_method(self):
        raw = "生成 Word 通知。请由团队协作完成。"
        requirements = [{"source": "user", "source_quote": fragment,
                         "acceptance_method": "检查文件格式" if fragment == "生成 Word 通知" else ""}
                        for fragment in contracts._fragments(raw)]
        model = {"target_kind": "docx", "audience": "同事", "purpose": "通知",
                 "missing_info": [], "allowed_assumptions": [], "requirements": requirements}
        contract = contracts.build_contract(raw, model_contract=model, deliver_kind="docx")
        collaboration = contract["requirements"][-1]
        self.assertEqual(collaboration["status"], "ready")
        self.assertIn("最终候选哈希", collaboration["acceptance_method"])
        self.assertEqual(contract["status"], "ready")

    def test_unlocated_attachment_is_unchecked(self):
        contract = contracts.build_contract("根据附件写报告", "无位置的提取文本", deliver_kind="docx")
        self.assertEqual(contract["attachment_sources"], [])
        self.assertTrue(any("缺少可定位" in reason for reason in contract["unchecked_reasons"]))

    def test_old_database_migrates_and_contract_survives_reload(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = os.path.join(directory, "old.db")
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE sprints (id TEXT PRIMARY KEY, requirement TEXT, status TEXT, stage TEXT, deliver_kind TEXT, started_at REAL, finished_at REAL)")
                conn.execute("CREATE TABLE tasks (sprint_id TEXT, id TEXT, title TEXT, role_hint TEXT, assignee_roster INTEGER, status TEXT, summary TEXT, code_files TEXT, PRIMARY KEY (sprint_id, id))")
                conn.execute("CREATE TABLE artifacts (id TEXT PRIMARY KEY, sprint_id TEXT, kind TEXT, title TEXT, author_roster INTEGER, task_id TEXT, content TEXT, file_name TEXT, created_at REAL, degraded INTEGER, degraded_reason TEXT, regen TEXT, parent_id TEXT, version INTEGER)")
                conn.execute("INSERT INTO sprints VALUES ('OLD','原话','done','','',0,0)")
            original = storage.DB_PATH
            try:
                storage.DB_PATH = path
                storage.init()
                self.assertEqual(storage.get_sprint("OLD")["contract"], {})
                self.assertEqual(storage.get_sprint("OLD")["budget"], 0)
                self.assertEqual(storage.get_sprint("OLD")["execution_mode"], "cloud")
                self.assertEqual(storage.list_handoffs("OLD"), [])
                budgeted = storage.create_sprint("限额任务", budget=1.25)
                self.assertEqual(storage.get_sprint(budgeted)["budget"], 1.25)
                contract = contracts.build_contract("原话", deliver_kind="docx")
                storage.save_contract("OLD", contract)
                storage.upsert_task("OLD", {"id": "T1", "title": "写作", "requirement_ids": ["R1"]})
                self.assertEqual(storage.get_sprint("OLD")["contract"], contract)
                self.assertEqual(storage.list_tasks("OLD")[0]["requirement_ids"], ["R1"])
                with sqlite3.connect(path) as conn:
                    self.assertEqual(json.loads(conn.execute("SELECT contract_json FROM sprints WHERE id='OLD'").fetchone()[0]), contract)
            finally:
                storage.DB_PATH = original


if __name__ == "__main__":
    unittest.main()
