"""多 Agent 敏捷开发工作流（LangGraph）：状态结构、流程图与路由判定。

流程：需求拆解 → 任务分派 → 并行开发 → 测试 → 评审 ─┬→ 通过 → 复盘收尾
                                              └→ 打回 → 缺陷修复 → 测试（最多 2 轮）

节点实现见 workflow_nodes，事件推送见 event_broadcaster，需求拆解见 task_planner。
本模块保留 SprintState / 路由判定 / build_sprint_graph，并继续转发节点与事件辅助，
使 `from graph import ...` 的既有调用无需改动。
"""

from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph


class SprintState(TypedDict):
    requirement: str
    requirement_contract: dict
    candidate_id: str
    candidate_sha256: str
    companion_candidates: list
    qa_report: dict
    lead_decision: dict
    repair_blocked: bool
    tasks: list
    rework_count: int
    review_note: str
    deliver_kind: str  # 最终交付物类型：docx/pptx/xlsx/code
    task_kind: str  # 用户在执行计划中明确选择的任务路线
    uploads_text: str  # 用户上传附件提取出的文本素材（可为空）
    uploads_images: list  # 图片附件的 data URL（视觉模型多模态输入，可为空）


# ---------- 事件辅助（实现见 event_broadcaster） ----------

from event_broadcaster import (  # noqa: E402
    BudgetExceeded,
    add_instruction,
    check_budget,
    drain_instructions,
    emote,
    pause,
    reset_speak_llm,
    set_state,
    speak,
    spent_cost,
    thought,
    update_task,
    visit,
)


# ---------- 需求拆解与任务规划（实现见 task_planner） ----------

from task_planner import (  # noqa: E402
    code_task_phases,
    compact_code_tasks,
    compact_short_document_tasks,
    decompose_plan,
    detect_final_kind,
    ensure_file_collaboration_tasks,
    mock_plan,
    pick_assignee,
)


# ---------- 节点实现（实现见 workflow_nodes） ----------

from workflow_nodes import (  # noqa: E402
    MAX_REWORK,
    _offer_candidate_to_qa,
    _sha256_file,
    _workflow_evidence,
    _zip_code_dir,
    assign,
    candidate,
    develop,
    fix,
    kickoff,
    publish,
    retrospective_content,
    review,
    test,
    wrapup,
)


def route_review(state: SprintState) -> str:
    if state["rework_count"] >= 1 and state["rework_count"] <= MAX_REWORK:
        buggy = [t for t in state["tasks"] if t["status"] == "打回"]
        if buggy:
            return "rework"
    return "approved"


def route_fix(state: SprintState) -> str:
    return "blocked" if state.get("repair_blocked") else "retest"


def build_sprint_graph():
    graph = StateGraph(SprintState)
    graph.add_node("kickoff", kickoff)
    graph.add_node("assign", assign)
    graph.add_node("develop", develop)
    graph.add_node("candidate", candidate)
    graph.add_node("test", test)
    graph.add_node("review", review)
    graph.add_node("fix", fix)
    graph.add_node("publish", publish)
    graph.add_node("wrapup", wrapup)

    graph.add_edge(START, "kickoff")
    graph.add_edge("kickoff", "assign")
    graph.add_edge("assign", "develop")
    graph.add_edge("develop", "candidate")
    graph.add_edge("candidate", "test")
    graph.add_edge("test", "review")
    graph.add_conditional_edges(
        "review",
        route_review,
        {"rework": "fix", "approved": "publish"},
    )
    graph.add_conditional_edges("fix", route_fix, {"retest": "test", "blocked": "publish"})
    graph.add_edge("publish", "wrapup")
    graph.add_edge("wrapup", END)
    # 每个节点执行后落一份内存快照，按 thread_id（sprint_id）隔离：
    # 运行中途异常时已完成的节点状态得以保留（便于排查或续跑），
    # 收尾时由 sprint_runner 删除该线程，避免长期运行进程堆积。
    return graph.compile(checkpointer=MemorySaver())
