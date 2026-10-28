"""内容生成：Prompt、模型输出清洗、单轮「生成 → 渲染」与有限重试。

这里只管「内容从模型来」的路径；真实文件的结构与版式由 deliverable_builder 负责，
文件检查与评审由 artifact_quality 负责。
"""

import json
import os
import re
import threading

from llm import chat_json, chat_text
from artifact_content import meta_leaks, payload_meta_leaks, strip_document_meta
from path_manager import _unique_name, _workspace_dir, output_path
from deliverable_builder import (
    CONTENT_KINDS,
    _build_docx,
    _build_pptx,
    _build_xlsx,
    _parse_docx_markdown,
    _sanitize_pptx,
    _sanitize_xlsx,
    _strip_fences,
)

# 线程内的构建错误信息：生成失败时给下一轮修订提供具体原因
_BUILD_ERROR = threading.local()


def _length_rule(spec: str) -> str:
    """按统一口径给出生成端的硬性字数预算。"""
    import artifact_quality
    budget = artifact_quality.length_budget(spec, "docx")
    if not budget:
        return ""
    return (f"正文汉字数目标 {budget['target']} 字，必须落在 {budget['low']}～{budget['high']} 字之间"
            "（按正文汉字计，不含标题、标点、空格与序号）；超出就压缩句子，不足就补充实质内容，"
            "不得靠标点、空格、重复句子或自报字数凑数。")


def _docx_prose(content: str) -> str:
    """正文文本：不含标题行、表格、列表符号与代码块。"""
    lines, fenced = [], False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced or not stripped or stripped.startswith(("#", "|")):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _fit_docx_length(content: str, spec: str) -> str:
    """正文超过目标上限时按句确定性压缩；只减不增，优先压缩最长段落。"""
    import artifact_quality
    budget = artifact_quality.length_budget(spec, "docx")
    if not budget:
        return content
    lines = content.splitlines()

    def measure() -> int:
        return artifact_quality.counted_length(_docx_prose("\n".join(lines)))

    if measure() <= budget["high"]:
        return content

    def is_prose(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and not stripped.startswith(("#", "|", "```")) \
            and not re.match(r"^(?:[-*+>]|\d+[.、])\s*", stripped)

    for _ in range(200):
        if measure() <= budget["high"]:
            break
        prose = [(index, line) for index, line in enumerate(lines) if is_prose(line)]
        if len(prose) <= 1:
            break
        prose.sort(key=lambda item: (-len(item[1]), item[0]))
        trimmed = False
        for index, line in prose:
            sentences = [part for part in re.split(r"(?<=[。！？!?…])", line.strip()) if part]
            if len(sentences) < 2:
                continue
            sentences.pop()
            lines[index] = "".join(sentences)
            trimmed = True
            break
        if not trimmed:
            break
    return "\n".join(lines)


_NUMERIC_STAT = re.compile(r"(?<![\w.])\d{1,4}\s*(?:条|项|个|例|次|人|页|天|%|％|小时|分钟)")


def _pending_data_claim(sentence: str) -> bool:
    """该句没有具体数字，却声称数据待附件/待确认，与正文已给出的统计矛盾。"""
    if re.search(r"\d", sentence):
        return False
    return bool(re.search(r"(?:待|需|须|尚待|尚未)[^。；]{0,20}(?:确认|提供|核验|附件)", sentence)) \
        and bool(re.search(r"数据|信息|情况|结果|分布|统计|附件", sentence))


def _drop_pending_clauses(sentence: str) -> str:
    """删除句中“数据待确认”的分句，保留同一句的结论部分。"""
    clauses = [part for part in re.split(r"([；;])", sentence)]
    # 偶数位是正文分句，奇数位是分隔符
    bodies = clauses[0::2]
    separators = clauses[1::2]
    kept = [part for part in bodies if not _pending_data_claim(part)]
    if not kept:
        return ""
    if not kept[-1].endswith(("。", "！", "？", "!", "?", "…")):
        kept[-1] = kept[-1].rstrip("，, ") + "。"
    rebuilt = kept[0]
    for index in range(1, len(kept)):
        separator = separators[index - 1] if index - 1 < len(separators) else "；"
        rebuilt += separator + kept[index]
    return rebuilt


def _reconcile_pending_claims(content: str) -> str:
    """正文已给出具体数量时，删除“数据需待附件确认”这类自相矛盾表述。"""
    if len(set(_NUMERIC_STAT.findall(_docx_prose(content)))) < 2:
        return content
    kept = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "|", "```")):
            kept.append(line)
            continue
        sentences = [part for part in re.split(r"(?<=[。！？!?…])", stripped) if part]
        updated, changed = [], False
        for part in sentences:
            if not _pending_data_claim(part):
                updated.append(part)
                continue
            changed = True
            remaining = _drop_pending_clauses(part)
            if remaining:
                updated.append(remaining)
        if not changed:
            kept.append(line)
        elif updated:
            kept.append("".join(updated))
    return "\n".join(kept)


