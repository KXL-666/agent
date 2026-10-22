"""多 Agent 敏捷开发工作流（LangGraph）

流程：需求拆解 → 任务分派 → 并行开发 → 测试 → 评审 ─┬→ 通过 → 复盘收尾
                                              └→ 打回 → 缺陷修复 → 测试（最多 2 轮）

每个节点都会通过 events.emit 把"办公室语言"的动作事件推给前端：
set_state（状态/任务牌）、desk_visit（起身找某人说话）、emote（表情动画）。
"""

import asyncio
import os
import random
import zipfile
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

import forge
import llm
from artifacts import add_artifact, artifacts_store
from events import emit, set_stage, sprint_meta, tasks_store, wait_if_paused
from llm import chat_json
from roles import DEV_BE, DEV_FE, LEAD, PO, QA, SM, roster_label, roster_name

MAX_REWORK = 2


class SprintState(TypedDict):
    requirement: str
    tasks: list
    rework_count: int
    review_note: str
    deliver_kind: str  # 最终交付物类型：docx/pptx/xlsx/code
    uploads_text: str  # 用户上传附件提取出的文本素材（可为空）
    uploads_images: list  # 图片附件的 data URL（视觉模型多模态输入，可为空）


# ---------- 事件辅助 ----------

def set_state(roster_no: int, state: str, task: str | None = None) -> None:
    emit({"type": "set_state", "rosterNo": roster_no, "state": state, "task": task})


# 气泡对话降级开关：连续限流时本轮 Sprint 气泡自动走模板，把模型配额留给产出型调用
_SPEAK_LLM_ENABLED = True


def visit(visitor: int, host: int, message: str) -> None:
    emit({"type": "desk_visit", "visitor": visitor, "host": host, "message": message})


async def speak(visitor: int, host: int, context: str, fallback: str) -> None:
    """让 LLM 现场生成一句角色对话作为气泡文案；失败时回退到 fallback"""
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
            f"「{roster_label(visitor)}」正走到「{roster_label(host)}」的工位前当面说话。"
            "要求：口语化、自然、符合角色身份，可以带一点职场个性，"
            "不超过 25 个字，不要引号，不要称呼前缀。"
            '输出 JSON：{"message":"..."}'
        ),
        f"话题：{context}",
        fallback,
        temperature=0.9,
        max_tokens=120,
    )

    bubble_fallback = llm.consume_fallback()
    if bubble_fallback and "429" in bubble_fallback["reason"]:
        # 限流持续：本轮后续气泡直接走模板，不再浪费配额
        _SPEAK_LLM_ENABLED = False
        print("[LLM] 气泡对话检测到 429 限流，本轮 Sprint 气泡切换为模板文案")
    if isinstance(result, dict):
        message = str(result.get("message", "")).strip()
    else:
        message = str(result).strip()
    if not message:
        message = fallback
    visit(visitor, host, message[:40])


def emote(roster_no: int, animation: str, label: str) -> None:
    emit({"type": "emote", "rosterNo": roster_no, "animation": animation, "label": label})


def update_task(task: dict) -> None:
    emit({"type": "task_update", "task": task})


# ---------- 老板指令注入（暂停/运行中均可补充，下一处 LLM 调用前生效） ----------

_pending_instructions: list[str] = []


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
    """给小人动画留出演出的时间（极速模式会大幅压缩）

    同时作为暂停闸门与成本检查点：暂停期间阻塞在这里，不再发起 LLM 调用。"""
    await wait_if_paused()
    check_budget()
    await asyncio.sleep(seconds * llm.SCENE_PACE)
    await wait_if_paused()


# ---------- Mock 决策（未配置 LLM 时使用） ----------

def mock_plan() -> dict:
    return {
        "tasks": [
            {"title": "设计数据库表结构与接口契约", "role_hint": "后端"},
            {"title": "实现核心业务接口", "role_hint": "后端"},
            {"title": "搭建前端页面并联调", "role_hint": "前端"},
            {"title": "编写接口自动化测试", "role_hint": "测试"},
        ]
    }


def mock_develop_result(title: str) -> str:
    return f"完成「{title}」，已提交代码"


def mock_test_report(tasks: list, first_pass: bool) -> dict:
    if first_pass:
        dev_tasks = [t for t in tasks if t["assignee_roster"] in (DEV_BE, DEV_FE)]
        buggy = random.choice(dev_tasks) if dev_tasks else None
        return {
            "verdict": "有缺陷",
            "bug_task": buggy["id"] if buggy else None,
            "note": f"发现缺陷：{buggy['title']} 边界条件未覆盖" if buggy else "全部通过",
        }
    return {"verdict": "通过", "bug_task": None, "note": "回归测试全部通过"}


def mock_review(first_pass: bool) -> str:
    return "存在缺陷，打回修复" if first_pass else "评审通过，同意交付"


