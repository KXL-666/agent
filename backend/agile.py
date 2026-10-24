"""可持久化的迭代计划字段；兼容旧模型只返回任务标题。"""
def plan_fields(item: dict, requirement: str, contract: dict | None = None) -> dict:
    points = item.get("story_points", 3)
    if type(points) is not int or points not in (1, 2, 3, 5, 8, 13):
        points = 3
    priority = item.get("priority", "P1")
    if priority not in ("P0", "P1", "P2"):
        priority = "P1"
    criteria = item.get("acceptance_criteria", [])
    if not isinstance(criteria, list):
        criteria = []
    criteria = [str(c).strip()[:300] for c in criteria[:6] if str(c).strip()]
    title = str(item.get("title", "任务"))[:80]
    linked = []
    if contract:
        from requirement_contract import task_requirement_ids
        linked = task_requirement_ids(item, contract)
    if not criteria and contract:
        by_id = {c["requirement_id"]: c for c in contract.get("requirements", [])}
        criteria = [by_id[key]["acceptance_method"] or f"核对原始要求：{by_id[key]['text']}"
                    for key in linked if key in by_id][:6]
    return {
        "user_story": str(item.get("user_story") or f"作为需求提出者，我希望完成{title}，以推进{requirement[:80]}")[:400],
        "priority": priority,
        "story_points": points,
        "acceptance_criteria": criteria or [f"核对原始要求：{requirement[:250]}"],
        "requirement_ids": linked,
    }


PLAN_FIELDS = ("user_story", "priority", "story_points", "acceptance_criteria", "requirement_ids", "role_scope", "assignment_handoff_id", "contribution_ref", "integration_conflicts")
