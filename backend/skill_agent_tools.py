"""Tools exposed to a roster member for discovering, installing and using skills."""
import json

import httpx

import skill_packages
import skills

TOOL_SPECS = [
    {"type": "function", "function": {"name": "search_skill_repositories",
        "description": "在 GitHub 搜索技能仓库，返回候选 URL。下载前先看名称和描述。",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "install_skill_package",
        "description": "下载并安装完整技能包（GitHub 仓库/目录或 HTTPS ZIP）；返回技能、脚本和依赖状态。",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"},
            "skill_path": {"type": "string", "description": "ZIP 内技能子目录，可留空"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "list_member_skills",
        "description": "列出当前成员已启用的技能、可运行脚本及依赖状态。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "run_skill_script",
        "description": "运行分配给当前成员的已安装技能脚本；输入为注册的相对脚本路径和参数。",
        "parameters": {"type": "object", "properties": {"skill_id": {"type": "string"},
            "script": {"type": "string"}, "arguments": {"type": "array", "items": {"type": "string"}}},
            "required": ["skill_id", "script"]}}},
    {"type": "function", "function": {"name": "list_skill_files",
        "description": "列出已安装技能包中的脚本、参考资料、附件与生成文件。",
        "parameters": {"type": "object", "properties": {"skill_id": {"type": "string"}},
            "required": ["skill_id"]}}},
    {"type": "function", "function": {"name": "read_skill_asset",
        "description": "读取技能包内的文本参考资料；二进制附件返回大小和哈希，可交给技能脚本使用。",
        "parameters": {"type": "object", "properties": {"skill_id": {"type": "string"},
            "path": {"type": "string"}}, "required": ["skill_id", "path"]}}},
]

TOOL_NOTE = ("你可以用提供的工具搜索、安装和运行技能包。只有工具返回成功时才能说已安装或已运行。"
             "安装结果若为 needs_attention，应说明缺少的依赖，不得声称脚本可用。"
             "脚本属于外部代码；仅在用户要求使用相关技能完成任务时运行。")


def execute(name: str, arguments: str, roster: int) -> dict:
    try:
        params = json.loads(arguments or "{}")
        if not isinstance(params, dict):
            raise ValueError("工具参数必须是对象")
        if name == "search_skill_repositories":
            query = str(params.get("query", "")).strip()[:100]
            if not query:
                raise ValueError("搜索词不能为空")
            response = httpx.get("https://api.github.com/search/repositories",
                                 params={"q": query + " skill", "per_page": 5}, timeout=15,
                                 headers={"Accept": "application/vnd.github+json"})
            response.raise_for_status()
            return {"candidates": [{"name": item.get("full_name"), "url": item.get("html_url"),
                                     "description": item.get("description")}
                                    for item in response.json().get("items", [])[:5]]}
        if name == "install_skill_package":
            items = skill_packages.install(str(params.get("url", "")), roster,
                                           str(params.get("skill_path", "")))
            return {"installed": [{"id": item["id"], "name": item["name"],
                                    "status": item.get("install_status"), "tools": item.get("tools", []),
                                    "issues": item.get("install_issues", [])} for item in items]}
        if name == "list_member_skills":
            return {"skills": [{"id": item["id"], "name": item["name"],
                                "status": item.get("install_status", "instructions_only"),
                                "tools": item.get("tools", [])} for item in skills.member_skills(roster)]}
        if name == "run_skill_script":
            return skill_packages.run_tool(str(params.get("skill_id", "")), roster,
                                           str(params.get("script", "")), params.get("arguments", []))
        if name == "list_skill_files":
            return {"files": skill_packages.list_files(str(params.get("skill_id", "")), roster)}
        if name == "read_skill_asset":
            return skill_packages.read_asset(str(params.get("skill_id", "")), roster,
                                             str(params.get("path", "")))
        raise ValueError("未知技能工具")
    except (ValueError, OSError, httpx.HTTPError) as error:
        return {"ok": False, "error": str(error)[:500]}