# ---------- 角色分派 ----------

def detect_final_kind(requirement: str) -> str:
    """关键词兜底：判断需求最终要交付什么（LLM 不可用时使用）"""
    text = requirement.lower()
    if any(w in text for w in ("ppt", "幻灯", "演示文稿", "slides")):
        return "pptx"
    if any(w in text for w in ("excel", "表格", "xlsx", "预算表", "统计表")):
        return "xlsx"
    if any(w in text for w in (
        "作文", "文章", "论文", "周报", "日报", "报告", "文档", "word", "docx",
        "方案", "总结", "计划书", "信", "稿", "文案", "申请书", "简历",
    )):
        return "docx"
    return "code"


def pick_assignee(title: str, index: int) -> int:
    backend_words = ("数据库", "接口", "后端", "API", "服务", "存储")
    frontend_words = ("前端", "页面", "界面", "UI", "组件", "联调")
    test_words = ("测试", "用例", "自动化")

    if any(w in title for w in backend_words):
        return DEV_BE
    if any(w in title for w in frontend_words):
        return DEV_FE
    if any(w in title for w in test_words):
        return QA
    return DEV_BE if index % 2 == 0 else DEV_FE


# ---------- LangGraph 节点 ----------

async def kickoff(state: SprintState) -> dict:
    """产品经理：拆解需求"""
    global _SPEAK_LLM_ENABLED
    _SPEAK_LLM_ENABLED = True  # 新 Sprint 开始，气泡对话恢复 LLM 生成
    set_stage("需求拆解")
    set_state(PO, "thinking", "分析需求")
    await pause(1.5)

    uploads_text = state.get("uploads_text", "")
    attachments_note = (
        f"\n\n用户提供了附件素材，任务拆解须围绕对这些素材的加工改造：\n{uploads_text[:2000]}"
        if uploads_text else ""
    )
    llm.consume_fallback()  # 清掉历史回退记录，只观察本次拆解调用
    plan = chat_json(
        system=(
            "你是敏捷团队的产品经理。把需求拆解为 3-5 个任务，"
            '输出 JSON：{"tasks":[{"title":"任务名","role_hint":"后端|前端|测试|写作|设计|其他"}]}。'
            "注意判断需求性质：如果需求是要一份成品内容（写作文/报告/方案/周报/PPT/表格），"
            "任务应为内容生产环节（如：资料与大纲、正文撰写、排版与导出成品）；"
            "只有需求是开发软件/接口/页面时，任务才是开发环节。"
            "如果提供了附件素材，任务必须体现对附件的加工（阅读、提取、改写、转换、扩展）。"
        ),
        user=f"需求：{state['requirement']}{attachments_note}{drain_instructions()}",
        fallback=mock_plan,
        roster=PO,
        images=state.get("uploads_images") or None,
    )
    fallback_info = llm.consume_fallback()
    if fallback_info:
        # 拆解未按预期输出：明示前端，避免用户误以为 LLM 拆解生效
        emit(
            {
                "type": "plan_fallback",
                "message": "任务拆解未按预期输出，已使用兜底拆解继续流程",
                "reason": fallback_info["reason"],
            }
        )

    tasks: list[dict] = []
    for i, item in enumerate(plan.get("tasks", [])):
        title = str(item.get("title", f"任务 {i + 1}")).strip()[:40]
        tasks.append(
            {
                "id": f"T{i + 1}",
                "title": title,
                "role_hint": item.get("role_hint", "其他"),
                "assignee_roster": None,
                "status": "待分派",
                "summary": "",
            }
        )

    tasks_store.clear()
    tasks_store.extend(tasks)
    for t in tasks:
        update_task(t)

    # 产物：需求拆解文档（PRD）——真实 .docx 文件，失败回退 Markdown
    prd_lines = [f"# 需求拆解文档（PRD）", "", f"## 原始需求", "", state["requirement"], "", "## 任务拆解", ""]
    for t in tasks:
        prd_lines.append(f"- **{t['id']} {t['title']}**（{t['role_hint']}）")
    prd_md = "\n".join(prd_lines)
    prd_spec = (
        f"生成一份《需求拆解文档（PRD）》Word 文档。原始需求：{state['requirement']}。"
        f"任务拆解清单：{[f"{t['id']} {t['title']}（{t['role_hint']}）" for t in tasks]}。"
        "文档须包含：文档标题页信息、1.背景与目标、2.需求范围、3.任务拆解表格（编号/任务/负责方向）、"
        "4.验收标准、5.风险与依赖。正文用规范书面语，内容详实。"
    )
    prd_file = await asyncio.to_thread(forge.forge_file, "docx", prd_spec, "PRD需求拆解文档.docx", PO)
    add_artifact(
        kind="prd",
        title="需求拆解文档（PRD）",
        author_roster=PO,
        content=prd_md if not prd_file else prd_md + f"\n\n真实文件：{prd_file}",
        file_name=prd_file,
        degraded=not prd_file,
        degraded_reason="" if prd_file else "Word 文件生成失败，当前仅为 Markdown 摘要",
        regen={
            "mode": "forge",
            "file_kind": "docx",
            "spec": prd_spec,
            "filename": "PRD需求拆解文档.docx",
            "roster": PO,
        },
    )

    # 判断最终交付物类型：内容成品（docx/pptx/xlsx）还是代码（code）
    classification = chat_json(
        system=(
            '判断这个需求的最终交付物类型。输出 JSON：{"type":"docx|pptx|xlsx|code"}。'
            "docx=作文/文章/报告/周报/方案/总结等文字成品；pptx=演示文稿；"
            "xlsx=表格/数据表；code=软件/系统/接口/页面等程序开发"
        ),
        user=f"需求：{state['requirement']}",
        fallback=lambda: {"type": detect_final_kind(state["requirement"])},
        roster=PO,
    )
    deliver_kind = (
        classification.get("type", "code") if isinstance(classification, dict) else "code"
    )
    if deliver_kind not in ("docx", "pptx", "xlsx", "code"):
        deliver_kind = "code"

    set_state(PO, "working", "编写需求说明")
    await pause(1.0)
    await speak(
        PO,
        SM,
        f"需求「{state['requirement']}」拆解出了 {len(tasks)} 个任务，请教练组织分派",
        f"需求拆解完成，共 {len(tasks)} 个任务，请组织分派",
    )
    return {"tasks": tasks, "deliver_kind": deliver_kind}


