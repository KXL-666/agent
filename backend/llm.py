"""LLM 封装：仅支持成员级专属 API（智能体页设置窗口维护，agent_config.py 持久化）

没有全局兜底配置：成员没填专属 API Key 时直接回退内置 Mock 决策（离线可跑）。
成员配置里 base_url / model 留空时使用下面的默认值。
"""

import json
import os
import threading
import time
from contextvars import ContextVar
from collections.abc import Iterator

import httpx
from dotenv import load_dotenv
from skills import augment_system

load_dotenv()
_call_guard = ContextVar("office_llm_call_guard", default=None)
_provider_locks: dict[str, threading.Lock] = {}
_provider_last_started: dict[str, float] = {}
_provider_lock_guard = threading.Lock()


def set_call_guard(callback):
    return _call_guard.set(callback)


def reset_call_guard(token):
    _call_guard.reset(token)

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
# 动画节奏倍率：调低可加速整场演出（环境变量 SCENE_PACE 可覆盖）
SCENE_PACE = float(os.getenv("SCENE_PACE", "0.5") or 0.5)

# ---------------------------------------------------------------------------
# 回退标志：chat_json/chat_text 每次回退到 Mock 时记录，供工作流节点感知并提示前端
# ---------------------------------------------------------------------------
last_fallback: dict | None = None
_agent_failures: dict[int, dict] = {}
_agent_health_lock = threading.Lock()


def consume_fallback() -> dict | None:
    """读取并清除最近一次回退信息（{"roster","reason","ts"}）"""
    global last_fallback
    info = last_fallback
    last_fallback = None
    return info


def _mark_fallback(roster: int | None, reason: str) -> None:
    global last_fallback
    last_fallback = {"roster": roster, "reason": reason[:150], "ts": time.time()}
    if roster is not None:
        with _agent_health_lock:
            _agent_failures[roster] = {"reason": reason[:150], "ts": time.time()}


def agent_failure(roster: int) -> dict | None:
    """返回最近 15 分钟内该成员的模型调用失败，供智能体状态卡展示。"""
    with _agent_health_lock:
        failure = _agent_failures.get(roster)
        if failure and time.time() - failure["ts"] <= 900:
            return dict(failure)
        _agent_failures.pop(roster, None)
        return None


def _mark_success(roster: int | None, api_key: str) -> None:
    if roster is None:
        return
    with _agent_health_lock:
        _agent_failures.pop(roster, None)
    try:
        from agent_config import resolve_llm
        if api_key == resolve_llm(roster).get("api_key"):
            _switched_to_backup.discard(roster)
    except Exception:  # noqa: BLE001 —— 健康状态更新不能影响模型调用
        pass


# ---------------------------------------------------------------------------
# 用量统计：按成员累计 tokens，持久化到 usage.json，供成本面板展示（费用为估算）
# ---------------------------------------------------------------------------
USAGE_FILE = os.path.join(os.path.dirname(__file__), "usage.json")

# 价格表：元 / 百万 tokens（输入价, 输出价），为公开刊例的约值，仅供估算
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "deepseek-chat": (2, 8),
    "deepseek-flash": (2, 8),
    "kimi-k2.6": (6.5, 27),
    "glm-4.7-flash": (0, 0),
    "glm-4.5-air": (0.8, 2),
}
DEFAULT_PRICE = (4.0, 12.0)

# 自定义模型单价：pricing.json 持久化，覆盖/补充内置价格表（元/百万 tokens）
PRICING_FILE = os.path.join(os.path.dirname(__file__), "pricing.json")
_BASE_PRICE_TABLE = dict(PRICE_TABLE)


