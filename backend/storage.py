"""Sprint 历史持久化（SQLite）

保存每轮 Sprint 的需求、任务、产物与状态，供历史列表、断线恢复和产物重试/修订使用。
数据库默认 backend/office.db，可用环境变量 OFFICE_DB_PATH 覆盖（测试用）。
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from agile import PLAN_FIELDS

DB_PATH = os.getenv("OFFICE_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "office.db"
)

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sprints (
  id TEXT PRIMARY KEY,
  requirement TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  stage TEXT NOT NULL DEFAULT '',
  deliver_kind TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL DEFAULT 0,
  finished_at REAL NOT NULL DEFAULT 0,
  usage_calls INTEGER NOT NULL DEFAULT 0,
  usage_tokens INTEGER NOT NULL DEFAULT 0,
  usage_cost REAL NOT NULL DEFAULT 0,
  contract_json TEXT NOT NULL DEFAULT '{}',
  execution_style TEXT NOT NULL DEFAULT 'legacy_unknown',
  budget REAL NOT NULL DEFAULT 0,
  execution_mode TEXT NOT NULL DEFAULT 'cloud'
);
CREATE TABLE IF NOT EXISTS tasks (
  sprint_id TEXT NOT NULL,
  id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  role_hint TEXT NOT NULL DEFAULT '',
  assignee_roster INTEGER,
  status TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  code_files TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (sprint_id, id)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  sprint_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  author_roster INTEGER,
  task_id TEXT,
  content TEXT NOT NULL DEFAULT '',
  file_name TEXT,
  created_at REAL NOT NULL DEFAULT 0,
  degraded INTEGER NOT NULL DEFAULT 0,
  degraded_reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'unknown',
  quality_status TEXT NOT NULL DEFAULT 'unknown',
  regen TEXT NOT NULL DEFAULT '',
  parent_id TEXT,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_artifacts_sprint ON artifacts(sprint_id);
CREATE INDEX IF NOT EXISTS idx_tasks_sprint ON tasks(sprint_id);
CREATE TABLE IF NOT EXISTS events (
  sprint_id TEXT NOT NULL,
  ts REAL NOT NULL DEFAULT 0,
  type TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_sprint ON events(sprint_id, ts);
CREATE TABLE IF NOT EXISTS handoffs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sprint_id TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  from_roster INTEGER NOT NULL,
  to_roster INTEGER NOT NULL,
  input_refs TEXT NOT NULL DEFAULT '[]',
  output_refs TEXT NOT NULL DEFAULT '[]',
  claims TEXT NOT NULL DEFAULT '[]',
  open_questions TEXT NOT NULL DEFAULT '[]',
  acceptance_criteria TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'unchecked',
  version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_handoffs_sprint ON handoffs(sprint_id, task_id, id);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _lock, _conn() as conn:
        conn.executescript(_SCHEMA)
        _ensure_columns(conn)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """老库平滑升级。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sprints)")}
    for name, decl in (
        ("usage_calls", "INTEGER NOT NULL DEFAULT 0"),
        ("usage_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("usage_cost", "REAL NOT NULL DEFAULT 0"),
        ("contract_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("execution_style", "TEXT NOT NULL DEFAULT 'legacy_unknown'"),
        ("budget", "REAL NOT NULL DEFAULT 0"),
        ("execution_mode", "TEXT NOT NULL DEFAULT 'cloud'"),
    ):
        if name not in existing:
            conn.execute(f"ALTER TABLE sprints ADD COLUMN {name} {decl}")
    task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "agile" not in task_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN agile TEXT NOT NULL DEFAULT '{}'")
    artifact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(artifacts)")}
    for name, decl in (
        ("source", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("quality_status", "TEXT NOT NULL DEFAULT 'unknown'"),
    ):
        if name not in artifact_columns:
            conn.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {decl}")


init()


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    if "contract_json" in data:
        try:
            data["contract"] = json.loads(data.pop("contract_json") or "{}")
        except (ValueError, TypeError):
            data["contract"] = {}
    if "agile" in data:
        try:
            values = json.loads(data.pop("agile") or "{}")
            data.update({k: values[k] for k in PLAN_FIELDS if k in values})
        except (ValueError, TypeError):
            pass
    if "code_files" in data:
        try:
            data["code_files"] = json.loads(data["code_files"] or "[]")
        except json.JSONDecodeError:
            data["code_files"] = []
    if "regen" in data:
        try:
            data["regen"] = json.loads(data["regen"]) if data["regen"] else None
        except json.JSONDecodeError:
            data["regen"] = None
    if "degraded" in data:
        data["degraded"] = bool(data["degraded"])
    return data


# ------------------------------ Sprint ------------------------------


def create_sprint(requirement: str, budget: float = 0) -> str:
    sprint_id = f"S{int(time.time() * 1000)}"
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO sprints(id, requirement, status, started_at, budget) VALUES(?,?,?,?,?)",
            (sprint_id, requirement, "running", time.time(), max(0.0, float(budget or 0))),
        )
    return sprint_id


def update_sprint(sprint_id: str, **fields) -> None:
    if not sprint_id:
        return
    allowed = {
        "requirement", "status", "stage", "deliver_kind", "started_at", "finished_at",
        "usage_calls", "usage_tokens", "usage_cost",
        "contract_json",
        "execution_style",
        "budget",
        "execution_mode",
    }
    pairs = [(k, v) for k, v in fields.items() if k in allowed]
    if not pairs:
        return
    sql = "UPDATE sprints SET " + ", ".join(f"{k}=?" for k, _ in pairs) + " WHERE id=?"
    with _lock, _conn() as conn:
        conn.execute(sql, [v for _, v in pairs] + [sprint_id])


def get_sprint(sprint_id: str) -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM sprints WHERE id=?", (sprint_id,)).fetchone()
    return _row(row)


def save_contract(sprint_id: str, contract: dict) -> None:
    update_sprint(sprint_id, contract_json=json.dumps(contract, ensure_ascii=False))


def latest_sprint() -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM sprints ORDER BY started_at DESC LIMIT 1").fetchone()
    return _row(row)


def list_sprints(limit: int = 50) -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sprints ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        item = _row(row) or {}
        item["task_count"] = _count("tasks", item["id"])
        item["artifact_count"] = _count("artifacts", item["id"])
        result.append(item)
    return result


def _count(table: str, sprint_id: str) -> int:
    with _lock, _conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
    return int(row["n"]) if row else 0


# ------------------------------- 任务 -------------------------------


def upsert_task(sprint_id: str, task: dict) -> None:
    if not sprint_id:
        return
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO tasks(sprint_id,id,title,role_hint,assignee_roster,status,summary,code_files,agile)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sprint_id,id) DO UPDATE SET
                 title=excluded.title, role_hint=excluded.role_hint,
                 assignee_roster=excluded.assignee_roster, status=excluded.status,
                 summary=excluded.summary, code_files=excluded.code_files, agile=excluded.agile""",
            (
                sprint_id,
                str(task.get("id", "")),
                str(task.get("title", "")),
                str(task.get("role_hint", "")),
                task.get("assignee_roster"),
                str(task.get("status", "")),
                str(task.get("summary", "")),
                json.dumps(task.get("code_files", []), ensure_ascii=False),
                json.dumps({k: task[k] for k in PLAN_FIELDS if k in task}, ensure_ascii=False),
            ),
        )


def list_tasks(sprint_id: str) -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE sprint_id=? ORDER BY id", (sprint_id,)
        ).fetchall()
    return [_row(row) for row in rows]


# ------------------------------ 交接 ------------------------------

_HANDOFF_LISTS = ("input_refs", "output_refs", "claims", "open_questions", "acceptance_criteria")


def _handoff_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in _HANDOFF_LISTS:
        try:
            item[key] = json.loads(item[key] or "[]")
        except (ValueError, TypeError):
            item[key] = []
    return item


def create_handoff(sprint_id: str, *, task_id: str, from_roster: int, to_roster: int,
                   input_refs: list[str], output_refs: list[str], claims: list[str],
                   open_questions: list[str] | None = None,
                   acceptance_criteria: list[str] | None = None,
                   version: int = 1, status: str = "unchecked", reason: str = "") -> dict:
    if status not in ("ready", "accepted", "rejected", "blocked", "unchecked"):
        raise ValueError("无效交接状态")
    now = time.time()
    values = (sprint_id, task_id, from_roster, to_roster,
              *[json.dumps(x, ensure_ascii=False) for x in
                (input_refs, output_refs, claims, open_questions or [], acceptance_criteria or [])],
              status, max(1, int(version)), now, now, reason)
    with _lock, _conn() as conn:
        cursor = conn.execute(
            """INSERT INTO handoffs(sprint_id,task_id,from_roster,to_roster,input_refs,
               output_refs,claims,open_questions,acceptance_criteria,status,version,created_at,updated_at,reason)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        row = conn.execute("SELECT * FROM handoffs WHERE id=?", (cursor.lastrowid,)).fetchone()
    return _handoff_row(row)


def set_handoff_status(handoff_id: int, status: str, reason: str = "") -> dict | None:
    if status not in ("accepted", "rejected", "blocked", "unchecked"):
        raise ValueError("无效接收状态")
    with _lock, _conn() as conn:
        conn.execute("UPDATE handoffs SET status=?, reason=?, updated_at=? WHERE id=?",
                     (status, reason, time.time(), handoff_id))
        row = conn.execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
    return _handoff_row(row) if row else None


def get_handoff(handoff_id: int) -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
    return _handoff_row(row) if row else None


def list_handoffs(sprint_id: str) -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT * FROM handoffs WHERE sprint_id=? ORDER BY id", (sprint_id,)).fetchall()
    return [_handoff_row(row) for row in rows]


# ------------------------------- 产物 -------------------------------


def next_artifact_id(sprint_id: str) -> str:
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE sprint_id=?", (sprint_id,)
        ).fetchone()
    return f"{sprint_id}-A{int(row['n']) + 1}"


