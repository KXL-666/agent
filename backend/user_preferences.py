"""用户偏好档案：跨 Sprint 记住用户的长期偏好，注入关键角色的提示词。

存储：workspace/preferences.json，结构 [{"id", "text", "created_at"}]，上限 30 条，超出删最旧。
注入：prompt_block() 返回可拼到系统提示词末尾的文本块；空档案返回空串，不增加任何 token。
"""

import json
import time
import uuid
from pathlib import Path

from forge import WORKSPACE_DIR

MAX_ITEMS = 30
STORE_PATH = Path(WORKSPACE_DIR) / "preferences.json"


def load() -> list[dict]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data
            if isinstance(item, dict) and str(item.get("text") or "").strip() and item.get("id")]


def _save(items: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def list_items() -> list[dict]:
    return load()


def add(text: str) -> dict:
    text = str(text or "").strip()[:200]
    if not text:
        raise ValueError("偏好内容不能为空")
    items = load()
    for item in items:
        if item["text"] == text:
            return item
    item = {"id": uuid.uuid4().hex[:8], "text": text, "created_at": time.time()}
    items.append(item)
    if len(items) > MAX_ITEMS:
        items = items[-MAX_ITEMS:]
    _save(items)
    return item


def remove(item_id: str) -> bool:
    items = load()
    remaining = [item for item in items if item.get("id") != item_id]
    if len(remaining) == len(items):
        return False
    _save(remaining)
    return True


def prompt_block() -> str:
    items = load()
    if not items:
        return ""
    lines = "\n".join(f"- {item['text']}" for item in items)
    return f"\n\n【用户偏好档案（最高优先级遵循）】\n{lines}"
