"""Skills persistence, API validation and actual outbound model payloads (no paid calls)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

import llm
import skills
from skills_api import router


class SkillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = patch.object(skills, "STORE_PATH", Path(self.temp.name) / "skills.json")
        self.store.start()
        self.app = FastAPI()
        self.app.include_router(router)
        self.body = dict(name="周报", description="周报任务", instructions="必须列出下周计划", rosters=[1], enabled=True)

    def tearDown(self):
        self.store.stop()
        self.temp.cleanup()

    async def test_crud_validation_and_member_isolation(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            created = await client.post("/api/skills", json=self.body)
            self.assertEqual(created.status_code, 200)
            skill_id = created.json()["id"]
            self.assertEqual(json.loads(skills.STORE_PATH.read_text(encoding="utf-8"))[0]["name"], "周报")
            self.assertIn("必须列出下周计划", (await client.get("/api/skills/member/1")).json()["prompt"])
            self.assertEqual((await client.get("/api/skills/member/2")).json()["prompt"], "")
            self.assertEqual((await client.post("/api/skills", json={**self.body, "rosters": [7]})).status_code, 400)
            self.assertEqual((await client.post("/api/skills", json={**self.body, "instructions": " "})).status_code, 400)
            self.assertEqual((await client.put(f"/api/skills/{skill_id}", json={**self.body, "enabled": False})).status_code, 200)
            self.assertEqual(skills.augment_system("base", 1), "base")
            self.assertEqual((await client.delete(f"/api/skills/{skill_id}")).status_code, 200)
            self.assertEqual((await client.delete(f"/api/skills/{skill_id}")).status_code, 404)
            self.assertEqual(skills.list_skills(), [])

    def test_markdown_import_export_and_limits(self):
        markdown = '---\nname: "专业周报"\ndescription: >\n  汇总工作\n  撰写周报\n---\n# 工作步骤\n必须检查数字'
        values = skills.parse_markdown(markdown)
        self.assertEqual(values["name"], "专业周报")
        self.assertEqual(values["description"], "汇总工作 撰写周报")
        self.assertFalse(values["enabled"])
        self.assertEqual(values["rosters"], [])
        skills.save_skill(values)
        self.assertEqual(skills.parse_markdown('# 简单技能\n步骤')["name"], "简单技能")
        # Exported JSON-quoted YAML values must round-trip punctuation and newlines.
        value = '带有"引号"的名称'
        exported = f'---\nname: {json.dumps(value, ensure_ascii=False)}\ndescription: "场景"\n---\n指令'
        self.assertEqual(skills.parse_markdown(exported)["name"], value)
        with self.assertRaises(ValueError):
            skills.parse_markdown('---\nname: broken')
        for i in range(2):
            skills.save_skill({**self.body, "name": str(i), "instructions": "a" * 12000})
        with self.assertRaises(ValueError):
            skills.save_skill({**self.body, "instructions": "b" * 12000})

    def test_text_and_json_payloads_use_live_assignment(self):
        item = skills.save_skill(self.body)
        with patch.object(llm, "_resolve", return_value=("http://model", "fake", "test")), patch.object(llm, "_post", return_value="ok") as post:
            meta = {}
            self.assertEqual(llm.chat_text("base", [], "fallback", roster=1, meta=meta), "ok")
            self.assertIn("必须列出下周计划", post.call_args.args[3]["messages"][0]["content"])
            self.assertEqual(meta["skills"][0]["name"], "周报")
            skills.save_skill({**self.body, "instructions": "更新后的技能"}, item["id"])
            post.return_value = '{"ok":true}'
            self.assertEqual(llm.chat_json("JSON only", "task", {}, roster=1), {"ok": True})
            self.assertIn("更新后的技能", post.call_args.args[3]["messages"][0]["content"])
            llm.chat_text("base", [], "fallback", roster=2)
            self.assertEqual(post.call_args.args[3]["messages"][0]["content"], "base")
            self.assertEqual(skills.augment_system("base", None), "base")

    def test_stream_payload(self):
        skills.save_skill(self.body)
        class Stream:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def raise_for_status(self): pass
            def iter_lines(self):
                return iter(['data: {"choices":[{"delta":{"content":"done"}}]}', 'data: [DONE]'])
        with patch.object(llm, "_resolve", return_value=("http://model", "fake", "test")), patch.object(llm.httpx, "stream", return_value=Stream()) as stream:
            self.assertEqual(list(llm.stream_chat("base", [], 1, "fallback")), ["done"])
            self.assertIn("必须列出下周计划", stream.call_args.kwargs["json"]["messages"][0]["content"])

    async def test_trial_never_reports_fallback_as_success(self):
        def fail(*args, **kwargs):
            kwargs["meta"]["fallback_reason"] = "test failure"
            return ""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            with patch.object(llm, "llm_available", return_value=False):
                self.assertEqual((await client.post("/api/skills/trial", json={"roster": 1, "message": "test"})).status_code, 400)
            with patch.object(llm, "llm_available", return_value=True), patch.object(llm, "chat_text", side_effect=fail):
                self.assertEqual((await client.post("/api/skills/trial", json={"roster": 1, "message": "test"})).status_code, 502)


if __name__ == "__main__":
    unittest.main()