def _docx_content_prompt(spec: str, feedback: str = "") -> str:
    return (
        "根据用户需求完成可直接使用的正文，用 Markdown 输出。先判断它是作文、故事、信函、简报、报告、方案、手册还是论文，采用合适结构。"
        "第一行用 # 写具体标题；只有适合分节的文档才使用 ## 和 ###，作文和故事保持自然段。"
        "先给读者最需要的结论、决定或说明，再展开依据和实施细节。"
        "如果提供上一版正文，保留已正确的内容和结构，只修复明确指出的问题，不要重新构思一篇新文档。"
        "不要把所有文档写成六章报告，不要强行凑字数或增加封面；明确要求的字数、章节和受众优先。"
        f"{_length_rule(spec)}"
        "对比、重复记录、数值用表格；长论述保留自然段。步骤可用列表。"
        "保留用户素材、数字、单位、口径和不确定性；不编造来源、统计或完成事项。作文和故事允许合理的虚构情节，直接写成作品，不在正文解释素材是否真实。其他文档中必要的假设应在对应内容旁标明。"
        "不能为了凑字数添加事实或绝对化结论；计算指标可从给定数据推导并注明口径，建议必须写成建议。"
        "正文已经给出具体数字时，不得再写“数据需待附件确认/待补充”这类自相矛盾的表述；数字没有来源就不要写数字。"
        "用户未提供且未要求收集的会议链接、报名方式、日期、署名等信息不要添加字段或待填写占位。"
        "确需说明缺失信息时，在相应事实旁写‘待确认’，不要增设空白表单。"
        "必要的引用在文中使用可识别的来源名称与真实链接。只给成品正文，不附加字数统计、写作说明、复制提示、示例免责声明、工作计划或自评。\n"
        f"需修正的问题：{feedback}\n需求与素材：{spec}"
    )


