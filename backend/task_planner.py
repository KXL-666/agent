"""需求拆解与任务规划：把一句需求变成可执行的任务计划。

这里只做「规划」这一件事——LLM 拆解、兜底模板、任务合并/补全、角色分派兜底。
不涉及产物生成、验收或事件广播，便于单独测试拆解质量。
"""

import asyncio

import user_preferences
from llm import chat_json
from roles import DEV_BE, DEV_FE, PO, QA

PLAN_SYSTEM_PROMPT = (
    "你是敏捷团队的产品经理。把需求拆解为 3-5 个任务，"
    '输出 JSON：{"thought":"一两句向团队说明拆解思路：为什么这样拆、重点和风险在哪",'
    '"contract":{"target_kind":"docx|pdf|pptx|xlsx|code",'
    '"audience":"读者或使用者","purpose":"用途",'
    '"requirements":[{"source":"user|attachment","source_quote":"原文连续片段",'
    '"source_name":"附件名（仅附件）","source_location":"附件位置（仅附件）",'
    '"text":"条款","priority":"P0|P1|P2","acceptance_method":"具体核验方法"}],'
    '"missing_info":[],"allowed_assumptions":[]},'
    '"tasks":[{"title":"任务名","role_hint":"后端|前端|测试|写作|设计|其他",'
    '"user_story":"作为某类用户，我希望某个能力，以便实现某个价值",'
    '"priority":"P0|P1|P2","story_points":3,"requirement_ids":["R1"],'
    '"acceptance_criteria":["可验证的验收条件"]}]}。'
    "把用户原话按句号、分号、逗号逐段列为 user 条款，source_quote 必须是原文连续片段，"
    "按出现顺序编号 R1、R2……；附件条款须写精确附件名和位置。"
    "不要把猜测写成事实，未知信息放 missing_info，allowed_assumptions 只放用户原文中明确授权的连续片段。"
    "按业务优先级排序；故事点仅使用 1/2/3/5/8/13，每个任务给出 2-4 条具体、可观察的验收条件。"
    "一页以内的通知或简短文件最多拆成 3 个任务：BE核对事实、FE完成正文与排版、QA按条款检查；"
    "正文与排版由同一个 FE 任务完成，不要为同一角色重复拆分调用。"
    "注意判断需求性质：如果需求是要一份成品内容（写作文/报告/方案/周报/PPT/表格），"
    "任务应为内容生产环节（如：资料与大纲、正文撰写、排版与导出成品）；"
    "只有需求是开发软件/接口/页面时，任务才是开发环节。"
    "如果提供了附件素材，任务必须体现对附件的加工（阅读、提取、改写、转换、扩展）。"
    "软件需求按不重叠职责拆解：BE负责核心逻辑/接口，FE负责入口/交互/使用说明，QA只负责测试与验收；"
    "同一职责的重复事项合并为一个任务，QA不得生成生产代码或 README，FE不得重写 BE 的核心模块。"
)


def mock_plan() -> dict:
    return {
        "thought": "模型不可用，按默认分工模板拆解：接口契约、核心实现、页面联调、自动化测试四条线并行推进。",
        "tasks": [
            {"title": "设计数据库表结构与接口契约", "role_hint": "后端"},
            {"title": "实现核心业务接口", "role_hint": "后端"},
            {"title": "搭建前端页面并联调", "role_hint": "前端"},
            {"title": "编写接口自动化测试", "role_hint": "测试"},
        ]
    }


async def decompose_plan(requirement: str, uploads_text: str = "",
                         uploads_images: list | None = None,
                         instructions: str = "",
                         plan_meta: dict | None = None) -> tuple[dict, str]:
    """LLM 拆解需求为任务计划；输出无效时回退到 mock_plan。

    返回 (plan, fallback_reason)。fallback_reason 为空表示本次拆解由模型正常产出；
    非空表示已兜底，节点据此向前端上报，避免用户误以为 LLM 拆解生效。
    """
    plan_meta = plan_meta if plan_meta is not None else {}
    attachments_note = (
        f"\n\n用户提供了附件素材，任务拆解须围绕对这些素材的加工改造：\n{uploads_text[:6000]}"
        if uploads_text else ""
    )
    plan = await asyncio.to_thread(
        chat_json,
        system=PLAN_SYSTEM_PROMPT + user_preferences.prompt_block(),
        user=f"需求：{requirement}{attachments_note}{instructions}",
        fallback=mock_plan,
        max_tokens=4500,
        meta=plan_meta,
        roster=PO,
        images=uploads_images,
    )
    if (not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list)
            or not any(isinstance(t, dict) and t.get("title") for t in plan["tasks"])):
        return mock_plan(), "模型返回的任务列表无效"
    return plan, str(plan_meta.get("fallback_reason") or "")


