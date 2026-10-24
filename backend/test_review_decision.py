"""LEAD always sees QA evidence and cannot approve a different file version."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import review_decision


class ReviewDecisionTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.scratch.name) / "candidate.zip"
        self.path.write_bytes(b"candidate")
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.candidate = {"id": "A1", "file_name": "candidate.zip", "regen": {"file_kind": "code"}}
        self.contract = {"raw_requirement": "转换温度", "requirements": [{"requirement_id": "R1", "text": "转换温度"}]}

    def tearDown(self):
        self.scratch.cleanup()

    def test_qa_failure_still_invokes_lead_and_final_is_failed(self):
        qa = {"candidate_sha256": self.digest, "status": "failed", "items": [
            {"requirement_id": "R1", "status": "failed", "evidence": "25 C 输出错误"}
        ]}
        calls = []

        def fake_chat(system, user, **kwargs):
            calls.append(user)
            return {"verdict": "passed", "note": "看似可用", "issues": []}

        with (patch.object(review_decision.forge, "safe_deliverable_path", return_value=str(self.path)),
              patch.object(review_decision.forge, "selected_project", return_value=self.scratch.name),
              patch.object(review_decision.forge, "project_report", return_value={"checks": []}),
              patch.object(review_decision.project_agent, "snapshot", return_value="源代码"),
              patch.object(review_decision.llm, "chat_json", side_effect=fake_chat),
              patch.object(review_decision.artifact_quality, "report_directory", return_value=Path(self.scratch.name))):
            decision = review_decision.decide(self.candidate, self.contract, qa, [], self.digest)
        self.assertEqual(len(calls), 1)
        self.assertIn("25 C 输出错误", calls[0])
        self.assertEqual(decision["status"], "failed")
        self.assertEqual(decision["candidate_sha256"], self.digest)

    def test_old_qa_hash_invalidates_review(self):
        qa = {"candidate_sha256": "old", "status": "passed"}
        with patch.object(review_decision.forge, "safe_deliverable_path", return_value=str(self.path)):
            decision = review_decision.decide(self.candidate, self.contract, qa, [], self.digest)
        self.assertEqual(decision["status"], "failed")
        self.assertIn("不是当前候选", decision["reason"])

    def test_model_unavailable_is_unchecked(self):
        qa = {"candidate_sha256": self.digest, "status": "passed"}
        with (patch.object(review_decision.forge, "safe_deliverable_path", return_value=str(self.path)),
              patch.object(review_decision.forge, "selected_project", return_value=self.scratch.name),
              patch.object(review_decision.forge, "project_report", return_value={"checks": []}),
              patch.object(review_decision.project_agent, "snapshot", return_value="源代码"),
              patch.object(review_decision.llm, "chat_json", return_value={}),
              patch.object(review_decision.artifact_quality, "report_directory", return_value=Path(self.scratch.name))):
            decision = review_decision.decide(self.candidate, self.contract, qa, [], self.digest)
        self.assertEqual(decision["status"], "unchecked")

    def test_pass_without_independent_reason_is_unchecked(self):
        qa = {"candidate_sha256": self.digest, "status": "passed"}
        with (patch.object(review_decision.forge, "safe_deliverable_path", return_value=str(self.path)),
              patch.object(review_decision.forge, "selected_project", return_value=self.scratch.name),
              patch.object(review_decision.forge, "project_report", return_value={"checks": []}),
              patch.object(review_decision.project_agent, "snapshot", return_value="源代码"),
              patch.object(review_decision.llm, "chat_json", return_value={
                  "verdict": "passed", "note": "", "issues": []}),
              patch.object(review_decision.artifact_quality, "report_directory", return_value=Path(self.scratch.name))):
            decision = review_decision.decide(self.candidate, self.contract, qa, [], self.digest)
        self.assertEqual(decision["status"], "unchecked")
        self.assertIn("复核依据", decision["reason"])

    def test_visual_review_fallback_model_is_recorded(self):
        self.candidate["regen"]["file_kind"] = "docx"
        qa = {"candidate_sha256": self.digest, "status": "passed", "companion_hashes": []}
        image_dir = Path(self.scratch.name)
        (image_dir / "page-1.png").write_bytes(b"small-rendered-page")

        def visual_pass(*_args, **kwargs):
            kwargs["meta"]["model"] = "deepseek-flash"
            kwargs["meta"]["vision_fallback_roster"] = 2
            return {"verdict": "passed", "note": "已查看候选页面并独立核对", "issues": []}

        with (patch.object(review_decision.forge, "safe_deliverable_path", return_value=str(self.path)),
              patch.object(review_decision.artifact_quality, "load_report", return_value={
                  "text": "通知正文", "checks": [], "pages": ["page-1.png"]}),
              patch.object(review_decision.artifact_quality, "report_directory", return_value=image_dir),
              patch.object(review_decision.llm, "chat_json", side_effect=visual_pass)):
            decision = review_decision.decide(self.candidate, {**self.contract, "status": "ready"}, qa, [], self.digest)

        self.assertEqual(decision["status"], "passed")
        self.assertEqual(decision["model"], "deepseek-flash")
        self.assertEqual(decision["vision_fallback_roster"], 2)

    def test_candidate_changed_during_lead_call_invalidates_decision(self):
        qa = {"candidate_sha256": self.digest, "status": "passed"}

        def mutate_then_pass(*args, **kwargs):
            self.path.write_bytes(b"changed during review")
            return {"verdict": "passed", "note": "候选内容满足需求", "issues": []}

        with (patch.object(review_decision.forge, "safe_deliverable_path", return_value=str(self.path)),
              patch.object(review_decision.forge, "selected_project", return_value=self.scratch.name),
              patch.object(review_decision.forge, "project_report", return_value={"checks": []}),
              patch.object(review_decision.project_agent, "snapshot", return_value="源代码"),
              patch.object(review_decision.llm, "chat_json", side_effect=mutate_then_pass),
              patch.object(review_decision.artifact_quality, "report_directory", return_value=Path(self.scratch.name))):
            decision = review_decision.decide(self.candidate, self.contract, qa, [], self.digest)
        self.assertEqual(decision["status"], "failed")
        self.assertIn("发生变化", decision["reason"])


if __name__ == "__main__":
    unittest.main()
