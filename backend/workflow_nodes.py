"""LangGraph 节点实现：拆解 → 分派 → 并行开发 → 候选 → 测试 → 评审 → 修复 → 发布 → 复盘。

每个节点只负责一件事：读 state、发动作事件、写 store、返回增量。
流程图与路由判定在 graph.py；事件推送在 event_broadcaster；拆解与规划在 task_planner。
节点不 import graph（SprintState 仅用于类型标注），避免循环依赖。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import forge
import llm
import storage
import handoffs
import contributions
import verification
import review_decision
import candidate_revision
import artifact_quality
import user_preferences
from agile import plan_fields
from requirement_contract import build_contract
from artifacts import add_artifact, artifacts_store
from events import emit, set_stage, sprint_meta, tasks_store
from llm import chat_json
from roles import DEV_BE, DEV_FE, LEAD, PO, QA, SM, roster_label, roster_name

from event_broadcaster import (
    drain_instructions,
    emote,
    pause,
    reset_speak_llm,
    set_state,
    speak,
    thought,
    update_task,
)
from task_planner import (
    code_task_phases,
    compact_code_tasks,
    compact_short_document_tasks,
    decompose_plan,
    detect_final_kind,
    ensure_file_collaboration_tasks,
    mock_plan,
    pick_assignee,
)

if TYPE_CHECKING:
    from graph import SprintState

# 返工不硬限轮数（数值上仍保留一个远端上限）：产物不合格就一直打回重修，
# 真正的停止条件是用户设置的花费上限（pause() 内的 check_budget）或缺陷修复完成。
MAX_REWORK = 99


# ---------- LangGraph 节点 ----------

async def kickoff(state: SprintState) -> dict:
    """产品经理：拆解需求"""
    reset_speak_llm()  # 新 Sprint 开始，气泡对话恢复 LLM 生成
    set_stage("需求拆解")
    set_state(PO, "thinking", "分析需求")
    await pause(1.5)

    uploads_text = state.get("uploads_text", "")
    plan_meta: dict = {}
    plan, fallback_reason = await decompose_plan(
        state["requirement"],
        uploads_text,
        state.get("uploads_images") or None,
        drain_instructions(),
        plan_meta,
    )
    if fallback_reason:
        # 拆解未按预期输出：明示前端，避免用户误以为 LLM 拆解生效
        emit(
            {
                "type": "plan_fallback",
                "message": "任务拆解未按预期输出，已使用兜底拆解继续流程",
                "reason": fallback_reason,
            }
        )

    # 产品经理的拆解思路对团队可见（LLM 顺路输出；兜底时使用模板说明）
    thought(PO, str(plan.get("thought") or mock_plan()["thought"]), "需求拆解")

    contract = build_contract(
        state["requirement"], uploads_text, plan.get("contract"),
        deliver_kind="code" if state.get("task_kind") == "software" else detect_final_kind(state["requirement"]),
        fallback_reason=fallback_reason,
    )
    storage.save_contract(sprint_meta.get("id", ""), contract)
    emit({"type": "requirement_contract", "contract": contract})

    tasks: list[dict] = []
    planned = [item for item in plan["tasks"] if isinstance(item, dict) and item.get("title")][:8]
    planned.sort(key=lambda item: {"P0": 0, "P1": 1, "P2": 2}.get(item.get("priority", "P1"), 1))
    if contract.get("target_kind") == "code":
        planned = compact_code_tasks(planned)
    else:
        planned = compact_short_document_tasks(planned, contract)
        planned = ensure_file_collaboration_tasks(planned, contract)
    for i, item in enumerate(planned):
        title = str(item.get("title", f"任务 {i + 1}")).strip()[:40]
        tasks.append(
            {
                "id": f"T{i + 1}",
                "title": title,
                "role_hint": item.get("role_hint", "其他"),
                "assignee_roster": None,
                "status": "待分派",
                "summary": "",
                **plan_fields(item, state["requirement"], contract),
            }
        )

    tasks_store.clear()
    tasks_store.extend(tasks)
    for t in tasks:
        update_task(t)
    sprint_id = sprint_meta.get("id", "")
    if sprint_id and storage.get_sprint(sprint_id):
        handoffs.offer(
            sprint_id, task_id="", from_roster=PO, to_roster=SM,
            input_refs=[f"requirement:{c['requirement_id']}" for c in contract["requirements"]],
            output_refs=[f"contract:{sprint_id}"],
            claims=["已拆解原始需求并保留每条要求的来源与验收方法"],
            open_questions=contract["missing_info"],
            acceptance_criteria=["逐条检查原文与附件条款", "检查任务对 requirement_id 的覆盖"],
        )

    # 产物：需求拆解文档（PRD）——真实 .docx 文件，失败回退 Markdown
    prd_lines = [f"# 需求拆解文档（PRD）", "", f"## 原始需求", "", state["requirement"], "", "## 任务拆解", ""]
    for t in tasks:
        prd_lines.append(f"- **{t['id']} {t['title']}**（{t['role_hint']} / {t['priority']} / {t['story_points']} 点）\n"
                         f"  {t['user_story']}\n  验收：{'；'.join(t['acceptance_criteria'])}")
    prd_md = "\n".join(prd_lines)
    # The plan already contains the complete PRD source. Render it locally so
    # a short file task does not spend another model call on a verbose process doc.
    prd_file = await asyncio.to_thread(
        forge.render_docx_from_markdown, prd_md, "PRD需求拆解文档.docx"
    )
    add_artifact(
        kind="prd",
        title="需求拆解文档（PRD）",
        source="fallback" if plan_meta.get("fallback_reason") else "model",
        author_roster=PO,
        content=prd_md if not prd_file else prd_md + f"\n\n真实文件：{prd_file}",
        file_name=prd_file,
        degraded=not prd_file,
        degraded_reason="" if prd_file else "Word 文件生成失败，当前仅为 Markdown 摘要",
        regen={
            "mode": "structured_plan",
            "file_kind": "docx",
            "source_markdown": prd_md,
            "filename": "PRD需求拆解文档.docx",
        },
    )

    # The PO contract already records the intended output format.
    deliver_kind = contract.get("target_kind") or detect_final_kind(state["requirement"])
    if deliver_kind not in ("docx", "pptx", "xlsx", "pdf", "code", "html"):
        deliver_kind = "docx"
    if state.get("task_kind") == "software":
        deliver_kind = "code"
    elif "pdf" in state["requirement"].lower() and any(word in state["requirement"].lower() for word in ("word", "docx")):
        deliver_kind = "docx"
    contract["target_kind"] = deliver_kind
    storage.save_contract(sprint_meta.get("id", ""), contract)

    set_state(PO, "working", "编写需求说明")
    await pause(1.0)
    await speak(
        PO,
        SM,
        f"需求「{state['requirement']}」拆解出了 {len(tasks)} 个任务，请教练组织分派",
        f"需求拆解完成，共 {len(tasks)} 个任务，请组织分派",
    )
    return {"tasks": tasks, "deliver_kind": deliver_kind, "requirement_contract": contract}


async def assign(state: SprintState) -> dict:
    """敏捷教练：任务分派到人"""
    tasks = state["tasks"]
    set_stage("任务分派")
    set_state(SM, "thinking", "评估工作量与分工")
    await pause(1.5)

    file_route = state.get("deliver_kind") != "code"
    for i, t in enumerate(tasks):
        hint = t.get("role_hint")
        title = t["title"]
        hinted = {"测试": QA, "前端": DEV_FE, "后端": DEV_BE,
                  "写作": DEV_FE, "设计": DEV_FE}.get(hint)
        if hinted is not None:
            t["assignee_roster"] = hinted
        elif file_route and any(word in title for word in ("测试", "验收", "校核", "检查", "验证")):
            t["assignee_roster"] = QA
        elif file_route and any(word in title for word in ("资料", "来源", "数据", "计算", "事实", "要素", "调研")):
            t["assignee_roster"] = DEV_BE
        elif file_route and any(word in title for word in ("正文", "撰写", "排版", "版式", "设计", "导出")):
            t["assignee_roster"] = DEV_FE
        else:
            t["assignee_roster"] = hinted or pick_assignee(title, i)
    dev_tasks = [t for t in tasks if t["assignee_roster"] != QA]
    if len(dev_tasks) >= 2:
        assigned = {t["assignee_roster"] for t in dev_tasks}
        if DEV_BE not in assigned:
            dev_tasks[0]["assignee_roster"] = DEV_BE
        if DEV_FE not in assigned:
            dev_tasks[-1]["assignee_roster"] = DEV_FE
    scopes = {
        DEV_BE: "事实、数据、计算和来源核对" if file_route else "业务逻辑、接口和数据结构",
        DEV_FE: "结构、正文与版式" if file_route else "命令行或界面交互与呈现",
        QA: "按条款设计并执行验证",
    }
    sprint_id = sprint_meta.get("id", "")
    po_offers = storage.list_handoffs(sprint_id) if sprint_id and storage.get_sprint(sprint_id) else []
    for offer in po_offers:
        if offer["from_roster"] == PO and offer["to_roster"] == SM and offer["status"] == "ready":
            handoffs.receive(offer["id"], accepted=True)
    for t in tasks:
        t["role_scope"] = scopes.get(t["assignee_roster"], "待确认")
        t["status"] = "待开发"
        update_task(t)
        if sprint_id and storage.get_sprint(sprint_id):
            refs = [f"contract:{sprint_id}"] + [f"requirement:{key}" for key in t.get("requirement_ids", [])]
            offer = handoffs.offer(
                sprint_id, task_id=t["id"], from_roster=SM, to_roster=t["assignee_roster"],
                input_refs=refs, output_refs=[f"task:{t['id']}"],
                claims=[f"职责：{t['role_scope']}；任务：{t['title']}"],
                acceptance_criteria=t.get("acceptance_criteria", []),
            )
            t["assignment_handoff_id"] = offer["id"]
            update_task(t)
    set_state(SM, "working", "分派任务")
    await pause(0.8)

    # 分工依据对团队可见：哪个任务给谁、各自的职责边界（确定性合成真实分派结果）
    assignment_parts = [
        f"「{t['title']}」→ {roster_name(t['assignee_roster'])}（{t.get('role_scope', '待确认')}）"
        for t in tasks if t.get("assignee_roster")
    ]
    thought(SM, "按角色匹配分派：" + "；".join(assignment_parts), "任务分派")

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
        if t.get("assignment_handoff_id"):
            handoffs.receive(t["assignment_handoff_id"], accepted=True)
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
            # 接手思路对团队可见：职责边界 + 验收条件 + 入手方向（确定性合成，不耗模型配额）
            criteria = t.get("acceptance_criteria", [])
            thought(
                no,
                f"接到「{t['title']}」：职责是{t.get('role_scope', '按任务要求执行')}；"
                f"需满足 {len(criteria)} 条验收（{criteria[0] if criteria else '按任务描述核验'}），"
                + ("重点排查上一轮指出的缺陷后重新提交。" if t["status"] == "打回"
                   else "先梳理输入素材与需求条款，再动手产出。"),
                "并行开发", t["id"],
            )
            # 产物：开发说明（LLM 生成实现要点，未配 LLM 时用模板兜底）
            note_meta: dict = {}
            if state.get("deliver_kind", "docx") == "code":
                summary = ""
            else:
                role_instruction = (
                    "你是后端工程师，负责事实、数字、计算和来源核对。提交可复用的素材核对表，"
                    "每条写明原文、来源位置、可确认事实、待确认项；不撰写空泛完成汇报。"
                    if no == DEV_BE else
                    "你是前端工程师，负责内容结构、正文和版式。提交可直接用于最终文件的完整内容，"
                    "明确标题、正文、表格或页面结构；不得捏造附件没有的数字和事实。"
                    if no == DEV_FE else
                    "你是测试工程师，提交可执行或可逐条核对的测试用例和预期结果；未执行的检查不得写通过。"
                )
                summary = await asyncio.to_thread(
                    llm.chat_text,
                    role_instruction,
                    [{"role": "user", "content": json.dumps({"requirement": state["requirement"], "task": t,
                        "requirement_contract": state.get("requirement_contract", {}),
                        "source_material": state.get("uploads_text", "")[:18000], "instructions": drain_instructions()}, ensure_ascii=False)}],
                    fallback="当前模型不可用，未能生成本任务的真实内容。", roster=no, meta=note_meta,
                    max_tokens=6000, timeout=240, temperature=.45, tool_mode=False,
                    fallback_roster=1 if no != 1 else None,
                )

            # 真实代码产出：仅软件类需求由 LLM 为该任务编写真实代码文件
            written: list[str] = []
            if state.get("deliver_kind", "docx") == "code":
                dev_requirement = state["requirement"]
                dev_requirement += f"\n本任务用户故事：{t.get('user_story', '')}\n验收条件：{'；'.join(t.get('acceptance_criteria', []))}"
                role_boundaries = {
                    DEV_BE: "只实现核心业务逻辑、公开接口和输入/领域校验；不要生成 CLI/界面、测试、README 或另一份完整项目。文件类任务须把已知事实、来源与待确认项分开，未知信息保持待确认。",
                    DEV_FE: "只实现命令行/用户入口、参数交互和 README；遵循 BE 贡献的模块/函数接口，不要重写核心逻辑或生成测试。文件类任务只能使用用户原话和 BE 明确核实的事实；未知日期、署名、渠道、链接、原因等必须标为待确认，不得补造具体信息。",
                    QA: "只生成测试文件（tests/ 下或 test_*.py），覆盖成功路径和错误路径；必须实际测试 CLI 成功与失败退出码；禁止 skip/xfail/跳过测试；禁止生成或修改生产代码、README、依赖文件。测试必须针对共享计划中的 BE 接口。",
                }
                dev_requirement += (f"\n角色文件边界：{role_boundaries.get(no, '只提交本任务职责范围内的文件，不要复制其他任务的实现。')}"
                                    "\n禁止复刻其他任务已负责的模块；如依赖接口尚未明确，以最少可检查的调用假设编写，并将问题写入测试。")
                if state.get("deliver_kind", "docx") == "code" and no in (DEV_FE, QA):
                    backend_material = []
                    for backend_task in tasks:
                        if backend_task.get("assignee_roster") != DEV_BE:
                            continue
                        valid, _, backend_manifest = contributions.inspect(sprint_meta.get("id", ""), backend_task["id"])
                        if not valid:
                            continue
                        for file_info in backend_manifest.get("files", []):
                            try:
                                source = Path(file_info["path"]).read_text(encoding="utf-8", errors="replace")
                            except OSError:
                                continue
                            backend_material.append(f"--- {file_info['logical_path']} ---\n{source[:5000]}")
                    if backend_material:
                        dev_requirement += "\n\n已完成并校验的 BE 接口源码；必须按此导入和调用，不得自行猜测接口：\n" + "\n".join(backend_material)[:12000]
                up = state.get("uploads_text", "")
                if up:
                    dev_requirement += f"\n\n（需加工改造的现有代码/素材如下，请在此基础上改写扩展）\n{up[:3000]}"
                dev_requirement += drain_instructions()
                dev_requirement += f"\n本轮共享任务计划：{json.dumps([{'id': x['id'], 'title': x['title'], 'role_scope': x.get('role_scope', '')} for x in tasks], ensure_ascii=False)}"
                written = await asyncio.to_thread(
                    forge.forge_code, t["title"], dev_requirement, t["id"], no, note_meta
                )
                summary = "实际代码文件：" + ("、".join(written) if written else "未生成")

        if t["status"] == "已取消":
            # 生成期间被取消：保留取消状态，不再翻回待测试
            set_state(no, "idle", "等待新任务")
            return
        sprint_id = sprint_meta.get("id", "")
        source_refs = [f"requirement:{key}" for key in t.get("requirement_ids", [])]
        if state.get("uploads_text"):
            source_refs += [f"attachment:{src['name']}:{src.get('location') or 'unknown'}"
                            for src in state.get("requirement_contract", {}).get("attachment_sources", [])]
        if sprint_id and storage.get_sprint(sprint_id):
            if state.get("deliver_kind", "docx") == "code":
                manifest = contributions.save_code(
                    sprint_id, t["id"], no, t.get("role_scope", ""), written,
                    t.get("requirement_ids", []), source_refs, generation_meta=note_meta,
                )
            else:
                manifest = contributions.save_text(
                    sprint_id, t["id"], no, t.get("role_scope", ""), summary,
                    t.get("requirement_ids", []), source_refs,
                    status="unchecked" if note_meta.get("fallback_reason") else "ready",
                    generation_meta=note_meta,
                )
            t["contribution_ref"] = f"contribution:{t['id']}"
        else:
            manifest = {}
        task_has_deliverable = bool(written) if state.get("deliver_kind", "docx") == "code" else bool(summary.strip()) and not note_meta.get("fallback_reason")
        t["status"] = "待测试" if task_has_deliverable else "打回"
        t["summary"] = summary[:1500]
        t["code_files"] = written
        update_task(t)
        code_note = (
            "\n\n真实代码文件：\n" + "\n".join(f"- `{p}`" for p in written)
            if written else ""
        )
        dev_artifact = add_artifact(
            kind="dev",
            title=f"开发说明 · {t['title']}",
            source="fallback" if note_meta.get("fallback_reason") else "model",
            author_roster=no,
            content=(
                f"# 开发说明 · {t['title']}\n\n"
                f"- 负责人：{roster_name(no)}\n"
                f"- 所属需求：{state['requirement']}\n\n"
                f"{summary[:14000]}{code_note}\n\n贡献清单：{manifest.get('files', [])}"
            ),
            task_id=t["id"],
            degraded=bool(note_meta.get("fallback_reason")) or (state.get("deliver_kind") == "code" and not written),
            degraded_reason=note_meta.get("fallback_reason", "") or ("未生成可复用代码文件" if state.get("deliver_kind") == "code" and not written else ""),
        )
        sprint_id = sprint_meta.get("id", "")
        if sprint_id and storage.get_sprint(sprint_id):
            handoffs.offer(
                sprint_id, task_id=t["id"], from_roster=no, to_roster=SM,
                input_refs=[f"contract:{sprint_id}"] + [f"requirement:{key}" for key in t.get("requirement_ids", [])] + [f"task:{t['id']}"],
                output_refs=[f"artifact:{dev_artifact['id']}", t["contribution_ref"]],
                claims=[f"已提交任务内容：{t['title']}"],
                open_questions=([] if (written or summary) and not note_meta.get("fallback_reason")
                                else [note_meta.get("fallback_reason") or "没有可复用的实际内容"]),
                acceptance_criteria=t.get("acceptance_criteria", []),
            )
        emote(no, "emotes/idea", "灵感")

    if state.get("deliver_kind", "docx") == "code":
        # BE owns the shared production interface. FE and QA receive its actual
        # source before they generate their dependent files, while FE and QA
        # remain parallel with each other.
        for phase in code_task_phases(pending):
            await asyncio.gather(*(run_one(t) for t in phase))
    else:
        await asyncio.gather(*(run_one(t) for t in pending))
    sprint_id = sprint_meta.get("id", "")
    if sprint_id and storage.get_sprint(sprint_id):
        issues = contributions.scan_conflicts(sprint_id)
        for issue in issues:
            emit({"type": "integration_issue", "issue": issue, "sprint_id": sprint_id})
            for task in tasks:
                if task["id"] in issue.get("tasks", []) or task["id"] == issue.get("task_id"):
                    task.setdefault("integration_conflicts", []).append(issue)
                    update_task(task)
        for offer in storage.list_handoffs(sprint_id):
            if offer["to_roster"] == SM and offer["from_roster"] in (DEV_BE, DEV_FE) and offer["status"] == "ready":
                related = next((task for task in tasks if task["id"] == offer["task_id"]), None)
                handoffs.receive(offer["id"], accepted=not bool(related and related.get("integration_conflicts")),
                                 reason="接口或文件冲突，等待整合" if related and related.get("integration_conflicts") else "")

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


def _workflow_evidence(sprint_id: str, tasks: list[dict], contract: dict, candidate_sha256: str) -> dict:
    manifests = []
    for task in tasks:
        roster = task.get("assignee_roster")
        if roster not in (DEV_BE, DEV_FE) or not sprint_id:
            continue
        valid, reason, manifest = contributions.inspect(sprint_id, task["id"])
        contents = []
        if valid:
            for file in manifest.get("files", []):
                try:
                    contents.append(Path(file["path"]).read_text(encoding="utf-8")[:5000])
                except OSError:
                    valid, reason = False, "贡献文件不可读取"
                    break
        manifests.append({"task_id": task["id"], "roster": roster, "valid": valid,
                          "reason": reason, "requirement_ids": task.get("requirement_ids", []),
                          "source_refs": manifest.get("source_refs", []),
                          "files": manifest.get("files", []), "content": "\n".join(contents)[:7000]})
    return {"contract_status": contract.get("status"), "candidate_sha256": candidate_sha256,
            "tasks": [{"task_id": t.get("id"), "roster": t.get("assignee_roster"),
                       "requirement_ids": t.get("requirement_ids", []), "status": t.get("status")}
                      for t in tasks],
            "contributions": manifests,
            "handoffs": storage.list_handoffs(sprint_id) if sprint_id and storage.get_sprint(sprint_id) else []}


def _offer_candidate_to_qa(sprint_id: str, state: SprintState, candidate_id: str,
                           candidate_sha256: str, companions: list[dict] | None = None) -> None:
    if not sprint_id or not storage.get_sprint(sprint_id) or not candidate_id:
        return
    refs = [f"requirement:{item['requirement_id']}"
            for item in state.get("requirement_contract", {}).get("requirements", [])]
    refs += [task["contribution_ref"] for task in state.get("tasks", []) if task.get("contribution_ref")]
    outputs = [f"artifact:{candidate_id}"] + [f"artifact:{item['id']}" for item in companions or []]
    handoffs.offer(
        sprint_id, task_id="", from_roster=SM, to_roster=QA,
        input_refs=refs, output_refs=outputs,
        claims=[f"已整合候选 SHA-256 {candidate_sha256}，请按 PO 条款检查同一版本"],
        acceptance_criteria=["逐条核对候选文件及协作来源；记录同一哈希"],
    )


async def test(state: SprintState) -> dict:
    """QA verifies the candidate bytes against every PO clause."""
    set_stage("测试验证")
    set_state(QA, "thinking", "逐条核验候选成品")
    await pause(0.8)
    sprint_id = sprint_meta.get("id", "")
    if sprint_id and storage.get_sprint(sprint_id):
        for offer in storage.list_handoffs(sprint_id):
            if offer["from_roster"] == SM and offer["to_roster"] == QA and offer["status"] == "ready":
                handoffs.receive(offer["id"], accepted=True)
    candidate_id = state.get("candidate_id", "")
    item = storage.get_artifact(candidate_id) if candidate_id else None
    if not item:
        item = next((a for a in artifacts_store if a.get("id") == candidate_id), None)
    workflow_evidence = _workflow_evidence(
        sprint_id, state.get("tasks", []), state.get("requirement_contract", {}),
        state.get("candidate_sha256", ""),
    )
    report = await asyncio.to_thread(
        verification.verify, item or {}, state.get("requirement_contract", {}),
        state.get("candidate_sha256", ""), workflow_evidence,
    )
    # The QA contribution is a submitted checklist/report. Once verification
    # has produced its evidence artifact, its contribution lifecycle is closed;
    # QA pass/fail remains in the separate verification report.
    if sprint_id:
        for task in state.get("tasks", []):
            if task.get("assignee_roster") == QA and task.get("contribution_ref"):
                contributions.mark_completed(sprint_id, task["id"])
    companion_reports = []
    for companion in state.get("companion_candidates", []):
        extra = storage.get_artifact(companion["id"]) if sprint_id and storage.get_sprint(sprint_id) else None
        if not extra:
            extra = next((a for a in artifacts_store if a.get("id") == companion["id"]), None)
        checked = await asyncio.to_thread(
            verification.verify, extra or {}, state.get("requirement_contract", {}), companion["sha256"],
            workflow_evidence,
        )
        companion_reports.append(checked)
    if companion_reports:
        report["companions"] = companion_reports
        report["companion_hashes"] = [entry["candidate_sha256"] for entry in companion_reports]
        statuses = [report["status"]] + [entry["status"] for entry in companion_reports]
        report["status"] = "failed" if "failed" in statuses else "unchecked" if "unchecked" in statuses else "passed"
        report["items"] += [
            {**part, "location": f"PDF {part.get('location', '')}"}
            for entry in companion_reports for part in entry.get("items", []) if part.get("status") == "failed"
        ]
    qa_artifact = add_artifact(
        kind="test", title="QA 逐条验证报告", author_roster=QA,
        content=json.dumps(report, ensure_ascii=False, indent=2),
        quality_status=report["status"], source="model" if report["status"] != "unchecked" else "fallback",
        degraded=report["status"] == "failed", degraded_reason=report.get("reason", ""),
        regen={"mode": "qa_report", "candidate_id": candidate_id,
               "candidate_sha256": state.get("candidate_sha256", "")},
    )
    tasks = state["tasks"]
    failed_ids = {item["requirement_id"] for item in report.get("items", []) if item["status"] == "failed"}
    for task in tasks:
        if task["status"] == "已取消":
            continue
        task["status"] = "打回" if failed_ids.intersection(task.get("requirement_ids", [])) else "待评审"
        update_task(task)

    # QA 的核验依据对团队可见：多少条通过/失败、主要问题在哪
    qa_items = report.get("items", [])
    qa_failed = [item for item in qa_items if item.get("status") == "failed"]
    qa_thought_text = (
        f"逐条核验 {len(qa_items)} 条：通过 {len(qa_items) - len(qa_failed)} 条，"
        f"未通过 {len(qa_failed)} 条，结论 {report['status']}。"
        + (f"首要问题：{qa_failed[0].get('problem') or report.get('reason', '')}"
           if qa_failed else "")
    )
    thought(QA, qa_thought_text, "测试验证")
    if sprint_id and storage.get_sprint(sprint_id):
        handoffs.offer(
            sprint_id, task_id="", from_roster=QA, to_roster=LEAD,
            input_refs=[f"contract:{sprint_id}", f"artifact:{candidate_id}"],
            output_refs=[f"artifact:{qa_artifact['id']}"],
            claims=[f"QA 对候选 {state.get('candidate_sha256', '')} 的逐条结论：{report['status']}"],
            open_questions=[report["reason"]] if report.get("reason") else [],
            acceptance_criteria=["LEAD 独立复核相同 SHA-256 候选"],
        )
    await speak(QA, LEAD, f"候选逐条核验为 {report['status']}，请复核同一版本", f"QA 结论：{report['status']}")
    return {"tasks": tasks, "qa_report": report, "review_note": report["status"]}


async def review(state: SprintState) -> dict:
    """LEAD independently reads the same candidate even when QA has failed."""
    set_stage("技术评审")
    set_state(LEAD, "thinking", "独立复核候选与 QA 证据")
    await pause(0.8)
    sprint_id = sprint_meta.get("id", "")
    records = storage.list_handoffs(sprint_id) if sprint_id and storage.get_sprint(sprint_id) else []
    for offer in records:
        if offer["to_roster"] == LEAD and offer["status"] == "ready":
            handoffs.receive(offer["id"], accepted=True)
    candidate_id = state.get("candidate_id", "")
    item = storage.get_artifact(candidate_id) if candidate_id else None
    if not item:
        item = next((a for a in artifacts_store if a.get("id") == candidate_id), None)
    companion_artifacts = []
    for companion in state.get("companion_candidates", []):
        extra = storage.get_artifact(companion["id"]) if sprint_id and storage.get_sprint(sprint_id) else None
        if not extra:
            extra = next((a for a in artifacts_store if a.get("id") == companion["id"]), None)
        if extra:
            companion_artifacts.append(extra)
    workflow_evidence = _workflow_evidence(
        sprint_id, state.get("tasks", []), state.get("requirement_contract", {}),
        state.get("candidate_sha256", ""),
    )
    decision = await asyncio.to_thread(
        review_decision.decide, item or {}, state.get("requirement_contract", {}),
        state.get("qa_report", {}), records, state.get("candidate_sha256", ""), companion_artifacts,
        workflow_evidence,
    )
    review_artifact = add_artifact(
        kind="review", title="LEAD 独立复核决定", author_roster=LEAD,
        content=json.dumps(decision, ensure_ascii=False, indent=2),
        quality_status=decision["status"], source="model" if decision["status"] != "unchecked" else "fallback",
        degraded=decision["status"] == "failed", degraded_reason=decision.get("reason", ""),
        regen={"mode": "lead_decision", "candidate_id": candidate_id,
               "candidate_sha256": state.get("candidate_sha256", "")},
    )
    tasks = state["tasks"]
    if decision["status"] == "failed":
        failed_ids = {part["requirement_id"] for part in state.get("qa_report", {}).get("items", [])
                      if part.get("status") == "failed"}
        failed_ids.update(issue.get("requirement_id") for issue in decision.get("issues", []))
        assigned = False
        for task in tasks:
            if task["status"] == "已取消":
                continue
            if failed_ids.intersection(task.get("requirement_ids", [])):
                task["status"] = "打回"
                assigned = True
                update_task(task)
        if not assigned:
            for task in tasks:
                if task["status"] != "已取消":
                    task["status"] = "打回"
                    update_task(task)
    elif decision["status"] == "passed":
        for task in tasks:
            if task["status"] not in ("已取消", "通过"):
                task["status"] = "通过"
                update_task(task)
    if sprint_id and storage.get_sprint(sprint_id):
        handoffs.offer(
            sprint_id, task_id="", from_roster=LEAD, to_roster=SM,
            input_refs=[f"contract:{sprint_id}", f"artifact:{candidate_id}"],
            output_refs=[f"artifact:{review_artifact['id']}"],
            claims=[f"LEAD 对候选 {state.get('candidate_sha256', '')} 的独立决定：{decision['status']}"],
            open_questions=[decision["reason"]] if decision.get("reason") else [],
            acceptance_criteria=["发布字节 SHA-256 与 LEAD 审阅候选一致"],
        )
    # LEAD 的复核依据对团队可见：结论 + 理由 + 定位到的问题
    lead_issues = decision.get("issues", [])
    lead_thought_text = (
        f"独立复核结论：{decision['status']}。"
        + (f"理由：{decision['reason']}。" if decision.get("reason") else "")
        + (f"共定位 {len(lead_issues)} 处需修复问题。" if lead_issues else "")
    )
    thought(LEAD, lead_thought_text, "技术评审")

    # 打回时当面指出问题：LEAD 逐个拜访被打回任务的负责人，当面说清楚哪里不行
    if decision["status"] == "failed":
        visit_targets: list[int] = []
        for task in tasks:
            owner = task.get("assignee_roster")
            if task.get("status") == "打回" and owner in (DEV_BE, DEV_FE, QA) and owner not in visit_targets:
                visit_targets.append(owner)
        for owner in visit_targets[:2]:
            owner_problems = [
                issue.get("problem") or issue.get("location") or "有条款未通过"
                for issue in lead_issues
                if issue.get("owner_roster") in (None, owner)
            ][:2]
            problem_text = "；".join(str(p)[:40] for p in owner_problems) or "部分验收条款未通过"
            await speak(
                LEAD,
                owner,
                f"评审未通过，向{roster_label(owner)}当面指出问题：{problem_text}，请其确认并认领修复",
                f"评审没通过：{problem_text}，麻烦尽快修复",
            )
    await speak(LEAD, SM, f"同版本独立评审：{decision['status']}", f"LEAD 结论：{decision['status']}")
    return {"tasks": tasks, "lead_decision": decision,
            "review_note": decision.get("reason", ""),
            "rework_count": state["rework_count"] + (1 if decision["status"] == "failed" else 0)}



async def fix(state: SprintState) -> dict:
    """Route located issues to the owner and create a new candidate version."""
    set_stage("定点返工")
    old_id = state.get("candidate_id", "")
    current = storage.get_artifact(old_id) if old_id else None
    if not current:
        current = next((a for a in artifacts_store if a.get("id") == old_id), None)
    if not current or not current.get("file_name"):
        return {"repair_blocked": True}
    kind = (current.get("regen") or {}).get("file_kind")
    qa_items = [item for item in state.get("qa_report", {}).get("items", []) if item.get("status") == "failed"]
    lead_items = state.get("lead_decision", {}).get("issues", [])
    issues = qa_items + lead_items
    # 字数精确兜底：不依赖条款文本是否含「字数」关键词。只要需求是「恰好 N 字」
    # 且 QA 统一口径实测不符，就把精确返工排到修复队列最前，杜绝路由漏判。
    if kind in ("docx", "pdf"):
        requested = artifact_quality.contract(state["requirement"], "docx")
        actual = (state.get("qa_report", {}).get("metrics") or {}).get("counted_characters")
        if (requested.get("unit") == "字" and requested.get("count_mode") == "exact"
                and requested.get("count") and isinstance(actual, int) and actual != requested["count"]):
            issues.insert(0, {"requirement_id": "", "location": "全文", "owner_roster": None,
                              "problem": f"字数不达标：实测 {actual} 字，要求恰好 {requested['count']} 字。"})
    succeeded = 0
    for issue in issues[:6]:
        requirement_id = issue.get("requirement_id")
        owner = issue.get("owner_roster")
        if owner not in (DEV_BE, DEV_FE):
            matched = next((t for t in state["tasks"] if requirement_id in t.get("requirement_ids", [])
                            and t.get("assignee_roster") in (DEV_BE, DEV_FE)), None)
            owner = matched["assignee_roster"] if matched else DEV_FE
        set_state(owner, "working", f"修复 {issue.get('location') or requirement_id or '候选问题'}")
        preview = await asyncio.to_thread(forge.extract_preview, current["file_name"], 14000)
        project_dir = (current.get("regen") or {}).get("project_dir", "")
        if kind == "code" and project_dir:
            import project_agent
            preview = project_agent.snapshot(Path(project_dir), limit=20000)
        meta = {}
        problem_text = f"{issue.get('problem') or ''} {issue.get('location') or ''}"
        if kind in ("docx", "pdf") and re.search(r"字数|篇幅|恰好.{0,8}字|正文.{0,10}\d+\s*字", problem_text):
            # 字数按统一口径精确返工：模型按“还差多少字”的确定性反馈迭代重写正文，
            # 直到恰好命中目标字数（页数等其它篇幅约束仍走确定性压缩）。
            patch = {"operation": "refit_length_exact", "location": "全文", "requirement": state["requirement"]}
        else:
            patch = await asyncio.to_thread(
                chat_json,
                system=(
                    f"你是{roster_name(owner)}，只修复指定位置的缺陷。"
                    '输出 JSON {"location":"第N页/第N段/工作表!A1/文件路径",'
                    '"operation":"replace|delete（仅 Word 完整段落允许 delete）",'
                    '"old_text":"文档或PPT中的精确旧文本","replacement":"完整替换文本/公式/代码文件内容",'
                    '"path":"代码相对路径（代码任务必填）"}。'
                    "Word/PPT 必须给精确旧文本；Excel 给精确单元格；代码只给一个既有文件的完整新内容。"
                    "若 Word 中整段是需求未提供的占位字段，可用 operation=delete，old_text 填完整段落原文。"
                    "不删除测试、功能或无关内容。无法定位则返回空 JSON。"
                ) + user_preferences.prompt_block(),
                user=json.dumps({"kind": kind, "issue": issue, "candidate_sha256": state.get("candidate_sha256"),
                                 "actual_content": preview, "requirement": state["requirement"]}, ensure_ascii=False),
                fallback={}, roster=owner, meta=meta, max_tokens=6500, timeout=180,
            )
        if meta.get("fallback_reason") or not isinstance(patch, dict):
            continue
        new_file, reason, detail = await asyncio.to_thread(
            candidate_revision.apply, sprint_meta.get("id", ""), kind, current["file_name"], patch, project_dir, owner
        )
        if not new_file:
            emit({"type": "repair_attempt", "status": "blocked", "reason": reason, "issue": issue})
            continue
        digest = _sha256_file(new_file)
        previous_regen = current.get("regen") or {}
        next_regen = {**previous_regen, "sha256": digest, "repair": {"issue": issue, "detail": detail,
                        "previous_sha256": state.get("candidate_sha256")}}
        if detail.get("project_dir"):
            next_regen["project_dir"] = detail["project_dir"]
        revised = add_artifact(
            kind="candidate", title=f"返工候选 v{int(current.get('version') or 1) + 1}",
            author_roster=owner, content=f"修订位置：{detail}\n原因：{issue.get('problem', '')}",
            file_name=new_file, quality_status="unchecked", source="model",
            parent_id=current["id"], version=int(current.get("version") or 1) + 1,
            regen=next_regen,
        )
        from artifacts import update_artifact
        update_artifact(revised["id"], quality_status="unchecked")
        if sprint_meta.get("id") and storage.get_sprint(sprint_meta["id"]):
            handoffs.offer(
                sprint_meta["id"], task_id="", from_roster=owner, to_roster=QA,
                input_refs=[f"contract:{sprint_meta['id']}", f"artifact:{current['id']}"],
                output_refs=[f"artifact:{revised['id']}"],
                claims=[f"定点修复 {detail}，旧哈希 {state.get('candidate_sha256')}，新哈希 {digest}"],
                acceptance_criteria=["QA 对新 SHA-256 重新逐条验收"],
                version=revised["version"],
            )
        emit({"type": "repair_attempt", "status": "ready", "from_candidate": current["id"],
              "to_candidate": revised["id"], "detail": detail, "sha256": digest})
        # 修复思路对团队可见：定位到哪里、怎么修的
        thought(
            owner,
            f"针对「{str(issue.get('problem') or issue.get('location') or '候选问题')[:60]}」："
            f"定位到 {str(patch.get('location', '原文位置'))[:30]}，"
            f"采用{patch.get('operation', 'replace')}完成修复，生成返工候选。",
            "定点返工",
        )
        current = revised
        state["candidate_id"] = revised["id"]
        state["candidate_sha256"] = digest
        if kind == "docx" and state.get("companion_candidates"):
            previous_companion = state["companion_candidates"][0]
            pdf_file = await asyncio.to_thread(forge.pdf_from_docx, new_file, "同源报告-修订.pdf")
            if not pdf_file:
                emit({"type": "repair_attempt", "status": "blocked", "reason": "新 Word 无法导出同源 PDF"})
                return {"repair_blocked": True, "candidate_id": revised["id"], "candidate_sha256": digest}
            pdf_hash = _sha256_file(pdf_file)
            pdf_artifact = add_artifact(
                kind="candidate", title=f"同源 PDF 候选 v{revised['version']}", author_roster=owner,
                content=f"由修订 Word {revised['id']} 导出", file_name=pdf_file,
                quality_status="unchecked", parent_id=previous_companion["id"], version=revised["version"],
                regen={"mode": "candidate", "file_kind": "pdf", "sha256": pdf_hash,
                       "source_candidate_id": revised["id"], "source_sha256": digest},
            )
            state["companion_candidates"] = [{"id": pdf_artifact["id"], "sha256": pdf_hash}]
        succeeded += 1
    if succeeded:
        for task in state["tasks"]:
            if task["status"] == "打回":
                task["status"] = "待测试"
                update_task(task)
    return {"tasks": state["tasks"], "candidate_id": state.get("candidate_id", ""),
            "candidate_sha256": state.get("candidate_sha256", ""),
            "companion_candidates": state.get("companion_candidates", []),
            "repair_blocked": not bool(succeeded)}


def retrospective_content(state: SprintState, accepted: bool, finals: list[dict]) -> tuple[str, str]:
    """Build a factual retrospective from task and review evidence only."""
    done = [task for task in state["tasks"] if task.get("status") == "通过"]
    qa_report = state.get("qa_report", {})
    lead_decision = state.get("lead_decision", {})
    status_label = "通过" if accepted else "未通过"
    task_rows = "\n".join(
        f"| {task.get('id', '')} | {roster_name(task.get('assignee_roster', 0))} | "
        f"{str(task.get('title', '')).replace('|', '/')} | {task.get('status', '未知')} |"
        for task in state["tasks"]
    ) or "| — | — | 没有任务 | — |"
    outstanding = [task for task in state["tasks"] if task.get("status") != "通过"]
    if outstanding:
        next_steps = "\n".join(f"- 补齐或返工 {task.get('id', '')}：{task.get('title', '')}（当前状态：{task.get('status', '未知')}）。"
                                for task in outstanding)
    elif not accepted:
        next_steps = "- 修复最终产物的未通过检查，并在同一候选版本上重新完成 QA 与主管复核。"
    else:
        next_steps = "- 记录本轮验收证据，并按计划继续下一轮工作。"
    final_rows = "\n".join(
        f"- {artifact.get('title', artifact.get('kind', '产物'))}：{artifact.get('quality_status', '未知')}；"
        f"{'降级' if artifact.get('degraded') else '未标记降级'}"
        for artifact in finals
    ) or "- 没有已登记的最终产物。"
    summary_text = (
        f"验收状态：{status_label}。通过任务 {len(done)}/{len(state['tasks'])}。"
        f"QA 状态：{qa_report.get('status', '未记录')}；主管复核状态：{lead_decision.get('status', '未记录')}。"
    )
    markdown = (
        "# Sprint 复盘总结\n\n"
        f"## 需求\n\n{state['requirement']}\n\n"
        "## 验收结论\n\n"
        f"- 最终交付验收：{status_label}\n"
        f"- 通过任务：{len(done)}/{len(state['tasks'])}\n"
        f"- QA：{qa_report.get('status', '未记录')}\n"
        f"- 主管复核：{lead_decision.get('status', '未记录')}\n\n"
        "## 任务结果\n\n| 编号 | 负责人 | 任务 | 状态 |\n|---|---|---|---|\n"
        f"{task_rows}\n\n## 最终产物\n\n{final_rows}\n\n"
        f"## 下一步\n\n{next_steps}\n"
    )
    return summary_text, markdown


async def wrapup(state: SprintState) -> dict:
    """Close the run and create a factual retrospective from stored evidence."""
    done = [t for t in state["tasks"] if t["status"] == "通过"]
    finals = [a for a in artifacts_store if a.get("kind") == "final"]
    accepted = bool(finals) and all(not a.get("degraded") and a.get("quality_status") == "passed" for a in finals)
    pending_checks = bool(finals) and not any(a.get("degraded") or a.get("quality_status") == "failed" for a in finals) and not accepted
    set_stage("复盘收尾")

    # 1) 立即收尾：庆祝 → 全员空闲 → 完成信号（前端播提示音、弹气泡）
    await speak(
        SM,
        PO,
        f"本轮迭代结束，{len(done)} 个任务通过；最终交付{'通过质检' if accepted else '仍需修正'}，请复盘问题与下一步",
        f"迭代结束：{len(done)} 个任务通过，交付{'通过质检' if accepted else '待修正'}",
    )
    if accepted:
        emote(SM, "emotes/hooray", "庆祝")
    for no in (PO, DEV_BE, DEV_FE, QA, LEAD, SM):
        set_state(no, "idle", "等待新任务")
    emit({"type": "sprint_done", "tasks": state["tasks"], "accepted": accepted, "pending_checks": pending_checks})

    # 2) Render a deterministic retrospective so failed work is never described
    # as completed by a second unconstrained model call.
    set_state(SM, "working", "整理复盘文档")
    summary_text, summary_markdown = retrospective_content(state, accepted, finals)
    # 复盘结论对团队可见：本轮做得如何、下一步做什么
    rework = state.get("rework_count", 0)
    thought(
        SM,
        f"本轮复盘：{len(done)}/{len(state['tasks'])} 个任务通过，验收{'通过' if accepted else '未通过'}；"
        f"共返工 {rework} 轮。" + ("全部任务一次通过，流程顺畅。" if rework == 0 and accepted else
        "主要改进点：减少返工、提升一次通过率。"),
        "复盘收尾",
    )
    summary_file = await asyncio.to_thread(
        forge.render_docx_from_markdown, summary_markdown, "Sprint复盘总结.docx"
    )
    add_artifact(
        kind="summary",
        title="Sprint 复盘总结",
        source="system",
        author_roster=SM,
        content=(
            f"# Sprint 复盘总结\n\n"
            f"- 需求：{state['requirement']}\n"
            f"- 交付：{len(done)}/{len(state['tasks'])} 个任务\n\n"
            f"{summary_text}\n\n{summary_markdown}"
            + (f"\n\n真实文件：{summary_file}" if summary_file else "")
        ),
        file_name=summary_file,
        degraded=not summary_file,
        degraded_reason="" if summary_file else "Word 复盘文档生成失败，当前仅为文字总结",
        regen={
            "mode": "forge",
            "file_kind": "docx",
            "spec": summary_markdown,
            "filename": "Sprint复盘总结.docx",
            "roster": SM,
        },
    )
    set_state(SM, "idle", "等待新任务")
    return {"tasks": state["tasks"]}


def _zip_code_dir(requirement: str) -> str | None:
    """把本次 Sprint 的代码目录打成 zip，返回相对 deliverables 的文件名"""
    project_root = Path(forge.selected_project())
    files = [p for p in project_root.rglob("*") if p.is_file()]
    if not files:
        return None
    zip_name = forge._unique_name(forge.semantic_filename(requirement, "zip", "代码项目"))
    zip_path = forge.output_path(zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, os.path.relpath(p, project_root))
    import artifact_quality
    artifact_quality.save_report(zip_path, forge.WORKSPACE_DIR, forge.project_report())
    return zip_name


def _sha256_file(file_name: str | None) -> str:
    path = forge.safe_deliverable_path(file_name) if file_name else None
    if not path:
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def candidate(state: SprintState) -> dict:
    """Build one traceable candidate before QA and LEAD see the file."""
    kind = state.get("deliver_kind", "docx") or "docx"
    tasks = state["tasks"]
    set_stage("形成候选成品")
    sprint_id = sprint_meta.get("id", "")
    if sprint_id and storage.get_sprint(sprint_id):
        contribution_issues = contributions.integrity_issues(
            sprint_id, [t["id"] for t in tasks if t.get("assignee_roster") in (DEV_BE, DEV_FE) and t["status"] != "已取消"]
        )
        if contribution_issues:
            add_artifact(kind="candidate", title="贡献缺失或冲突，无法整合", author_roster=SM,
                         source="fallback", quality_status="failed",
                         content=json.dumps(contribution_issues, ensure_ascii=False, indent=2),
                         degraded=True, degraded_reason="真实贡献缺失、已改动或接口冲突")
            return {"tasks": tasks}

    if kind == "code":
        files = [p for p in Path(forge.CODE_DIR).rglob("*") if p.is_file()]
        names = "、".join(p.name for p in files[:8]) or "（无）"
        check_failures = await asyncio.to_thread(forge.verify_generated_code) if files else []
        added: list[str] = []
        if files:
            added = await asyncio.to_thread(forge.finalize_project, state["requirement"])
            files = [p for p in Path(forge.selected_project()).rglob("*") if p.is_file()]
            names = "、".join(p.name for p in files[:8]) or "（无）"
            report = forge.project_report()
            check_failures = [c["detail"] for c in report["checks"] if c["ok"] is False]
        else:
            report = {"status": "failed"}
        # 代码项目在 QA 前整合、打包；后续发布复用同一字节。
        zip_name = await asyncio.to_thread(_zip_code_dir, state["requirement"]) if files else None
        check_note = (
            "\n\n## 可运行性检查\n\n"
            + (
                forge.self_check_note(report) if report.get("checks") else "未完成项目验收"
                if not check_failures
                else "⚠️ 以下文件未通过语法检查：\n"
                + "\n".join(f"- `{item}`" for item in check_failures[:8])
            )
            + ("\n\n已整合项目入口、依赖和测试文件。" if added else "")
        )
        candidate_hash = _sha256_file(zip_name)
        source_refs = [t.get("contribution_ref") for t in tasks if t.get("contribution_ref")]
        candidate_artifact = add_artifact(
            kind="candidate",
            title=f"候选成品 · 代码包（{len(files)} 个文件）",
            source="fallback" if any(a.get("source") == "fallback" for a in artifacts_store) else "model",
            quality_status="unchecked" if zip_name else "failed",
            author_roster=LEAD,
            content=(
                f"# 最终交付物\n\n"
                f"- 需求：{state['requirement']}\n"
                f"- 形式：源代码文件包\n"
                f"- 文件：{names}\n"
                f"- README：{'已生成' if 'README.md' in added else '未生成'}"
                f"{check_note}"
            ),
            file_name=zip_name,
            degraded=not zip_name or bool(check_failures),
            degraded_reason=(
                ""
                if zip_name and not check_failures
                else ("未产出可打包的代码文件" if not zip_name else "部分代码未通过语法检查")
            ),
            regen={"mode": "candidate", "file_kind": "code", "sha256": candidate_hash,
                   "requirement_ids": [c["requirement_id"] for c in state.get("requirement_contract", {}).get("requirements", [])],
                   "contribution_refs": source_refs, "source_structure": added,
                   "project_dir": forge.selected_project()},
        )
        if zip_name and not check_failures:
            from artifacts import update_artifact
            update_artifact(candidate_artifact["id"], quality_status="unchecked")
        _offer_candidate_to_qa(sprint_id, state, candidate_artifact["id"], candidate_hash)
        set_state(SM, "working", "整合候选代码")
        await pause(1.0)
        return {"tasks": tasks, "candidate_id": candidate_artifact["id"], "candidate_sha256": candidate_hash}

    set_state(SM, "working", "整合最终成品文件")
    await pause(1.0)

    # 直接读取完整贡献源文件，避免过程产物预览截断后再次猜写。
    team_parts: list[str] = []
    for t in tasks:
        if t.get("assignee_roster") not in (DEV_BE, DEV_FE):
            continue
        valid, _, manifest = contributions.inspect(sprint_id, t["id"])
        if not valid or manifest.get("kind") != "text":
            continue
        source = Path(manifest["files"][0]["path"]).read_text(encoding="utf-8")
        team_parts.append(f"【{roster_label(t['assignee_roster'])} · {t['id']} · 来源 {manifest['source_refs']}】\n{source}")
    team_digest = "\n\n".join(team_parts)[:40000]
    tasks_digest = "、".join(
        f"{t['id']} {t['title']}（{t['status']}）" for t in tasks
    )

    up = state.get("uploads_text", "")
    if up:
        spec = (
            f"用户上传了附件素材，要求基于附件加工改造产出新成品。\n"
            f"附件内容：\n{up[:12000]}\n\n"
            f"加工需求：{state['requirement']}。\n"
            f"团队各环节的产出（请吸收其中的实质内容）：\n{team_digest}\n\n"
            "重要：成品必须真实体现附件内容与团队产出（改写、提炼、转换格式、补充扩展都算），"
            "把实际内容完整做出来，而不是描述怎么做或给模板。"
            "日期、数字、名称、状态只能取自附件原文或用户需求；正文已给出具体数字时，"
            "不得再写“数据待确认/待补充”。"
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
    generation_meta = {}
    final_file = await asyncio.to_thread(
        forge.forge_file, kind, spec, forge.semantic_filename(state["requirement"], kind, "最终交付物"),
        SM, generation_meta,
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
        check_failed = check.get("status", "failed" if not check.get("ok") else "passed") == "failed"
    candidate_hash = _sha256_file(final_file)
    candidate_artifact = add_artifact(
        kind="candidate",
        title=f"候选成品 · {state['requirement'][:20]}",
        source="fallback" if any(a.get("source") == "fallback" for a in artifacts_store) else "model",
        quality_status="unchecked" if final_file and not check_failed else "failed",
        author_roster=SM,
        content=(
            preview
            or f"需求：{state['requirement']}\n\n成品文件：{final_file or '生成失败，请查看过程产物或重试'}"
        ) + check_note,
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
        regen={"mode": "candidate", "file_kind": kind, "sha256": candidate_hash,
               "generation_model": generation_meta.get("model"),
               "generation_primary_model": generation_meta.get("primary_model"),
               "generation_fallback_roster": generation_meta.get("fallback_roster"),
               "requirement_ids": [c["requirement_id"] for c in state.get("requirement_contract", {}).get("requirements", [])],
               "contribution_refs": [t.get("contribution_ref") for t in tasks if t.get("contribution_ref")],
               "source_structure": [t["id"] for t in tasks if t.get("contribution_ref")]},
    )
    # The file report describes structure only. QA/LEAD have not reviewed it.
    if final_file and not check_failed:
        from artifacts import update_artifact
        update_artifact(candidate_artifact["id"], quality_status="unchecked")
    companions = []
    if kind == "docx" and final_file and "pdf" in state["requirement"].lower() and any(
        word in state["requirement"].lower() for word in ("word", "docx")
    ):
        pdf_file = await asyncio.to_thread(forge.pdf_from_docx, final_file, "同源报告.pdf")
        pdf_hash = _sha256_file(pdf_file)
        pdf_artifact = add_artifact(
            kind="candidate", title="同源 PDF 候选", author_roster=SM,
            content=f"由 Word 候选 {candidate_artifact['id']} 直接导出；Word SHA-256：{candidate_hash}",
            file_name=pdf_file, quality_status="unchecked" if pdf_file else "failed",
            degraded=not bool(pdf_file), degraded_reason="Word 同源 PDF 转换失败" if not pdf_file else "",
            parent_id=candidate_artifact["id"],
            regen={"mode": "candidate", "file_kind": "pdf", "sha256": pdf_hash,
                   "source_candidate_id": candidate_artifact["id"], "source_sha256": candidate_hash,
                   "requirement_ids": [c["requirement_id"] for c in state.get("requirement_contract", {}).get("requirements", [])],
                   "contribution_refs": [t.get("contribution_ref") for t in tasks if t.get("contribution_ref")]},
        )
        if pdf_file:
            from artifacts import update_artifact
            update_artifact(pdf_artifact["id"], quality_status="unchecked")
        companions.append({"id": pdf_artifact["id"], "sha256": pdf_hash})
    _offer_candidate_to_qa(sprint_id, state, candidate_artifact["id"], candidate_hash, companions)
    return {"tasks": tasks, "candidate_id": candidate_artifact["id"],
            "candidate_sha256": candidate_hash, "companion_candidates": companions}


async def publish(state: SprintState) -> dict:
    """Publish the already inspected bytes; never generate a second document."""
    set_stage("发布或保留候选")
    tasks = state["tasks"]
    candidate_id = state.get("candidate_id", "")
    item = storage.get_artifact(candidate_id) if candidate_id else None
    expected = state.get("candidate_sha256", "")
    unresolved = [t for t in tasks if t["status"] not in ("通过", "已取消")]
    qa_report = state.get("qa_report", {})
    lead_decision = state.get("lead_decision", {})
    companion_expected = [entry["sha256"] for entry in state.get("companion_candidates", [])]
    same_version = (qa_report.get("candidate_sha256") == expected
                    and lead_decision.get("candidate_sha256") == expected
                    and qa_report.get("companion_hashes", []) == companion_expected
                    and lead_decision.get("companion_hashes", []) == companion_expected)
    verified = (same_version and state.get("requirement_contract", {}).get("status") == "ready"
                and qa_report.get("status") == "passed" and lead_decision.get("status") == "passed")
    current = _sha256_file(item.get("file_name")) if item and item.get("file_name") else ""
    companion_items = []
    for entry in state.get("companion_candidates", []):
        extra = storage.get_artifact(entry["id"]) or next((a for a in artifacts_store if a.get("id") == entry["id"]), None)
        companion_items.append((extra, entry["sha256"]))
    companions_valid = all(extra and extra.get("file_name") and not extra.get("degraded")
                           and _sha256_file(extra["file_name"]) == digest for extra, digest in companion_items)
    if not item or not current or current != expected or not companions_valid or (unresolved and lead_decision.get("status") == "failed") or item.get("degraded"):
        reasons = []
        if unresolved:
            reasons.append("仍有任务未通过：" + "、".join(t["id"] for t in unresolved))
        if not item or not current:
            reasons.append("候选文件缺失")
        elif current != expected:
            reasons.append("候选文件 SHA-256 已变化，旧检查失效")
        if item and item.get("degraded"):
            reasons.append("候选成品生成或结构检查失败")
        if not companions_valid:
            reasons.append("同源附属候选缺失或 SHA-256 已变化")
        add_artifact(kind="final", title="交付待修正", author_roster=SM,
                     source="fallback", quality_status="failed", degraded=True,
                     degraded_reason="；".join(reasons), content="；".join(reasons))
        return {"tasks": tasks}
    published = add_artifact(
        kind="final", title=f"{'正式交付' if verified else '待验证交付'} · {state['requirement'][:20]}",
        author_roster=SM, source=item.get("source", "model"),
        content=item.get("content", "") + f"\n\n候选 SHA-256：{expected}\nQA：{qa_report.get('status', 'unchecked')}；LEAD：{lead_decision.get('status', 'unchecked')}。",
        file_name=item["file_name"], quality_status="passed" if verified else "unchecked",
        parent_id=candidate_id, version=item.get("version", 1),
        regen={"mode": "published_candidate", "candidate_id": candidate_id, "sha256": expected},
    )
    actual = _sha256_file(published.get("file_name"))
    from artifacts import update_artifact
    if actual != expected:
        update_artifact(published["id"], quality_status="failed", degraded=True,
                        degraded_reason="发布后的文件 SHA-256 与候选不一致")
    else:
        update_artifact(published["id"], quality_status="passed" if verified else "unchecked")
        target_dir = forge.extract_target_dir(state["requirement"])
        if target_dir:
            await asyncio.to_thread(forge.copy_to_target, published["file_name"], target_dir)
        for extra, digest in companion_items:
            published_extra = add_artifact(
                kind="final", title=f"{'正式交付' if verified else '待验证交付'} · PDF",
                author_roster=SM, content=f"与 Word 候选同源；PDF SHA-256：{digest}",
                file_name=extra["file_name"], quality_status="passed" if verified else "unchecked",
                source=extra.get("source", "model"), parent_id=extra["id"], version=extra.get("version", 1),
                regen={"mode": "published_candidate", "candidate_id": extra["id"], "sha256": digest},
            )
            if _sha256_file(published_extra["file_name"]) != digest:
                update_artifact(published_extra["id"], quality_status="failed", degraded=True,
                                degraded_reason="PDF 发布字节与候选不一致")
            else:
                update_artifact(published_extra["id"], quality_status="passed" if verified else "unchecked")
                if target_dir:
                    await asyncio.to_thread(forge.copy_to_target, published_extra["file_name"], target_dir)
    return {"tasks": tasks}
