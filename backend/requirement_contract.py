"""Traceable requirement contract shared by collaborative and direct runs.

User wording is the authority. Model annotations are accepted only when they
point at an exact source quote; missing annotations remain unchecked.
"""

import re


_SPLIT = re.compile(r"[。；;，,\n]+")
_SOURCE = re.compile(r"📎《([^》]+)》([^：\n]*)：")
_KINDS = {"docx", "pdf", "pptx", "xlsx", "code", "html"}
_AUTHORIZED_ASSUMPTION = re.compile(r"(?:允许|可以|可)(?:自行|由你)?(?:合理)?(?:假设|虚构|编造)")


def _inferred_acceptance_method(fragment: str, kind: str) -> str:
    """Infer executable checks only where the user's wording makes them concrete."""
    text = fragment.strip()
    if kind == "pptx":
        pages = re.search(r"(?:生成|制作|交付)?\s*(\d+)\s*页(?:含封面|包含封面|带封面)?", text)
        if pages:
            cover = "，并确认首页为封面" if any(word in text for word in ("含封面", "包含封面", "带封面")) else ""
            return f"检查最终 PPTX 的实际幻灯片数为 {pages.group(1)} 页{cover}，并核对文件语言和主题要求。"
        if "原生" in text and "图表" in text:
            return "检查 PPTX 中有原生图表对象及内嵌图表数据工作簿，确认图表可编辑。"
        if "图表" in text and any(word in text for word in ("附件数值", "附件数据", "来源数值")):
            return "逐项读取图表系列、分类和数值，与附件原始记录核对；不一致标记 failed。"
        if any(word in text for word in ("不能自行猜测", "不可猜测", "不得猜测")):
            return "核对原因分析是否有附件或用户原话依据；无依据的原因必须明确标为待确认。"
        if "待确认" in text:
            return "检查成品对应缺失信息明确写为待确认，且未写成已确定事实。"
        if "三周" in text or "3 周" in text:
            return "核对成品的三周进展内容与附件记录逐项一致，并确认面向项目负责人的汇报目的。"
    if re.search(r"(?:控制在|不超过|最多)\s*(?:一|1)\s*页|(?:一|1)\s*页以内", text):
        return "检查最终文件的真实页数，要求不超过 1 页；分页能力不可用时标记 unchecked。"
    page_count = re.search(r"(?:恰好|正好|共)\s*(\d+)\s*页", text)
    if page_count:
        return f"检查最终文件真实页数，要求恰好 {page_count.group(1)} 页。"
    if "不要封面" in text or "不带封面" in text or "无封面" in text:
        return "检查真实分页与首页内容，确认没有独立封面页；无法取得分页证据时标记 unchecked。"
    if any(term in text for term in ("团队协作", "六智能体", "六人协作")):
        return "核对本次 Sprint 的有效角色贡献、候选交接记录和最终候选哈希；缺少任一证据时标记 unchecked。"
    if kind == "code":
        if "README" in text and any(term in text for term in ("测试", "断言")):
            return "检查 README 存在且含运行/测试命令；运行自动化测试并确认有真实断言且全部通过。"
        if any(term in text for term in ("非零", "退出码")):
            return "运行一个明确非法输入，断言进程退出码非零并输出可读错误。"
        if any(term in text for term in ("无效格式", "无效输入", "不支持", "错误")):
            return "分别运行格式错误输入与不支持单位，断言 stderr 有清晰错误且没有成功结果输出。"
        example = re.search(r"输入[“\"]([^”\"]+)[”\"]\s*输出[“\"]([^”\"]+)[”\"]", text)
        if example:
            return f"实际运行命令行输入 {example.group(1)}，断言标准输出为 {example.group(2)} 且退出码为 0。"
        if any(term in text for term in ("可运行", "命令行", "自动化测试")):
            return "检查项目入口和 README；在隔离环境执行用户给定的成功/错误用例及自动化测试。"
    return ""


def _fragments(text: str) -> list[str]:
    return [part.strip() for part in _SPLIT.split(text) if part.strip()]


def _sources(context: str) -> list[dict]:
    seen = set()
    result = []
    for name, location in _SOURCE.findall(context or ""):
        key = (name.strip(), location.strip())
        if key not in seen:
            seen.add(key)
            result.append({"name": key[0], "location": key[1] or None})
    return result


def _attachment_has_quote(context: str, name: str, location: str, quote: str) -> bool:
    markers = list(_SOURCE.finditer(context))
    for index, marker in enumerate(markers):
        if marker.group(1).strip() != name or marker.group(2).strip() != location:
            continue
        end = markers[index + 1].start() if index + 1 < len(markers) else len(context)
        if quote in context[marker.end():end]:
            return True
    return False


def _attachment_fragments(context: str):
    """Yield exact, located source sentences for deterministic coverage fallback."""
    markers = list(_SOURCE.finditer(context or ""))
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(context)
        body = context[marker.end():end].strip()
        for part in re.split(r"(?<=[。；;])\s*|\n+", body):
            quote = part.strip()
            if quote:
                yield marker.group(1).strip(), marker.group(2).strip(), quote


