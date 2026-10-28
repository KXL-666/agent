"""QA verification of the actual candidate bytes, one requirement at a time."""

import json
import hashlib
import re
from pathlib import Path

import artifact_quality
import forge
import llm
import project_agent
from roles import QA


def source_material(contract: dict) -> str:
    """验收依据 = 用户需求 + 附件原文，与生成端使用同一份来源，避免把附件事实判成无依据。"""
    raw = str(contract.get("raw_requirement") or "").strip()
    attachment = str(contract.get("attachment_context") or "").strip()
    prior = list(dict.fromkeys(list(contract.get('prior_user_quotes') or [])
        + [str(item.get('quote') or '') for item in contract.get('format_scoped_sources', [])] + [
        str(c.get('source', {}).get('quote') or c.get('text') or '') for c in contract.get('superseded', [])
        if c.get('source', {}).get('type') == 'user']))
    if prior:
        attachment += '\n历史用户原话（仅作事实出处；旧要求已失效，与当前要求冲突之处以当前要求为准）：\n' + '\n'.join(prior)
    if not attachment or attachment in raw:
        return raw
    return f"{raw}\n\n附件原文：\n{attachment}"


def _items(clauses: list[dict], model_items: list[dict] | None,
           workflow_evidence: dict | None = None) -> list[dict]:
    by_id = {str(item.get("requirement_id")): item for item in model_items or [] if isinstance(item, dict)}
    result = []
    for clause in clauses:
        key = clause["requirement_id"]
        item = by_id.get(key, {})
        status = item.get("status") if item.get("status") in ("passed", "failed", "unchecked") else "unchecked"
        evidence = str(item.get("evidence") or "")[:1000]
        location = str(item.get("location") or "")[:300]
        if status == "passed" and (not evidence or not location):
            status = "unchecked"
        result.append({"requirement_id": key, "status": status,
                       "evidence": evidence, "location": location,
                       "problem": str(item.get("problem") or "")[:600],
                       "fix": str(item.get("fix") or "")[:600],
                       "owner_roster": item.get("owner_roster") if item.get("owner_roster") in (2, 3, 4, 6) else None})
    workflow = workflow_evidence or {}
    contributions = workflow.get("contributions", [])
    valid_rosters = {entry.get("roster") for entry in contributions if entry.get("valid")}
    handoffs = workflow.get("handoffs", [])
    collaboration_ready = (
        workflow.get("contract_status") == "ready"
        and {2, 3}.issubset(valid_rosters)
        and workflow.get("candidate_sha256")
        and any(record.get("from_roster") == 6 and record.get("to_roster") == 4
                and record.get("status") in ("accepted", "ready") for record in handoffs)
    )
    for item, clause in zip(result, clauses):
        text = str(clause.get("text") or "")
        # 「团队协作完成/产出」是过程条款；标题或正文主题含「团队协作」字样不算协作条款。
        if not re.search(r"团队(?:协作|合作)(?:完成|开发|产出|撰写)|由.{0,6}团队.{0,4}(?:完成|协作)|六智能体|六人协作", text):
            continue
        item["status"] = "passed" if collaboration_ready else "unchecked"
        item["evidence"] = ("PO 契约、BE/FE 已校验贡献、SM→QA 候选交接与当前候选哈希均可追溯。"
                            if collaboration_ready else "协作证据不完整：需要 PO 契约、BE/FE 实际贡献及 SM→QA 同版本交接。")
        item["location"] = "协作溯源记录"
        item["problem"] = "" if collaboration_ready else "缺少可核验的角色贡献或交接记录。"
        # The workflow trace is the source of truth for collaboration. Discard
        # model-generated remediation text that contradicts a mechanically
        # verified pass (for example, asking for evidence already present).
        item["fix"] = "" if collaboration_ready else item["fix"]
    return result


def _apply_mechanical_page_checks(items: list[dict], clauses: list[dict], metrics: dict) -> None:
    """Use rendered page counts for explicit page limits instead of model claims."""
    pages = metrics.get("pages")
    if not isinstance(pages, int):
        return
    for item, clause in zip(items, clauses):
        text = str(clause.get("text") or "")
        limit = None
        exact = False
        if "一页" in text and any(word in text for word in ("控制在", "不超过", "最多", "以内")):
            limit = 1
        else:
            import re
            match = re.search(r"(?:恰好|正好|共)\s*(\d+)\s*页", text)
            if match:
                limit, exact = int(match.group(1)), True
        if limit is None:
            continue
        passed = pages == limit if exact else pages <= limit
        item["status"] = "passed" if passed else "failed"
        item["evidence"] = f"文件渲染页数为 {pages} 页；条款要求{'恰好' if exact else '不超过'} {limit} 页。"
        item["location"] = "整份文件 / 渲染页数"
        item["problem"] = "" if passed else f"实际 {pages} 页，超过条款限制 {limit} 页。"


