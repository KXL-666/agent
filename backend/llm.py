"""LLM 封装：仅支持成员级专属 API（智能体页设置窗口维护，agent_config.py 持久化）

没有全局兜底配置：成员没填专属 API Key 时直接回退内置 Mock 决策（离线可跑）。
成员配置里 base_url / model 留空时使用下面的默认值。
"""

import json
import os
import threading
import time
from collections.abc import Iterator

import httpx
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
# 动画节奏倍率：调低可加速整场演出（环境变量 SCENE_PACE 可覆盖）
SCENE_PACE = float(os.getenv("SCENE_PACE", "0.5") or 0.5)

# ---------------------------------------------------------------------------
# 回退标志：chat_json/chat_text 每次回退到 Mock 时记录，供工作流节点感知并提示前端
# ---------------------------------------------------------------------------
last_fallback: dict | None = None


def consume_fallback() -> dict | None:
    """读取并清除最近一次回退信息（{"roster","reason","ts"}）"""
    global last_fallback
    info = last_fallback
    last_fallback = None
    return info


def _mark_fallback(roster: int | None, reason: str) -> None:
    global last_fallback
    last_fallback = {"roster": roster, "reason": reason[:150], "ts": time.time()}


# ---------------------------------------------------------------------------
# 用量统计：按成员累计 tokens，持久化到 usage.json，供成本面板展示（费用为估算）
# ---------------------------------------------------------------------------
USAGE_FILE = os.path.join(os.path.dirname(__file__), "usage.json")

# 价格表：元 / 百万 tokens（输入价, 输出价），为公开刊例的约值，仅供估算
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "deepseek-chat": (2, 8),
    "deepseek-flash": (2, 8),
    "kimi-k2.6": (4, 16),
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


def _post(
    base_url: str,
    api_key: str,
    model: str,
    payload: dict,
    timeout: float,
    roster: int | None = None,
) -> str:
    """发起一次 chat completion，返回 content 文本。
    部分模型（如 Kimi K2.6）只接受固定 temperature，被拒时自动去掉该参数重试一次。
    响应里的 usage 会按成员累计到用量统计（roster 为 None 时不记录）。"""
    body = {"model": model, **payload}

    def _send(data: dict) -> httpx.Response:
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
        time.sleep(wait)
        resp = _send(body)
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage") or {}
    if roster is not None and usage:
        _record_usage(roster, model, usage)
    return data["choices"][0]["message"]["content"]


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
        chat_messages = [{"role": "system", "content": system}, *messages]
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
        return _post(
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
        ).strip()

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
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": 400,
        "stream": True,
    }
    emitted = False
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
                delta = (choices[0].get("delta") or {}).get("content") or ""
                if delta:
                    emitted = True
                    yield delta
    except Exception as error:  # noqa: BLE001 —— 流式失败时退回整段兜底
        print(f"[LLM] 流式对话失败(roster={roster})：{error}")
        _mark_fallback(roster, str(error))
        if meta is not None:
            meta["fallback_reason"] = str(error)[:120]
        if not emitted:
            yield fallback
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
                {"role": "system", "content": system},
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

    print(f"[LLM] 调用失败(roster={roster})，回退 Mock：{last_error}")
    _mark_fallback(roster, str(last_error))
    if meta is not None:
        meta["fallback_reason"] = str(last_error)[:120]
    return _fallback_value(fallback)
