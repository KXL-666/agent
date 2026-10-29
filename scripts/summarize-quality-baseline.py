"""Offline summary of saved baseline runs; never starts tasks or calls a model."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
from statistics import mean


def outcome(run: dict) -> str:
    finals = [item for item in run.get("artifacts", []) if item.get("kind") == "final"]
    if run.get("timed_out") or run.get("runner_interrupted") or run.get("sprint_status") in ("failed", "stopped", "interrupted"):
        return "failed"
    if not finals:
        return "no_final"
    if any(item.get("degraded") or item.get("quality_status") == "failed" for item in finals):
        return "failed"
    if run.get("sprint_status") == "completed" and all(item.get("quality_status") == "passed" and item.get("file_sha256") for item in finals):
        return "passed"
    return "unchecked"


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def summarize(runs: list[dict]) -> dict:
    groups = defaultdict(list)
    seen = set()
    for run in runs:
        identity = run.get("sprint_id")
        if not identity:
            raise ValueError("样本缺少 sprint_id")
        if identity in seen:
            raise ValueError(f"重复 Sprint：{identity}；请勿把原始和刷新后的同一运行同时计入")
        seen.add(identity)
        groups[run.get("execution_style", "unknown")].append(run)
    summary = {}
    for style, values in sorted(groups.items()):
        counts = {key: sum(outcome(run) == key for run in values) for key in ("passed", "failed", "unchecked", "no_final")}
        costs = [(run.get("stored_usage") or {}).get("estimated_cost_yuan") for run in values]
        durations = [run.get("duration_s") for run in values]
        summary[style] = {"runs": len(values), **counts, "pass_rate": counts["passed"] / len(values),
                          "mean_duration_s": mean([value for value in durations if numeric(value)]) if any(map(numeric, durations)) else None,
                          "mean_estimated_cost_yuan": mean([value for value in costs if numeric(value)]) if any(map(numeric, costs)) else None,
                          "missing_cost_runs": sum(not numeric(value) for value in costs)}
    inputs = defaultdict(set)
    for run in runs:
        if run.get("input_sha256"):
            inputs[(run.get("case"), run["input_sha256"])].add(run.get("execution_style"))
    return {"groups": summary, "comparable_cases": sorted({key[0] for key, styles in inputs.items() if {"quick", "collaborative"} <= styles}),
            "notice": "只有输入指纹一致的样本可作同题对比；仍需固定模型、环境与预算并重复运行。未验证不计通过，缺失费用不计零元。自动验收不替代人工盲评。"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Result JSON files or directories")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = sorted({file.resolve() for path in args.paths for file in (path.rglob("*.json") if path.is_dir() else [path])})
    runs = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and "sprint_id" in data and "case" in data:
            runs.append(data)
    if not runs:
        parser.error("没有找到运行样本 JSON")
    try:
        report = summarize(runs)
    except ValueError as error:
        parser.error(str(error))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