def detect_final_kind(requirement: str) -> str:
    """关键词兜底：判断需求最终要交付什么（LLM 不可用时使用）。
    注意：只有明确的软件/代码需求才返回 code，其余兜底为 docx，
    避免模糊需求误入 code 路径触发 Docker 沙箱长时间运行。"""
    text = requirement.lower()
    if any(w in text for w in ("html", "网页", "svg", "canvas", "前端页面", "h5")):
        return "html"
    if "pdf" in text and any(word in text for word in ("word", "docx")):
        return "docx"
    if "pdf" in text:
        return "pdf"
    if any(w in text for w in ("ppt", "幻灯", "演示文稿", "slides")):
        return "pptx"
    if any(w in text for w in ("excel", "表格", "xlsx", "预算表", "统计表")):
        return "xlsx"
    if any(w in text for w in (
        "作文", "文章", "论文", "周报", "日报", "报告", "文档", "word", "docx",
        "方案", "总结", "计划书", "信", "稿", "文案", "申请书", "简历",
    )):
        return "docx"
    if any(w in text for w in (
        "代码包", "代码项目", "源代码", "编程", "写一个程序", "写个程序",
        "开发一个", "开发个", "python脚本", "python 脚本", "软件系统",
        "接口开发", "后端", "前端", "数据库", "api", "应用", "网站",
        "命令行", "cli", "小工具", "脚本",
    )):
        return "code"
    # 兜底为 docx：模糊需求不应走 code 路径触发 Docker
    return "docx"


def pick_assignee(title: str, index: int) -> int:
    backend_words = ("数据库", "接口", "后端", "API", "服务", "存储")
    frontend_words = ("前端", "页面", "界面", "UI", "组件", "联调")
    test_words = ("测试", "用例", "自动化", "校核", "验证", "验收", "检查")

    if any(w in title for w in backend_words):
        return DEV_BE
    if any(w in title for w in frontend_words):
        return DEV_FE
    if any(w in title for w in test_words):
        return QA
    return DEV_BE if index % 2 == 0 else DEV_FE


def compact_short_document_tasks(planned: list[dict], contract: dict) -> list[dict]:
    """Collapse duplicate FE calls for one-page file requests without losing coverage."""
    kind = contract.get("target_kind")
    raw = str(contract.get("raw_requirement") or "")
    if kind not in {"docx", "pdf", "pptx"} or not any(
        marker in raw for marker in ("控制在一页", "不超过一页", "最多一页", "一页以内")
    ):
        return planned
    fe_hints = {"前端", "写作", "设计"}
    fe = [item for item in planned if str(item.get("role_hint") or "") in fe_hints]
    if len(fe) < 2:
        return planned
    primary = dict(fe[0])
    primary["title"] = "撰写通知正文并完成一页排版"
    primary["role_hint"] = "前端"
    stories = [str(item.get("user_story") or "").strip() for item in fe]
    primary["user_story"] = "；".join(story for story in stories if story)[:400]
    primary["acceptance_criteria"] = list(dict.fromkeys(
        str(criteria).strip() for item in fe
        for criteria in (item.get("acceptance_criteria") if isinstance(item.get("acceptance_criteria"), list) else [])
        if str(criteria).strip()
    ))[:6]
    primary["requirement_ids"] = list(dict.fromkeys(
        str(req_id) for item in fe
        for req_id in (item.get("requirement_ids") if isinstance(item.get("requirement_ids"), list) else [])
    ))
    primary["story_points"] = max((item.get("story_points", 3) for item in fe
                                   if isinstance(item.get("story_points"), int)), default=3)
    merged = []
    inserted = False
    for item in planned:
        if item in fe:
            if not inserted:
                merged.append(primary)
                inserted = True
            continue
        merged.append(item)
    return merged