async def assign(state: SprintState) -> dict:
    """敏捷教练：任务分派到人"""
    tasks = state["tasks"]
    set_stage("任务分派")
    set_state(SM, "thinking", "评估工作量与分工")
    await pause(1.5)

    for i, t in enumerate(tasks):
        t["assignee_roster"] = pick_assignee(t["title"], i)
        t["status"] = "待开发"
        update_task(t)

    set_state(SM, "working", "分派任务")
    await pause(0.8)
    for t in tasks:
        no = t["assignee_roster"]
        if no:
            set_state(no, "working", f"准备开发：{t['title']}")
            await pause(0.4)

    visit_fallback = f"{len(tasks)} 个任务已分派到人，进入开发"
    await speak(
        SM,
        PO,
        f"已把 {len(tasks)} 个任务按角色分派给后端、前端、测试工程师",
        visit_fallback,
    )
    return {"tasks": tasks}


async def develop(state: SprintState) -> dict:
    """开发工程师：并行完成任务（最多 3 个并发，对应真实团队的并行开发）"""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["status"] in ("待开发", "打回")]
    set_stage("并行开发")

    semaphore = asyncio.Semaphore(3)

    async def run_one(t: dict) -> None:
        if t["status"] == "已取消":
            return
        no = t["assignee_roster"] or DEV_BE
        verb = "修复缺陷" if t["status"] == "打回" else "开发中"
        set_state(no, "working", f"{verb}：{t['title']}")
        await pause(1.8)
        if t["status"] == "已取消":
            # 动画期间被老板取消：不再消耗模型配额
            set_state(no, "idle", "等待新任务")
            return

        async with semaphore:
            if t["status"] == "已取消":
                return
            # 产物：开发说明（LLM 生成实现要点，未配 LLM 时用模板兜底）
            note_meta: dict = {}
            note = await asyncio.to_thread(
                chat_json,
                (
                    f"你是{roster_name(no)}，刚完成开发任务「{t['title']}」（需求：{state['requirement']}）。"
                    "用 2 句话向团队说明你做了什么、实现要点是什么。"
                    '输出 JSON：{"note":"..."}'
                ),
                f"任务：{t['title']}{drain_instructions()}",
                lambda: mock_develop_result(t["title"]),
                0.7,
                160,
                60,
                no,
                note_meta,
            )
            summary = ""
            if isinstance(note, dict):
                summary = str(note.get("note", "")).strip()
            if not summary:
                summary = mock_develop_result(t["title"])

            # 真实代码产出：仅软件类需求由 LLM 为该任务编写真实代码文件
            written: list[str] = []
            if state.get("deliver_kind", "code") == "code":
                dev_requirement = state["requirement"]
                up = state.get("uploads_text", "")
                if up:
                    dev_requirement += f"\n\n（需加工改造的现有代码/素材如下，请在此基础上改写扩展）\n{up[:3000]}"
                dev_requirement += drain_instructions()
                written = await asyncio.to_thread(
                    forge.forge_code, t["title"], dev_requirement, t["id"], no
                )

        if t["status"] == "已取消":
            # 生成期间被取消：保留取消状态，不再翻回待测试
            set_state(no, "idle", "等待新任务")
            return
        t["status"] = "待测试"
        t["summary"] = summary
        t["code_files"] = written
        update_task(t)
        code_note = (
            "\n\n真实代码文件：\n" + "\n".join(f"- `{p}`" for p in written)
            if written else ""
        )
        add_artifact(
            kind="dev",
            title=f"开发说明 · {t['title']}",
            author_roster=no,
            content=(
                f"# 开发说明 · {t['title']}\n\n"
                f"- 负责人：{roster_name(no)}\n"
                f"- 所属需求：{state['requirement']}\n\n"
                f"{summary}{code_note}"
            ),
            task_id=t["id"],
            degraded=bool(note_meta.get("fallback_reason")),
            degraded_reason=note_meta.get("fallback_reason", ""),
        )
        emote(no, "emotes/idea", "灵感")

    await asyncio.gather(*(run_one(t) for t in pending))

    # 交接给测试：由最后一个"非测试岗"开发者去向 QA 交接；
    # 若本轮待测任务全部由 QA 自研（如自动化用例），直接进入测试，避免自己拜访自己
    dev_tasks = [t for t in pending if (t["assignee_roster"] or DEV_BE) != QA]
    if dev_tasks:
        developer = dev_tasks[-1]["assignee_roster"] or DEV_BE
        set_state(developer, "thinking", "整理提交物")
        await pause(0.8)
        dev_titles = "、".join(t["title"] for t in dev_tasks) or "本轮任务"
        await speak(
            developer,
            QA,
            f"开发任务（{dev_titles}）已全部完成并提交，请开始测试",
            "开发完成，请开始测试",
        )
    else:
        set_state(QA, "working", "测试脚本就绪，开始执行测试")
        await pause(0.8)
    return {"tasks": tasks}


