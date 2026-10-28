"""LLM 用量统计与模型单价。

从 llm.py 拆分出来，专注两件事：
- 按成员累计 tokens 调用次数（持久化到 usage.json），供成本面板展示
- 维护模型单价表（内置 + pricing.json 自定义覆盖），用于估算花费

LLM 调用层（llm.py）在每次成功响应后调用 record_usage() 记账。
"""

import json
import os
import threading

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


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按刊例约值估算费用（元），未命中价格表时用默认价"""
    price = PRICE_TABLE.get(model.lower(), DEFAULT_PRICE)
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000


def record_usage(roster: int, model: str, usage: dict) -> None:
    """记录一次 LLM 调用的 token 用量。

    通过 `llm._save_usage` 落盘（而非本模块内的 _save_usage），
    这样测试里 `patch.object(llm, "_save_usage")` 才能真正拦截写入。
    """
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    if not (prompt_tokens or completion_tokens):
        return
    with _usage_lock:
        entry = _usage.setdefault(
            str(roster), {"model": model, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                          "estimated_cost": 0.0}
        )
        if "estimated_cost" not in entry:
            entry["estimated_cost"] = estimate_cost(
                entry.get("model", model), int(entry.get("prompt_tokens", 0)),
                int(entry.get("completion_tokens", 0)),
            )
        entry["estimated_cost"] += estimate_cost(model, prompt_tokens, completion_tokens)
        entry["model"] = model
        entry["calls"] += 1
        entry["prompt_tokens"] += prompt_tokens
        entry["completion_tokens"] += completion_tokens
        # 通过 llm 模块间接调用，使 patch.object(llm, "_save_usage") 生效
        import llm
        llm._save_usage()


_load_usage()


def usage_report() -> dict:
    """返回每位成员的累计用量与估算成本（元）"""
    with _usage_lock:
        snapshot = {k: dict(v) for k, v in _usage.items()}
    members = []
    total_cost = 0.0
    for roster_key, entry in sorted(snapshot.items(), key=lambda kv: int(kv[0])):
        cost = float(entry.get("estimated_cost", estimate_cost(
            entry["model"], entry["prompt_tokens"], entry["completion_tokens"]
        )))
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
            float(entry.get("estimated_cost", estimate_cost(
                entry["model"], entry["prompt_tokens"], entry["completion_tokens"]
            )))
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
