"""QA status follows the candidate and actual checks, never report-file quality."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import verification


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.scratch.name) / "candidate.docx"
        self.path.write_bytes(b"candidate bytes")
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.contract = {"raw_requirement": "一页通知", "requirements": [
            {"requirement_id": "R1", "text": "一页通知"}
        ]}
        self.candidate = {"id": "A1", "file_name": "candidate.docx",
                          "regen": {"file_kind": "docx", "contribution_refs": ["contribution:T1"]}}

    def tearDown(self):
        self.scratch.cleanup()

    def test_changed_candidate_fails_before_model(self):
        with patch.object(verification.forge, "safe_deliverable_path", return_value=str(self.path)):
            report = verification.verify(self.candidate, self.contract, "0" * 64)
        self.assertEqual(report["status"], "failed")
        self.assertIn("SHA-256", report["reason"])

    def test_visual_input_missing_stays_unchecked(self):
        structural = {"checks": [{"label": "文件可读取", "ok": True}], "pages": ["page.png"]}
        reviewed = {**structural, "review": {"visual": False, "verification": [
            {"requirement_id": "R1", "status": "passed", "evidence": "正文含通知", "location": "第1页"}
        ]}}
        with (patch.object(verification.forge, "safe_deliverable_path", return_value=str(self.path)),
              patch.object(verification.artifact_quality, "inspect", return_value=structural),
              patch.object(verification.artifact_quality, "model_review", return_value=reviewed),
              patch.object(verification.artifact_quality, "report_directory", return_value=Path(self.scratch.name))):
            report = verification.verify(self.candidate, self.contract, self.digest)
        self.assertEqual(report["status"], "unchecked")
        self.assertEqual(report["candidate_sha256"], self.digest)

    def test_pass_without_evidence_location_is_unchecked(self):
        clauses = [{"requirement_id": "R1", "text": "写明联调时间"}]
        item = verification._items(clauses, [{
            "requirement_id": "R1", "status": "passed", "evidence": "正文里包含 14:00 至 16:00"
        }])[0]
        self.assertEqual(item["status"], "unchecked")
        self.assertEqual(item["location"], "")

    def test_pass_with_evidence_and_candidate_location_is_retained(self):
        clauses = [{"requirement_id": "R1", "text": "写明联调时间"}]
        item = verification._items(clauses, [{
            "requirement_id": "R1", "status": "passed", "evidence": "正文里包含 14:00 至 16:00",
            "location": "第 1 页 / 联调安排段"
        }])[0]
        self.assertEqual(item["status"], "passed")
        self.assertEqual(item["location"], "第 1 页 / 联调安排段")

    def test_rendered_page_limit_overrides_model_claim(self):
        items = [{"requirement_id": "R1", "text": "控制在一页", "status": "passed",
                  "evidence": "模型自述符合"}]
        verification._apply_mechanical_page_checks(items, items, {"pages": 2})
        self.assertEqual(items[0]["status"], "failed")
        self.assertIn("实际 2 页", items[0]["problem"])

    def test_teamwork_clause_requires_real_be_fe_and_candidate_handoff(self):
        clauses = [{"requirement_id": "R9", "text": "请由团队协作完成"}]
        evidence = {"contract_status": "ready", "candidate_sha256": self.digest,
                    "contributions": [{"roster": 2, "valid": True}, {"roster": 3, "valid": True}],
                    "handoffs": [{"from_roster": 6, "to_roster": 4, "status": "accepted"}]}
        item = verification._items(clauses, None, evidence)[0]
        self.assertEqual(item["status"], "passed")
        evidence["contributions"].pop()
        self.assertEqual(verification._items(clauses, None, evidence)[0]["status"], "unchecked")

    def test_verified_teamwork_clears_model_fix_that_contradicts_workflow_trace(self):
        clauses = [{"requirement_id": "R9", "text": "请由团队协作完成"}]
        evidence = {"contract_status": "ready", "candidate_sha256": self.digest,
                    "contributions": [{"roster": 2, "valid": True}, {"roster": 3, "valid": True}],
                    "handoffs": [{"from_roster": 6, "to_roster": 4, "status": "accepted"}]}
        model_items = [{"requirement_id": "R9", "status": "passed", "evidence": "团队已协作",
                        "location": "handoffs", "problem": "仍需补交协作记录", "fix": "请补充任务分配"}]
        item = verification._items(clauses, model_items, evidence)[0]
        self.assertEqual(item["status"], "passed")
        self.assertEqual(item["problem"], "")
        self.assertEqual(item["fix"], "")

    def test_code_without_runtime_cannot_pass(self):
        self.candidate["regen"]["file_kind"] = "code"
        project = {"checks": [{"label": "语法与解析", "ok": True},
                              {"label": "隔离安装与运行测试", "ok": None}], "status": "unchecked"}
        response = {"verification": [{"requirement_id": "R1", "status": "passed", "evidence": "静态阅读"}]}
        with (patch.object(verification.forge, "safe_deliverable_path", return_value=str(self.path)),
              patch.object(verification.forge, "project_report", return_value=project),
              patch.object(verification.forge, "selected_project", return_value=self.scratch.name),
              patch.object(verification.project_agent, "snapshot", return_value="main.py source"),
              patch.object(verification.llm, "chat_json", return_value=response),
              patch.object(verification.artifact_quality, "report_directory", return_value=Path(self.scratch.name))):
            report = verification.verify(self.candidate, self.contract, self.digest)
        self.assertEqual(report["status"], "unchecked")
        self.assertEqual(report["items"][0]["status"], "unchecked")


if __name__ == "__main__":
    unittest.main()