async def test(state: SprintState) -> dict:
    """测试工程师：真实语法检查 + 生成真实测试用例 .xlsx"""
    set_state(QA, "thinking", "设计测试用例")
    await pause(1.5)

    tasks = state["tasks"]
    set_stage("测试验证")
    # 真实质量检查：Python 语法编译 + JS 语法检查（不产生 __pycache__ 污染交付包）
    compile_failures: list[str] = await asyncio.to_thread(forge.verify_generated_code)
    compile_ok = not compile_failures

    first_pass = state["rework_count"] == 0
    report_meta: dict = {}
    report = chat_json(
        system=(
            "你是测试工程师，根据任务列表和真实语法检查结果给出测试结论。"
            '输出 JSON：{"verdict":"通过|有缺陷","bug_task":"任务id或null","note":"一句话说明"}'
        ),
        user=(
            f"任务列表：{[{'id': t['id'], 'title': t['title']} for t in tasks]}\n"
            f"真实语法检查：{'全部通过' if compile_ok else '；'.join(compile_failures)}"
            f"{drain_instructions()}"
        ),
        fallback=lambda: mock_test_report(tasks, first_pass),
        roster=QA,
        meta=report_meta,
    )
    if not compile_ok and report.get("verdict") == "通过":
        # 真实检查发现问题时，以真实结果为准
        report["verdict"] = "有缺陷"
        report["note"] = f"语法检查未过：{compile_failures[0]}"
        dev_tasks = [t for t in tasks if t["assignee_roster"] in (DEV_BE, DEV_FE)]
        if dev_tasks and not report.get("bug_task"):
            report["bug_task"] = dev_tasks[0]["id"]

    bug_task_id = report.get("bug_task")
    if report.get("verdict") == "有缺陷" and bug_task_id:
        for t in tasks:
            if t["status"] == "已取消":
                continue
            t["status"] = "打回" if t["id"] == bug_task_id else "通过"
            update_task(t)
    else:
        for t in tasks:
            if t["status"] not in ("通过", "已取消"):
                t["status"] = "通过"
                update_task(t)

    set_state(QA, "working", f"测试结论：{report.get('note', '')}")
    await pause(1.0)

    # 产物：真实测试用例 .xlsx + 文本报告
    round_no = state["rework_count"] + 1
    done_ids = [t["id"] for t in tasks if t["status"] == "通过"]
    xlsx_spec = (
        f"生成一份《测试报告（第 {round_no} 轮）》Excel 工作簿。"
        f"需求背景：{state['requirement']}。"
        f"Sheet1「测试结论」：结论={report.get('verdict', '通过')}，说明={report.get('note', '')}，"
        f"语法检查={'全部通过' if compile_ok else '；'.join(compile_failures)}。"
        f"Sheet2「用例清单」：表头为 用例编号/关联任务/用例标题/前置条件/步骤/预期结果/实际结果/状态，"
        f"为以下任务各设计 2-3 条真实用例：{[f'{t['id']} {t['title']}' for t in tasks]}，"
        f"状态列按实际结论填 通过/失败。表头加粗、列宽自适应内容。"
    )
    xlsx_file = await asyncio.to_thread(
        forge.forge_file, "xlsx", xlsx_spec, f"测试报告-第{round_no}轮.xlsx", QA
    )
    add_artifact(
        kind="test",
        title=f"测试报告（第 {round_no} 轮）",
        author_roster=QA,
        content=(
            f"# 测试报告（第 {round_no} 轮）\n\n"
            f"- 结论：{report.get('verdict', '通过')}\n"
            f"- 说明：{report.get('note', '')}\n"
            f"- 语法检查：{'全部通过' if compile_ok else '；'.join(compile_failures)}\n"
            f"- 本轮通过：{'、'.join(done_ids) or '无'}\n"
            f"- 缺陷任务：{report.get('bug_task') or '无'}"
            + (f"\n- 真实文件：{xlsx_file}" if xlsx_file else "")
        ),
        file_name=xlsx_file,
        degraded=not xlsx_file or bool(report_meta.get("fallback_reason")),
        degraded_reason=(
            "Excel 测试报告生成失败，当前仅为文字汇总"
            if not xlsx_file
            else report_meta.get("fallback_reason", "")
        ),
        regen={
            "mode": "forge",
            "file_kind": "xlsx",
            "spec": xlsx_spec,
            "filename": f"测试报告-第{round_no}轮.xlsx",
            "roster": QA,
        },
    )
    await speak(
        QA,
        LEAD,
        f"测试执行完毕，结论是：{report.get('note', '见报告')}",
        f"测试完成：{report.get('note', '见报告')}",
    )
    return {"tasks": tasks, "review_note": report.get("verdict", "通过")}