def ensure_file_collaboration_tasks(planned: list[dict], contract: dict) -> list[dict]:
    """A file Sprint needs distinct fact, composition, and QA contributions."""
    if contract.get("target_kind") == "code":
        return planned
    aliases = {"后端": {"后端", "backend", "be"},
               "前端": {"前端", "frontend", "fe", "写作", "设计"},
               "测试": {"测试", "qa"}}
    present = {role for role, names in aliases.items()
               if any(str(item.get("role_hint") or "").strip().lower() in names
                      for item in planned)}
    requirement_ids = [str(item.get("requirement_id")) for item in contract.get("requirements", [])
                       if item.get("requirement_id")]
    templates = {
        "后端": ("核对事实、来源与约束", "逐条核对用户原话与附件，提交可追溯的事实核对材料"),
        "前端": ("撰写正文并完成版式", "依据已核实材料完成可直接交付的正文和版式"),
        "测试": ("按条款测试验收", "对真实候选文件逐条检查内容、事实与格式"),
    }
    result = list(planned)
    for role in ("后端", "前端", "测试"):
        if role in present:
            continue
        title, story = templates[role]
        result.append({"title": title, "role_hint": role, "user_story": story,
                       "priority": "P0", "story_points": 2,
                       "requirement_ids": requirement_ids,
                       "acceptance_criteria": [story, "产出引用对应需求条款和实际文件/来源"]})
    return result


def compact_code_tasks(planned: list[dict]) -> list[dict]:
    """Merge duplicate same-role coding stories into one bounded contribution."""
    role_aliases = {
        "后端": {"后端", "backend", "be"},
        "前端": {"前端", "frontend", "fe", "写作", "设计"},
        "测试": {"测试", "qa"},
    }
    title_signals = {
        "测试": ("测试", "验收", "验证", "检查"),
        "后端": ("逻辑", "接口", "数据", "后端", "服务", "校验", "错误处理"),
        "前端": ("前端", "界面", "命令行", "交互", "README", "说明", "文档", "入口"),
    }
    grouped: dict[str, list[dict]] = {}
    other: list[dict] = []
    for item in planned:
        hint = str(item.get("role_hint") or "").strip().lower()
        title = str(item.get("title") or "")
        role = next((name for name, aliases in role_aliases.items() if hint in aliases), None)
        if role is None:
            role = next((name for name, signals in title_signals.items()
                         if any(signal.lower() in title.lower() for signal in signals)), None)
        if role:
            grouped.setdefault(role, []).append(item)
        else:
            other.append(item)

    merged = []
    role_titles = {
        "后端": "实现核心逻辑、接口与输入校验",
        "前端": "实现命令行入口与使用说明",
        "测试": "编写自动化测试并验证错误路径",
    }
    for role in ("后端", "前端", "测试"):
        entries = grouped.get(role, [])
        if not entries:
            continue
        combined = dict(entries[0])
        combined.update({
            "title": role_titles[role],
            "role_hint": role,
            "user_story": "；".join(str(item.get("user_story") or "").strip()
                                    for item in entries if str(item.get("user_story") or "").strip())[:600],
            "acceptance_criteria": list(dict.fromkeys(
                str(criteria).strip() for item in entries
                for criteria in (item.get("acceptance_criteria") if isinstance(item.get("acceptance_criteria"), list) else [])
                if str(criteria).strip()
            ))[:8],
            "requirement_ids": list(dict.fromkeys(
                str(req_id) for item in entries
                for req_id in (item.get("requirement_ids") if isinstance(item.get("requirement_ids"), list) else [])
                if str(req_id).strip()
            )),
            "story_points": max((item.get("story_points", 3) for item in entries
                                 if isinstance(item.get("story_points"), int)), default=3),
        })
        merged.append(combined)
    return merged + other


def code_task_phases(tasks: list[dict]) -> list[list[dict]]:
    """Finish BE interface contributions before parallel FE and QA work begins."""
    backend = [task for task in tasks if task.get("assignee_roster") == DEV_BE]
    downstream = [task for task in tasks if task.get("assignee_roster") != DEV_BE]
    return [phase for phase in (backend, downstream) if phase]