def add_artifact(sprint_id: str, artifact: dict) -> dict:
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO artifacts(id,sprint_id,kind,title,author_roster,task_id,content,file_name,
               created_at,degraded,degraded_reason,source,quality_status,regen,parent_id,version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                artifact["id"],
                sprint_id,
                artifact.get("kind", ""),
                artifact.get("title", ""),
                artifact.get("author_roster"),
                artifact.get("task_id"),
                artifact.get("content", ""),
                artifact.get("file_name"),
                artifact.get("created_at", time.time()),
                1 if artifact.get("degraded") else 0,
                artifact.get("degraded_reason", ""),
                artifact.get("source", "unknown"),
                artifact.get("quality_status", "unchecked"),
                json.dumps(artifact.get("regen"), ensure_ascii=False)
                if artifact.get("regen")
                else "",
                artifact.get("parent_id"),
                artifact.get("version", 1),
            ),
        )
    return artifact


def update_artifact(artifact_id: str, **fields) -> dict | None:
    allowed = {
        "title",
        "content",
        "file_name",
        "degraded",
        "degraded_reason",
        "source",
        "quality_status",
        "regen",
    }
    pairs = [(k, v) for k, v in fields.items() if k in allowed]
    if pairs:
        converted = []
        for key, value in pairs:
            if key == "regen":
                value = json.dumps(value, ensure_ascii=False) if value else ""
            elif key == "degraded":
                value = 1 if value else 0
            converted.append((key, value))
        sql = "UPDATE artifacts SET " + ", ".join(f"{k}=?" for k, _ in converted) + " WHERE id=?"
        with _lock, _conn() as conn:
            conn.execute(sql, [v for _, v in converted] + [artifact_id])
    return get_artifact(artifact_id)


