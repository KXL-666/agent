"""演示模式：零 API 消耗的脚本化 Sprint 回放

不调用任何 LLM：按脚本节拍广播办公室事件，并用确定性渲染直接产出
真实 docx / xlsx 成品文件。让新用户在未配置 API Key 时也能完整体验
「发起 Sprint → 看板流转 → 产物交付」的全过程，产物可下载、可打开。
"""

import asyncio
import os
import time

import artifacts as artifact_store
import events as bus
import forge
import storage
from artifacts import add_artifact
from events import sprint_meta, tasks_store
from roles import DEV_BE, DEV_FE, LEAD, PO, QA, SM, roster_name

DEMO_REQUIREMENT = (
    "【演示模式】为团队生成一份《项目周报》Word 文档与《任务进度表》Excel"
    "（脚本化回放，零 API 消耗）"
)

# 脚本化的任务看板（固定 4 个任务，覆盖分派/开发/测试/交付全环节）
DEMO_TASKS = [
    {"id": "T1", "title": "收集团队本周工作素材", "role_hint": "写作", "assignee_roster": PO},
    {"id": "T2", "title": "撰写周报正文与数据汇总", "role_hint": "写作", "assignee_roster": DEV_BE},
    {"id": "T3", "title": "制作任务进度 Excel 表", "role_hint": "设计", "assignee_roster": DEV_FE},
    {"id": "T4", "title": "校对排版并导出成品", "role_hint": "测试", "assignee_roster": QA},
]

_WEEKLY_REPORT_MD = """# 项目周报（演示样例）

## 一、本周工作概览

本周团队围绕「AI 敏捷开发办公室」演示版本推进，完成了需求拆解、并行开发、测试验证与成品交付四个环节的完整演练，整体进度符合预期。

## 二、各环节进展

- 需求拆解：完成演示需求的任务拆分，输出 PRD 一份，任务粒度适中
- 并行开发：后端、前端两位工程师并行推进，3 个并发上限运行平稳
- 测试验证：测试工程师完成用例设计与执行，关键路径全部覆盖
- 交付环节：最终成品完成自检并归档，可在产物中心直接下载

## 三、数据一览

- 完成任务：4 / 4
- 产物数量：6 份（PRD、开发说明、测试报告、评审记录、最终成品、复盘总结）
- 缺陷轮次：0 轮返工

## 四、风险与下周计划

- 风险：演示数据为脚本化内容，真实生成需为成员配置专属 API Key
- 下周计划：接入真实模型跑一轮完整 Sprint，验证限流退避与预算熔断
"""


async def _beat(seconds: float) -> None:
    """演示节拍：遵守暂停闸门，停/继续与正式 Sprint 行为一致"""
    await bus.wait_if_paused()
    await asyncio.sleep(seconds)


def _emit_state(roster: int, state: str, task: str | None = None) -> None:
    bus.emit({"type": "set_state", "rosterNo": roster, "state": state, "task": task})


def _update_task(task: dict) -> None:
    bus.emit({"type": "task_update", "task": task})


