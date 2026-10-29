"""办公室可视化事件层：状态牌、气泡对话、表情、任务更新与节奏控制。

本模块只负责把「办公室语言」的动作事件推给前端，并处理暂停闸门、预算检查和
动画节奏。业务编排（需求拆解、开发、验收、交付）不在这里，避免和 workflow
节点逻辑耦合。
"""

import asyncio

import llm
from events import emit, sprint_meta, wait_if_paused
from llm import chat_json
from roles import roster_label


def set_state(roster_no: int, state: str, task: str | None = None) -> None:
    emit({"type": "set_state", "rosterNo": roster_no, "state": state, "task": task})


# 气泡对话降级开关：连续限流时本轮 Sprint 气泡自动走模板，把模型配额留给产出型调用
_SPEAK_LLM_ENABLED = True


def reset_speak_llm() -> None:
    """新 Sprint 开始时恢复气泡对话由 LLM 生成"""
    global _SPEAK_LLM_ENABLED
    _SPEAK_LLM_ENABLED = True


def visit(visitor: int, host: int, message: str,
          lines: list[dict] | None = None) -> None:
    event = {"type": "desk_visit", "visitor": visitor, "host": host, "message": message}
    if lines:
        event["lines"] = lines
    emit(event)


def thought(roster_no: int, text: str, phase: str = "", task_id: str = "") -> None:
    """推送一条成员的决策依据/思考说明，让「为什么这么做」对用户可见。

    与气泡对话不同：气泡是口语化表演，思考是决策依据（拆解思路、分工理由、
    核验结论、评审意见），全文可在通知中心回溯。不额外消耗模型调用——
    内容来自各节点 LLM 的顺路输出或已有实质数据的确定性合成。"""
    text = (text or "").strip()
    if not text:
        return
    emit({
        "type": "thought",
        "rosterNo": roster_no,
        "thought": text[:220],
        "phase": phase,
        "taskId": task_id,
    })


async def speak(visitor: int, host: int, context: str, fallback: str) -> None:
    """让 LLM 现场生成 2~3 回合的当面对话剧本；失败时回退到 fallback 单句。

    多回合剧本让协作从「说一句就走」变成「有来有往」：访客说明来意 →
    被访者回应 → 访客收尾。一次模型调用生成整段，前端按节拍分播气泡。"""
    global _SPEAK_LLM_ENABLED
    if not _SPEAK_LLM_ENABLED or sprint_meta.get("fast"):
        visit(visitor, host, fallback)
        return
    await wait_if_paused()
    check_budget()
    llm.consume_fallback()  # 只观察本次气泡调用
    result = await asyncio.to_thread(
        chat_json,
        (
            "你在模拟一个软件团队的办公室。"
            f"「{roster_label(visitor)}」正走到「{roster_label(host)}」的工位前当面沟通。"
            "生成 2~3 回合的简短对话剧本：第一句由访客（visitor）说明来意，"
            "随后两人交替回应，最后一句由访客收尾确认。"
            "要求：口语化、自然、符合各自角色身份，可以带一点职场个性，"
            "每句不超过 25 个字，不要引号，不要称呼前缀，对话要围绕话题有实质推进。"
            '输出 JSON：{"lines":[{"speaker":"visitor","text":"..."},'
            '{"speaker":"host","text":"..."}]}'
        ),
        f"话题：{context}",
        {"lines": [{"speaker": "visitor", "text": fallback}]},
        temperature=0.9,
        max_tokens=220,
    )

    bubble_fallback = llm.consume_fallback()
    if bubble_fallback and "429" in bubble_fallback["reason"]:
        # 限流持续：本轮后续气泡直接走模板，不再浪费配额
        _SPEAK_LLM_ENABLED = False
        print("[LLM] 气泡对话检测到 429 限流，本轮 Sprint 气泡切换为模板文案")
    lines: list[dict] = []
    if isinstance(result, dict) and isinstance(result.get("lines"), list):
        for item in result["lines"][:3]:
            if not isinstance(item, dict):
                continue
            speaker = "host" if item.get("speaker") == "host" else "visitor"
            text = str(item.get("text", "")).strip()[:40]
            if text:
                lines.append({"speaker": speaker, "text": text})
    if not lines:
        lines = [{"speaker": "visitor", "text": fallback[:40]}]
    visit(visitor, host, lines[0]["text"], lines)


def emote(roster_no: int, animation: str, label: str) -> None:
    emit({"type": "emote", "rosterNo": roster_no, "animation": animation, "label": label})


def update_task(task: dict) -> None:
    emit({"type": "task_update", "task": task})


# ---------- 老板指令注入（暂停/运行中均可补充，下一处 LLM 调用前生效） ----------

_pending_instructions: list[str] = []


def snapshot_instructions() -> list[str]:
    return list(_pending_instructions)


def restore_instructions(items: list[str]) -> None:
    _pending_instructions[:] = items


def add_instruction(text: str) -> None:
    """缓存一条老板补充指示，等工作流走到下一个 LLM 调用点时注入提示词"""
    text = (text or "").strip()[:300]
    if text:
        _pending_instructions.append(text)


def drain_instructions() -> str:
    """取出并清空全部待注入指示，拼成提示词片段；无指示时返回空串"""
    if not _pending_instructions:
        return ""
    items = list(_pending_instructions)
    _pending_instructions.clear()
    return "\n\n【老板最新指示（最高优先级，务必遵循）】\n" + "\n".join(
        f"- {item}" for item in items
    )


class BudgetExceeded(RuntimeError):
    """单轮花费超过用户设定上限：工作流主动收尾，避免继续烧钱"""


def spent_cost() -> float:
    import sprint_accounting
    usage = sprint_accounting.totals()
    if usage is not None:
        return max(0.0, float(usage.get("usage_cost") or 0))
    return max(
        0.0,
        round(llm.usage_total_cost() - float(sprint_meta.get("usage_baseline") or 0), 4),
    )


def check_budget() -> None:
    budget = float(sprint_meta.get("budget") or 0)
    if budget <= 0:
        return
    cost = spent_cost()
    if cost >= budget:
        raise BudgetExceeded(
            f"本轮花费已达上限 ¥{budget:.2f}（当前约 ¥{cost:.2f}），已自动收尾停止继续调用模型"
        )


async def pause(seconds: float) -> None:
    """节点间的暂停闸门 + 成本检查点 + 可选的演出等待。

    前两件事是真实功能：暂停期间阻塞在这里不再发起 LLM 调用、超预算立刻中断。
    最后的等待只服务于前端小人动画，SCENE_ANIMATION=0 或极速模式（SCENE_PACE≈0）
    时直接跳过，接口/自动化调用不再为表演白等。"""
    await wait_if_paused()
    check_budget()
    delay = seconds * llm.SCENE_PACE
    if llm.SCENE_ANIMATION and delay > 0:
        await asyncio.sleep(delay)
    await wait_if_paused()
