"""单兵任务：单飞对话中指派某个成员独立完成一项任务

不走 Sprint 编排，直接用该成员的专属 LLM 配置生成产物：
- 产物持久化到专用桶 sprint_id="solo"，在交付面板按「单飞」分组展示
- 通过 WS 广播工作状态（source=solo_task）与任务进度（solo_task_update）
"""
from __future__ import annotations

import asyncio
import time

import llm
from artifacts import add_artifact
from events import emit
from roles import ROSTER, roster_name

# 产物类型按角色分工
SOLO_KIND = {
    1: "prd",
    2: "dev",
    3: "dev",
    4: "test",
    5: "review",
    6: "summary",
}

SOLO_PROMPT = {
    1: "请独立完成一份需求拆解 / 产品设计文档，包含背景、目标、功能拆解与验收标准。",
    2: "请独立完成一份后端开发说明文档，包含接口设计、数据结构与核心逻辑。",
    3: "请独立完成一份前端开发说明 / 组件设计文档，包含组件结构、交互流程与关键代码示例。",
    4: "请独立完成一份测试方案 / 测试用例文档，包含测试范围、用例列表与预期结果。",
    5: "请独立完成一份技术评审报告，包含架构评估、风险点与改进建议。",
    6: "请独立完成一份复盘 / 总结文档，包含成果回顾与改进建议。",
}


def _solo_title(message: str) -> str:
    """从任务描述取一行的短标题（用于产物标题与状态牌）"""
    title = message.strip().replace("#", "").splitlines()[0].strip()[:30]
    return title or "单飞任务"


async def _run_solo(roster: int, task_id: str, message: str) -> None:
    entry = ROSTER[roster]
    name = roster_name(roster)
    kind = SOLO_KIND.get(roster, "custom")
    title = _solo_title(message)

    emit({
        "type": "set_state",
        "rosterNo": roster,
        "state": "working",
        "task": f"单飞·{title[:16]}",
        "source": "solo_task",
    })

    system = (
        f"你是 AI 敏捷开发办公室的{entry['role']}「{name}」，工位 {roster}。\n"
        "老板直接指派你独立完成一项任务，产出完整可交付的成果内容（Markdown 格式）。\n"
        f"{SOLO_PROMPT.get(roster, '')}\n"
        "直接输出成果内容，不要寒暄，不要解释过程，不要声称调用了不存在的外部工具。"
    )
    meta: dict = {}
    reply = await asyncio.to_thread(
        llm.chat_text,
        system,
        [{"role": "user", "content": message}],
        lambda: None,  # 失败不回退兜底文案，如实上报失败
        temperature=0.7,
        max_tokens=3000,
        timeout=180,
        roster=roster,
        meta=meta,
    )

    failed = reply is None
    error = meta.get("fallback_reason") or ("生成失败" if failed else "")
    artifact = None
    if not failed:
        try:
            artifact = add_artifact(
                kind=kind,
                title=title,
                author_roster=roster,
                content=reply,
                task_id=task_id,
                sprint_id="solo",
                source="model",
            )
        except Exception as exc:  # noqa: BLE001 —— 落库失败如实反馈，状态在 finally 恢复
            failed = True
            error = str(exc)[:160]

    summary = (
        f"搞定了，产物《{title}》已放入「交付」面板。"
        if not failed
        else f"这次任务没完成：{error}。请检查我的 API 配置后重试。"
    )
    emit({
        "type": "solo_task_update",
        "roster": roster,
        "task_id": task_id,
        "title": title,
        "status": "failed" if failed else "done",
        "artifact_id": artifact["id"] if artifact else None,
        "summary": summary,
    })
    # 不再单独发 set_state idle：前端在 solo_task_update 里按任务计数摘牌，
    # 避免清掉该成员同时进行中的 Sprint 任务状态牌


async def start_solo_task(roster: int, message: str) -> str:
    """创建单兵任务并后台执行，立即返回 task_id 供前端关联任务卡片"""
    task_id = f"solo-{int(time.time() * 1000)}-{roster}"
    asyncio.create_task(_run_solo(roster, task_id, message))
    return task_id