async def review(state: SprintState) -> dict:
    """技术主管：代码评审"""
    tasks = state["tasks"]
    buggy = [t for t in tasks if t["status"] == "打回"]
    first_pass = state["rework_count"] < 1
    set_stage("技术评审")

    set_state(LEAD, "thinking", "代码评审")
    await pause(1.8)

    if first_pass and buggy:
        names = "、".join(t["title"] for t in buggy)
        note = f"打回：{names} 需修复后重新提交"
        set_state(LEAD, "working", note)
        await pause(1.0)
        await speak(
            LEAD,
            SM,
            f"评审发现问题，{names} 要打回给开发修复",
            note,
        )
        return {"rework_count": state["rework_count"] + 1, "review_note": note}

    verdict_meta: dict = {}
    verdict = chat_json(
        system="你是技术主管，给出最终评审结论。输出 JSON：{\"verdict\":\"通过|打回\",\"note\":\"一句话说明\"}",
        user=f"任务状态：{[{'id': t['id'], 'status': t['status']} for t in tasks]}{drain_instructions()}",
        fallback=lambda: {"verdict": "通过", "note": mock_review(not first_pass)},
        roster=LEAD,
        meta=verdict_meta,
    )
    note = f"评审结论：{verdict.get('note', '通过')}"
    set_state(LEAD, "working", note)
    await pause(1.0)

    # 产物：真实代码评审记录 .docx（评审基于真实产出的代码内容）
    code_excerpt = ""
    for py_file in sorted(Path(forge.CODE_DIR).rglob("*.py"))[:5]:
        try:
            snippet = py_file.read_text(encoding="utf-8", errors="replace")[:400]
            code_excerpt += f"\n### {py_file.name}\n{snippet}\n"
        except OSError:
            continue
    review_opinion = chat_json(
        system=(
            "你是技术主管，阅读真实代码后给出评审意见（2-4 条具体建议或肯定）。"
            '输出 JSON：{"opinion":"..."}'
        ),
        user=f"代码节选：{code_excerpt or '（本次未产出代码）'}",
        fallback=lambda: {"opinion": verdict.get("note", "通过")},
        temperature=0.6,
        max_tokens=300,
        roster=LEAD,
    )
    opinion_text = (
        review_opinion.get("opinion", "") if isinstance(review_opinion, dict) else ""
    ) or verdict.get("note", "通过")

    review_spec = (
        f"生成一份《代码评审记录》Word 文档。评审人：{roster_name(LEAD)}。"
        f"评审结论：{verdict.get('verdict', '通过')}。评审意见：{opinion_text}。"
        f"任务状态：{[{'id': t['id'], 'status': t['status']} for t in tasks]}。"
        "文档须包含：1.评审概述、2.代码质量评价、3.具体意见（分条）、4.结论与后续建议。"
    )
    review_file = await asyncio.to_thread(
        forge.forge_file, "docx", review_spec, "代码评审记录.docx", LEAD
    )
    add_artifact(
        kind="review",
        title="代码评审记录",
        author_roster=LEAD,
        content=(
            f"# 代码评审记录\n\n"
            f"- 评审人：{roster_name(LEAD)}\n"
            f"- 结论：{verdict.get('verdict', '通过')}\n"
            f"- 意见：{opinion_text}\n"
            f"- 任务状态：{[{'id': t['id'], 'status': t['status']} for t in tasks]}"
            + (f"\n- 真实文件：{review_file}" if review_file else "")
        ),
        file_name=review_file,
        degraded=not review_file or bool(verdict_meta.get("fallback_reason")),
        degraded_reason=(
            "Word 评审记录生成失败，当前仅为文字汇总"
            if not review_file
            else verdict_meta.get("fallback_reason", "")
        ),
        regen={
            "mode": "forge",
            "file_kind": "docx",
            "spec": review_spec,
            "filename": "代码评审记录.docx",
            "roster": LEAD,
        },
    )
    await speak(
        LEAD,
        PO,
        f"评审结束，结论：{verdict.get('note', '通过')}，请知悉",
        note,
    )
    return {"review_note": note}