def _strip_unrequested_docx_placeholders(content: str, spec: str) -> str:
    """Drop invented blank fields in deliverables, preserving requested forms."""
    # 协作汇编 spec 的前缀（"根据需求与团队各环节的实际产出……"）和团队产出正文都可能
    # 含“待确认”，不能把整段 spec 当作需求范围；只截取用户原始需求文本来判定严格清理。
    request_scope = spec.split("团队各环节的产出", 1)[0].split("任务完成情况", 1)[0]
    requirement_match = (
        re.search(r"加工需求[:：]\s*(.+)", request_scope, re.S)
        or re.search(r"需求[:：]\s*(.+)", request_scope, re.S)
    )
    if requirement_match:
        request_scope = requirement_match.group(1)
    if re.search(r"模板|表单|空白表|待填写|请填写|预留填写", request_scope):
        return content
    lines = content.splitlines()
    strict_notice = "通知" in request_scope and "待确认" not in request_scope
    kept = []
    skip_unrequested_section = False
    for line in lines:
        if strict_notice and re.match(r"^\s*#{1,3}\s*(?:[一二三四五六七八九十]+[、.．]\s*)?"
                                      r"(?:需说明的信息|待确认事项|待确认信息)\s*$", line):
            skip_unrequested_section = True
            continue
        if skip_unrequested_section and re.match(r"^\s*#{1,3}\s+", line):
            skip_unrequested_section = False
        if skip_unrequested_section:
            continue
        if strict_notice:
            line = re.sub(r"[（(][^（）()\n]{0,100}待确认[）)]", "", line)
        blank_words = "待补充|待填写|请填写|TBD|TODO" + ("|待确认" if strict_notice else "")
        if re.fullmatch(r"\s*(?:[-*]\s*)?[^：:\n]{1,24}[：:]\s*"
                        rf"(?:{blank_words})\s*", line, re.I):
            continue
        # 独立成行的空白占位（如整行只有“待补充”“待确认”）不是正文。
        if re.fullmatch(rf"\s*(?:[-*]\s*)?(?:{blank_words})[。．.]?\s*", line, re.I):
            continue
        if strict_notice and "待确认" in line:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _pptx_content_prompt(spec: str, feedback: str = "") -> str:
    return (
        "根据以下需求设计一份完整的中文演示文稿，输出一个 JSON 对象：\n"
        '{"title":"演示标题","subtitle":"副标题","slides":[{"heading":"页标题",'
        '"claim":"本页中心结论","layout":"conclusion|compare|process|timeline|chart|table|bullets",'
        '"bullets":["要点1"],"evidence":"依据说明","source_refs":["附件名/用户原话"],'
        '"table":{"headers":["列"],"rows":[["值"]]},'
        '"chart":{"title":"图表标题","categories":["第1周","第2周"],'
        '"series":[{"name":"新增","values":[12,9]}]},"note":"演讲备注"}]}\n'
        "内容要求：\n"
        "1. 先规划逐页故事线，每页一个中心结论；明确页数和封面优先，短需求不要凑页；\n"
        "2. 按内容选结论、双栏对比、流程、时间线、原生图表、表格或简短要点，不强迫固定要点数量；\n"
        "3. 图表只使用用户或附件真实数值，categories/series 的每个值都要有 source_refs；无数据不造图；\n"
        "4. note 写必要的演讲备注；\n"
        "严禁：「略/待补充/示例」式内容、空话套话。\n"
        f"{feedback}\n"
        "事实要求：保留用户素材与关键约束；不编造来源、统计、已完成事项或测试结果；假设与模拟数据必须明确标记。\n"
        "只写观众需要看到的幻灯片和必要演讲备注，不附加生成说明、示例免责声明或产品水印。\n"
        f"需求内容：{spec}"
    )


def _xlsx_content_prompt(spec: str, feedback: str = "") -> str:
    return (
        "设计能实际使用、可编辑且计算逻辑清楚的 Excel 工作簿，只输出 JSON：\n"
        '{"sheets":[{"name":"明细","headers":["项目","数量","单价","金额"],'
        '"rows":[["模拟样例",2,10,"=B2*C2"]],'
        '"column_types":{"数量":"number","单价":"currency","金额":"currency"},'
        '"formats":{"金额":"#,##0.00"},"formulas":{},'
        '"charts":[{"type":"bar","title":"金额对比","category_column":1,"data_columns":[4]}],'
        '"validations":[{"column":"A","options":["选项一","选项二"]}],"notes":{"B2":"数据来源或输入说明"}}]}\n'
        "样例仅说明结构，不能照抄其中的模拟数据。根据需求决定工作表数量，能一张表解决的问题不要拆很多表。"
        "原始输入、假设和计算结果要能区分；统计与汇总使用原生公式，不把算好的数字冒充公式。"
        "公式行号从 2 开始，引用实际存在的单元格；跨表引用加单引号。公式必须能被 Excel/LibreOffice 计算。"
        "图表必须有真实的数值列和分类列，列号从 1 开始。只在能帮助理解或用户要求时添加图表。"
        "日期使用 ISO 日期并声明 date 类型；百分数保存为小数并声明 percent；编号保留文本前导零。"
        "使用上传素材里的全部相关记录，不为达到行数编造数据。缺少输入时明确给出需要填写的字段与说明，"
        "仅在用户允许或明确标记模拟时使用模拟数据。禁止外部工作簿、网络公式或可执行调用。\n"
        "工作表、单元格和备注只写使用者需要的数据与业务说明，不附加生成说明、复制提示或示例免责声明。\n"
        f"需修正的问题：{feedback}\n需求与素材：{spec}"
    )



