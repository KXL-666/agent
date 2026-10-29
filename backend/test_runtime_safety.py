"""Recovery, single-process ownership and actual HTTP/WS access boundaries."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from local_access import allowed_origin
from process_lease import backend_lease
import runtime_diagnostics
import office_runtime
import storage


class RecoveryTest(unittest.TestCase):
    def test_repeated_restoration_replaces_snapshots_instead_of_duplicating_tasks(self):
        import main
        import events
        with tempfile.TemporaryDirectory() as directory, patch.object(storage, "DB_PATH", str(Path(directory) / "office.db")):
            storage.init()
            active = storage.create_sprint("恢复最新任务")
            storage.upsert_task(active, {"id": "T1", "title": "仅保留一次", "status": "待测试"})
            storage.recover_interrupted_sprints()
            before = dict(events.sprint_meta)
            before_tasks = events.tasks_store[:]
            before_events = events.event_log[:]
            try:
                with patch.object(main.forge, "begin_sprint"), patch.object(main.artifact_store, "set_sprint"):
                    main.restore_latest_sprint()
                    main.restore_latest_sprint()
                self.assertEqual(events.sprint_meta["status"], "interrupted")
                self.assertEqual([task["id"] for task in events.tasks_store], ["T1"])
            finally:
                events.sprint_meta.clear()
                events.sprint_meta.update(before)
                events.tasks_store[:] = before_tasks
                events.event_log[:] = before_events

    def test_recovery_preserves_evidence_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(storage, "DB_PATH", str(Path(directory) / "office.db")):
            storage.init()
            active = storage.create_sprint("中断需求")
            complete = storage.create_sprint("完成需求")
            storage.update_sprint(complete, status="completed")
            storage.upsert_task(active, {"id": "T1", "status": "待测试", "title": "保留任务"})
            storage.add_artifact(active, {"id": "candidate", "kind": "final", "quality_status": "unchecked", "content": "原有候选"})
            self.assertEqual(storage.recover_interrupted_sprints(), [active])
            self.assertEqual(storage.recover_interrupted_sprints(), [])
            self.assertEqual(storage.get_sprint(active)["status"], "interrupted")
            self.assertGreater(storage.get_sprint(active)["finished_at"], 0)
            self.assertEqual(storage.get_sprint(complete)["status"], "completed")
            self.assertEqual(storage.list_tasks(active)[0]["status"], "待测试")
            self.assertEqual(storage.get_artifact("candidate")["content"], "原有候选")
            self.assertEqual(len(storage.list_events(active)), 1)

    def test_second_process_is_rejected_then_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "office.db")
            command = [sys.executable, "-c",
                       "import sys; from process_lease import backend_lease; "
                       "lease=backend_lease(sys.argv[1]); lease.__enter__(); lease.__exit__(None,None,None)", database]
            options = dict(cwd=Path(__file__).parent, capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with backend_lease(database):
                self.assertNotEqual(subprocess.run(command, **options).returncode, 0)
            self.assertEqual(subprocess.run(command, **options).returncode, 0)


class BrowserAccessTest(unittest.TestCase):
    def test_origins_are_parsed_without_suffix_or_null_bypasses(self):
        for origin in ("http://localhost:5173", "https://127.0.0.1:8000", "http://[::1]:5173"):
            self.assertTrue(allowed_origin(origin), origin)
        for origin in ("null", "https://evil.example", "http://localhost.evil.example", "http://localhost@evil.example",
                       "http://evil.example@localhost", "file://localhost", "http://localhost/path", "http://localhost:99999"):
            self.assertFalse(allowed_origin(origin), origin)

    def test_http_and_websocket_reject_hostile_origin_before_dispatch(self):
        import main
        # No lifespan: this test must never recover or migrate user data.
        client = TestClient(main.app)
        self.addCleanup(client.close)
        self.assertEqual(client.post("/api/sprint/stop", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(client.get("/api/health", headers={"Origin": "null"}).status_code, 403)
        self.assertEqual(client.get("/api/health", headers={"Origin": "http://localhost:5173"}).status_code, 200)
        self.assertEqual(client.get("/api/health").status_code, 200)
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/ws/office", headers={"Origin": "https://evil.example"}):
                self.fail("hostile socket accepted")
        with client.websocket_connect("/ws/office", headers={"Origin": "http://localhost:5173"}) as socket:
            self.assertEqual(socket.receive_json()["type"], "hello")


class ShutdownStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_is_interrupted_and_user_cancellation_is_stopped(self):
        import asyncio
        import events
        import llm
        import runtime_state
        import sprint_runner
        with tempfile.TemporaryDirectory() as directory, patch.object(storage, "DB_PATH", str(Path(directory) / "office.db")):
            storage.init()
            for shutdown, expected in ((True, "interrupted"), (False, "stopped")):
                sid = storage.create_sprint("取消原因检查")
                metadata = {"id": sid, "requirement": "取消原因检查", "usage_baseline": llm.usage_total_cost(),
                            "usage_baseline_calls": llm.usage_total_calls(), "usage_baseline_tokens": llm.usage_total_tokens(),
                            "finished_at": 0, "budget": 0, "started_at": 0}
                fake_graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=asyncio.CancelledError), checkpointer=MagicMock())
                with (patch.dict(events.sprint_meta, metadata),
                      patch.object(runtime_state, "graph", fake_graph),
                      patch.object(runtime_state, "shutting_down", shutdown),
                      patch.object(runtime_state, "running", True),
                      patch.object(events, "emit"),
                      patch.object(sprint_runner.forge, "set_explicit_save_dir")):
                    with self.assertRaises(asyncio.CancelledError):
                        await sprint_runner.run_sprint("取消原因检查")
                self.assertEqual(storage.get_sprint(sid)["status"], expected)


class RuntimeDiagnosisTest(unittest.TestCase):
    def test_success_exit_without_new_output_cannot_reuse_old_pdf(self):
        process = MagicMock(returncode=0)
        process.communicate.return_value = (b"", b"")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.docx"
            source.write_bytes(b"original")
            destination = Path(directory) / "pdf"
            destination.mkdir()
            old = destination / "source.pdf"
            old.write_bytes(b"stale output")
            with (patch.object(office_runtime, "find_office", return_value="soffice"),
                  patch.object(office_runtime.subprocess, "Popen", return_value=process)):
                with self.assertRaises(RuntimeError):
                    office_runtime.convert(source, destination)
            self.assertEqual(old.read_bytes(), b"stale output")
            self.assertEqual(source.read_bytes(), b"original")

    def test_only_fresh_conversion_replaces_destination(self):
        process = MagicMock(returncode=0)
        process.communicate.return_value = (b"converted", b"")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.docx"
            source.write_bytes(b"original")
            destination = Path(directory) / "pdf"
            destination.mkdir()
            old = destination / "source.pdf"
            old.write_bytes(b"stale output")
            def convert_to_staging(command, **kwargs):
                Path(command[command.index("--outdir") + 1], "source.pdf").write_bytes(b"fresh output")
                return process
            with (patch.object(office_runtime, "find_office", return_value="soffice"),
                  patch.object(office_runtime.subprocess, "Popen", side_effect=convert_to_staging)):
                result = office_runtime.convert(source, destination)
            self.assertEqual(result, old)
            self.assertEqual(result.read_bytes(), b"fresh output")
            self.assertEqual(list(destination.iterdir()), [old])

    def test_docker_cli_without_daemon_is_not_reported_ready(self):
        with (patch.object(runtime_diagnostics.shutil, "which", side_effect=lambda name: name),
              patch.object(runtime_diagnostics, "probe", side_effect=lambda command: "v24.18.0" if command[0] == "node" else None),
              patch("office_runtime.find_office", return_value=None)):
            checks = {check["key"]: check for check in runtime_diagnostics.diagnose()["checks"]}
        self.assertEqual(checks["docker"]["status"], "missing")
        self.assertEqual(checks["office"]["status"], "missing")
        self.assertEqual(checks["model"]["status"], "unchecked")
        self.assertEqual(checks["node"]["status"], "ready")

    def test_office_timeout_terminates_its_tree_and_keeps_source(self):
        from unittest.mock import MagicMock
        process = MagicMock(pid=12345)
        process.communicate.side_effect = subprocess.TimeoutExpired("soffice", 120)
        process.poll.return_value = None
        process.stdout = process.stderr = None
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.docx"
            source.write_bytes(b"original")
            with (patch.object(office_runtime, "find_office", return_value="soffice.com"),
                  patch.object(office_runtime.subprocess, "Popen", return_value=process),
                  patch.object(office_runtime.subprocess, "run") as cleanup,
                  patch.object(office_runtime, "os") as platform):
                platform.name = "nt"
                with self.assertRaisesRegex(RuntimeError, "120"):
                    office_runtime.convert(source, Path(directory) / "rendered")
                self.assertEqual(cleanup.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])
                process.kill.assert_called_once()
                process.wait.assert_called_once()
            self.assertEqual(source.read_bytes(), b"original")
