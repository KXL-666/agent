"""单兵任务路由。

- POST /api/solo/task  指派某个成员独立完成一项任务（单飞模式）
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import llm
from roles import ROSTER
from solo_runner import start_solo_task

router = APIRouter(prefix="/api/solo", tags=["solo"])


class SoloTaskRequest(BaseModel):
    roster: int
    message: str


@router.post("/task")
async def solo_task(req: SoloTaskRequest):
    if req.roster not in ROSTER:
        raise HTTPException(status_code=400, detail="roster 必须在 1-6")
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message 不能为空")
    if not llm.llm_available(req.roster):
        raise HTTPException(status_code=503, detail="该成员未配置专属 API，请先在成员配置页设置后再指派任务")
    task_id = await start_solo_task(req.roster, text)
    return {"task_id": task_id}