def get_artifact(artifact_id: str) -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    return _row(row)


def list_artifacts(sprint_id: str) -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE sprint_id=? ORDER BY created_at", (sprint_id,)
        ).fetchall()
    return [_row(row) for row in rows]


def list_deliverables() -> list[dict]:
    """所有 Sprint 的最终交付物、独立生成成品与单飞产物，附带所属需求和日期。"""
    with _lock, _conn() as conn:
        rows = conn.execute(
            """SELECT a.*, s.requirement AS sprint_requirement,
                      s.started_at AS sprint_started_at
               FROM artifacts a LEFT JOIN sprints s ON s.id=a.sprint_id
               WHERE a.kind IN ('final', 'custom') OR a.sprint_id = 'solo'
               ORDER BY COALESCE(s.started_at, a.created_at) DESC,
                        a.title COLLATE NOCASE ASC, a.created_at DESC"""
        ).fetchall()
    return [_row(row) for row in rows]


def latest_artifact_version(sprint_id: str, artifact_id: str) -> int:
    """整条版本链上的最大版本号（修订可能链式挂载，需先溯源再统计）"""
    root_id = artifact_root(sprint_id, artifact_id)
    chain = artifact_versions(sprint_id, root_id)
    if not chain:
        return 1
    return max(int(row.get("version") or 1) for row in chain)


