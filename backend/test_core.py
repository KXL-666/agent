"""后端核心逻辑测试（不依赖网络与 LLM）

运行：cd backend && python -m unittest test_core -v
覆盖：Sprint/任务/产物持久化、产物版本链、路径解析、内容解析与代码目录隔离、
API Key 加密落盘、显式保存位置覆盖、Sprint 用量落库、迁移命名修正。
"""

import os
import shutil
import tempfile
import unittest

# 必须在导入 storage 前指定测试数据库，避免污染真实历史
_TMP_DB_DIR = tempfile.mkdtemp(prefix="office_test_db_")
os.environ["OFFICE_DB_PATH"] = os.path.join(_TMP_DB_DIR, "test.db")

import forge  # noqa: E402
import storage  # noqa: E402
import secretbox  # noqa: E402


class StorageTest(unittest.TestCase):
    def test_sprint_lifecycle(self):
        sprint_id = storage.create_sprint("写一份周报")
        self.assertTrue(sprint_id.startswith("S"))
        storage.update_sprint(sprint_id, status="completed", stage="已完成")
        item = storage.get_sprint(sprint_id)
        self.assertEqual(item["requirement"], "写一份周报")
        self.assertEqual(item["status"], "completed")
        ids = [s["id"] for s in storage.list_sprints(50)]
        self.assertIn(sprint_id, ids)

    def test_task_upsert(self):
        sprint_id = storage.create_sprint("测试任务")
        storage.upsert_task(
            sprint_id,
            {"id": "T1", "title": "搭建接口", "role_hint": "后端", "assignee_roster": 2,
             "status": "待开发", "summary": "", "code_files": []},
        )
        storage.upsert_task(
            sprint_id,
            {"id": "T1", "title": "搭建接口", "role_hint": "后端", "assignee_roster": 2,
             "status": "通过", "summary": "done", "code_files": ["api.py"]},
        )
        tasks = storage.list_tasks(sprint_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "通过")
        self.assertEqual(tasks[0]["code_files"], ["api.py"])

    def test_artifact_and_versions(self):
        sprint_id = storage.create_sprint("产物测试")
        first_id = storage.next_artifact_id(sprint_id)
        storage.add_artifact(
            sprint_id,
            {"id": first_id, "kind": "custom", "title": "初稿", "content": "v1",
             "source": "model", "quality_status": "passed",
             "created_at": 1.0, "regen": {"mode": "custom", "prompt": "x"}},
        )
        second_id = storage.next_artifact_id(sprint_id)
        self.assertNotEqual(first_id, second_id)
        storage.add_artifact(
            sprint_id,
            {"id": second_id, "kind": "custom", "title": "修订", "content": "v2",
             "parent_id": first_id, "version": 2, "created_at": 2.0},
        )
        self.assertEqual(storage.latest_artifact_version(sprint_id, first_id), 2)
        storage.update_artifact(first_id, degraded=True, degraded_reason="兜底",
                                source="fallback", quality_status="failed")
        updated = storage.get_artifact(first_id)
        self.assertTrue(updated["degraded"])
        self.assertEqual(updated["source"], "fallback")
        self.assertEqual(updated["quality_status"], "failed")
        self.assertEqual(updated["regen"]["mode"], "custom")
        self.assertEqual(len(storage.list_artifacts(sprint_id)), 2)