def _apply_mechanical_toc_checks(items: list[dict], clauses: list[dict], metrics: dict) -> None:
    """「目录」条款用本地文件结构检查判定，不依赖模型从截图推断。"""
    toc_present = metrics.get("toc_present")
    if not isinstance(toc_present, bool):
        return
    for item, clause in zip(items, clauses):
        text = str(clause.get("text") or "")
        if "目录" not in text:
            continue
        negated = not artifact_quality._affirmed(text, r"目录")
        passed = (not toc_present) if negated else toc_present
        # A compound clause can also forbid a cover or illustrations. A TOC
        # check may fail that clause but cannot prove its other requirements.
        if passed and re.search(r"封面|插图|图表|正文|标题|字体|页码", text):
            continue
        item["status"] = "passed" if passed else "failed"
        item["evidence"] = ("文件结构检查：未发现目录字段或目录样式段落。" if not toc_present
                            else "文件结构检查：检测到目录字段或目录样式段落。")
        item["location"] = "整份文件 / 文档结构"
        if passed:
            item["problem"] = ""
        elif negated:
            item["problem"] = "需求明确要求无目录，但文件结构中检测到目录字段或目录样式段落。"
        else:
            item["problem"] = "需求要求包含目录，但文件结构中未检测到目录字段或目录样式段落。"


def _apply_mechanical_length_checks(items: list[dict], clauses: list[dict], metrics: dict,
                                    requested: dict | None) -> None:
    """字数条款用本地统一口径统计结果判定，不采信模型自报的字数。

    模型无法稳定逐字计数，实测常出现 155/227/168 这类偏差；自检的 counted_characters
    才是全链路统一口径，因此字数类条款一律以它为准。
    """
    requested = requested or {}
    count, unit, mode = requested.get("count"), requested.get("unit"), requested.get("count_mode")
    actual = metrics.get("counted_characters")
    if not count or unit != "字" or not isinstance(actual, int):
        return
    # 「恰好/正好 N 字」精确命中；「约 N 字」允许 ±20%。判定只认自检 counted_characters。
    if mode == "exact":
        passed = actual == count
        evidence = (f"按统一口径（汉字+英文/数字词，剔除标题段）实测 {actual} 字；"
                    f"要求恰好 {count} 字，须精确命中。")
    else:
        low, high = round(count * .8), round(count * 1.2)
        passed = low <= actual <= high
        evidence = (f"按统一口径（汉字+英文/数字词，剔除标题段）实测 {actual} 字；"
                    f"要求约 {count} 字，容差 ±20%（{low}~{high} 字）。")
    for item, clause in zip(items, clauses):
        text = str(clause.get("text") or "")
        if not re.search(r"字数|篇幅|\d+\s*字", text):
            continue
        item["status"] = "passed" if passed else "failed"
        item["evidence"] = evidence
        item["location"] = "正文全文 / 自检统计"
        if passed:
            item["problem"] = ""
        elif mode == "exact":
            item["problem"] = f"字数不达标：实测 {actual} 字，要求恰好 {count} 字（相差 {actual - count} 字）。"
        else:
            item["problem"] = f"字数不达标：实测 {actual} 字，超出约 {count} 字的允许区间。"