def _render_demo_xlsx() -> str | None:
    """用 openpyxl 确定性生成真实的任务进度表（不走 LLM）"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font

        wb = Workbook()
        ws = wb.active
        ws.title = "任务进度"
        ws.append(["任务编号", "任务名称", "负责人", "状态", "完成度"])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for t in DEMO_TASKS:
            ws.append([t["id"], t["title"], roster_name(t["assignee_roster"]), "通过", "100%"])
        for col, width in zip("ABCDE", (10, 30, 12, 10, 10)):
            ws.column_dimensions[col].width = width
        filename = forge._unique_name("任务进度表（演示）.xlsx")
        wb.save(forge.output_path(filename))
        return filename
    except Exception as error:  # noqa: BLE001
        print(f"[Demo] Excel 生成失败：{error}")
        return None


async def run_demo_sprint() -> None:
    """脚本化跑完一场完整 Sprint：广播事件 + 落库真实产物"""
    sprint_id = storage.create_sprint(DEMO_REQUIREMENT)
    sprint_meta.update(
        {
            "id": sprint_id,
            "requirement": DEMO_REQUIREMENT,
            "started_at": time.time(),
            "finished_at": 0.0,
            "status": "running",
            "stage": "准备开工",
            "stage_started": time.time(),
            "deliver_kind": "docx",
            "usage_baseline": 0.0,
            "usage_baseline_calls": 0,
            "usage_baseline_tokens": 0,
            "fast": False,
            "budget": 0.0,
        }
    )
    tasks_store.clear()
    bus.event_log.clear()
    bus.set_paused(False)
    artifact_store.set_sprint(sprint_id)
    forge.begin_sprint(sprint_id)

    bus.emit({"type": "sprint_start", "requirement": DEMO_REQUIREMENT, "sprint_id": sprint_id})

    # 1) 需求拆解
    bus.set_stage("需求拆解")
    _emit_state(PO, "thinking", "分析需求（演示）")
    await _beat(1.5)
    tasks = [dict(t, status="待分派", summary="") for t in DEMO_TASKS]
    tasks_store.extend(tasks)
    for t in tasks:
        _update_task(t)
        await _beat(0.4)
    prd_md = (
        "# 需求拆解文档（PRD·演示）\n\n## 原始需求\n\n"
        + DEMO_REQUIREMENT
        + "\n\n## 任务拆解\n\n"
        + "\n".join(f"- **{t['id']} {t['title']}**（{t['role_hint']}）" for t in tasks)
    )
    prd_file = await asyncio.to_thread(
        forge.render_docx_from_markdown, prd_md, "PRD需求拆解文档（演示）.docx"
    )
    add_artifact(
        kind="prd",
        title="需求拆解文档（PRD·演示）",
        source="demo",
        author_roster=PO,
        content=prd_md + (f"\n\n真实文件：{prd_file}" if prd_file else ""),
        file_name=prd_file,
        degraded=not prd_file,
        quality_status="failed" if not prd_file else "unchecked",
        degraded_reason="" if prd_file else "Word 文件生成失败，当前仅为 Markdown 摘要",
    )

    # 2) 任务分派
    bus.set_stage("任务分派")
    _emit_state(SM, "working", "分派任务（演示）")
    await _beat(1.2)
    for t in tasks:
        t["status"] = "待开发"
        _update_task(t)
    bus.emit(
        {
            "type": "desk_visit",
            "visitor": SM,
            "host": PO,
            "message": f"{len(tasks)} 个任务已分派到人，进入开发（演示）",
        }
    )
    await _beat(0.8)

    # 3) 并行开发
    bus.set_stage("并行开发")
    for t in tasks:
        if t.get("status") == "已取消":
            continue
        no = t["assignee_roster"]
        _emit_state(no, "working", f"开发中：{t['title']}")
        await _beat(1.5)
        t["status"] = "待测试"
        t["summary"] = f"完成「{t['title']}」（演示内容）"
        _update_task(t)
        add_artifact(
            kind="dev",
            title=f"开发说明 · {t['title']}",
            source="demo",
            author_roster=no,
            content=(
                f"# 开发说明 · {t['title']}\n\n"
                f"- 负责人：{roster_name(no)}\n"
                f"- 所属需求：{DEMO_REQUIREMENT}\n\n"
                f"{t['summary']}"
            ),
            task_id=t["id"],
        )
        _emit_state(no, "idle", "等待新任务")
    bus.emit(
        {"type": "desk_visit", "visitor": DEV_BE, "host": QA, "message": "开发完成，请开始测试（演示）"}
    )
    await _beat(0.8)

    # 4) 测试验证：真实 xlsx 测试报告
    bus.set_stage("测试验证")
    _emit_state(QA, "working", "执行测试（演示）")
    await _beat(1.5)
    xlsx_file = await asyncio.to_thread(_render_demo_xlsx)
    for t in tasks:
        if t.get("status") == "已取消":
            continue
        t["status"] = "通过"
        _update_task(t)
    add_artifact(
        kind="test",
        title="测试报告（第 1 轮·演示）",
        source="demo",
        author_roster=QA,
        content=(
            "# 测试报告（第 1 轮·演示）\n\n- 结论：通过\n- 说明：演示脚本全部用例通过\n"
            + (f"- 真实文件：{xlsx_file}" if xlsx_file else "")
        ),
        file_name=xlsx_file,
        degraded=not xlsx_file,
        quality_status="failed" if not xlsx_file else "unchecked",
        degraded_reason="" if xlsx_file else "Excel 测试报告生成失败，当前仅为文字汇总",
    )
    await _beat(0.8)

    # 5) 技术评审
    bus.set_stage("技术评审")
    _emit_state(LEAD, "working", "内容评审（演示）")
    await _beat(1.2)
    add_artifact(
        kind="review",
        title="内容评审记录（演示）",
        source="demo",
        author_roster=LEAD,
        content="# 内容评审记录（演示）\n\n- 结论：通过\n- 意见：周报结构完整、数据齐备，同意交付",
    )
    await _beat(0.6)

    # 6) 交付成品：真实 docx 周报
    bus.set_stage("交付成品")
    _emit_state(SM, "working", "整合最终成品（演示）")
    await _beat(1.5)
    final_file = await asyncio.to_thread(
        forge.render_docx_from_markdown, _WEEKLY_REPORT_MD, "项目周报（演示）.docx"
    )
    add_artifact(
        kind="final",
        title="最终交付物 · 项目周报（演示）",
        source="demo",
        author_roster=SM,
        content=_WEEKLY_REPORT_MD + (f"\n\n真实文件：{final_file}" if final_file else ""),
        file_name=final_file,
        degraded=not final_file,
        quality_status="failed" if not final_file else "unchecked",
        degraded_reason="" if final_file else "Word 成品生成失败，当前仅为内容摘要",
    )
    bus.emit({"type": "emote", "rosterNo": SM, "animation": "emotes/hooray", "label": "成品交付"})
    await _beat(0.8)

    # 7) 复盘收尾
    bus.set_stage("复盘收尾")
    add_artifact(
        kind="summary",
        title="Sprint 复盘总结（演示）",
        source="demo",
        author_roster=SM,
        content=(
            "# Sprint 复盘总结（演示）\n\n"
            f"- 需求：{DEMO_REQUIREMENT}\n- 交付：4/4 个任务\n\n"
            "演示 Sprint 走完全部环节，产物均为真实可下载文件。"
        ),
    )
    for no in (PO, DEV_BE, DEV_FE, QA, LEAD, SM):
        _emit_state(no, "idle", "等待新任务")
    bus.emit({"type": "sprint_done", "tasks": tasks})

    sprint_meta["status"] = "completed"
    sprint_meta["stage"] = "已完成"
    sprint_meta["finished_at"] = time.time()
    storage.update_sprint(
        sprint_id, status="completed", stage="已完成",
        deliver_kind="docx", finished_at=sprint_meta["finished_at"],
    )
    bus.emit(
        {
            "type": "stage",
            "stage": "已完成",
            "started_at": sprint_meta["started_at"],
            "stage_started": time.time(),
        }
    )
    bus.emit({"type": "sprint_usage", "cost": 0, "tokens": 0})