# ------------------------------- 事件 -------------------------------

# 只持久化对用户有意义的事件，动画类高频事件不入库
EVENT_PERSIST_TYPES = {
    "sprint_start", "sprint_done", "sprint_stopped", "sprint_error", "stage",
    "task_update", "artifact_new", "artifact_update", "plan_fallback", "requirement_contract", "handoff", "integration_issue", "repair_attempt",
    "desk_visit", "budget_exceeded", "sprint_paused", "sprint_resumed",
    "instruction_added",
}


def add_event(sprint_id: str, event: dict) -> None:
    """把事件落库，刷新/重启后通知中心与时间线可完整恢复"""
    if not sprint_id or event.get("type") not in EVENT_PERSIST_TYPES:
        return
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO events(sprint_id, ts, type, payload) VALUES(?,?,?,?)",
            (
                sprint_id,
                float(event.get("ts") or time.time()),
                str(event.get("type", "")),
                json.dumps(event, ensure_ascii=False),
            ),
        )


def list_events(sprint_id: str, limit: int = 300) -> list[dict]:
    if not sprint_id:
        return []
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT payload FROM events WHERE sprint_id=? ORDER BY ts DESC LIMIT ?",
            (sprint_id, limit),
        ).fetchall()
    items = []
    for row in reversed(rows):
        try:
            items.append(json.loads(row["payload"]))
        except json.JSONDecodeError:
            continue
    return items


def delete_sprint(sprint_id: str) -> list[str]:
    """删除一条 Sprint 的全部数据，返回其产物文件名列表（供调用方清理磁盘）"""
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT file_name FROM artifacts WHERE sprint_id=? AND file_name IS NOT NULL",
            (sprint_id,),
        ).fetchall()
        files = [row["file_name"] for row in rows if row["file_name"]]
        conn.execute("DELETE FROM artifacts WHERE sprint_id=?", (sprint_id,))
        conn.execute("DELETE FROM tasks WHERE sprint_id=?", (sprint_id,))
        conn.execute("DELETE FROM events WHERE sprint_id=?", (sprint_id,))
        conn.execute("DELETE FROM handoffs WHERE sprint_id=?", (sprint_id,))
        conn.execute("DELETE FROM sprints WHERE id=?", (sprint_id,))
    return files


def delete_artifact(artifact_id: str) -> str | None:
    """删除单条产物，返回关联文件名"""
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT file_name FROM artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        file_name = row["file_name"] if row and row["file_name"] else None
        conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
    return file_name


def artifact_root(sprint_id: str, artifact_id: str) -> str:
    """沿 parent_id 找到版本链真正的根（修订可能链式挂载：v3 的父级是 v2）"""
    seen: set[str] = set()
    current = get_artifact(artifact_id)
    if not current:
        return artifact_id
    while current.get("parent_id") and current["parent_id"] not in seen:
        seen.add(current["id"])
        parent = get_artifact(current["parent_id"])
        if not parent or parent.get("sprint_id") != sprint_id:
            break
        current = parent
    return current["id"]


