"""Complete skill package and model tool-call regression tests (offline fixtures)."""
import io
import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

import httpx
from fastapi import FastAPI

import llm
import skill_agent_tools
import skill_packages
import skills
from skills_api import router


def archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for name, content in files.items():
            output.writestr(name, content)
    return buffer.getvalue()


class PackageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store_patch = patch.object(skills, "STORE_PATH", self.root / "skills.json")
        self.package_patch = patch.object(skill_packages, "PACKAGE_ROOT", self.root / "packages")
        self.store_patch.start()
        self.package_patch.start()

    def tearDown(self):
        self.package_patch.stop()
        self.store_patch.stop()
        self.temp.cleanup()

    def test_full_package_script_asset_and_output(self):
        data = archive({
            "example/SKILL.md": "---\nname: Example\ndescription: Uses an asset\n---\nRun scripts/use.py",
            "example/references/input.txt": "attached reference",
            "example/scripts/use.py": "from pathlib import Path\nprint(Path('references/input.txt').read_text())\nPath('result.txt').write_text('created')\n",
        })
        item = skill_packages.install(data, 1)[0]
        self.assertEqual(item["install_status"], "ready")
        self.assertEqual(item["tools"], [{"path": "scripts/use.py", "runtime": "python"}])
        self.assertEqual(skill_packages.read_asset(item["id"], 1, "references/input.txt")["text"], "attached reference")
        result = skill_packages.run_tool(item["id"], 1, "scripts/use.py")
        self.assertTrue(result["ok"], result)
        self.assertIn("attached reference", result["output"])
        self.assertIn("result.txt", result["files_created"])
        self.assertEqual(skill_packages.read_asset(item["id"], 1, "result.txt")["text"], "created")
        with self.assertRaises(ValueError):
            skill_packages.run_tool(item["id"], 2, "scripts/use.py")

    def test_github_directory_and_multiple_skill_discovery(self):
        data = archive({"repo-main/skills/a/SKILL.md": "# A\nsteps",
                        "repo-main/skills/b/SKILL.md": "# B\nsteps"})
        source = "https://github.com/owner/repo/tree/main/skills"
        with patch.object(skill_packages, "_download", return_value=data) as download:
            items = skill_packages.install(source, 2)
        self.assertEqual({item["name"] for item in items}, {"A", "B"})
        self.assertEqual(download.call_args.args[0], "https://api.github.com/repos/owner/repo/zipball/main")

    def test_unknown_dependency_is_reported_and_not_enabled(self):
        data = archive({"skill/SKILL.md": "# External tool\nsteps",
                        "skill/skill-package.json": json.dumps({"commands": ["missing-skill-command-xyz"]}),
                        "skill/scripts/run.js": "console.log('ok')"})
        item = skill_packages.install(data, 1)[0]
        self.assertEqual(item["install_status"], "needs_attention")
        self.assertFalse(item["enabled"])
        self.assertIn("缺少外部工具", item["install_issues"][0])
        with self.assertRaises(ValueError):
            skills.save_skill({**item, "enabled": True}, item["id"])

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_node_script_uses_packaged_asset(self):
        data = archive({"skill/SKILL.md": "# Node example\nRun scripts/use.mjs",
                        "skill/assets/value.txt": "bundle asset",
                        "skill/scripts/use.mjs": "import fs from 'node:fs'; console.log(fs.readFileSync('assets/value.txt', 'utf8'))"})
        item = skill_packages.install(data, 3)[0]
        result = skill_packages.run_tool(item["id"], 3, "scripts/use.mjs")
        self.assertTrue(result["ok"], result)
        self.assertIn("bundle asset", result["output"])

    def test_archive_rejects_escape_and_symlink(self):
        with self.assertRaises(ValueError):
            skill_packages.install(archive({"../outside.txt": "bad", "SKILL.md": "# x"}), 1)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            output.writestr("SKILL.md", "# x\nsteps")
            link = zipfile.ZipInfo("scripts/link.py")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(link, "../../outside")
        with self.assertRaises(ValueError):
            skill_packages.install(buffer.getvalue(), 1)
        with self.assertRaises(ValueError):
            skill_packages._public_https("https://127.0.0.1/skill.zip")
        self.assertEqual(skill_packages._github_archive("https://github.com/a/b/archive/refs/heads/main.zip"),
                         ("https://github.com/a/b/archive/refs/heads/main.zip", ""))

    def test_model_tool_call_roundtrip(self):
        first = httpx.Response(200, request=httpx.Request("POST", "https://model/chat/completions"),
                               json={"choices": [{"message": {"role": "assistant", "content": None,
                                   "tool_calls": [{"id": "c1", "type": "function", "function":
                                                   {"name": "list_member_skills", "arguments": "{}"}}]}}]})
        second = httpx.Response(200, request=httpx.Request("POST", "https://model/chat/completions"),
                                json={"choices": [{"message": {"role": "assistant", "content": "已检查技能"}}]})
        with patch.object(llm.httpx, "post", side_effect=[first, second]) as post:
            result = llm._post("https://model", "fake", "test", {"messages": [{"role": "system", "content": "base"}]},
                               10, roster=1, tool_mode=True)
        self.assertEqual(result, "已检查技能")
        self.assertEqual(post.call_count, 2)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["messages"][-1]["role"], "tool")
        self.assertEqual(sent["messages"][-1]["tool_call_id"], "c1")

    def test_agent_installs_package_for_its_own_roster(self):
        data = archive({"skill/SKILL.md": "# Agent install\nSteps"})
        with patch.object(skill_packages, "_download", return_value=data):
            result = skill_agent_tools.execute("install_skill_package",
                                              json.dumps({"url": "https://example.org/skill.zip"}), 4)
        self.assertEqual(result["installed"][0]["name"], "Agent install")
        self.assertEqual(skills.member_skills(1), [])
        self.assertEqual(len(skills.member_skills(4)), 1)

    def test_streaming_tool_call_roundtrip(self):
        class Stream:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def raise_for_status(self): pass
            def iter_lines(self):
                return iter([
                    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"list_member_skills","arguments":"{}"}}]}}]}',
                    'data: [DONE]',
                ])
        with patch.object(llm, "_resolve", return_value=("https://model", "fake", "test")), \
             patch.object(llm.httpx, "stream", return_value=Stream()), \
             patch.object(llm, "_post", return_value="技能已列出") as post:
            chunks = list(llm.stream_chat("system", [{"role": "user", "content": "列出技能"}], 1, "fallback"))
        self.assertEqual(chunks, ["技能已列出"])
        self.assertEqual(post.call_args.kwargs["tool_mode"], True)
        self.assertEqual(post.call_args.args[3]["messages"][-1]["role"], "tool")

    async def test_zip_upload_endpoint(self):
        app = FastAPI()
        app.include_router(router)
        data = archive({"skill/SKILL.md": "# Uploaded\nSteps", "skill/references/note.txt": "attached"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/skills/install-zip", data={"roster": "1"},
                                         files={"file": ("skill.zip", data, "application/zip")})
            self.assertEqual(response.status_code, 200, response.text)
            skill_id = response.json()["skills"][0]["id"]
            files = await client.get(f"/api/skills/{skill_id}/files", params={"roster": 1})
            self.assertEqual(files.status_code, 200)
            self.assertIn("references/note.txt", [entry["path"] for entry in files.json()["files"]])
            attachment = await client.get(f"/api/skills/{skill_id}/files/references/note.txt", params={"roster": 1})
            self.assertEqual(attachment.status_code, 200)
            self.assertEqual(attachment.content, b"attached")
            exported = await client.get(f"/api/skills/{skill_id}/export", params={"roster": 1})
            self.assertEqual(exported.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(exported.content)) as bundle:
                self.assertEqual(bundle.read("references/note.txt"), b"attached")


if __name__ == "__main__":
    unittest.main()
