"""Real SQLite and subprocess restart tests. All workflow/LLM work is offline."""
import asyncio
from contextlib import ExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from langgraph.errors import NodeCancelledError

import artifacts
import events
import forge
import graph as graph_module
import llm
import runtime_state
import sprint_accounting
import sprint_recovery as recovery
import storage

REQUIREMENT = "恢复测试：生成通知并完成验收"


def options():
    return {"schema": recovery.SCHEMA, "workflow_signature": recovery.signature(), "fast": True,
            "save_dir": "", "task_kind": "auto"}


def configure_paths(root):
    forge.WORKSPACE_DIR = str(root / "workspace")
    forge.DELIVERABLES_DIR = str(root / "deliverables")
    forge.CODE_ROOT = str(root / "workspace/code")
    forge.CODE_DIR = forge.CODE_ROOT
    Path(forge.WORKSPACE_DIR).mkdir(exist_ok=True)
    Path(forge.DELIVERABLES_DIR).mkdir(exist_ok=True)
    Path(forge.CODE_ROOT).mkdir(exist_ok=True)


def fake_node(name, root, stopping=None, worker=False):
    async def run(state):
        with (root / "calls.txt").open("a", encoding="utf-8") as output:
            output.write(name + "\n")
        if name == stopping:
            if worker:
                (root / "started.flag").write_text("ready")
                await asyncio.Event().wait()
            raise asyncio.CancelledError()
        if name == "kickoff":
            source = Path(forge.WORKSPACE_DIR) / "seed.txt"
            source.write_text("immutable source", encoding="utf-8")
            storage.add_artifact(events.sprint_meta["id"], {
                "id": "seed", "kind": "dev", "file_name": "_process/seed.txt", "content": "seed"})
            return {"tasks": [{"id": "T1", "title": "通知", "status": "待开发", "assignee_roster": 2}], "rework_count": 0}
        if name == "develop":
            from event_broadcaster import drain_instructions
            feedback = drain_instructions()
            if feedback:
                (root / "feedback.txt").write_text(feedback, encoding="utf-8")
            for task in state["tasks"]:
                if task["status"] != "已取消":
                    task["status"] = "通过"
        if name == "publish":
            artifacts.add_artifact("final", "offline final", 6, "done", quality_status="passed")
        return {"tasks": state.get("tasks", []), "deliver_kind": "docx"}
    run.__name__ = name
    return run


@asynccontextmanager
async def offline_graph(root, stopping=None, worker=False):
    with ExitStack() as patches:
        for node in recovery.NODES:
            patches.enter_context(patch.object(graph_module, node, fake_node(node, root, stopping, worker)))
        async with recovery.persistent_graph() as compiled:
            with (patch.object(runtime_state, "graph", compiled),
                  patch.object(runtime_state, "persistent_ready", True)):
                yield compiled


def initial():
    return {"requirement": REQUIREMENT, "requirement_contract": {}, "tasks": [], "rework_count": 0,
            "uploads_text": "retained attachment", "uploads_images": [], "task_kind": "auto",
            "deliver_kind": "docx", "pending_instructions": [], "instruction_cutoff": time.time()}


async def crash_worker(root):
    configure_paths(root)
    sid = storage.create_sprint(REQUIREMENT, budget=1)
    saved = {**options(), "owner": recovery.owner()}
    storage.update_sprint(sid, execution_style="collaborative", run_options_json=json.dumps(saved))
    events.sprint_meta.update(id=sid, requirement=REQUIREMENT)
    artifacts.set_sprint(sid)
    (root / "sprint-id.txt").write_text(sid)
    async with offline_graph(root, "develop", worker=True) as compiled:
        await compiled.ainvoke(initial(), config={"configurable": {"thread_id": sid}}, durability="sync")


class RecoveryIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(storage, "DB_PATH", str(self.root / "office.db")))
        self.stack.enter_context(patch.multiple(forge, WORKSPACE_DIR=str(self.root / "workspace"),
            DELIVERABLES_DIR=str(self.root / "deliverables"), CODE_ROOT=str(self.root / "workspace/code"), CODE_DIR=str(self.root / "workspace/code")))
        configure_paths(self.root)
        storage.init()
        self.stack.enter_context(patch.object(runtime_state, "running", False))
        self.stack.enter_context(patch.object(runtime_state, "requires_restart", False))
        self.stack.enter_context(patch.object(runtime_state, "recovery_lock", asyncio.Lock()))
        self.stack.enter_context(patch.object(runtime_state, "sprint_task", None))
        self.stack.enter_context(patch.object(events, "emit"))
        self.previous_meta = dict(events.sprint_meta)
        self.previous_tasks = events.tasks_store[:]
        self.previous_artifacts = artifacts.artifacts_store[:]
        self.previous_sid = artifacts.current_sprint_id
        import event_broadcaster
        self.previous_instructions = event_broadcaster.snapshot_instructions()
        event_broadcaster.restore_instructions([])

    async def asyncTearDown(self):
        if runtime_state.sprint_task and not runtime_state.sprint_task.done():
            runtime_state.sprint_task.cancel()
            await asyncio.gather(runtime_state.sprint_task, return_exceptions=True)
        events.sprint_meta.clear()
        events.sprint_meta.update(self.previous_meta)
        events.tasks_store[:] = self.previous_tasks
        artifacts.artifacts_store[:] = self.previous_artifacts
        artifacts.current_sprint_id = self.previous_sid
        import event_broadcaster
        event_broadcaster.restore_instructions(self.previous_instructions)
        self.stack.close()
        self.temp.cleanup()

    async def interrupted(self):
        sid = storage.create_sprint(REQUIREMENT, budget=1)
        storage.update_sprint(sid, execution_style="collaborative", run_options_json=json.dumps(options()))
        events.sprint_meta.update(id=sid, requirement=REQUIREMENT)
        artifacts.set_sprint(sid)
        async with offline_graph(self.root, "develop") as compiled:
            with self.assertRaises((asyncio.CancelledError, NodeCancelledError)):
                await compiled.ainvoke(initial(), config={"configurable": {"thread_id": sid}}, durability="sync")
        storage.recover_interrupted_sprints()
        return sid

    async def test_reopen_continues_next_node_and_retains_attachment_and_files(self):
        sid = await self.interrupted()
        async with offline_graph(self.root) as compiled:
            preview = await recovery.plan(sid)
            self.assertTrue(preview["resumable"], preview)
            self.assertEqual(preview["next_nodes"], ["develop"])
            result = await compiled.ainvoke(None, config={"configurable": {"thread_id": sid}}, durability="sync")
            self.assertEqual(result["uploads_text"], "retained attachment")
        calls = (self.root / "calls.txt").read_text().splitlines()
        self.assertEqual(calls.count("kickoff"), 1)
        self.assertEqual(calls.count("assign"), 1)
        self.assertEqual(calls.count("develop"), 2)
        self.assertEqual(calls.count("publish"), 1)

    async def test_tampered_files_stale_tokens_and_code_changes_block_recovery(self):
        sid = await self.interrupted()
        async with offline_graph(self.root):
            preview = await recovery.plan(sid)
            from routers.sprint import recover_sprint, RecoverRequest
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as caught:
                await recover_sprint(sid, RecoverRequest(checkpoint_id="outdated", request_id="old"))
            self.assertEqual(caught.exception.status_code, 409)
            with patch.object(recovery, "signature", return_value="changed"):
                self.assertFalse((await recovery.plan(sid))["resumable"])
            (self.root / "workspace/seed.txt").write_text("changed")
            self.assertFalse((await recovery.plan(sid))["resumable"])
        self.assertEqual((self.root / "calls.txt").read_text().splitlines().count("develop"), 1)

    async def test_activation_failure_releases_runtime_and_preserves_checkpoint(self):
        from fastapi import HTTPException
        from routers.sprint import recover_sprint, RecoverRequest
        sid = await self.interrupted()
        async with offline_graph(self.root):
            checkpoint_id = (await recovery.plan(sid))["checkpoint_id"]
            with patch("routers.sprint.llm_available", return_value=True), patch.object(recovery, "activate", side_effect=OSError("permission")):
                with self.assertRaises(HTTPException) as caught:
                    await recover_sprint(sid, RecoverRequest(checkpoint_id=checkpoint_id, request_id="failed-activation"))
            self.assertEqual(caught.exception.status_code, 503)
            self.assertFalse(runtime_state.running)
            self.assertIsNone(runtime_state.sprint_task)
            self.assertEqual(storage.get_sprint(sid)["status"], "interrupted")
            snapshot = await runtime_state.graph.aget_state({"configurable": {"thread_id": sid}})
            self.assertEqual(snapshot.config["configurable"]["checkpoint_id"], checkpoint_id)

    async def test_resume_api_is_idempotent_and_preserves_paid_usage(self):
        sid = await self.interrupted()
        storage.increment_sprint_usage(sid, 200, .02)
        storage.add_event(sid, {"type": "instruction_added", "ts": time.time(), "text": "最后补充要求"})
        async with offline_graph(self.root):
            from routers.sprint import recover_sprint, RecoverRequest
            request = RecoverRequest(checkpoint_id=(await recovery.plan(sid))["checkpoint_id"], request_id="same-request")
            with patch("routers.sprint.llm_available", return_value=True):
                first = await recover_sprint(sid, request)
                second = await recover_sprint(sid, request)
                self.assertFalse(first["reused"])
                self.assertTrue(second["reused"])
                await runtime_state.sprint_task
            self.assertEqual(storage.get_sprint(sid)["status"], "completed")
            self.assertAlmostEqual(storage.get_sprint(sid)["usage_cost"], .02)
            self.assertEqual(storage.get_sprint(sid)["usage_calls"], 1)
            self.assertIn("最后补充要求", (self.root / "feedback.txt").read_text(encoding="utf-8"))
            self.assertIsNotNone(storage.get_resume_request(sid, "same-request"))
            changed = storage.get_sprint(sid)["run_options"]
            changed["last_resume_request"] = {"id": "newer-request", "checkpoint_id": "different"}
            storage.update_sprint(sid, run_options_json=json.dumps(changed))
            self.assertTrue((await recover_sprint(sid, request))["reused"])

    async def test_real_graph_shutdown_keeps_a_durable_next_node(self):
        from sprint_runner import run_sprint
        sid = storage.create_sprint(REQUIREMENT, budget=1)
        storage.update_sprint(sid, execution_style="collaborative", run_options_json=json.dumps(options()))
        events.sprint_meta.update(id=sid, requirement=REQUIREMENT, budget=1, usage_baseline=llm.usage_total_cost(),
                                 usage_baseline_calls=llm.usage_total_calls(), usage_baseline_tokens=llm.usage_total_tokens(), finished_at=0)
        artifacts.set_sprint(sid)
        async with offline_graph(self.root, "develop", worker=True) as compiled:
            task = asyncio.create_task(run_sprint(REQUIREMENT))
            while not (self.root / "started.flag").exists():
                if task.done():
                    await task
                    self.fail("graph ended before develop")
                await asyncio.sleep(.01)
            with patch.object(runtime_state, "shutting_down", True):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(storage.get_sprint(sid)["status"], "interrupted")
            checkpoint = await compiled.aget_state({"configurable": {"thread_id": sid}})
            self.assertEqual(checkpoint.next, ("develop",))

    async def test_remaining_budget_owner_and_busy_guards(self):
        sid = await self.interrupted()
        async with offline_graph(self.root):
            saved = {**options(), "owner": recovery.owner()}
            storage.update_sprint(sid, run_options_json=json.dumps(saved))
            self.assertFalse((await recovery.plan(sid))["resumable"])
            storage.update_sprint(sid, run_options_json=json.dumps(options()), usage_cost=1)
            self.assertFalse((await recovery.plan(sid))["resumable"])
            storage.update_sprint(sid, usage_cost=0)
            from routers.sprint import recover_sprint, RecoverRequest
            from fastapi import HTTPException
            preview = await recovery.plan(sid)
            with patch.object(runtime_state, "running", True):
                with self.assertRaises(HTTPException) as caught:
                    await recover_sprint(sid, RecoverRequest(checkpoint_id=preview["checkpoint_id"], request_id="busy"))
                self.assertEqual(caught.exception.status_code, 409)

    async def test_task_controls_and_pending_instructions_survive_mid_node_restart(self):
        sid = await self.interrupted()
        storage.add_event(sid, {"type": "task_control", "ts": time.time(), "task_id": "T1", "action": "cancel"})
        storage.add_event(sid, {"type": "task_control", "ts": time.time(), "task_id": "T1", "action": "reassign", "roster": 5})
        storage.add_event(sid, {"type": "instruction_added", "ts": time.time(), "text": "保留最新要求"})
        async with offline_graph(self.root) as compiled:
            state = await compiled.aget_state({"configurable": {"thread_id": sid}})
            self.assertEqual(storage.instructions_since(sid, state.values["instruction_cutoff"]), ["保留最新要求"])
            result = await compiled.ainvoke(None, config={"configurable": {"thread_id": sid}}, durability="sync")
            self.assertEqual(result["tasks"][0]["status"], "已取消")
            self.assertEqual(result["tasks"][0]["assignee_roster"], 5)

    async def test_sqlite_snapshot_includes_checkpoint_wal_pages(self):
        sid = await self.interrupted()
        async with offline_graph(self.root) as compiled:
            expected = await compiled.aget_state({"configurable": {"thread_id": sid}})
            backup = str(self.root / "backup.db")
            storage.snapshot_database(backup)
        with patch.object(storage, "DB_PATH", backup):
            async with offline_graph(self.root) as restored:
                actual = await restored.aget_state({"configurable": {"thread_id": sid}})
                self.assertEqual(actual.config["configurable"]["checkpoint_id"], expected.config["configurable"]["checkpoint_id"])

    async def test_hard_killed_process_resumes_without_repeating_completed_nodes(self):
        env = {**os.environ, "OFFICE_DB_PATH": storage.DB_PATH,
               "OFFICE_AGENT_CONFIG_PATH": str(self.root / "agents.json"), "OFFICE_USAGE_PATH": str(self.root / "usage.json")}
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--crash-worker", str(self.root)],
                                   cwd=Path(__file__).parent, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            began = time.monotonic()
            while not (self.root / "started.flag").exists():
                if process.poll() is not None:
                    self.fail(process.stderr.read().decode("utf-8", errors="replace"))
                if time.monotonic() - began > 20:
                    self.fail("worker did not reach interrupted node")
                await asyncio.sleep(.02)
            process.kill()
            await asyncio.to_thread(process.wait, 10)
            sid = (self.root / "sprint-id.txt").read_text()
            storage.recover_interrupted_sprints()
            events.sprint_meta.update(id=sid, requirement=REQUIREMENT)
            artifacts.set_sprint(sid)
            async with offline_graph(self.root) as compiled:
                self.assertTrue((await recovery.plan(sid))["resumable"])
                await compiled.ainvoke(None, config={"configurable": {"thread_id": sid}}, durability="sync")
            calls = (self.root / "calls.txt").read_text().splitlines()
            self.assertEqual(calls.count("kickoff"), 1)
            self.assertEqual(calls.count("assign"), 1)
            self.assertEqual(calls.count("publish"), 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stderr.close()


class ScopedUsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_threaded_calls_persist_only_their_own_sprint(self):
        import copy
        previous = copy.deepcopy(llm._usage)
        self.addCleanup(lambda: (llm._usage.clear(), llm._usage.update(previous)))
        with tempfile.TemporaryDirectory() as directory, patch.object(storage, "DB_PATH", str(Path(directory) / "db")):
            storage.init()
            sid = storage.create_sprint("usage")
            token = sprint_accounting.current.set(sprint_accounting.Scope(sid))
            try:
                with patch.object(llm, "_save_usage"):
                    await asyncio.to_thread(llm._record_usage, 1, "deepseek-chat", {"prompt_tokens": 1000, "completion_tokens": 100})
            finally:
                sprint_accounting.current.reset(token)
            before = storage.get_sprint(sid)
            with patch.object(llm, "_save_usage"):
                llm._record_usage(2, "deepseek-chat", {"prompt_tokens": 5000, "completion_tokens": 1000})
            after = storage.get_sprint(sid)
            self.assertEqual(after["usage_calls"], 1)
            self.assertEqual(after["usage_tokens"], 1100)
            self.assertEqual(after["usage_cost"], before["usage_cost"])


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--crash-worker":
    asyncio.run(crash_worker(Path(sys.argv[2])))