def _load_pricing() -> None:
    if not os.path.exists(PRICING_FILE):
        return
    try:
        with open(PRICING_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for model, pair in data.items():
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    PRICE_TABLE[str(model).lower()] = (float(pair[0]), float(pair[1]))
    except Exception as error:  # noqa: BLE001
        print(f"[Pricing] 读取自定义单价失败：{error}")


def _save_pricing() -> None:
    # 只保存与内置表不一致的覆盖项，内置默认值无需落盘
    overrides = {
        model: [pair[0], pair[1]]
        for model, pair in PRICE_TABLE.items()
        if _BASE_PRICE_TABLE.get(model) != pair
    }
    try:
        with open(PRICING_FILE, "w", encoding="utf-8") as f:
            json.dump(overrides, f, ensure_ascii=False, indent=2)
    except Exception as error:  # noqa: BLE001
        print(f"[Pricing] 写入自定义单价失败：{error}")


_load_pricing()


def pricing_view() -> dict:
    """当前生效的完整价格表（含自定义覆盖）"""
    return {
        "prices": {m: [p[0], p[1]] for m, p in sorted(PRICE_TABLE.items())},
        "default": [DEFAULT_PRICE[0], DEFAULT_PRICE[1]],
        "builtin": sorted(_BASE_PRICE_TABLE.keys()),
    }


def update_pricing(prices: dict) -> dict:
    """自定义模型单价：{模型名: [输入价, 输出价]} upsert；值为 null 时恢复内置/删除覆盖"""
    for model, pair in (prices or {}).items():
        key = str(model).strip().lower()
        if not key:
            continue
        if pair is None:
            if key in _BASE_PRICE_TABLE:
                PRICE_TABLE[key] = _BASE_PRICE_TABLE[key]
            else:
                PRICE_TABLE.pop(key, None)
            continue
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"模型 {model} 的单价格式应为 [输入价, 输出价]")
        in_price, out_price = float(pair[0]), float(pair[1])
        if in_price < 0 or out_price < 0:
            raise ValueError("单价不能为负数")
        PRICE_TABLE[key] = (in_price, out_price)
    _save_pricing()
    return pricing_view()

_usage_lock = threading.Lock()
_usage: dict[str, dict] = {}


def _load_usage() -> None:
    if not os.path.exists(USAGE_FILE):
        return
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _usage.update(data)
    except Exception as error:  # noqa: BLE001
        print(f"[Usage] 读取持久化文件失败：{error}")


def _save_usage() -> None:
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(_usage, f, ensure_ascii=False, indent=2)
    except Exception as error:  # noqa: BLE001
        print(f"[Usage] 写入持久化文件失败：{error}")


_load_usage()


