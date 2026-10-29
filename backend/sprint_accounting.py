"""Per-run model usage survives crashes and excludes unrelated conversations."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading

import storage


@dataclass
class Scope:
    sprint_id: str
    failed: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)


current: ContextVar[Scope | None] = ContextVar("sprint_usage_scope", default=None)


def totals() -> dict | None:
    scope = current.get()
    return storage.get_sprint(scope.sprint_id) if scope else None


def check() -> None:
    scope = current.get()
    if scope and scope.failed.is_set():
        raise RuntimeError("用量持久化失败，已停止后续模型调用；请检查数据库后重试")
    if scope and scope.stopped.is_set():
        raise RuntimeError("本轮执行已结束，停止后续模型调用")


def record(tokens: int, cost: float) -> None:
    scope = current.get()
    if scope:
        try:
            storage.increment_sprint_usage(scope.sprint_id, tokens, cost)
        except Exception:
            scope.failed.set()
            raise RuntimeError("用量持久化失败，已停止后续模型调用") from None