async def fix(state: SprintState) -> dict:
    """开发工程师：并行修复缺陷"""
    buggy = [t for t in state["tasks"] if t["status"] == "打回"]
    set_stage("缺陷修复")

    async def run_one(t: dict) -> None:
        if t["status"] == "已取消":
            return
        no = t["assignee_roster"] or DEV_BE
        set_state(no, "thinking", f"定位缺陷：{t['title']}")
        await pause(1.2)
        if t["status"] == "已取消":
            set_state(no, "idle", "等待新任务")
            return
        set_state(no, "working", f"修复缺陷：{t['title']}")
        await pause(1.5)
        if t["status"] == "已取消":
            set_state(no, "idle", "等待新任务")
            return
        t["status"] = "待测试"
        t["summary"] = f"修复「{t['title']}」缺陷并补充用例"
        update_task(t)
        emote(no, "emotes/excited", "修复完成")

    await asyncio.gather(*(run_one(t) for t in buggy))

    # 修复人若就是 QA 本人（缺陷出在测试类任务上），直接回归，不再自我拜访
    fixer = buggy[-1]["assignee_roster"] if buggy else DEV_BE
    if fixer != QA:
        fix_titles = "、".join(t["title"] for t in buggy) or "缺陷"
        await speak(
            fixer,
            QA,
            f"缺陷「{fix_titles}」已修复并补充了用例，请回归测试",
            "缺陷已修复，请回归测试",
        )
    else:
        set_state(QA, "working", "缺陷修复完毕，直接回归")
        await pause(0.8)
    return {"tasks": state["tasks"]}


async def wrapup(state: SprintState) -> dict:
    """敏捷教练：复盘收尾。先庆祝并宣告完成（人物转空闲），复盘文档后台生成后补推，
    避免「产物已交付、人还在忙」的进度错位。"""
    done = [t for t in state["tasks"] if t["status"] == "通过"]
    set_stage("复盘收尾")

    # 1) 立即收尾：庆祝 → 全员空闲 → 完成信号（前端播提示音、弹气泡）
    await speak(
        SM,
        PO,
        f"Sprint 完成，共交付 {len(done)} 个任务，团队表现不错",
        f"Sprint 完成！共交付 {len(done)} 个任务",
    )
    emote(SM, "emotes/hooray", "庆祝")
    for no in (PO, DEV_BE, DEV_FE, QA, LEAD, SM):
        set_state(no, "idle", "等待新任务")
    emit({"type": "sprint_done", "tasks": state["tasks"]})

    # 2) 复盘文档后台生成：不阻塞完成信号，完成后经 artifact_new 推送到产物中心
    set_state(SM, "working", "整理复盘文档")
    summary = chat_json(
        system=(
            "你是敏捷教练，为刚结束的 Sprint 写一段 3 句话以内的复盘总结，"
            "覆盖交付结果与团队协作亮点。输出 JSON：{\"summary\":\"...\"}"
        ),
        user=(
            f"需求：{state['requirement']}；"
            f"任务：{[{'id': t['id'], 'title': t['title'], 'status': t['status']} for t in state['tasks']]}"
        ),
        fallback=lambda: (
            f"本轮 Sprint 交付 {len(done)}/{len(state['tasks'])} 个任务，"
            "历经拆解、开发、测试与评审环节，团队协作顺畅。"
        ),
        temperature=0.7,
        max_tokens=200,
        roster=SM,
    )
    summary_text = ""
    if isinstance(summary, dict):
        summary_text = str(summary.get("summary", "")).strip()
    if not summary_text:
        summary_text = (
            f"本轮 Sprint 交付 {len(done)}/{len(state['tasks'])} 个任务，"
            "历经拆解、开发、测试与评审环节，团队协作顺畅。"
        )

    summary_spec = (
        f"生成一份《Sprint 复盘总结》Word 文档。需求：{state['requirement']}。"
        f"交付情况：{len(done)}/{len(state['tasks'])} 个任务通过。"
        f"复盘总结：{summary_text}。"
        "文档须包含：1.Sprint 概览（需求/交付数/周期）、2.交付清单表格、3.亮点与不足、4.下轮改进建议。"
    )
    summary_file = await asyncio.to_thread(
        forge.forge_file, "docx", summary_spec, "Sprint复盘总结.docx", SM
    )
    add_artifact(
        kind="summary",
        title="Sprint 复盘总结",
        author_roster=SM,
        content=(
            f"# Sprint 复盘总结\n\n"
            f"- 需求：{state['requirement']}\n"
            f"- 交付：{len(done)}/{len(state['tasks'])} 个任务\n\n"
            f"{summary_text}"
            + (f"\n\n真实文件：{summary_file}" if summary_file else "")
        ),
        file_name=summary_file,
        degraded=not summary_file,
        degraded_reason="" if summary_file else "Word 复盘文档生成失败，当前仅为文字总结",
        regen={
            "mode": "forge",
            "file_kind": "docx",
            "spec": summary_spec,
            "filename": "Sprint复盘总结.docx",
            "roster": SM,
        },
    )
    set_state(SM, "idle", "等待新任务")
    return {"tasks": state["tasks"]}


