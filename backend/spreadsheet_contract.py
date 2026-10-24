"""Deterministic source-to-formula mapping for explicit budget records."""

import re
from collections import Counter


_RECORD = re.compile(r"([^。；;\n：:，,]{2,35})[：:]\s*预算\s*([\d,]+(?:\.\d+)?)\s*元[，,]\s*实际\s*([\d,]+(?:\.\d+)?)\s*元")


def budget_records(source: str) -> list[dict]:
    result = []
    for match in _RECORD.finditer(source or ""):
        label = match.group(1).strip()
        # Attachment wrappers may precede the line; use the last logical label.
        label = re.split(r"[\n。；;]", label)[-1].strip()
        budget, actual = float(match.group(2).replace(",", "")), float(match.group(3).replace(",", ""))
        result.append({"project": label, "budget": budget,
                       "actual": actual,
                       "source_quote": match.group(0)})
    return result


def budget_workbook(source: str) -> list[dict] | None:
    if not all(word in source for word in ("预算", "实际", "差额", "执行率")):
        return None
    records = budget_records(source)
    if not records:
        return None
    rows, notes = [], {}
    for index, item in enumerate(records, 2):
        rows.append([item["project"], item["budget"], item["actual"],
                     f"=B{index}-C{index}", f"=IFERROR(C{index}/B{index},0)"])
        notes[f"B{index}"] = item["source_quote"]
        notes[f"C{index}"] = item["source_quote"]
    last = len(records) + 1
    total = last + 1
    rows.append(["合计", f"=SUM(B2:B{last})", f"=SUM(C2:C{last})",
                 f"=B{total}-C{total}", f"=IFERROR(C{total}/B{total},0)"])
    return [{"name": "预算执行", "headers": ["项目", "预算（元）", "实际（元）", "差额（元）", "执行率"],
             "rows": rows, "column_types": {"预算（元）": "currency", "实际（元）": "currency",
                                          "差额（元）": "currency", "执行率": "percent"},
             "formats": {"执行率": "0.0%"}, "notes": notes,
             "charts": [{"type": "bar", "title": "各项目预算与实际", "category_column": 1,
                         "data_columns": [2, 3], "end_row": last}]}]


def audit_budget_records(workbook, source: str) -> dict | None:
    """Compare source records with actual workbook cells, including amounts."""
    expected = budget_records(source)
    if not expected:
        return None
    for sheet in workbook:
        headers = [str(cell.value or "") for cell in sheet[1]]
        columns = [next((index for index, name in enumerate(headers, 1) if keyword in name), None)
                   for keyword in ("项目", "预算", "实际")]
        if any(column is None for column in columns):
            continue
        observed = []
        for row in sheet.iter_rows(min_row=2, values_only=True):
            label = str(row[columns[0] - 1] or "").strip()
            if not label or label in ("合计", "总计", "汇总"):
                continue
            try:
                observed.append((label, float(row[columns[1] - 1]), float(row[columns[2] - 1])))
            except (TypeError, ValueError):
                observed.append((label, None, None))
        wanted = Counter((item["project"], item["budget"], item["actual"]) for item in expected)
        found = Counter(observed)
        missing = list((wanted - found).elements())
        extra = list((found - wanted).elements())
        return {"ok": not missing and not extra, "expected": len(expected),
                "observed": len(observed), "missing": missing, "extra": extra,
                "sheet": sheet.title}
    return {"ok": None, "expected": len(expected), "observed": 0,
            "missing": [], "extra": [], "sheet": None}
