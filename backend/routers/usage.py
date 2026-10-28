"""用量统计与模型单价路由。

- GET /api/usage           各成员用量与估算成本
- GET /api/usage/export.csv 用量明细导出
- GET /api/pricing         当前单价表
- PUT /api/pricing         自定义单价
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import llm
from events import sprint_meta
from roles import roster_name

router = APIRouter(tags=["usage"])


@router.get("/api/usage")
async def get_usage():
    """各成员 LLM 调用用量与估算成本。"""
    report = llm.usage_report()
    for member in report["members"]:
        member["name"] = roster_name(member["roster"])
    baseline = sprint_meta.get("usage_baseline") or 0.0
    report["sprint_cost"] = max(0.0, round(llm.usage_total_cost() - baseline, 4))
    report["sprint_tokens"] = llm.usage_total_tokens()
    return report


@router.get("/api/usage/export.csv")
async def export_usage_csv():
    """用量明细导出为 CSV（带 BOM，Excel 直接打开不乱码）。"""
    report = llm.usage_report()
    lines = ["成员编号,成员姓名,模型,调用次数,输入tokens,输出tokens,估算花费(元)"]
    for member in report["members"]:
        lines.append(
            f"{member['roster']},{roster_name(member['roster'])},{member['model']},"
            f"{member['calls']},{member['prompt_tokens']},{member['completion_tokens']},"
            f"{member['cost']}"
        )
    lines.append(f"合计,,,,,,{report['total_cost']}")
    content = "﻿" + "\n".join(lines)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="usage.csv"'},
    )


@router.get("/api/pricing")
async def get_pricing():
    """当前生效的模型单价表（元/百万 tokens，含自定义覆盖）。"""
    return llm.pricing_view()


class PricingRequest(BaseModel):
    prices: dict


@router.put("/api/pricing")
async def put_pricing(req: PricingRequest):
    """自定义模型单价：{模型名: [输入价, 输出价]}；值为 null 时恢复内置价。"""
    try:
        return llm.update_pricing(req.prices)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail=str(error))