def verify(candidate: dict, contract: dict, candidate_sha256: str,
           workflow_evidence: dict | None = None) -> dict:
    filename = candidate.get("file_name")
    path = forge.safe_deliverable_path(filename) if filename else None
    kind = (candidate.get("regen") or {}).get("file_kind")
    clauses = contract.get("requirements", [])
    base = {"schema": 1, "candidate_id": candidate.get("id"), "candidate_sha256": candidate_sha256,
            "kind": kind, "source_refs": (candidate.get("regen") or {}).get("contribution_refs", []),
            "workflow_evidence": workflow_evidence or {},
            "items": _items(clauses, None), "checks": [], "status": "unchecked", "reason": "候选未完成核验"}
    if not path or not candidate_sha256 or not clauses:
        base["reason"] = "候选文件、哈希或 PO 条款缺失"
        return base
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != candidate_sha256:
        base["status"] = "failed"
        base["reason"] = "候选文件字节已变化，旧 SHA-256 失效"
        return base
    base["reason"] = ""
    if kind == "code":
        project_dir = (candidate.get("regen") or {}).get("project_dir")
        if project_dir and Path(project_dir).is_dir():
            previous = candidate.get('regen') or {}
            report = project_agent.evaluate(Path(project_dir), previous.get("planned_files", []),
                                            previous.get("runtime", "python"))
        else:
            report = forge.project_report()
        base["checks"] = report.get("checks", [])
        runtime_check = next((c for c in base["checks"] if c.get("label") == "隔离安装与运行测试"), None)
        source = project_agent.snapshot(Path(project_dir or forge.selected_project()), limit=24000)
        meta = {}
        response = llm.chat_json(
            "你是独立 QA。阅读真实集成项目源码和实际检查结果，逐条核对 PO 条款。"
            '输出 JSON {"verification":[{"requirement_id":"R1","status":"passed|failed|unchecked",'
            '"evidence":"文件/测试证据","location":"文件:行","problem":"问题","fix":"修复建议","owner_roster":2}]}。'
            "未执行的功能测试必须是 unchecked，不能把语法正确当功能通过。",
            json.dumps({"requirement": source_material(contract), "clauses": clauses,
                        "actual_source": source, "checks": base["checks"],
                        "workflow_evidence": workflow_evidence or {}}, ensure_ascii=False),
            fallback={}, roster=QA, meta=meta, max_tokens=3000, timeout=150,
            fallback_roster=2,
        )
        model_items = response.get("verification") if isinstance(response, dict) and not meta.get("fallback_reason") else None
        base["items"] = _items(clauses, model_items, workflow_evidence)
        if runtime_check is None or runtime_check.get("ok") is not True:
            for item in base["items"]:
                if item["status"] == "passed":
                    item["status"] = "unchecked"
                    item["problem"] = "未完成隔离环境功能测试"
    else:
        report = artifact_quality.inspect(path, kind, source_material(contract), forge.WORKSPACE_DIR)
        has_failed = any(c.get("ok") is False for c in report.get("checks", []))
        # 条款级核验需要模型给出证据与位置；若本地全过就跳过评审，QA 永远无法通过，
        # 交付会永久卡在 unchecked。因此只要存在验收条款就执行模型评审。
        reviewed_by_model = has_failed or bool(clauses)
        if reviewed_by_model:
            reviewed = artifact_quality.model_review(report, path, forge.WORKSPACE_DIR, QA, clauses=clauses)
        else:
            reviewed = report
        base["checks"] = reviewed.get("checks", [])
        model_items = reviewed.get("review", {}).get("verification") if reviewed_by_model else None
        base["items"] = _items(clauses, model_items, workflow_evidence)
        _apply_mechanical_page_checks(base["items"], clauses, reviewed.get("metrics", {}))
        _apply_mechanical_toc_checks(base["items"], clauses, reviewed.get("metrics", {}))
        _apply_mechanical_length_checks(base["items"], clauses, reviewed.get("metrics", {}),
                                        reviewed.get("contract"))
        base["metrics"] = reviewed.get("metrics", {})
        base["pages"] = reviewed.get("pages", [])
        if reviewed_by_model:
            base["review"] = reviewed.get("review", {})
            base["visual"] = reviewed.get("review", {}).get("visual", False)
        else:
            # 无条款时不做模型评审，页面已由 office_runtime 真实渲染，以本地渲染成功作为视觉证据
            render_ok = any(c.get("label") == "真实分页与页面边界" and c.get("ok") is True
                            for c in base["checks"])
            base["visual"] = bool(render_ok and base["pages"])
        if not base["visual"] and reviewed.get("pages"):
            base["reason"] = "页面视觉输入未成功送达 QA，视觉检查未核验"
    if any(check.get("ok") is False for check in base["checks"]) or any(item["status"] == "failed" for item in base["items"]):
        base["status"] = "failed"
        base["reason"] = "存在实际检查失败或条款不满足"
    elif any(check.get("ok") is None for check in base["checks"]) or any(item["status"] != "passed" for item in base["items"]):
        base["status"] = "unchecked"
        base["reason"] = base.get("reason") or "存在尚未核验的检查或条款"
    elif kind != "code" and base.get("pages") and not base.get("visual"):
        base["status"] = "unchecked"
    elif kind != "code" and base.get("review", {}).get("status") == "failed":
        base["status"] = "failed"
        issues = base["review"].get("issues") or []
        base["reason"] = "；".join(str(i.get("problem") or i) for i in issues if isinstance(i, dict))[:400] \
            or "模型复核发现实质缺陷"
    elif contract.get("status") != "ready":
        base["status"] = "unchecked"
        base["reason"] = "PO 需求契约仍有未核验条款或来源"
    else:
        base["status"] = "passed"
        base["reason"] = ""
    directory = artifact_quality.report_directory(path, forge.WORKSPACE_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "qa-report.json").write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    return base