def _zip_code_dir() -> str | None:
    """把本次 Sprint 的代码目录打成 zip，返回相对 deliverables 的文件名"""
    files = [p for p in Path(forge.CODE_DIR).rglob("*") if p.is_file()]
    if not files:
        return None
    zip_name = forge._unique_name("sprint-code.zip")
    zip_path = os.path.join(forge.DELIVERABLES_DIR, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, os.path.relpath(p, forge.CODE_DIR))
    return zip_name


async def deliver(state: SprintState) -> dict:
    """交付环节：把老板要的最终成品做出来（内容类=成品文件，软件类=代码包说明）"""
    kind = state.get("deliver_kind", "code") or "code"
    tasks = state["tasks"]
    set_stage("交付成品")

    if kind == "code":
        files = [p for p in Path(forge.CODE_DIR).rglob("*") if p.is_file()]
        names = "、".join(p.name for p in files[:8]) or "（无）"
        check_failures = await asyncio.to_thread(forge.verify_generated_code) if files else []
        added: list[str] = []
        if files:
            added = await asyncio.to_thread(forge.finalize_project, state["requirement"])
            files = [p for p in Path(forge.CODE_DIR).rglob("*") if p.is_file()]
        # 真实代码包：始终打包并挂到最终产物，用户可直接下载
        zip_name = await asyncio.to_thread(_zip_code_dir) if files else None
        saved_note = ""
        target_dir = forge.extract_target_dir(state["requirement"])
        if target_dir and zip_name:
            dest = await asyncio.to_thread(forge.copy_to_target, zip_name, target_dir)
            saved_note = (
                f"\n\n📁 代码包已按你的要求保存到：{dest}"
                if dest
                else f"\n\n⚠ 保存到「{target_dir}」失败：目录不可写或不存在，代码包仍可在产物中心下载"
            )
        check_note = (
            "\n\n## 可运行性检查\n\n"
            + (
                "✅ Python/JS 语法检查全部通过"
                if not check_failures
                else "⚠️ 以下文件未通过语法检查：\n"
                + "\n".join(f"- `{item}`" for item in check_failures[:8])
            )
            + ("\n\n已自动补充：`README.md`、`requirements.txt`" if added else "")
        )
        add_artifact(
            kind="final",
            title=f"最终交付物 · 代码包（{len(files)} 个文件）",
            author_roster=LEAD,
            content=(
                f"# 最终交付物\n\n"
                f"- 需求：{state['requirement']}\n"
                f"- 形式：源代码文件包\n"
                f"- 文件：{names}\n"
                f"- README：{'已生成' if 'README.md' in added else '未生成'}"
                f"{check_note}"
            ) + saved_note,
            file_name=zip_name,
            degraded=not zip_name or bool(check_failures),
            degraded_reason=(
                ""
                if zip_name and not check_failures
                else ("未产出可打包的代码文件" if not zip_name else "部分代码未通过语法检查")
            ),
            regen={"mode": "code", "target_dir": target_dir or ""},
        )
        set_state(LEAD, "working", "打包交付代码")
        await pause(1.0)
        return {"tasks": tasks}

    set_state(SM, "working", "整合最终成品文件")
    await pause(1.0)

    # 汇总本 Sprint 全员实际产出（PRD/开发说明/测试报告/评审记录），
    # 让最终成品基于团队真实工作成果汇编，而不是凭空重写
    team_parts: list[str] = []
    for a in artifacts_store:
        if a.get("kind") in ("prd", "dev", "test", "review"):
            team_parts.append(f"【{a['title']}】\n{a['content'][:1500]}")
    team_digest = "\n\n".join(team_parts)[:6000]
    tasks_digest = "、".join(
        f"{t['id']} {t['title']}（{t['status']}）" for t in tasks
    )

    up = state.get("uploads_text", "")
    if up:
        spec = (
            f"用户上传了附件素材，要求基于附件加工改造产出新成品。\n"
            f"附件内容：\n{up[:4000]}\n\n"
            f"加工需求：{state['requirement']}。\n"
            f"团队各环节的产出（请吸收其中的实质内容）：\n{team_digest}\n\n"
            "重要：成品必须真实体现附件内容与团队产出（改写、提炼、转换格式、补充扩展都算），"
            "把实际内容完整做出来，而不是描述怎么做或给模板。"
        )
    else:
        spec = (
            f"根据需求与团队各环节的实际产出，汇编最终成品。需求：{state['requirement']}。\n"
            f"任务完成情况：{tasks_digest}。\n\n"
            f"团队各环节的产出（请吸收其中的实质内容）：\n{team_digest}\n\n"
            "重要：必须把实际内容完整做出来（例如作文就写出全文、周报就写出完整周报、"
            "演示文稿就做出每页的实际内容），充分吸收团队产出中的实质内容，"
            "而不是描述怎么做或给模板。"
        )
    spec += drain_instructions()
    final_file = await asyncio.to_thread(
        forge.forge_file, kind, spec, f"最终交付物.{kind}", SM
    )
    preview = (
        await asyncio.to_thread(forge.extract_preview, final_file)
        if final_file
        else ""
    )
    # 成品自检：章节/页数/行列等可交付要素，结果如实写入产物正文
    check_note = ""
    check_failed = False
    if final_file:
        check = await asyncio.to_thread(forge.self_check, final_file, kind)
        check_note = forge.self_check_note(check)
        check_failed = not check.get("ok", False)
    # 用户在需求中指定保存位置时，把成品复制过去（如「生成word放在D:\资料」）
    saved_note = ""
    target_dir = forge.extract_target_dir(state["requirement"])
    if target_dir and final_file:
        dest = await asyncio.to_thread(forge.copy_to_target, final_file, target_dir)
        saved_note = (
            f"\n\n📁 已按你的要求保存到：{dest}"
            if dest
            else f"\n\n⚠ 保存到「{target_dir}」失败：目录不可写或不存在，成品仍可在产物中心下载"
        )
    add_artifact(
        kind="final",
        title=f"最终交付物 · {state['requirement'][:20]}",
        author_roster=SM,
        content=(
            preview
            or f"需求：{state['requirement']}\n\n成品文件：{final_file or '生成失败，请查看过程产物或重试'}"
        ) + saved_note + check_note,
        file_name=final_file,
        degraded=not final_file or check_failed,
        degraded_reason=(
            ""
            if final_file and not check_failed
            else (
                "成品文件生成失败，当前仅为内容摘要，请点击重试生成"
                if not final_file
                else "成品自检未全部通过，成品可能不完整，建议重新生成"
            )
        ),
        regen={
            "mode": "forge",
            "file_kind": kind,
            "spec": spec,
            "filename": f"最终交付物.{kind}",
            "roster": SM,
            "target_dir": target_dir or "",
        },
    )
    emote(SM, "emotes/hooray", "成品交付")
    return {"tasks": tasks}


def route_review(state: SprintState) -> str:
    if state["rework_count"] >= 1 and state["rework_count"] <= MAX_REWORK:
        buggy = [t for t in state["tasks"] if t["status"] == "打回"]
        if buggy:
            return "rework"
    return "approved"


def build_sprint_graph():
    graph = StateGraph(SprintState)
    graph.add_node("kickoff", kickoff)
    graph.add_node("assign", assign)
    graph.add_node("develop", develop)
    graph.add_node("test", test)
    graph.add_node("review", review)
    graph.add_node("fix", fix)
    graph.add_node("deliver", deliver)
    graph.add_node("wrapup", wrapup)

    graph.add_edge(START, "kickoff")
    graph.add_edge("kickoff", "assign")
    graph.add_edge("assign", "develop")
    graph.add_edge("develop", "test")
    graph.add_edge("test", "review")
    graph.add_conditional_edges(
        "review",
        route_review,
        {"rework": "fix", "approved": "deliver"},
    )
    graph.add_edge("fix", "test")
    graph.add_edge("deliver", "wrapup")
    graph.add_edge("wrapup", END)
    return graph.compile()