def _gen_docx(spec: str, roster: int | None, feedback: str = "", generation_meta: dict | None = None) -> str:
    _BUILD_ERROR.sections = []
    long_form = bool(re.search(r"长篇|白皮书|论文|多章节|不少于\s*[2-9]\d{3}\s*字|至少\s*[2-9]\d{3}\s*字", spec[:1500]))
    if long_form and not feedback:
        plan = chat_json(
            "你是文档主编。先规划长文档章节，不写正文。输出 JSON："
            '{"title":"具体标题","sections":[{"heading":"章节标题","goal":"本节要解决的问题"}]}。'
            "最多 6 节，遵守用户明确要求；不能把计划写成已完成。",
            spec[:18000], fallback={}, roster=roster, max_tokens=1600, timeout=120,
            meta=generation_meta, fallback_roster=1 if roster != 1 else None,
        )
        sections = plan.get("sections") if isinstance(plan, dict) else None
        if isinstance(sections, list) and 2 <= len(sections) <= 6:
            pieces = []
            for section in sections:
                if not isinstance(section, dict) or not section.get("heading"):
                    pieces = []
                    break
                heading = str(section["heading"]).strip()[:80]
                body = _strip_fences(chat_text(
                    "你是文档作者。只写本节可直接交付的正文，不写计划、生成说明或虚构事实。"
                    "如给定资料只说明计划，不能把它写成已完成。不要重复其他章节。",
                    [{"role": "user", "content": json.dumps({"document_title": plan.get("title"),
                        "section_heading": heading, "section_goal": section.get("goal", ""),
                        "all_sections": [s.get("heading") for s in sections if isinstance(s, dict)],
                        "requirement_and_sources": spec[:22000]}, ensure_ascii=False)}],
                    fallback="", temperature=.4, max_tokens=5500, timeout=300, roster=roster,
                ))
                if not body:
                    pieces = []
                    break
                pieces.append({"heading": heading, "body": body})
            if pieces:
                _BUILD_ERROR.sections = pieces
                title = str(plan.get("title") or "文档")[:100]
                return "# " + title + "\n\n" + "\n\n".join(
                    f"## {part['heading']}\n\n{part['body']}" for part in pieces
                )
    draft = _strip_fences(chat_text(
        "你是专业文档作者，完成实际内容并遵守用户的格式和篇幅要求。",
        [{"role": "user", "content": _docx_content_prompt(spec, feedback)}],
        fallback="", temperature=.5, max_tokens=12000, timeout=300, roster=roster,
        meta=generation_meta, fallback_roster=1 if roster == 6 else None))
    return draft


def _gen_pptx(spec: str, roster: int | None, feedback: str = "",
              generation_meta: dict | None = None) -> dict | None:
    import artifact_quality
    requested = artifact_quality.contract(spec, "pptx")
    prompt = _pptx_content_prompt(spec, feedback)
    explicit = requested.get("unit") == "页"
    scope = artifact_quality.requirement_scope(spec)
    include_cover = requested["cover"] or (not explicit and "封面" not in scope)
    content_pages = None
    if explicit:
        is_content_page_count = bool(re.search(r"\d{1,5}\s*页(?:内容页|内容)", scope[:300]))
        amount = requested["count"] if is_content_page_count else requested["count"] - int(include_cover)
        amount = max(1, amount)
        content_pages = amount
        prompt = prompt.replace("8~12 页内容页（不含标题页）", f"{max(1, amount)} 页内容页")
        prompt += f"\n用户明确指定页数；只生成 {max(1, amount)} 个 slides 元素。不要自行扩展页数。"
    first_payload = chat_json(
        "你是演示文稿设计师，只输出完整 JSON。", prompt,
        fallback={}, temperature=.5, max_tokens=10000, timeout=300, roster=roster,
        meta=generation_meta, fallback_roster=1 if roster != 1 else None)
    payload = _sanitize_pptx(first_payload, spec)
    if content_pages is not None and payload and len(payload["slides"]) != content_pages:
        correction_prompt = (
            f"首次结果有 {len(payload['slides'])} 张内容页，但用户明确要求 {content_pages} 张内容页。"
            "请修订以下 JSON，严格输出指定数量的 slides；合并重复或次要内容，保留所有关键结论、用户数据、"
            "来源和必要风险，不得添加虚构内容，也不要增加封面（封面由渲染器单独处理）。"
            f"\n需求：{spec}\n首次结果：{json.dumps(first_payload, ensure_ascii=False)}"
        )
        corrected = chat_json(
            "你是演示文稿编辑，按精确页数修订 JSON。", correction_prompt,
            fallback={}, temperature=.25, max_tokens=10000, timeout=300, roster=roster,
            meta=generation_meta, fallback_roster=1 if roster != 1 else None,
        )
        corrected_payload = _sanitize_pptx(corrected, spec)
        if corrected_payload and len(corrected_payload["slides"]) == content_pages:
            payload = corrected_payload
    if payload:
        payload["include_cover"] = include_cover
    return payload


