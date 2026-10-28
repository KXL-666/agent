"""角色对话路由。

- POST /api/chat         同步对话
- POST /api/chat/stream  流式对话（SSE）
- POST /api/chat/intent  意图识别：区分闲聊与工作需求（对话页发送前的轻量判断）
"""
from __future__ import annotations

import asyncio
import json
import re
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import agent_config
import llm
import uploads
from artifacts import artifacts_store
from chat_context import build_chat_system, clock_reply, unavailable_reply
from events import sprint_meta, tasks_store
from llm import llm_available
from roles import ROSTER

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    roster: int
    message: str
    history: list[ChatMessage] = []
    upload_ids: list[str] = []


class IntentRequest(BaseModel):
    message: str


# 明显闲聊的本地规则：问候/寒暄/无意义短输入，不调用模型直接判 chat
_CHAT_GREETING = re.compile(
    r"^(你好|您好|hi+|hello+|hey+|在吗|在不在|早|早上好|晚上好|下午好|谢谢|感谢|再见|拜拜|哈哈+|嗯+|哦+|ok+|好的|行|test|测试|吃了吗|你是谁|名字)[!！。~～\s]*$",
    re.IGNORECASE,
)
# 任务关键词：命中即判 requirement
_TASK_HINT = re.compile(
    r"(写|做|开发|设计|分析|方案|文档|ppt|报告|总结|复盘|测试|评审|需求|功能|实现|修复|bug|生成|页面|接口|代码|计划|拆解)",
    re.IGNORECASE,
)


def _heuristic_intent(text: str) -> str | None:
    """本地规则快速判断；拿不准返回 None 交给模型。"""
    t = text.strip()
    if not t:
        return "chat"
    if _CHAT_GREETING.match(t):
        return "chat"
    if _TASK_HINT.search(t):
        return "requirement"
    # 纯字母/数字且无意义短输入（如 nh、asdf）按闲聊处理
    if len(t) <= 4 and re.fullmatch(r"[a-zA-Z0-9\s]+", t):
        return "chat"
    return None


@router.post("/intent")
async def chat_intent(req: IntentRequest):
    """轻量意图识别：chat（闲聊）/ requirement（工作需求）

    先走本地规则（问候、任务关键词、无意义短输入），拿不准再问模型；
    模型不可用或输出无法解析时按闲聊处理——最坏只是需求被当成闲聊回复一句，
    不会再出现闲聊误触发整个 Sprint。"""
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message 不能为空")
    heur = _heuristic_intent(text)
    if heur:
        return {"intent": heur, "via": "rule"}
    roster = next((r for r in range(1, 7) if llm_available(r)), None)
    if roster is None:
        return {"intent": "chat", "via": "fallback"}
    system = (
        "你是消息分类器。判断用户消息属于「闲聊」还是「工作需求」。\n"
        "闲聊：问候、寒暄、日常聊天、一般知识问答，例如「你好」「在吗」「今天天气怎么样」。\n"
        "工作需求：希望团队完成某件事并产出结果，例如写文档、做 PPT、开发功能、设计方案、分析数据。\n"
        "只输出一个词：chat 或 requirement，不要输出其他内容。"
    )
    meta: dict = {}
    import main
    reply = await asyncio.to_thread(
        main.chat_text,
        system,
        [{"role": "user", "content": text}],
        lambda: "",
        temperature=0,
        max_tokens=10,
        timeout=30,
        roster=roster,
        meta=meta,
    )
    cleaned = str(reply or "").strip().lower()
    if "chat" in cleaned and "requirement" not in cleaned:
        return {"intent": "chat", "via": "llm"}
    if "requirement" in cleaned:
        return {"intent": "requirement", "via": "llm"}
    # 模型输出无法解析：回退本地规则，仍拿不准按闲聊处理（宁可漏判需求，不误触发 Sprint）
    return {"intent": _heuristic_intent(text) or "chat", "via": "fallback"}


def _chat_system(roster: int) -> str:
    cfg = agent_config.get_agent_config(roster)
    entry = {**ROSTER[roster], "name": cfg.get("name") or ROSTER[roster]["name"]}
    return build_chat_system(entry, roster, sprint_meta, tasks_store, artifacts_store)


@router.post("")
async def chat(req: ChatRequest):
    entry = ROSTER.get(req.roster)
    if not entry:
        raise HTTPException(status_code=400, detail="roster 必须在 1-6")
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message 不能为空")

    system = _chat_system(req.roster)
    local_reply = clock_reply(text)
    if local_reply:
        return {"reply": local_reply, "llm": False, "model": "系统时钟", "roster": req.roster, "degraded": False}
    history = [
        {"role": m.role, "content": m.content}
        for m in req.history[-12:]
        if m.role in ("user", "assistant")
    ]
    history.append({"role": "user", "content": text})

    meta: dict = {}
    images = uploads.extract_images(req.upload_ids)
    import main
    reply = await asyncio.to_thread(
        main.chat_text,
        system,
        history,
        lambda: unavailable_reply(text),
        roster=req.roster,
        meta=meta,
        images=images or None,
    )
    return {
        "reply": reply,
        "llm": llm_available(req.roster),
        "model": llm.effective_model(req.roster),
        "roster": req.roster,
        "degraded": bool(meta.get("fallback_reason")),
        "backup_used": bool(meta.get("backup_used")),
        "images_attached": len(images),
        "images_dropped": bool(meta.get("images_dropped")),
    }


@router.post("/stream")
async def chat_stream(req: ChatRequest):
    """流式对话（SSE）：逐段推送模型输出，前端可边生成边显示、可中断。"""
    if req.roster not in ROSTER:
        raise HTTPException(status_code=400, detail="roster 必须在 1-6")
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message 不能为空")
    history = [
        {"role": m.role, "content": m.content}
        for m in req.history[-12:]
        if m.role in ("user", "assistant")
    ]
    history.append({"role": "user", "content": text})
    fallback = unavailable_reply(text)
    meta: dict = {}
    system = _chat_system(req.roster)
    local_reply = clock_reply(text)
    images = uploads.extract_images(req.upload_ids)
    if images:
        history[-1] = {**history[-1], "content": llm._with_images(text, images)}

    async def event_source():
        import main
        if local_reply:
            for payload in ({"start": True, "model": "系统时钟"}, {"delta": local_reply}, {"done": True, "degraded": False}):
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def worker() -> None:
            try:
                for chunk in main.stream_chat(system, history, req.roster, fallback, meta):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()
        yield f"data: {json.dumps({'model': llm.effective_model(req.roster) or '', 'start': True}, ensure_ascii=False)}\n\n"
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield f"data: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'done': True, 'degraded': bool(meta.get('fallback_reason')), 'images_dropped': bool(meta.get('images_dropped')), 'backup_used': bool(meta.get('backup_used'))}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")