class EventAndSearchTest(unittest.TestCase):
    def test_event_roundtrip(self):
        sprint_id = storage.create_sprint("事件测试")
        storage.add_event(sprint_id, {"type": "sprint_start", "ts": 1.0, "requirement": "x"})
        storage.add_event(sprint_id, {"type": "emote", "ts": 2.0})  # 动画类不落库
        storage.add_event(sprint_id, {"type": "stage", "ts": 3.0, "stage": "需求拆解"})
        events = storage.list_events(sprint_id)
        self.assertEqual([e["type"] for e in events], ["sprint_start", "stage"])

    def test_search_covers_history(self):
        sprint_id = storage.create_sprint("写一份季度预算分析表")
        storage.upsert_task(
            sprint_id,
            {"id": "T1", "title": "预算表结构设计", "status": "通过"},
        )
        storage.add_artifact(
            sprint_id,
            {"id": f"{sprint_id}-A1", "kind": "final", "title": "最终交付物 · 季度预算",
             "content": "预算正文", "file_name": "最终交付物.xlsx", "created_at": 1.0},
        )
        result = storage.search("预算")
        self.assertTrue(any(s["id"] == sprint_id for s in result["sprints"]))
        self.assertTrue(any(t["title"] == "预算表结构设计" for t in result["tasks"]))
        self.assertTrue(any(a["file_name"] == "最终交付物.xlsx" for a in result["artifacts"]))

    def test_delete_sprint_cascades(self):
        sprint_id = storage.create_sprint("待删除")
        storage.upsert_task(sprint_id, {"id": "T1", "title": "任务", "status": "通过"})
        storage.add_artifact(
            sprint_id,
            {"id": f"{sprint_id}-A1", "kind": "custom", "title": "产物",
             "content": "x", "file_name": None, "created_at": 1.0},
        )
        storage.add_event(sprint_id, {"type": "stage", "ts": 1.0, "stage": "x"})
        files = storage.delete_sprint(sprint_id)
        self.assertEqual(files, [])
        self.assertIsNone(storage.get_sprint(sprint_id))
        self.assertEqual(storage.list_tasks(sprint_id), [])
        self.assertEqual(storage.list_artifacts(sprint_id), [])
        self.assertEqual(storage.list_events(sprint_id), [])

    def test_artifact_version_chain(self):
        sprint_id = storage.create_sprint("版本链")
        storage.add_artifact(
            sprint_id,
            {"id": f"{sprint_id}-A1", "kind": "custom", "title": "初稿", "content": "v1", "created_at": 1.0},
        )
        storage.add_artifact(
            sprint_id,
            {"id": f"{sprint_id}-A2", "kind": "custom", "title": "修订", "content": "v2",
             "parent_id": f"{sprint_id}-A1", "version": 2, "created_at": 2.0},
        )
        # 从修订版查询应返回整条链
        chain = storage.artifact_versions(sprint_id, f"{sprint_id}-A1")
        self.assertEqual([a["version"] for a in chain], [1, 2])

    def test_version_chain_linear_mount(self):
        """链式挂载（v3 的父级是 v2）也能溯源并取全所有版本"""
        sprint_id = storage.create_sprint("链式版本")
        storage.add_artifact(
            sprint_id,
            {"id": f"{sprint_id}-A1", "kind": "custom", "title": "v1", "content": "1", "created_at": 1.0},
        )
        storage.add_artifact(
            sprint_id,
            {"id": f"{sprint_id}-A2", "kind": "custom", "title": "v2", "content": "2",
             "parent_id": f"{sprint_id}-A1", "version": 2, "created_at": 2.0},
        )
        storage.add_artifact(
            sprint_id,
            {"id": f"{sprint_id}-A3", "kind": "custom", "title": "v3", "content": "3",
             "parent_id": f"{sprint_id}-A2", "version": 3, "created_at": 3.0},
        )
        # 从任意一个版本溯源都应得到根
        self.assertEqual(storage.artifact_root(sprint_id, f"{sprint_id}-A3"), f"{sprint_id}-A1")
        chain = storage.artifact_versions(sprint_id, f"{sprint_id}-A1")
        self.assertEqual([a["version"] for a in chain], [1, 2, 3])
        self.assertEqual(storage.latest_artifact_version(sprint_id, f"{sprint_id}-A3"), 3)

    def test_duplicate_deliverable(self):
        original = forge._unique_name("回滚测试.txt")
        with open(forge.output_path(original), "w", encoding="utf-8") as f:
            f.write("旧版内容")
        try:
            copied = forge.duplicate_deliverable(original)
            self.assertTrue(copied)
            self.assertNotEqual(copied, original)
            with open(forge.output_path(copied), encoding="utf-8") as f:
                self.assertEqual(f.read(), "旧版内容")
            self.assertIsNone(forge.duplicate_deliverable("不存在的文件.txt"))
        finally:
            for name in (original, locals().get("copied")):
                if name:
                    path = forge.output_path(name)
                    if os.path.exists(path):
                        os.remove(path)


class PathTest(unittest.TestCase):
    def test_absolute_path(self):
        result = forge.extract_target_dir(r"把文件放在 D:\资料\周报\目录下")
        self.assertEqual(os.path.normpath(result), os.path.normpath(r"D:\资料\周报"))

    def test_desktop_folder(self):
        result = forge.extract_target_dir("生成 word 放在桌面的 AI产物文件夹下")
        self.assertTrue(result.endswith("AI产物"))
        self.assertNotIn("文件夹", os.path.basename(result))

    def test_clause_is_cut(self):
        result = forge.extract_target_dir("把文件放在桌面上并生成一份报告")
        self.assertEqual(os.path.basename(result), "Desktop")

    def test_non_path_phrase_ignored(self):
        self.assertIsNone(forge.extract_target_dir("把结论放在第一页"))
        self.assertIsNone(forge.extract_target_dir("放在最后"))