def _gen_xlsx(spec: str, roster: int | None, feedback: str = "",
              generation_meta: dict | None = None) -> list[dict] | None:
    from spreadsheet_contract import budget_workbook
    source = spec.split("附件内容：\n", 1)[1].split("\n\n加工需求", 1)[0] if "附件内容：\n" in spec else spec.split("团队各环节的产出", 1)[0]
    grounded = budget_workbook(source)
    if grounded:
        return grounded
    return _sanitize_xlsx(chat_json(
        "你是电子表格建模专家，只输出完整 JSON。", _xlsx_content_prompt(spec, feedback),
        fallback={}, temperature=.4, max_tokens=14000, timeout=300, roster=roster,
        meta=generation_meta, fallback_roster=1 if roster != 1 else None))


def _gen_html(spec: str, roster: int | None, feedback: str = "",
              generation_meta: dict | None = None) -> str:
    """让 LLM 直接产出完整可运行的 HTML 文件（含内联 CSS/JS/SVG）。"""
    prompt = (
        "你是资深前端工程师。根据需求输出一个完整、可直接在浏览器打开运行的 HTML 文件。"
        "要求：\n"
        "1. 只输出 HTML 源码本身，不要用 ```html 代码块包裹，不要写任何解释文字；\n"
        "2. 所有 CSS 和 JavaScript 必须内联在同一个 HTML 文件中，不依赖外部资源；\n"
        "3. 如涉及 SVG/Canvas 动画，必须确保动画能正常运行，代码完整无缺；\n"
        "4. 页面要有合理的视觉样式，居中布局，适配常见屏幕；\n"
        "5. 不得包含 TODO、占位符、生成说明或模型对话痕迹。"
    )
    user_content = spec
    if feedback:
        user_content += f"\n\n上一轮反馈（请针对性修改）：\n{feedback}"
    raw = chat_text(
        prompt,
        [{"role": "user", "content": user_content}],
        fallback="", temperature=.5, max_tokens=16000, timeout=300, roster=roster,
        meta=generation_meta, fallback_roster=1 if roster != 1 else None,
    )
    text = (raw or "").strip()
    # 去掉模型可能添加的 markdown 代码围栏
    if text.startswith("```"):
        text = re.sub(r"^```(?:html)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _forge_once(kind: str, spec: str, output_path: str,
                roster: int | None, feedback: str = "", generation_meta: dict | None = None) -> bool:
    """单轮「生成内容 → 渲染」；失败返回 False（不残留半成品文件）"""
    _BUILD_ERROR.message = ""
    try:
        if kind in ("docx", "pdf"):
            content = strip_document_meta(_gen_docx(spec, roster, feedback, generation_meta))
            content = _strip_unrequested_docx_placeholders(content, spec)
            content = _reconcile_pending_claims(content)
            content = _fit_docx_length(content, spec)
            leaks = meta_leaks(content)
            if leaks:
                _BUILD_ERROR.message = "成品正文含生成说明或字数自报，请只返回正文：" + "；".join(leaks[:3])
                return False
            if kind == "pdf":
                from pdf_builder import build_pdf
                built = bool(content) and build_pdf(_parse_docx_markdown(content), output_path)
            else:
                import artifact_quality
                built = bool(content) and _build_docx(content, output_path, cover=artifact_quality.contract(spec, kind)["cover"])
            if built:
                import artifact_quality
                directory = artifact_quality.report_directory(output_path, _workspace_dir())
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "source.md").write_text(content, encoding="utf-8")
                for index, section in enumerate(getattr(_BUILD_ERROR, "sections", []), 1):
                    section_dir = directory / "sections"
                    section_dir.mkdir(exist_ok=True)
                    (section_dir / f"{index:02d}.md").write_text(
                        f"## {section['heading']}\n\n{section['body']}", encoding="utf-8")
            return built
        if kind == "pptx":
            payload = _gen_pptx(spec, roster, feedback, generation_meta)
            leaks = payload_meta_leaks(payload)
            if leaks:
                _BUILD_ERROR.message = "演示文稿含生成说明，请删除：" + "；".join(leaks[:3])
                return False
            built = payload is not None and _build_pptx(payload, output_path)
            if built:
                import artifact_quality
                directory = artifact_quality.report_directory(output_path, _workspace_dir())
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "source.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return built
        if kind == "xlsx":
            sheets = _gen_xlsx(spec, roster, feedback, generation_meta)
            leaks = payload_meta_leaks(sheets)
            if leaks:
                _BUILD_ERROR.message = "表格含生成说明，请删除：" + "；".join(leaks[:3])
                return False
            built = sheets is not None and _build_xlsx(sheets, output_path)
            if built:
                import artifact_quality
                directory = artifact_quality.report_directory(output_path, _workspace_dir())
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "source.json").write_text(json.dumps({"sheets": sheets}, ensure_ascii=False), encoding="utf-8")
            return built
        if kind == "html":
            content = _gen_html(spec, roster, feedback, generation_meta)
            if not content or not content.strip():
                _BUILD_ERROR.message = "HTML 内容为空"
                return False
            if meta_leaks(content):
                _BUILD_ERROR.message = "HTML 含生成说明或模型对话痕迹，请只返回可直接运行的 HTML"
                return False
            try:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(content)
                built = True
            except OSError as error:
                _BUILD_ERROR.message = f"HTML 落盘失败：{error}"
                built = False
            if built:
                import artifact_quality
                directory = artifact_quality.report_directory(output_path, _workspace_dir())
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "source.html").write_text(content, encoding="utf-8")
            return built
    except Exception as error:  # noqa: BLE001 —— 单类产物失败不影响整体流程
        _BUILD_ERROR.message = str(error)[:2000]
        print(f"[Forge] {kind} 生成异常：{error}")
    return False


