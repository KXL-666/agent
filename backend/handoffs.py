"""Durable, source checked handoffs. Speech bubbles are never evidence."""

import storage
import contributions
from events import emit


def _reference_state(sprint_id: str, ref: str) -> tuple[bool, bool, str]:
    """Return exists, verified, reason."""
    kind, _, identifier = str(ref).partition(":")
    if not identifier:
        return False, False, "无效引用"
    if kind == "requirement":
        contract = (storage.get_sprint(sprint_id) or {}).get("contract", {})
        clause = next((c for c in contract.get("requirements", [])
                       if c.get("requirement_id") == identifier), None)
        return (bool(clause), bool(clause and clause.get("status") == "ready"),
                "需求条款未核验" if clause else "需求条款不存在")
    if kind == "contract":
        contract = (storage.get_sprint(sprint_id) or {}).get("contract", {})
        exists = bool(contract.get("requirements")) and identifier == sprint_id
        return exists, exists and contract.get("status") == "ready", "需求契约未核验" if exists else "需求契约不存在"
    if kind == "task":
        exists = any(t["id"] == identifier for t in storage.list_tasks(sprint_id))
        return exists, exists, "任务不存在"
    if kind == "artifact":
        artifact = storage.get_artifact(identifier)
        exists = bool(artifact and artifact.get("sprint_id") == sprint_id)
        return exists, exists and not artifact.get("degraded"), "产物不存在或已降级"
    if kind == "contribution":
        try:
            valid, reason, manifest = contributions.inspect(sprint_id, identifier)
        except ValueError:
            return False, False, "贡献引用无效"
        return bool(manifest.get("files")), valid, reason
    return False, False, "未知引用类型"


def offer(sprint_id: str, *, task_id: str, from_roster: int, to_roster: int,
          input_refs: list[str], output_refs: list[str], claims: list[str],
          open_questions: list[str] | None = None,
          acceptance_criteria: list[str] | None = None, version: int = 1) -> dict:
    refs = [*input_refs, *output_refs]
    checks = [_reference_state(sprint_id, ref) for ref in refs]
    if not refs or not output_refs or not claims:
        status, reason = "blocked", "交接缺少引用、输出或具体声明"
    elif any(not exists for exists, _, _ in checks):
        status, reason = "blocked", "；".join(r for exists, _, r in checks if not exists)
    elif any(not verified for _, verified, _ in checks):
        status, reason = "unchecked", "；".join(r for _, verified, r in checks if not verified)
    else:
        status, reason = "ready", ""
    handoff = storage.create_handoff(
        sprint_id, task_id=task_id, from_roster=from_roster, to_roster=to_roster,
        input_refs=input_refs, output_refs=output_refs, claims=claims,
        open_questions=open_questions, acceptance_criteria=acceptance_criteria,
        version=version, status=status, reason=reason,
    )
    emit({"type": "handoff", "handoff": handoff, "sprint_id": sprint_id})
    return handoff


def receive(handoff_id: int, *, accepted: bool, reason: str = "") -> dict:
    # A receiver cannot accept an offer whose source or output is missing.
    current = storage.get_handoff(handoff_id)
    if current is None:
        raise ValueError("交接不存在")
    refs = current["input_refs"] + current["output_refs"]
    checks = [_reference_state(current["sprint_id"], ref) for ref in refs]
    if not accepted:
        status = "rejected"
    elif current["status"] != "ready" or not all(exists and verified for exists, verified, _ in checks):
        status = "blocked" if any(not exists for exists, _, _ in checks) else "unchecked"
        reason = reason or "接收时来源或输出未核验"
    else:
        status = "accepted"
    updated = storage.set_handoff_status(handoff_id, status, reason)
    emit({"type": "handoff", "handoff": updated, "sprint_id": current["sprint_id"]})
    return updated
