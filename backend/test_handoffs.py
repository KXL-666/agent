"""Offline handoff checks across file/code roles, persistence and concurrency."""

import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import events
import graph
import workflow_nodes
import handoffs
import storage
from requirement_contract import build_contract
from roles import DEV_BE, DEV_FE, PO, SM


class HandoffTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = os.path.join(self.scratch.name, "handoffs.db")
        storage.init()
        self.sprint_id = storage.create_sprint("交付一份 Word 文件，一页。")
        events.sprint_meta.update(id=self.sprint_id, fast=True, usage_baseline=0.0, started_at=0.0)
        self.contract = build_contract(
            "交付一份 Word 文件，一页。", model_contract={
                "target_kind": "docx", "audience": "同事", "purpose": "通知",
                "missing_info": [], "allowed_assumptions": [],
                "requirements": [
                    {"source": "user", "source_quote": part, "acceptance_method": f"核对：{part}"}
                    for part in ("交付一份 Word 文件", "一页")
                ],
            }, deliver_kind="docx")
        storage.save_contract(self.sprint_id, self.contract)

    async def asyncTearDown(self):
        storage.DB_PATH = self.old_db
        self.scratch.cleanup()

    async def test_accept_reject_and_missing_references(self):
        item = {"id": "T1", "title": "资料核对", "requirement_ids": ["R1"]}
        storage.upsert_task(self.sprint_id, item)
        ready = handoffs.offer(
            self.sprint_id, task_id="T1", from_roster=SM, to_roster=DEV_BE,
            input_refs=["requirement:R1"], output_refs=["task:T1"],
            claims=["核对文件来源"], acceptance_criteria=["与原始要求一致"],
        )
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(handoffs.receive(ready["id"], accepted=True)["status"], "accepted")
        rejected = handoffs.offer(
            self.sprint_id, task_id="T1", from_roster=SM, to_roster=DEV_FE,
            input_refs=["requirement:R1"], output_refs=["task:T1"], claims=["撰写正文"],
        )
        self.assertEqual(handoffs.receive(rejected["id"], accepted=False, reason="输入不足")["status"], "rejected")
        blocked = handoffs.offer(
            self.sprint_id, task_id="T1", from_roster=DEV_FE, to_roster=SM,
            input_refs=["requirement:R1"], output_refs=["artifact:missing"], claims=["已提交"],
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertNotEqual(handoffs.receive(blocked["id"], accepted=True)["status"], "accepted")
        self.assertEqual(len(storage.list_handoffs(self.sprint_id)), 3)

    async def test_missing_po_contract_is_unchecked(self):
        storage.save_contract(self.sprint_id, build_contract("交付一份 Word 文件，一页。", deliver_kind="docx"))
        offer = handoffs.offer(
            self.sprint_id, task_id="", from_roster=PO, to_roster=SM,
            input_refs=["requirement:R1"], output_refs=[f"contract:{self.sprint_id}"], claims=["需求拆解"],
        )
        self.assertEqual(offer["status"], "unchecked")
        self.assertEqual(handoffs.receive(offer["id"], accepted=True)["status"], "unchecked")

    async def test_file_and_code_assignment_have_distinct_scopes(self):
        for kind in ("docx", "code"):
            tasks = [
                {"id": "T1", "title": "资料核对", "role_hint": "其他", "requirement_ids": ["R1"]},
                {"id": "T2", "title": "正文与界面", "role_hint": "写作" if kind == "docx" else "前端", "requirement_ids": ["R2"]},
            ]
            with patch.object(workflow_nodes, "pause", new_callable=AsyncMock), patch.object(workflow_nodes, "speak", new_callable=AsyncMock):
                result = await workflow_nodes.assign({"tasks": tasks, "deliver_kind": kind})
            self.assertEqual({task["assignee_roster"] for task in result["tasks"]}, {DEV_BE, DEV_FE})
            self.assertNotEqual(result["tasks"][0]["role_scope"], result["tasks"][1]["role_scope"])
            self.assertTrue(all(task.get("assignment_handoff_id") for task in result["tasks"]))

    async def test_concurrent_storage_writes_keep_unique_ids(self):
        def write(index):
            return storage.create_handoff(
                self.sprint_id, task_id=f"T{index}", from_roster=SM, to_roster=DEV_BE,
                input_refs=["requirement:R1"], output_refs=["task:T1"], claims=["分派"],
            )["id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(write, range(20)))
        self.assertEqual(len(set(ids)), 20)
        self.assertEqual(len(storage.list_handoffs(self.sprint_id)), 20)


if __name__ == "__main__":
    unittest.main()