def forge_file(kind: str, spec: str, filename: str, roster: int | None = None,
               generation_meta: dict | None = None, *, quick: bool = False) -> str | None:
    """制作 -> 真实文件检查 -> 成品评审 -> 按问题修订；保留最佳候选。

    quick=True 时跳过模型评审（model_review）并减少重试次数，用于快速生成模式。
    """
    import artifact_quality
    if kind not in CONTENT_KINDS:
        return None
    best_name, best_report, feedback = None, None, ""
    attempts = []
    max_attempts = 2 if quick else 3
    for attempt in range(max_attempts):
        candidate = _unique_name(filename)
        candidate_path = output_path(candidate)
        attempt_meta = {}
        generated = (
            _forge_once(kind, spec, candidate_path, roster, feedback, attempt_meta)
            if generation_meta is not None
            else _forge_once(kind, spec, candidate_path, roster, feedback)
        )
        if not generated:
            if generation_meta is not None:
                generation_meta.update(attempt_meta)
            feedback = getattr(_BUILD_ERROR, "message", "") or "未生成可用文件，请检查内容结构并返回完整内容。"
            attempts.append({"attempt": attempt + 1, "stage": "build", "status": "failed", "detail": feedback})
            continue
        # 保存完整需求，检查的是实际文件而不是模型自述。
        report = artifact_quality.inspect(candidate_path, kind, spec, _workspace_dir())
        # 快速模式或本地自检全通过时，跳过模型评审以节省一次 LLM 调用
        if not quick and not any(c["ok"] is False for c in report["checks"]):
            report = artifact_quality.model_review(report, candidate_path, _workspace_dir(), roster)
        attempts.append({"attempt": attempt + 1, "stage": "review", "status": report["status"]})
        def score_of(result):
            return (sum(c["ok"] is False for c in result["checks"]),
                    len(result.get("review", {}).get("issues", [])) if result.get("review", {}).get("status") == "failed" else 0,
                    sum(c["ok"] is None for c in result["checks"]))
        score = score_of(report)
        old_score = score_of(best_report) if best_report else None
        if best_report is None or score <= old_score:
            best_name, best_report = candidate, report
            if generation_meta is not None:
                generation_meta.update(attempt_meta)
        feedback = artifact_quality.feedback(report)
        if report["ok"] or not feedback:
            break
        directory = artifact_quality.report_directory(candidate_path, _workspace_dir())
        previous = next((path for path in (directory / "source.md", directory / "source.json") if path.is_file()), None)
        if previous:
            feedback += "\n\n上一版完整内容或结构（以它为基准定点修订，保留正确部分）：\n" + previous.read_text(encoding="utf-8")[:30000]
    if best_name:
        best_report["attempts"] = attempts
        artifact_quality.save_report(output_path(best_name), _workspace_dir(), best_report)
    return best_name
