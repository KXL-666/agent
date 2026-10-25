"""Actual source files, hashes, concurrent task isolation and interface checks."""

import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import contributions
import forge


class ContributionTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old = forge.WORKSPACE_DIR, forge.CODE_ROOT
        forge.WORKSPACE_DIR = self.scratch.name
        forge.CODE_ROOT = str(Path(self.scratch.name) / "code")

    def tearDown(self):
        forge.WORKSPACE_DIR, forge.CODE_ROOT = self.old
        self.scratch.cleanup()

    def _code(self, task_id: str, name: str, content: str):
        path = Path(forge.CODE_ROOT) / "S1" / task_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return "_process/" + path.relative_to(self.scratch.name).as_posix()

    def test_word_source_is_complete_and_tamper_invalidates(self):
        content = "通知\n11 月 4 日 14:00 至 16:00 联调。\n前端 11 月 3 日 18:00 前提交调用清单。"
        manifest = contributions.save_text("S1", "T1", 3, "正文与版式", content, ["R1", "R2"], ["requirement:R1", "requirement:R2"])
        valid, _, restored = contributions.inspect("S1", "T1")
        self.assertTrue(valid)
        self.assertEqual(restored["requirement_ids"], ["R1", "R2"])
        self.assertEqual(Path(manifest["files"][0]["path"]).read_text(encoding="utf-8"), content)
        Path(manifest["files"][0]["path"]).write_text("被修改", encoding="utf-8")
        self.assertFalse(contributions.inspect("S1", "T1")[0])
        self.assertEqual(contributions.integrity_issues("S1", ["T1"])[0]["kind"], "missing_or_changed_contribution")

    def test_fallback_model_is_recorded_without_changing_logical_role(self):
        manifest = contributions.save_text(
            "S1", "T1", 3, "正文与版式", "通知正文", ["R1"], ["requirement:R1"],
            generation_meta={"model": "deepseek-flash", "primary_model": "kimi-k2.6", "fallback_roster": 1},
        )
        self.assertEqual(manifest["roster"], 3)
        self.assertEqual(manifest["model"], "deepseek-flash")
        self.assertEqual(manifest["primary_model"], "kimi-k2.6")
        self.assertEqual(manifest["fallback_roster"], 1)

    def test_fallback_text_is_not_accepted_as_a_real_contribution(self):
        contributions.save_text("S1", "T1", 3, "正文与版式", "当前模型不可用，未能生成本任务的真实内容。",
                                ["R1"], ["requirement:R1"], status="unchecked")
        valid, reason, _ = contributions.inspect("S1", "T1")
        self.assertFalse(valid)
        self.assertIn("未完成", reason)

    def test_completed_contribution_remains_valid_and_traceable(self):
        manifest = contributions.save_text("S1", "T1", 4, "验收", "QA 检查记录", ["R1"], ["requirement:R1"])
        self.assertTrue(contributions.mark_completed("S1", "T1"))
        valid, _, restored = contributions.inspect("S1", "T1")
        self.assertTrue(valid)
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["files"], manifest["files"])

    def test_empty_code_output_is_blocked_instead_of_marked_ready(self):
        manifest = contributions.save_code("S1", "T1", 2, "核心逻辑", [], ["R1"], ["requirement:R1"])
        self.assertEqual(manifest["status"], "blocked")
        valid, reason, _ = contributions.inspect("S1", "T1")
        self.assertFalse(valid)
        self.assertIn("实际文件", reason)

    def test_contribution_without_requirement_or_source_reference_is_rejected(self):
        manifest = contributions.save_text("S1", "T1", 3, "正文与版式", "通知正文", [], [])
        valid, reason, _ = contributions.inspect("S1", "T1")
        self.assertFalse(valid)
        self.assertIn("需求条款", reason)

        manifest["requirement_ids"] = ["R1"]
        manifest["source_refs"] = ["attachment:brief.txt:page:1"]
        Path(contributions._root("S1"), "T1.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        valid, reason, _ = contributions.inspect("S1", "T1")
        self.assertFalse(valid)
        self.assertIn("来源引用", reason)

    def test_malformed_contribution_path_is_rejected_without_crashing(self):
        contributions.save_text("S1", "T1", 3, "正文与版式", "通知正文", ["R1"], ["requirement:R1"])
        manifest_path = Path(contributions._root("S1"), "T1.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = None
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        valid, reason, _ = contributions.inspect("S1", "T1")
        self.assertFalse(valid)
        self.assertIn("路径或哈希", reason)

    def test_parent_directory_in_logical_code_path_is_rejected(self):
        path = self._code("T1", "main.py", "print('ok')\n")
        manifest = contributions.save_code("S1", "T1", 2, "逻辑", [path], ["R1"], ["requirement:R1"])
        manifest["files"][0]["logical_path"] = "../outside.py"
        Path(contributions._root("S1"), "T1.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        valid, reason, _ = contributions.inspect("S1", "T1")
        self.assertFalse(valid)
        self.assertIn("逻辑路径越界", reason)

    def test_code_conflict_and_interface_mismatch(self):
        one = self._code("T1", "main.py", "print('first entry')\n")
        utils = self._code("T1", "utils.py", "def calculate(value):\n    return value\n")
        cli = self._code("T1", "cli.py", "from utils import convert\nprint(convert(25))\n")
        two = self._code("T2", "main.py", "print('other entry')\n")
        contributions.save_code("S1", "T1", 2, "逻辑", [one, utils, cli], ["R1"], ["requirement:R1"])
        contributions.save_code("S1", "T2", 3, "交互", [two], ["R2"], ["requirement:R2"])
        self.assertTrue(contributions.inspect("S1", "T1")[0])
        issues = contributions.scan_conflicts("S1")
        self.assertTrue(any(issue["kind"] == "file_conflict" and issue["path"] == "main.py" for issue in issues))
        self.assertTrue(any(issue["kind"] == "interface_mismatch" and issue["symbol"] == "convert" for issue in issues))

    def test_public_module_constants_are_valid_imports(self):
        core = self._code("T1", "temperature/core.py", "CELSIUS = 'C'\n\ndef convert(value): return value\n")
        package = self._code("T1", "temperature/__init__.py",
                             "from .core import CELSIUS, convert\n__all__ = ['CELSIUS', 'convert']\n")
        contributions.save_code("S1", "T1", 2, "核心逻辑", [core, package], ["R1"], ["requirement:R1"])
        self.assertEqual(contributions.scan_conflicts("S1"), [])

    def test_qa_contribution_is_test_only_and_dev_cannot_duplicate_tests(self):
        qa_test = self._code("T3", "tests/test_cli.py", "def test_cli(): assert True\n")
        production = self._code("T3", "converter.py", "def convert(x): return x\n")
        contributions.save_code("S1", "T3", 4, "测试", [qa_test, production], ["R1"], ["requirement:R1"])
        valid, reason, manifest = contributions.inspect("S1", "T3")
        self.assertFalse(valid)
        self.assertIn("QA 代码贡献只允许测试文件", reason)
        self.assertEqual(manifest["status"], "blocked")

        dev_test = self._code("T4", "tests/test_logic.py", "def test_logic(): assert True\n")
        contributions.save_code("S1", "T4", 3, "界面", [dev_test], ["R1"], ["requirement:R1"])
        valid, reason, _ = contributions.inspect("S1", "T4")
        self.assertFalse(valid)
        self.assertIn("测试文件应由 QA 负责", reason)

    def test_forge_code_enforces_role_file_boundaries(self):
        raw = ("===FILE: converter.py\ndef convert(x): return x\n===END\n"
               "===FILE: tests/test_cli.py\ndef test_cli(): assert True\n===END\n")
        with patch.object(forge, "CODE_DIR", str(Path(self.scratch.name) / "generated")), \
             patch.object(forge, "chat_text", return_value=raw):
            dev_files = forge.forge_code("实现核心逻辑", "需求", "T1", roster=2)
            qa_files = forge.forge_code("编写测试", "需求", "T2", roster=4)
        self.assertEqual([Path(item).name for item in dev_files], ["converter.py"])
        self.assertEqual([Path(item).name for item in qa_files], ["test_cli.py"])

    def test_forge_code_retries_invalid_file_wrapping_without_tool_calls(self):
        with patch.object(forge, "CODE_DIR", str(Path(self.scratch.name) / "generated")), \
             patch.object(forge, "chat_text", side_effect=["plain explanation", "===FILE: converter.py\ndef convert(x): return x\n===END"]) as request:
            files = forge.forge_code("实现核心逻辑", "需求", "T1", roster=2)
        self.assertEqual([Path(item).name for item in files], ["converter.py"])
        self.assertEqual(request.call_count, 2)
        self.assertFalse(request.call_args_list[0].kwargs["tool_mode"])
        self.assertFalse(request.call_args_list[1].kwargs["tool_mode"])


if __name__ == "__main__":
    unittest.main()