def build_contract(
    requirement: str,
    attachment_context: str = "",
    model_contract: dict | None = None,
    *,
    deliver_kind: str = "",
    fallback_reason: str = "",
) -> dict:
    """Normalize PO output without allowing it to replace original instructions."""
    raw = (requirement or "").strip()
    model = model_contract if isinstance(model_contract, dict) else {}
    source_items = model.get("requirements")
    source_items = source_items if isinstance(source_items, list) else []
    attached = _sources(attachment_context)
    clauses = []
    unmatched = []
    for index, fragment in enumerate(_fragments(raw), 1):
        annotation = next((item for item in source_items
                           if isinstance(item, dict)
                           and item.get("source") == "user"
                           and str(item.get("source_quote", "")).strip() == fragment), None)
        inferred_method = _inferred_acceptance_method(fragment, deliver_kind)
        if annotation is None and not inferred_method:
            unmatched.append(f"R{index}")
        priority = annotation.get("priority") if annotation else None
        method = str(annotation.get("acceptance_method", "")).strip() if annotation else ""
        method = method or inferred_method
        clauses.append({
            "requirement_id": f"R{index}",
            "text": fragment,
            "priority": priority if priority in ("P0", "P1", "P2") else "P1",
            "acceptance_method": method[:300] if method else None,
            "source": {"type": "user", "quote": fragment},
            "status": "ready" if method else "unchecked",
        })
    # Attachment requirements need a precise file/location reference and an
    # exact quote from the extracted context. Unlocated fallback text is kept
    # as source material but cannot be marked verified.
    for item in source_items:
        if not isinstance(item, dict) or item.get("source") != "attachment":
            continue
        quote = str(item.get("source_quote", "")).strip()
        name = str(item.get("source_name", "")).strip()
        location = str(item.get("source_location", "")).strip()
        method = str(item.get("acceptance_method", "")).strip()
        if not quote or not _attachment_has_quote(attachment_context, name, location, quote):
            continue
        clauses.append({
            "requirement_id": f"R{len(clauses) + 1}",
            "text": str(item.get("text") or quote).strip()[:500],
            "priority": item.get("priority") if item.get("priority") in ("P0", "P1", "P2") else "P1",
            "acceptance_method": method[:300] if method else None,
            "source": {"type": "attachment", "name": name, "location": location, "quote": quote},
            "status": "ready" if method else "unchecked",
        })
    uncovered_attachment = False
    for name, location, quote in _attachment_fragments(attachment_context):
        if any(c["source"]["type"] == "attachment"
               and c["source"].get("name") == name
               and c["source"].get("location") == location
               and quote in c["source"]["quote"]
               for c in clauses):
            continue
        if len(quote) > 500 or len(clauses) >= 60:
            uncovered_attachment = True
            continue
        method = ("逐项核对最终产物引用的该附件片段中的数值、名称、状态和统计口径；"
                  "图表数据须与原文一致，未提供的信息不得补造。")
        clauses.append({"requirement_id": f"R{len(clauses) + 1}", "text": quote,
                        "priority": "P1", "acceptance_method": method,
                        "source": {"type": "attachment", "name": name, "location": location,
                                   "quote": quote}, "status": "ready"})
    missing = model.get("missing_info") if isinstance(model.get("missing_info"), list) else []
    assumptions = model.get("allowed_assumptions") if isinstance(model.get("allowed_assumptions"), list) else []
    authorized_assumptions = [str(x).strip() for x in assumptions
                              if str(x).strip() and str(x).strip() in raw
                              and _AUTHORIZED_ASSUMPTION.search(str(x).strip())]
    rejected_assumptions = [str(x).strip() for x in assumptions
                            if str(x).strip() and str(x).strip() not in authorized_assumptions]
    reasons = []
    if fallback_reason:
        reasons.append(f"PO 模型降级：{fallback_reason}")
    if not model:
        reasons.append("PO 未返回结构化需求契约")
    elif not all(key in model for key in ("target_kind", "audience", "purpose", "requirements", "missing_info", "allowed_assumptions")):
        reasons.append("PO 需求契约缺少必需字段")
    if unmatched:
        reasons.append(f"原始要求缺少 PO 验收方法：{', '.join(unmatched)}")
    if any(c["status"] == "unchecked" for c in clauses):
        reasons.append("部分条款缺少可检查的验收方法")
    if attachment_context and not attached:
        reasons.append("附件文本缺少可定位的文件来源")
    if attached and not any(c["source"]["type"] == "attachment" for c in clauses):
        reasons.append("附件条款尚未逐条标注")
    if uncovered_attachment:
        reasons.append("部分附件片段过长或数量超限，尚未逐条标注")
    kind = model.get("target_kind") if model.get("target_kind") in _KINDS else deliver_kind
    if kind not in _KINDS:
        reasons.append("目标产物类型尚未确定")
    return {
        "schema_version": 1,
        "raw_requirement": raw,
        "attachment_context": attachment_context or "",
        "attachment_sources": attached,
        "target_kind": kind if kind in _KINDS else None,
        "audience": str(model.get("audience") or "").strip() or None,
        "purpose": str(model.get("purpose") or "").strip() or None,
        "requirements": clauses,
        "explicit_constraints": [c["text"] for c in clauses if c["source"]["type"] == "user"],
        "missing_info": [str(x).strip() for x in missing if str(x).strip()],
        "allowed_assumptions": authorized_assumptions,
        "assumption_warnings": ["已剔除未获用户明确授权的模型假设：" + item
                                for item in rejected_assumptions[:10]],
        "status": "ready" if not reasons and all(c["status"] == "ready" for c in clauses) else "unchecked",
        "unchecked_reasons": reasons,
    }


def task_requirement_ids(item: dict, contract: dict) -> list[str]:
    valid = {c["requirement_id"] for c in contract.get("requirements", [])}
    requested = item.get("requirement_ids")
    if isinstance(requested, list):
        ids = list(dict.fromkeys(str(value) for value in requested if str(value) in valid))
        if ids:
            return ids
    # Conservative fallback: no clause is silently dropped from the plan.
    return sorted(valid, key=lambda value: int(value[1:]))