class ContentParseTest(unittest.TestCase):
    def test_markdown_blocks(self):
        blocks = forge._parse_docx_markdown(
            "# 标题\n\n## 章节一\n正文段落。\n- 要点一\n\n| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n"
        )
        types = [b["type"] for b in blocks]
        self.assertEqual(types, ["h1", "h2", "p", "bullet", "table"])
        self.assertEqual(blocks[-1]["rows"], [["列A", "列B"], ["1", "2"]])

    def test_bold_segments(self):
        segs = forge._bold_segments("普通**加粗**结尾")
        self.assertEqual(segs, [("普通", False), ("加粗", True), ("结尾", False)])

    def test_pptx_payload_salvage(self):
        # 模型换了字段名（title/points）也能被清洗为可用结构
        payload = forge._sanitize_pptx({
            "title": "测试演示",
            "pages": [
                {"title": "现状", "points": ["要点一内容足够长", "要点二内容也足够长"]},
                {"title": "方案", "points": ["要点三内容足够长"]},
            ],
        })
        self.assertIsNotNone(payload)
        self.assertEqual(len(payload["slides"]), 2)
        self.assertEqual(payload["slides"][0]["heading"], "现状")

    def test_code_marker_parse(self):
        raw = "===FILE: src/main.py\nprint(1)\n===END\n===FILE: a/b.txt\nhello\n===END\n"
        files = forge._parse_code_files(raw)
        self.assertEqual([f[0] for f in files], ["src/main.py", "a/b.txt"])
        self.assertIn("print(1)", files[0][1])

    def test_code_dir_isolation(self):
        original_root = forge.CODE_ROOT
        temp_root = tempfile.mkdtemp(prefix="office_code_")
        try:
            forge.CODE_ROOT = temp_root
            dir_a = forge.begin_sprint("S1")
            with open(os.path.join(dir_a, "a.py"), "w", encoding="utf-8") as f:
                f.write("print(1)\n")
            dir_b = forge.begin_sprint("S2")
            self.assertFalse(os.path.exists(os.path.join(dir_b, "a.py")))
            self.assertTrue(os.path.exists(os.path.join(dir_a, "a.py")))
        finally:
            forge.CODE_ROOT = original_root
            forge.CODE_DIR = os.path.join(original_root, "code")
            shutil.rmtree(temp_root, ignore_errors=True)


class SecretBoxTest(unittest.TestCase):
    def test_roundtrip(self):
        cipher = secretbox.encrypt_value("test-api-key-123456")
        if os.name == "nt":
            self.assertTrue(secretbox.is_encrypted(cipher))
            self.assertEqual(secretbox.decrypt_value(cipher), "test-api-key-123456")
        else:
            self.assertEqual(cipher, "test-api-key-123456")

    def test_decrypt_plain_passthrough(self):
        self.assertEqual(secretbox.decrypt_value("sk-plain"), "sk-plain")
        self.assertEqual(secretbox.decrypt_value(""), "")

    def test_decrypt_corrupted_returns_empty(self):
        if os.name == "nt":
            self.assertEqual(secretbox.decrypt_value("dpapi:not-base64!!"), "")

    def test_agent_config_encrypts_at_rest(self):
        import agent_config

        roster = 99  # 使用独立成员号，避免污染真实配置
        try:
            agent_config.update_agent_config(roster, llm={"api_key": "test-api-key-secret-abc", "base_url": "", "model": ""})
            with open(agent_config.CONFIG_PATH, encoding="utf-8") as f:
                import json

                raw = json.load(f)
            on_disk = raw[str(roster)]["llm"]["api_key"]
            if os.name == "nt":
                self.assertTrue(secretbox.is_encrypted(on_disk))
                self.assertNotIn("test-api-key-secret-abc", on_disk)
            resolved = agent_config.resolve_llm(roster)
            self.assertEqual(resolved["api_key"], "test-api-key-secret-abc")
        finally:
            import json

            with open(agent_config.CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            data.pop(str(roster), None)
            agent_config._save(data)


class ExplicitSaveDirTest(unittest.TestCase):
    def test_override_takes_priority(self):
        target = os.path.join(tempfile.gettempdir(), "office_explicit_dir_test")
        try:
            forge.set_explicit_save_dir(target)
            # 即使需求文本指向别处，显式选择也优先生效
            result = forge.extract_target_dir("把文件放在桌面上")
            self.assertEqual(os.path.normpath(result), os.path.normpath(target))
        finally:
            forge.set_explicit_save_dir("")
        # 清除后恢复文本解析
        result = forge.extract_target_dir("把文件放在桌面上")
        self.assertNotEqual(os.path.normpath(result), os.path.normpath(target))

    def test_known_locations_nonempty(self):
        items = forge.list_known_locations()
        self.assertTrue(len(items) >= 5)
        self.assertTrue(all(item["path"] for item in items))


class UsageStorageTest(unittest.TestCase):
    def test_sprint_usage_persisted(self):
        sprint_id = storage.create_sprint("用量测试")
        storage.update_sprint(
            sprint_id, usage_calls=7, usage_tokens=12345, usage_cost=0.1234
        )
        item = storage.get_sprint(sprint_id)
        self.assertEqual(item["usage_calls"], 7)
        self.assertEqual(item["usage_tokens"], 12345)
        self.assertAlmostEqual(item["usage_cost"], 0.1234)

    def test_legacy_name_fixed(self):
        sprint_id = storage.create_sprint("历史 Sprint（自动迁移）")
        storage.update_sprint(sprint_id, status="completed", started_at=1700000000.0)
        storage.add_artifact(
            sprint_id,
            {"id": f"{sprint_id}-A1", "kind": "custom", "title": "旧产物", "content": "x", "created_at": 1.0},
        )
        storage._fix_legacy_sprint_names()
        item = storage.get_sprint(sprint_id)
        self.assertIn("迁移", item["requirement"])
        self.assertIn("含 1 项", item["requirement"])
        self.assertNotEqual(item["requirement"], "历史 Sprint（自动迁移）")


if __name__ == "__main__":
    unittest.main()
