"""成员配置与 API 测试路由。

- GET  /api/agents              列出所有成员配置
- POST /api/agents/{roster}/test  测试 API 连接
- PUT  /api/agents/{roster}      更新成员配置
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import agent_config
import llm
from roles import ROSTER
from route_helpers import _agent_view

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentConfigPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    avatar: str | None = None
    skin: str | None = None
    backup_base_url: str | None = None
    backup_api_key: str | None = None
    backup_model: str | None = None


class AgentTestRequest(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


@router.get("")
async def list_agents():
    return {"agents": [_agent_view(no) for no in sorted(ROSTER)]}


@router.post("/{roster}/test")
async def test_agent(roster: int, req: AgentTestRequest):
    """用弹窗里当前填写的配置真实调一次模型，验证配置是否可用。"""
    if roster not in ROSTER:
        raise HTTPException(status_code=404, detail="成员不存在")

    saved = agent_config.get_agent_config(roster)["llm"]
    api_key = (req.api_key or "").strip() or saved.get("api_key", "")
    base_url = ((req.base_url or "").strip() or saved.get("base_url") or llm.DEFAULT_BASE_URL).rstrip("/")
    model = (req.model or "").strip() or saved.get("model") or llm.DEFAULT_MODEL
    if not api_key:
        return {"ok": False, "error": "未配置 API Key"}
    start = time.time()
    try:
        reply = await asyncio.to_thread(llm.ping, base_url, api_key, model, 60)
        if api_key == saved.get("api_key", ""):
            llm._mark_success(roster, api_key)
        return {"ok": True, "reply": reply.strip(), "latency": round(time.time() - start, 2)}
    except Exception as error:  # noqa: BLE001 —— 把真实错误透传给前端展示
        if api_key == saved.get("api_key", ""):
            llm._mark_fallback(roster, str(error))
        return {"ok": False, "error": str(error)[:200]}


@router.put("/{roster}")
async def update_agent(roster: int, patch: AgentConfigPatch):
    if roster not in ROSTER:
        raise HTTPException(status_code=400, detail="roster 必须在 1-6")
    if patch.api_key and ("…" in patch.api_key or "*" in patch.api_key):
        raise HTTPException(
            status_code=400,
            detail="API Key 包含遮罩占位符，疑似把星号显示值当成真实 Key 保存，请重新输入完整 Key",
        )
    llm_patch = {
        k: v
        for k, v in {
            "base_url": patch.base_url,
            "api_key": patch.api_key,
            "model": patch.model,
        }.items()
        if v is not None
    }
    if patch.backup_api_key and ("…" in patch.backup_api_key or "*" in patch.backup_api_key):
        raise HTTPException(
            status_code=400,
            detail="备用 API Key 包含遮罩占位符，请重新输入完整 Key",
        )
    backup_patch = {
        k: v
        for k, v in {
            "base_url": patch.backup_base_url,
            "api_key": patch.backup_api_key,
            "model": patch.backup_model,
        }.items()
        if v is not None
    }
    try:
        await asyncio.to_thread(
            agent_config.update_agent_config,
            roster,
            name=patch.name,
            llm=llm_patch or None,
            llm_backup=backup_patch or None,
            avatar=patch.avatar,
            skin=patch.skin,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _agent_view(roster)
