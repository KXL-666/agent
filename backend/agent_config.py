"""成员配置：每个成员可自定义姓名、可配置专属 LLM API（持久化到 agent_config.json）

生效规则：成员配置了专属 api_key 就用专属配置；缺省字段回落到全局 env 配置；
改名会同步 roles.ROSTER（内存生效 + 重启后仍在）。
"""

import json
import os

import secretbox
from roles import ROSTER

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "agent_config.json")


def _load() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    # 读时解密：落盘的是密文，内存里永远是明文
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        for section in ("llm", "llm_backup"):
            llm = entry.get(section)
            if llm and secretbox.is_encrypted(llm.get("api_key", "")):
                llm["api_key"] = secretbox.decrypt_value(llm["api_key"])
    return data


def _save(data: dict) -> None:
    # 写前加密：落盘副本里 API Key 全部走 DPAPI 密文，传入的内存字典保持明文
    disk = json.loads(json.dumps(data, ensure_ascii=False))
    for entry in disk.values():
        if not isinstance(entry, dict):
            continue
        for section in ("llm", "llm_backup"):
            llm = entry.get(section)
            if llm and llm.get("api_key"):
                llm["api_key"] = secretbox.encrypt_value(llm["api_key"])
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(disk, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _migrate_plaintext_keys() -> None:
    """启动时把历史明文 API Key 一次性加密重写"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    changed = False
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        for section in ("llm", "llm_backup"):
            llm = entry.get(section)
            key = llm.get("api_key", "") if llm else ""
            if key and not secretbox.is_encrypted(key):
                llm["api_key"] = secretbox.encrypt_value(key)
                changed = True
    if changed:
        _save(raw)
        print("[AgentConfig] 已将本机明文 API Key 加密存储（Windows DPAPI）")


_migrate_plaintext_keys()


def _llm_view(entry: dict, section: str) -> dict:
    llm = entry.get(section, {}) if isinstance(entry, dict) else {}
    return {
        "base_url": llm.get("base_url", ""),
        "api_key": llm.get("api_key", ""),
        "model": llm.get("model", ""),
    }


def get_agent_config(roster: int) -> dict:
    """读取成员配置：姓名/头像/人物形象/主 API/备用 API（主挂了自动切备用）"""
    entry = _load().get(str(roster), {})
    return {
        "name": entry.get("name"),
        "avatar": entry.get("avatar", ""),
        "skin": entry.get("skin", ""),
        "llm": _llm_view(entry, "llm"),
        "llm_backup": _llm_view(entry, "llm_backup"),
    }


def _merge_llm(cur: dict, patch: dict) -> None:
    """合并一段 LLM 配置：api_key 空串=保留旧值，__CLEAR__=清除"""
    for field in ("base_url", "api_key", "model"):
        if field not in patch:
            continue
        value = str(patch[field]).strip()
        if field == "api_key":
            if value == "":
                continue
            if value == "__CLEAR__":
                cur[field] = ""
                continue
        cur[field] = value


# 前端 chibi-stickers 角色包可用的 9 套皮肤；空串 = 按名册默认分配
VALID_SKINS = {
    "", "misaki", "erikari", "nate", "harri", "luke",
    "soeren", "mario", "sinisa", "spineboy",
}


def update_agent_config(
    roster: int,
    name: str | None = None,
    llm: dict | None = None,
    llm_backup: dict | None = None,
    avatar: str | None = None,
    skin: str | None = None,
) -> dict:
    """合并更新成员配置。llm 里 api_key 为空字符串表示「不修改旧值」；
    传 api_key="__CLEAR__" 表示清除专属 key 回退全局。"""
    data = _load()
    entry = data.setdefault(str(roster), {})
    if name is not None and name.strip():
        entry["name"] = name.strip()[:20]
        # 同步名册：对话气泡、任务分派等文案立即使用新名字
        if roster in ROSTER:
            ROSTER[roster]["name"] = entry["name"]
    if avatar is not None:
        entry["avatar"] = avatar.strip()[:8]
    if skin is not None:
        skin_value = skin.strip().lower()
        if skin_value not in VALID_SKINS:
            raise ValueError(f"未知人物形象：{skin}")
        entry["skin"] = skin_value
    if llm is not None:
        _merge_llm(entry.setdefault("llm", {}), llm)
    if llm_backup is not None:
        _merge_llm(entry.setdefault("llm_backup", {}), llm_backup)
    _save(data)
    return get_agent_config(roster)


def apply_saved_names() -> None:
    """启动时把持久化的姓名应用到名册"""
    data = _load()
    for key, entry in data.items():
        name = entry.get("name")
        roster = int(key) if key.isdigit() else None
        if name and roster in ROSTER:
            ROSTER[roster]["name"] = name


def resolve_llm(roster: int) -> dict:
    """该成员实际生效的 LLM 配置；未配置专属 key 时返回空 dict（调用方用全局）"""
    llm = get_agent_config(roster)["llm"]
    if not llm["api_key"]:
        return {}
    return llm


def resolve_llm_backup(roster: int) -> dict:
    """该成员的备用 LLM 配置（主 API 故障时自动切换）；未配置返回空 dict"""
    llm = get_agent_config(roster)["llm_backup"]
    if not llm["api_key"]:
        return {}
    return llm