def _record_usage(roster: int, model: str, usage: dict) -> None:
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    if not (prompt_tokens or completion_tokens):
        return
    with _usage_lock:
        entry = _usage.setdefault(
            str(roster), {"model": model, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        entry["model"] = model
        entry["calls"] += 1
        entry["prompt_tokens"] += prompt_tokens
        entry["completion_tokens"] += completion_tokens
        _save_usage()


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按刊例约值估算费用（元），未命中价格表时用默认价"""
    price = PRICE_TABLE.get(model.lower(), DEFAULT_PRICE)
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000


def usage_report() -> dict:
    """返回每位成员的累计用量与估算成本（元）"""
    with _usage_lock:
        snapshot = {k: dict(v) for k, v in _usage.items()}
    members = []
    total_cost = 0.0
    for roster_key, entry in sorted(snapshot.items(), key=lambda kv: int(kv[0])):
        cost = estimate_cost(entry["model"], entry["prompt_tokens"], entry["completion_tokens"])
        total_cost += cost
        members.append(
            {
                "roster": int(roster_key),
                "model": entry["model"],
                "calls": entry["calls"],
                "prompt_tokens": entry["prompt_tokens"],
                "completion_tokens": entry["completion_tokens"],
                "cost": round(cost, 4),
            }
        )
    return {"members": members, "total_cost": round(total_cost, 4), "estimated": True}


def usage_total_cost() -> float:
    """全部成员累计花费（元），用于计算单轮 Sprint 的增量花费"""
    with _usage_lock:
        snapshot = {k: dict(v) for k, v in _usage.items()}
    return round(
        sum(
            estimate_cost(entry["model"], entry["prompt_tokens"], entry["completion_tokens"])
            for entry in snapshot.values()
        ),
        4,
    )


def usage_total_tokens() -> int:
    with _usage_lock:
        snapshot = {k: dict(v) for k, v in _usage.items()}
    return sum(
        entry["prompt_tokens"] + entry["completion_tokens"] for entry in snapshot.values()
    )


def usage_total_calls() -> int:
    """全部成员累计调用次数，用于计算单轮 Sprint 的调用增量"""
    with _usage_lock:
        snapshot = {k: dict(v) for k, v in _usage.items()}
    return sum(int(entry.get("calls", 0)) for entry in snapshot.values())


def _resolve(roster: int | None) -> tuple[str, str, str] | None:
    """返回 (base_url, api_key, model)；成员无专属 key 或未传 roster 时返回 None"""
    if roster is None:
        return None
    try:
        from agent_config import resolve_llm

        custom = resolve_llm(roster)
    except Exception as error:  # noqa: BLE001 —— 配置读取异常不阻断主流程
        print(f"[LLM] 读取成员 {roster} 配置失败：{error}")
        return None
    if not custom or not custom.get("api_key"):
        return None
    return (
        (custom.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        custom["api_key"],
        custom.get("model") or DEFAULT_MODEL,
    )


def _resolve_backup(roster: int | None) -> tuple[str, str, str] | None:
    """备用 (base_url, api_key, model)：主 API 故障时自动切换的兜底通道"""
    if roster is None:
        return None
    try:
        from agent_config import resolve_llm_backup

        custom = resolve_llm_backup(roster)
    except Exception as error:  # noqa: BLE001
        print(f"[LLM] 读取成员 {roster} 备用配置失败：{error}")
        return None
    if not custom or not custom.get("api_key"):
        return None
    return (
        (custom.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        custom["api_key"],
        custom.get("model") or DEFAULT_MODEL,
    )


def _cross_provider_fallbacks(
    roster: int | None, preferred_roster: int | None = None
) -> list[tuple[int, tuple[str, str, str]]]:
    """Return configured fallback channels in ascending estimated token cost.

    A failed key must not fall back to another key at the same provider: account
    balance and rate limits are commonly shared across keys. The explicitly
    requested fallback remains a tie-breaker among equally priced providers.
    """
    primary = _resolve(roster) if roster is not None else None
    primary_host = ""
    if primary:
        from urllib.parse import urlsplit

        primary_host = urlsplit(primary[0]).netloc.lower()

    candidates: list[tuple[float, int, tuple[str, str, str]]] = []
    seen_hosts: set[str] = set()
    for candidate_roster in range(1, 7):
        if candidate_roster == roster:
            continue
        resolved = _resolve(candidate_roster)
        if not resolved:
            continue
        from urllib.parse import urlsplit

        host = urlsplit(resolved[0]).netloc.lower()
        if not host or host == primary_host or host in seen_hosts:
            continue
        seen_hosts.add(host)
        input_price, output_price = PRICE_TABLE.get(resolved[2].lower(), DEFAULT_PRICE)
        unit_cost = input_price + output_price
        # The preferred roster only breaks ties; the cheapest configured
        # provider is always attempted first.
        tie_break = 0 if candidate_roster == preferred_roster else 1
        candidates.append((unit_cost, tie_break, (candidate_roster, resolved)))

    candidates.sort(key=lambda item: (item[0], item[1], item[2][0]))
    return [item[2] for item in candidates]


# 本次进程内已切换到备用通道的成员（供配置页标注「备用生效中」）
_switched_to_backup: set[int] = set()


def backup_in_use(roster: int) -> bool:
    return roster in _switched_to_backup


def _with_images(text: str, images: list[str] | None):
    """有图片时把 user content 组装成 OpenAI 多模态数组，否则原样返回文本"""
    if not images:
        return text
    parts: list[dict] = [{"type": "text", "text": text}]
    for url in images[:4]:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def effective_model(roster: int) -> str | None:
    resolved = _resolve(roster)
    return resolved[2] if resolved else None


def llm_available(roster: int | None = None) -> bool:
    """指定成员是否配置了专属 API；roster=None 时表示「是否有任意成员已配置」"""
    if roster is not None:
        return _resolve(roster) is not None
    return any(_resolve(no) is not None for no in range(1, 7))


def ping(base_url: str, api_key: str, model: str, timeout: float = 20) -> str:
    """测试连接：直接发起一次最小对话，失败抛异常（供配置校验接口使用）"""
    try:
        return _post(
            base_url,
            api_key,
            model,
            {
                "messages": [{"role": "user", "content": "只回复两个字：正常"}],
                "temperature": 0,
                "max_tokens": 32,
            },
            timeout,
        )
    except httpx.HTTPStatusError as error:
        # 把服务商返回的错误原文带上（如 Kimi 的 invalid model / max_tokens 限制）
        body = (error.response.text or "").strip().replace("\n", " ")[:300]
        raise RuntimeError(
            f"HTTP {error.response.status_code}：{body or error.response.reason_phrase}"
        ) from error


def _limit_thinking(model: str, body: dict) -> dict:
    """这三个低价模型默认会产生付费思考 token；常规任务关闭思考以控费。"""
    if model.lower() in {"deepseek-flash", "kimi-k2.6", "glm-4.7-flash"}:
        body["thinking"] = {"type": "disabled"}
    return body


def _post(
    base_url: str,
    api_key: str,
    model: str,
    payload: dict,
    timeout: float,
    roster: int | None = None,
    tool_mode: bool = False,
) -> str:
    """Serialize requests to each provider to avoid shared-key RPM bursts."""
    from urllib.parse import urlsplit
    provider = urlsplit(base_url).netloc.lower() or base_url.lower()
    with _provider_lock_guard:
        lock = _provider_locks.setdefault(provider, threading.Lock())
    with lock:
        previous = _provider_last_started.get(provider, 0.0)
        delay = 1.5 - (time.monotonic() - previous)
        if delay > 0:
            time.sleep(delay)
        _provider_last_started[provider] = time.monotonic()
        return _post_request(base_url, api_key, model, payload, timeout, roster, tool_mode)


def _post_request(
    base_url: str,
    api_key: str,
    model: str,
    payload: dict,
    timeout: float,
    roster: int | None = None,
    tool_mode: bool = False,
) -> str:
    """发起一次 chat completion，返回 content 文本。
    部分模型（如 Kimi K2.6）只接受固定 temperature，被拒时自动去掉该参数重试一次。
    响应里的 usage 会按成员累计到用量统计（roster 为 None 时不记录）。"""
    body = _limit_thinking(model, {"model": model, **payload})
    if tool_mode and roster is not None:
        from skill_agent_tools import TOOL_NOTE, TOOL_SPECS
        body["tools"] = TOOL_SPECS
        body["tool_choice"] = "auto"
        body["messages"] = [dict(message) for message in body["messages"]]
        original_system = body["messages"][0]["content"]
        body["messages"][0]["content"] += "\n\n" + TOOL_NOTE

    def _send(data: dict) -> httpx.Response:
        guard = _call_guard.get()
        if guard is not None:
            guard()
        return httpx.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=data,
            timeout=timeout,
        )

    resp = _send(body)
    if resp.status_code == 400 and "tools" in body and any(
        word in resp.text.lower() for word in ("tool", "function", "unsupported")
    ):
        # 部分兼容 API 不支持 tool_calls；普通对话仍可继续，不能假称执行过工具。
        body.pop("tools", None)
        body.pop("tool_choice", None)
        body["messages"][0]["content"] = original_system
        tool_mode = False
        resp = _send(body)
    if (
        resp.status_code == 400
        and "temperature" in body
        and "temperature" in resp.text.lower()
    ):
        body = {k: v for k, v in body.items() if k != "temperature"}
        resp = _send(body)
    # 429 限流：指数退避重试，大部分限流可自愈
    for wait in (3, 8):
        if resp.status_code != 429:
            break
        retry_after = resp.headers.get("Retry-After", "")
        try:
            delay = min(30.0, max(0.0, float(retry_after))) if retry_after else wait
        except ValueError:
            delay = wait
        time.sleep(delay)
        resp = _send(body)
    resp.raise_for_status()
    for turn in range(5):
        data = resp.json()
        _mark_success(roster, api_key)
        usage = data.get("usage") or {}
        if roster is not None and usage:
            _record_usage(roster, model, usage)
        message = data["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls or not tool_mode or roster is None:
            return message.get("content") or ""
        if turn == 4:
            return "技能工具调用次数已达上限，请缩小本次任务范围。"
        from skill_agent_tools import execute
        body["messages"].append({**message, "tool_calls": calls[:3]})
        for call in calls[:3]:
            function = call.get("function") or {}
            outcome = execute(function.get("name", ""), function.get("arguments", "{}"), roster)
            body["messages"].append({"role": "tool", "tool_call_id": call.get("id", ""),
                                     "content": json.dumps(outcome, ensure_ascii=False)[:14000]})
        resp = _send(body)
        resp.raise_for_status()
    return ""


def _fallback_value(fallback):
    return fallback() if callable(fallback) else fallback


def chat_text(
    system: str,
    messages: list[dict],
    fallback,
    temperature: float = 0.8,
    max_tokens: int = 200,
    timeout: float = 60,
    roster: int | None = None,
    meta: dict | None = None,
    images: list[str] | None = None,
    tool_mode: bool = True,
    fallback_roster: int | None = None,
):
    """多轮文本对话（角色聊天用）；失败或未配置时回退 fallback（值或无参函数）

    meta 传入一个 dict 时，会写入 fallback_reason / model / backup_used，
    供调用方标注「兜底生成」「备用通道」。images 为图片 data URL 列表（视觉模型）。"""
    resolved = _resolve(roster)
    if resolved is None:
        _mark_fallback(roster, "该成员未配置专属 API Key")
        if meta is not None:
            meta["fallback_reason"] = "该成员未配置专属 API Key"
        return _fallback_value(fallback)
    base_url, api_key, model = resolved

    def _attempt(url: str, key: str, mdl: str, imgs: list[str] | None) -> str:
        chat_messages = [{"role": "system", "content": augment_system(system, roster, meta)}, *messages]
        if imgs:
            # 图片挂到最后一条 user 消息上（无则补一条）
            for i in range(len(chat_messages) - 1, -1, -1):
                if chat_messages[i].get("role") == "user":
                    chat_messages[i] = {
                        **chat_messages[i],
                        "content": _with_images(str(chat_messages[i].get("content", "")), imgs),
                    }
                    break
            else:
                chat_messages.append({"role": "user", "content": _with_images("", imgs)})
        result = _post(
            url,
            key,
            mdl,
            {
                "messages": chat_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout,
            roster,
            tool_mode=tool_mode,
        ).strip()
        if not result:
            raise ValueError("模型返回空文本")
        return result

    try:
        result = _attempt(base_url, api_key, model, images)
        if meta is not None:
            meta["model"] = model
        return result
    except Exception as error:  # noqa: BLE001
        print(f"[LLM] chat 调用失败(roster={roster})：{error}")
        # 视觉参数可能不被该模型支持：去图重试一次
        if images:
            try:
                result = _attempt(base_url, api_key, model, None)
                if meta is not None:
                    meta["model"] = model
                    meta["images_dropped"] = True
                return result
            except Exception as error2:  # noqa: BLE001
                print(f"[LLM] 去图重试仍失败(roster={roster})：{error2}")
                error = error2
        # 主通道故障：配置了备用 API 时自动切换重试一次（不带图，最大化成功率）
        backup = _resolve_backup(roster)
        if backup:
            try:
                result = _attempt(backup[0], backup[1], backup[2], None)
                if roster is not None:
                    _switched_to_backup.add(roster)
                print(f"[LLM] 成员 {roster} 已切换备用 API（{backup[2]}）")
                if meta is not None:
                    meta["backup_used"] = True
                    meta["model"] = backup[2]
                return result
            except Exception as error3:  # noqa: BLE001
                print(f"[LLM] 备用 API 也失败(roster={roster})：{error3}")
                error = error3
        # Cross-role failover preserves this agent's prompt and sends usage to
        # the cheapest configured provider, skipping the failed provider host.
        for alternate_roster, alternate in _cross_provider_fallbacks(roster, fallback_roster):
            try:
                alternate_messages = [{"role": "system", "content": augment_system(system, roster, meta)}, *messages]
                result = _post(
                    alternate[0], alternate[1], alternate[2],
                    {"messages": alternate_messages, "temperature": temperature, "max_tokens": max_tokens},
                    timeout, alternate_roster, tool_mode=tool_mode,
                ).strip()
                if meta is not None:
                    meta["model"] = alternate[2]
                    meta["fallback_roster"] = alternate_roster
                    meta["primary_model"] = model
                print(f"[LLM] 成员 {roster} 主通道不可用，已用成员 {alternate_roster} 的模型 {alternate[2]} 回退")
                return result
            except Exception as alternate_error:  # noqa: BLE001 —— 回退失败后如实返回原兜底
                print(f"[LLM] 跨成员回退失败(roster={roster}, fallback={alternate_roster})：{alternate_error}")
                error = alternate_error
        _mark_fallback(roster, str(error))
        if meta is not None:
            meta["fallback_reason"] = str(error)[:120]
        return _fallback_value(fallback)


def stream_chat(
    system: str,
    messages: list[dict],
    roster: int,
    fallback: str,
    meta: dict | None = None,
) -> Iterator[str]:
    """流式对话：逐段 yield 模型输出，前端可边生成边显示并随时中断。

    未配置 API/网络失败且尚未输出任何内容时，回退整段 fallback（并记录原因）。"""
    resolved = _resolve(roster)
    if resolved is None:
        _mark_fallback(roster, "该成员未配置专属 API Key")
        if meta is not None:
            meta["fallback_reason"] = "该成员未配置专属 API Key"
        yield fallback
        return
    base_url, api_key, model = resolved
    from skill_agent_tools import TOOL_NOTE, TOOL_SPECS, execute
    base_messages = [{"role": "system", "content": augment_system(system, roster, meta)}, *messages]
    payload = _limit_thinking(model, {
        "model": model,
        "messages": [{**base_messages[0], "content": base_messages[0]["content"] + "\n\n" + TOOL_NOTE}, *base_messages[1:]],
        "max_tokens": 1600,
        "stream": True,
        "tools": TOOL_SPECS,
        "tool_choice": "auto",
    })
    emitted = False
    calls: dict[int, dict] = {}
    try:
        with httpx.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content") or ""
                if content:
                    emitted = True
                    yield content
                for part in delta.get("tool_calls") or []:
                    index = int(part.get("index", 0))
                    call = calls.setdefault(index, {"id": "", "type": "function",
                                                    "function": {"name": "", "arguments": ""}})
                    if part.get("id"):
                        call["id"] = part["id"]
                    function = part.get("function") or {}
                    call["function"]["name"] += function.get("name") or ""
                    call["function"]["arguments"] += function.get("arguments") or ""
    except Exception as error:  # noqa: BLE001 —— 流式失败时退回整段兜底
        print(f"[LLM] 流式对话失败(roster={roster})：{error}")
        if (not emitted and isinstance(error, httpx.HTTPStatusError)
                and error.response.status_code == 400
                and any(word in error.response.text.lower() for word in ("tool", "function", "unsupported"))):
            try:
                yield _post(base_url, api_key, model,
                            {"messages": base_messages, "max_tokens": 1600}, 120, roster)
                return
            except Exception as plain_error:  # noqa: BLE001
                error = plain_error
        _mark_fallback(roster, str(error))
        if meta is not None:
            meta["fallback_reason"] = str(error)[:120]
        if not emitted:
            yield fallback
        return
    if calls:
        tool_calls = [calls[index] for index in sorted(calls)[:3]]
        followup = [*base_messages, {"role": "assistant", "content": None, "tool_calls": tool_calls}]
        for call in tool_calls:
            function = call["function"]
            outcome = execute(function["name"], function["arguments"], roster)
            followup.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(outcome, ensure_ascii=False)[:14000]})
        try:
            answer = _post(base_url, api_key, model, {"messages": followup, "max_tokens": 1600},
                           120, roster, tool_mode=True)
            if answer:
                yield answer
            return
        except Exception as error:  # noqa: BLE001
            if meta is not None:
                meta["fallback_reason"] = str(error)[:120]
            yield "技能工具已尝试执行，但结果整理失败：" + str(error)[:120]
            return
    if not emitted:
        _mark_fallback(roster, "模型未返回内容")
        if meta is not None:
            meta["fallback_reason"] = "模型未返回内容"
        yield fallback


def chat_json(
    system: str,
    user: str,
    fallback,
    temperature: float = 0.6,
    max_tokens: int = 500,
    timeout: float = 60,
    roster: int | None = None,
    meta: dict | None = None,
    images: list[str] | None = None,
    vision_fallback_roster: int | None = None,
    fallback_roster: int | None = None,
):
    """请求 LLM 返回 JSON 对象；失败或未配置时回退到 fallback（值或无参函数）

    JSON 解析失败（截断/格式错误）时自动重试一次：追加格式提醒并加倍 max_tokens。
    硬失败收尾顺序：去图重试（如带过图）→ 备用 API → 回退 Mock。"""
    resolved = _resolve(roster)
    if resolved is None:
        _mark_fallback(roster, "该成员未配置专属 API Key")
        if meta is not None:
            meta["fallback_reason"] = "该成员未配置专属 API Key"
        return _fallback_value(fallback)
    base_url, api_key, model = resolved

    def _payload(url_model: str, imgs: list[str] | None, hint: str, tokens: int) -> dict:
        return {
            "messages": [
                {"role": "system", "content": augment_system(system, roster, meta)},
                {"role": "user", "content": _with_images(user + hint, imgs)},
            ],
            "temperature": temperature,
            "max_tokens": tokens,
            "response_format": {"type": "json_object"},
        }

    last_error: Exception | None = None
    for attempt in range(2):
        hint = (
            ""
            if attempt == 0
            else "\n\n注意：只输出一个完整合法的 JSON 对象，不要输出任何其他文字。"
        )
        try:
            content = _post(
                base_url,
                api_key,
                model,
                _payload(model, images, hint, max_tokens if attempt == 0 else max_tokens * 2),
                timeout,
                roster,
                tool_mode=True,
            )
            parsed = json.loads(content)
            if meta is not None:
                meta["model"] = model
            return parsed
        except json.JSONDecodeError as error:
            # 输出被截断或格式错误：值得重试一次
            last_error = error
            print(f"[LLM] JSON 解析失败(roster={roster})，第 {attempt + 1} 次：{error}")
            continue
        except Exception as error:  # noqa: BLE001 —— 网络/鉴权类错误重试无意义
            last_error = error
            break

    # 视觉回退：当前成员模型不支持图片时，允许调用已配置的低价视觉模型，
    # 仍使用原角色提示词与工具身份；调用用量计入当前审核角色，并在 meta 标记实际模型。
    if images and vision_fallback_roster is not None:
        vision = _resolve(vision_fallback_roster)
        if vision:
            try:
                content = _post(
                    vision[0], vision[1], vision[2],
                    _payload(vision[2], images, "", max_tokens),
                    timeout, roster, tool_mode=True,
                )
                parsed = json.loads(content)
                if meta is not None:
                    meta["model"] = vision[2]
                    meta["vision_fallback_roster"] = vision_fallback_roster
                return parsed
            except Exception as error:  # noqa: BLE001 —— 视觉回退失败时保留原有安全降级
                last_error = error
                print(f"[LLM] 视觉兼容回退失败(roster={roster}, provider_roster={vision_fallback_roster})：{error}")

    # 收尾 1：带过图时去图重试一次（部分模型不支持视觉参数）
    if images:
        try:
            content = _post(
                base_url,
                api_key,
                model,
                _payload(model, None, "", max_tokens),
                timeout,
                roster,
                tool_mode=True,
            )
            parsed = json.loads(content)
            if meta is not None:
                meta["model"] = model
                meta["images_dropped"] = True
            return parsed
        except Exception as error:  # noqa: BLE001
            last_error = error
            print(f"[LLM] 去图重试失败(roster={roster})：{error}")

    # 收尾 2：配置了备用 API 时自动切换重试一次（不带图，最大化成功率）
    backup = _resolve_backup(roster)
    if backup:
        try:
            content = _post(
                backup[0],
                backup[1],
                backup[2],
                _payload(backup[2], None, "", max_tokens),
                timeout,
                roster,
                tool_mode=True,
            )
            parsed = json.loads(content)
            if roster is not None:
                _switched_to_backup.add(roster)
            print(f"[LLM] 成员 {roster} 已切换备用 API（{backup[2]}）")
            if meta is not None:
                meta["backup_used"] = True
                meta["model"] = backup[2]
            return parsed
        except Exception as error:  # noqa: BLE001
            last_error = error
            print(f"[LLM] 备用 API 也失败(roster={roster})：{error}")

    # Cheapest configured cross-role provider, while retaining this role's
    # system prompt and attributing usage to the provider that answered.
    for alternate_roster, alternate in _cross_provider_fallbacks(roster, fallback_roster):
        try:
            content = _post(
                alternate[0], alternate[1], alternate[2],
                _payload(alternate[2], images, "", max_tokens), timeout,
                alternate_roster, tool_mode=True,
            )
            parsed = json.loads(content)
            if meta is not None:
                meta["model"] = alternate[2]
                meta["primary_model"] = model
                meta["fallback_roster"] = alternate_roster
            print(f"[LLM] 成员 {roster} 主通道不可用，已用成员 {alternate_roster} 的模型 {alternate[2]} 回退")
            return parsed
        except Exception as error:  # noqa: BLE001 —— 所有真实提供商失败后才允许 Mock 收尾
            last_error = error
            print(f"[LLM] 跨成员 JSON 回退失败(roster={roster}, fallback={alternate_roster})：{error}")

    print(f"[LLM] 调用失败(roster={roster})，回退 Mock：{last_error}")
    _mark_fallback(roster, str(last_error))
    if meta is not None:
        meta["fallback_reason"] = str(last_error)[:120]
    return _fallback_value(fallback)
