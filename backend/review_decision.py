"""Independent LEAD decision tied to the exact QA candidate hash."""

import base64
import hashlib
import json
from pathlib import Path

import artifact_quality
import forge
import llm
import project_agent
from roles import DEV_BE, LEAD


def decide(candidate: dict, contract: dict, qa_report: dict, handoff_records: list[dict], expected_sha256: str,
           companions: list[dict] | None = None, workflow_evidence: dict | None = None) -> dict:
    path = forge.safe_deliverable_path(candidate.get("file_name")) if candidate.get("file_name") else None
    kind = (candidate.get("regen") or {}).get("file_kind")
    result = {"schema": 2, "candidate_id": candidate.get("id"), "candidate_sha256": expected_sha256,
              "qa_candidate_sha256": qa_report.get("candidate_sha256"), "status": "unchecked",
              "issues": [], "advisories": [], "reason": "LEAD 尚未完成独立复核", "reviewer_roster": LEAD}
    if not path or not expected_sha256:
        result["reason"] = "候选文件或 SHA-256 缺失"
        return result
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected_sha256:
        result.update(status="failed", reason="候选文件已变化，旧 QA/LEAD 结论失效")
        return result
    if qa_report.get("candidate_sha256") != expected_sha256:
        result.update(status="failed", reason="QA 审阅的不是当前候选版本")
        return result
    companion_hashes = []
    companion_text = []
    for extra in companions or []:
        extra_path = forge.safe_deliverable_path(extra.get("file_name")) if extra.get("file_name") else None
        expected_extra = (extra.get("regen") or {}).get("sha256", "")
        if not extra_path or not expected_extra or hashlib.sha256(Path(extra_path).read_bytes()).hexdigest() != expected_extra:
            result.update(status="failed", reason="附属候选文件缺失或已变化")
            return result
        companion_hashes.append(expected_extra)
        companion_report = artifact_quality.load_report(extra_path, forge.WORKSPACE_DIR) or {}
        companion_text.append(f"[{(extra.get('regen') or {}).get('file_kind', 'file')} SHA-256 {expected_extra}]\n"
                              + companion_report.get("text", ""))
    result["companion_hashes"] = companion_hashes
    if qa_report.get("companion_hashes", []) != companion_hashes:
        result.update(status="failed", reason="QA 审阅的附属候选版本与当前不一致")
        return result
    images = []
    if kind == "code":
        project_dir = (candidate.get("regen") or {}).get("project_dir") or forge.selected_project()
        actual = project_agent.snapshot(Path(project_dir), limit=28000)
        actual_checks = qa_report.get("checks", [])
    else:
        file_report = artifact_quality.load_report(path, forge.WORKSPACE_DIR) or {}
        actual = file_report.get("text", "")
        actual_checks = file_report.get("checks", [])
        directory = artifact_quality.report_directory(path, forge.WORKSPACE_DIR)
        for name in file_report.get("pages", [])[:6]:
            image = directory / name
            if image.is_file() and image.stat().st_size < 2 * 1024 * 1024:
                images.append("data:image/png;base64," + base64.b64encode(image.read_bytes()).decode())
    # 非 code 类型快速通道：QA 全通过且本地检查非空且无失败时，跳过 LEAD 的 LLM 复核
    qa_passed = qa_report.get("status") == "passed"
    local_ok = bool(actual_checks) and not any(c.get("ok") is False for c in actual_checks)
    if kind != "code" and qa_passed and local_ok:
        result.update(
            status="passed",
            reason="QA 逐条验证通过且本地自检无失败，LEAD 快速放行",
            note="快速放行：QA 全通过 + 本地自检通过",
            model="fast-path",
        )
        directory = artifact_quality.report_directory(path, forge.WORKSPACE_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "lead-decision.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    meta = {}
    candidate_ref = f"artifact:{candidate.get('id')}"
    current_handoffs = [record for record in handoff_records
                        if candidate_ref in (record.get("input_refs") or [])
                        and record.get("to_roster") == LEAD]
    process_evidence = dict(workflow_evidence or {})
    # Member drafts establish participation, but assembled candidate bytes are authoritative.
    process_evidence["contributions"] = [
        {key: value for key, value in entry.items() if key != "content"}
        for entry in process_evidence.get("contributions", []) if isinstance(entry, dict)
    ]
    process_evidence["handoffs"] = current_handoffs[-30:]
    response = llm.chat_json(
        "你是独立技术主管 LEAD。阅读原始需求、PO 条款、候选真实内容、QA 逐条证据和角色交接。"
        "无论 QA 通过还是失败都须独立审阅；不得把 QA 结论照抄为自己的结论。"
        '输出 JSON {"verdict":"passed|failed|unchecked","note":"判断依据",'
        '"issues":[{"requirement_id":"R1","location":"页/节/文件:行","severity":"high|medium|low",'
        '"problem":"实际问题","fix":"具体修复","owner_roster":2}]}。'
        "只依据当前候选版本的内容和 QA→LEAD 交接判断。成员原始草稿只证明参与，不代表最终候选内容。"
        "QA 的逐条验证报告及同版交接证明 QA 履职；QA 不需要 BE/FE 类型的源码贡献清单。"
        "旧候选因返工降级，其交接状态不能作为当前候选的失败原因。"
        "旧版本已修复的问题不要列入 issues。"
        "issues 只列当前候选违反需求或缺失必需证据的阻断问题；非阻断建议写入 note。"
        "缺少实际内容、运行或视觉证据时写 unchecked；证据明确显示错误写 failed。"
        "字数判定只认 qa_report.metrics.counted_characters（唯一权威，统一口径=汉字+英文/数字词、剔除标题段），"
        "禁止自行重新估算或改写该数值。『恰好/正好 N 字』须 actual==N 才算通过；『约 N 字』允许 ±20%。",
        json.dumps({"raw_requirement": contract.get("raw_requirement"),
                    "clauses": contract.get("requirements", []), "candidate_sha256": expected_sha256,
                    "actual_content": actual, "companion_content": companion_text, "actual_checks": actual_checks,
                    "qa_report": qa_report, "handoffs": current_handoffs[-30:],
                    "workflow_evidence": process_evidence}, ensure_ascii=False),
        fallback={}, roster=LEAD, meta=meta, max_tokens=3500, timeout=150, images=images or None,
        vision_fallback_roster=DEV_BE,
    )
    result["model"] = meta.get("model", "")
    if meta.get("vision_fallback_roster") is not None:
        result["vision_fallback_roster"] = meta["vision_fallback_roster"]
    if meta.get("fallback_reason") or not isinstance(response, dict) or response.get("verdict") not in ("passed", "failed", "unchecked"):
        result["reason"] = "LEAD 模型不可用或返回无效结论"
    else:
        issues = response.get("issues") if isinstance(response.get("issues"), list) else []
        valid_ids = {c.get("requirement_id") for c in contract.get("requirements", [])}
        valid_issues = [item for item in issues if isinstance(item, dict)
                        and (item.get("requirement_id") in valid_ids or not item.get("requirement_id"))][:20]
        result["issues"] = [item for item in valid_issues if item.get("severity") != "low"]
        result["advisories"] = [item for item in valid_issues if item.get("severity") == "low"]
        result["note"] = str(response.get("note") or "")[:1800]
        result["status"] = response["verdict"]
        result["reason"] = result["note"]
        if result["status"] == "passed" and not result["note"].strip():
            result.update(status="unchecked", reason="LEAD 未提供独立复核依据")
        if result["status"] == "passed" and result["issues"]:
            result.update(status="failed", reason="LEAD 同时声明通过和问题，按失败处理")
        if kind != "code" and (not images or meta.get("images_dropped")) and result["status"] == "passed":
            result.update(status="unchecked", reason="LEAD 未收到候选页面视觉证据")
    if qa_report.get("status") == "failed":
        result.update(status="failed", reason="QA 已给出失败证据；" + result["reason"])
    elif qa_report.get("status") != "passed" and result["status"] == "passed":
        result.update(status="unchecked", reason="QA 尚未完成同版本核验")
    if contract.get("status") != "ready" and result["status"] == "passed":
        result.update(status="unchecked", reason="PO 需求契约尚未完整核验")
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected_sha256:
        result.update(status="failed", reason="LEAD 审阅期间候选文件发生变化")
    directory = artifact_quality.report_directory(path, forge.WORKSPACE_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "lead-decision.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
