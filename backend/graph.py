"""多 Agent 敏捷开发工作流（LangGraph）：状态结构、流程图与路由判定。

流程：需求拆解 → 任务分派 → 并行开发 → 测试 → 评审 ─┬→ 通过 → 复盘收尾
                                              └→ 打回 → 缺陷修复 → 测试（不硬限轮数，花费上限兜底）

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
    resume_files: list  # immutable artifact references captured at node boundaries
    resume_sources: list
    resume_projects: list
    pending_instructions: list
    instruction_cutoff: float


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


def build_sprint_graph(checkpointer=None, *, durable: bool = False):
    graph = StateGraph(SprintState)
    for name, function in (("kickoff", kickoff), ("assign", assign), ("develop", develop),
                           ("candidate", candidate), ("test", test), ("review", review),
                           ("fix", fix), ("publish", publish), ("wrapup", wrapup)):
        if durable:
            from sprint_recovery import durable_node
            function = durable_node(function)
        graph.add_node(name, function)

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
    return graph.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())