def artifact_versions(sprint_id: str, root_id: str) -> list[dict]:
    """返回同一版本链（根产物 + 其直接/间接修订）的全部产物，按版本升序"""
    rows = list_artifacts(sprint_id)
    chain = [row for row in rows if row["id"] == root_id]
    frontier = {root_id}
    collected = {root_id}
    while True:
        children = [
            row
            for row in rows
            if row.get("parent_id") in frontier and row["id"] not in collected
        ]
        if not children:
            break
        for child in children:
            collected.add(child["id"])
        chain.extend(children)
        frontier = {child["id"] for child in children}
    return sorted(chain, key=lambda row: (row.get("version") or 1, row.get("created_at") or 0))


def search(keyword: str, limit: int = 8) -> dict:
    """全局搜索：历史需求 / 任务 / 产物 / 真实文件名"""
    q = f"%{keyword}%"
    with _lock, _conn() as conn:
        sprints = conn.execute(
            "SELECT id, requirement, status, started_at FROM sprints "
            "WHERE requirement LIKE ? ORDER BY started_at DESC LIMIT ?",
            (q, limit),
        ).fetchall()
        tasks = conn.execute(
            "SELECT sprint_id, id, title, status FROM tasks "
            "WHERE title LIKE ? OR summary LIKE ? ORDER BY rowid DESC LIMIT ?",
            (q, q, limit),
        ).fetchall()
        artifacts = conn.execute(
            "SELECT id, sprint_id, title, kind, file_name, created_at FROM artifacts "
            "WHERE title LIKE ? OR content LIKE ? OR file_name LIKE ? ORDER BY created_at DESC LIMIT ?",
            (q, q, q, limit),
        ).fetchall()
    return {
        "sprints": [dict(row) for row in sprints],
        "tasks": [dict(row) for row in tasks],
        "artifacts": [dict(row) for row in artifacts],
    }


# ----------------------------- 旧数据迁移 -----------------------------


def _legacy_sprint_name(artifact_count: int, started_at: float) -> str:
    """迁移 Sprint 的可读名称：带迁移日期与产物数量，不再是生硬的占位文案"""
    date = time.strftime("%Y-%m-%d", time.localtime(started_at or time.time()))
    return f"历史产物（{date} 迁移，含 {artifact_count} 项）"


def _fix_legacy_sprint_names() -> None:
    """存量旧命名「历史 Sprint（自动迁移）」一并改成更友好的名称"""
    old_name = "历史 Sprint（自动迁移）"
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT id, started_at FROM sprints WHERE requirement=?", (old_name,)
        ).fetchall()
        for row in rows:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM artifacts WHERE sprint_id=?", (row["id"],)
            ).fetchone()["n"]
            conn.execute(
                "UPDATE sprints SET requirement=? WHERE id=?",
                (_legacy_sprint_name(int(count), row["started_at"]), row["id"]),
            )


def migrate_legacy_artifacts() -> None:
    """首次启动时把旧的 artifacts_data.json 导入为一条历史 Sprint"""
    legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts_data.json")
    if not os.path.exists(legacy):
        _fix_legacy_sprint_names()
        return
    with _lock, _conn() as conn:
        exists = conn.execute("SELECT COUNT(*) AS n FROM sprints").fetchone()["n"]
    if exists:
        _fix_legacy_sprint_names()
        return
    try:
        with open(legacy, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not items:
        return
    started_at = time.time()
    sprint_id = f"S{int(started_at * 1000)}"
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO sprints(id,requirement,status,stage,started_at,finished_at) VALUES(?,?,?,?,?,?)",
            (
                sprint_id,
                _legacy_sprint_name(len(items), started_at),
                "completed",
                "已归档",
                started_at,
                started_at,
            ),
        )
    for index, item in enumerate(items, 1):
        item = dict(item)
        item["id"] = f"{sprint_id}-A{index}"
        item.setdefault("created_at", started_at)
        add_artifact(sprint_id, item)


migrate_legacy_artifacts()
