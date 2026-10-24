"""Run fixed, non-sensitive artifact samples through the existing six-agent Sprint API.

Usage: python scripts/run-quality-baseline.py --case W1 P1 C1
The output contains metadata and quality checks, not API keys or full model output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

import httpx


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "backend" / "workspace" / "diagnostics" / "baseline"
DELIVERABLES = ROOT / "backend" / "deliverables"
WORKSPACE = ROOT / "backend" / "workspace"
CASES = {
    "W1": {
        "requirement": "生成一份给研发与测试同事的 Word 通知，说明 11 月 4 日 14:00 至 16:00 进行接口联调，地点为线上会议室 A。前端在 11 月 3 日 18:00 前提交调用清单，后端在联调开始前准备测试环境，测试同事记录阻塞问题并在当日 20:00 前汇总。控制在一页，不要封面。",
    },
    "D1": {
        "requirement": "根据附件制作一份给项目负责人的进度分析报告，先给结论，再写已完成、待解决问题和下一步行动。交付 Word，随后用同一内容源输出 PDF。不得把计划写成已完成。",
        "attachment": "项目：预约系统试点。日期：2026-10-20。\n已完成：预约表单页面、手机号格式校验、预约记录写入接口。\n未完成：短信提醒接口；取消预约功能。\n测试：已执行 20 条用例，16 条通过，4 条失败。4 条失败中有 3 条与取消预约有关，1 条与重复提交有关。\n计划：10 月 28 日前完成取消预约修复；11 月 2 日前完成短信提醒联调。以上两项尚未完成。\n风险：短信服务商沙箱账号尚未开通，联调时间可能受影响。",
        "attachment_name": "D1-进度资料.txt",
    },
    "P1": {
        "requirement": "生成一份 6 页、不带封面的中文 PPT，向学校导师汇报校园预约系统的改进方案。已知问题只有“学生不知道预约状态”和“管理员每周手工导出记录”；建议状态通知和管理端筛选导出。没有用户量、节省时间或预算数据，不要编造统计或效果。重点说明问题、方案、实施顺序、风险和需要导师确认的决定。",
    },
    "P2": {
        "requirement": "根据附件生成 5 页含封面的中文 PPT，向项目负责人汇报三周缺陷处理进展。至少一页原生可编辑图表，图表只使用附件数值；解释第三周未关闭缺陷增加的原因不能自行猜测，应列为待确认。",
        "attachment": "第 1 周：新增缺陷 12，关闭缺陷 8，周末未关闭缺陷 7。\n第 2 周：新增缺陷 9，关闭缺陷 11，周末未关闭缺陷 5。\n第 3 周：新增缺陷 14，关闭缺陷 8，周末未关闭缺陷 11。\n统计口径：新增和关闭是各周事件数；“周末未关闭”是该周末快照值。未提供缺陷严重程度或原因分类。",
        "attachment_name": "P2-缺陷记录.txt",
    },
    "X1": {
        "requirement": "把附件三条预算记录制作成可编辑 Excel，含预算、实际、差额和执行率原生公式，以及按项目比较预算与实际的原生图表。保留单位“元”，添加汇总行，但图表不要把汇总行混进项目对比。",
        "attachment": "界面设计：预算 10000 元，实际 9000 元。\n后端接口：预算 15000 元，实际 16500 元。\n测试验收：预算 8000 元，实际 7200 元。\n差额口径：预算减实际；执行率口径：实际除预算。",
        "attachment_name": "X1-预算记录.txt",
    },
    "C1": {
        "requirement": "生成可运行的 Python 项目，实现命令行温度换算：输入“25 C”输出“77.0 F”，输入“77 F”输出“25.0 C”。无效格式与不支持的单位返回清楚错误，并以非零状态退出。提供 README 和有实际断言的自动化测试。",
    },
}


def get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def usage_delta(before: dict, after: dict) -> dict:
    old = {int(item["roster"]): item for item in before.get("members", [])}
    members = []
    for item in after.get("members", []):
        roster = int(item["roster"])
        prior = old.get(roster, {})
        calls = int(item.get("calls", 0)) - int(prior.get("calls", 0))
        if calls:
            members.append({"roster": roster, "model": item.get("model", ""),
                            "calls": calls,
                            "prompt_tokens": int(item.get("prompt_tokens", 0)) - int(prior.get("prompt_tokens", 0)),
                            "completion_tokens": int(item.get("completion_tokens", 0)) - int(prior.get("completion_tokens", 0))})
    # Per-member cost can be repriced during a run. Use the Sprint's stored total
    # for cost, while these snapshots remain useful for call/token attribution.
    return {"members": members, "calls": sum(m["calls"] for m in members)}


def artifact_summary(client: httpx.Client, artifact: dict) -> dict:
    name = artifact.get("file_name") or ""
    root = WORKSPACE if name.startswith("_process/") else DELIVERABLES
    path = (root / name.removeprefix("_process/")).resolve()
    file_exists = bool(name) and path.is_relative_to(root.resolve()) and path.is_file()
    entry = {"id": artifact.get("id"), "kind": artifact.get("kind"),
             "author_roster": artifact.get("author_roster"),
             "quality_status": artifact.get("quality_status"),
             "degraded": artifact.get("degraded"), "file_name": name,
             "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if file_exists else None}
    if name:
        response = client.get(f"/api/artifacts/{artifact['id']}/quality")
        if response.is_success:
            report = response.json()
            entry["quality"] = {"status": report.get("status"), "review": report.get("review", {}).get("status"),
                                "metrics": report.get("metrics"), "checks": report.get("checks"),
                                "pages": len(report.get("pages") or []), "report_created_at": report.get("created_at")}
    return entry


def run_case(client: httpx.Client, case_id: str, output: Path, timeout_s: int,
             execution_style: str = "collaborative", budget_yuan: float = 0.0) -> dict:
    case = CASES[case_id]
    before = get(client, "/api/usage")
    attachment_ids = []
    if case.get("attachment"):
        response = client.post("/api/uploads", files={"files": (case["attachment_name"], case["attachment"].encode("utf-8"), "text/plain")})
        response.raise_for_status()
        attachment_ids = [item["id"] for item in response.json()["uploads"]]
    # The current auto route skips the team on single-file requests. Freeze the
    # collaboration instruction as part of this test input, before any code change.
    requirement = case["requirement"] + (
        " 请由团队协作完成。"
        if execution_style == "collaborative" and case_id != "C1" else ""
    )
    request = {"requirement": requirement, "upload_ids": attachment_ids,
               "fast": True, "task_kind": "auto", "budget": max(0.0, budget_yuan),
               "execution_style": execution_style}
    route = client.post("/api/sprint/plan", json=request)
    route.raise_for_status()
    planned_route = route.json().get("route", "")
    start = client.post("/api/sprint/start", json=request)
    start.raise_for_status()
    sprint_id = start.json()["sprint_id"]
    print(f"START {case_id} {sprint_id} route={planned_route}", flush=True)
    began = time.time()
    timed_out = False
    while True:
        state = get(client, "/api/state")
        if not state.get("running") and state.get("sprint", {}).get("id") == sprint_id:
            break
        if time.time() - began > timeout_s:
            timed_out = True
            break
        time.sleep(8)
    detail = get(client, f"/api/sprints/{sprint_id}")
    after = get(client, "/api/usage")
    sprint = detail["sprint"]
    runner_interrupted = not state.get("running") and sprint.get("status") == "running"
    reported_status = "failed" if runner_interrupted else sprint.get("status")
    artifacts = [artifact_summary(client, item) for item in detail["artifacts"]]
    output_data = {"case": case_id, "sprint_id": sprint_id, "route": planned_route,
                   "execution_style": execution_style,
                   "timed_out": timed_out, "runner_interrupted": runner_interrupted,
                   "failure_reason": ("后台执行器已停止，但持久化状态仍为 running；按中断失败记录。"
                                      if runner_interrupted else ""),
                   "sprint_status": reported_status, "stage": sprint.get("stage"),
                   "duration_s": round(time.time() - began, 1), "usage": usage_delta(before, after),
                   "stored_usage": {"calls": sprint.get("usage_calls"), "tokens": sprint.get("usage_tokens"),
                                    "estimated_cost_yuan": sprint.get("usage_cost")},
                   "task_count": len(detail["tasks"]), "tasks": [
                       {"id": t.get("id"), "title": t.get("title"), "assignee_roster": t.get("assignee_roster"),
                        "status": t.get("status")} for t in detail["tasks"]],
                   "artifacts": artifacts,
                   "provenance": [get(client, f"/api/artifacts/{item['id']}/provenance")
                                  for item in detail["artifacts"] if item.get("kind") == "final"],
                   "events": {name: sum(e.get("type") == name for e in detail["events"])
                              for name in sorted({e.get("type", "") for e in detail["events"]})}}
    (output / f"{case_id}.json").write_text(json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DONE {case_id} status={output_data['sprint_status']} duration={output_data['duration_s']}s "
          f"calls={output_data['usage']['calls']} cost={output_data['stored_usage']['estimated_cost_yuan']}", flush=True)
    return output_data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--execution-style", choices=("collaborative", "quick"), default="collaborative")
    parser.add_argument("--budget-yuan", type=float, default=0.0,
                        help="Per-Sprint estimated-cost ceiling in yuan; 0 means unlimited")
    parser.add_argument("--refresh", type=Path, help="Refresh file hashes and stored Sprint usage in an existing result JSON")
    args = parser.parse_args()
    if args.refresh:
        with httpx.Client(base_url=args.base_url, timeout=45, trust_env=False) as client:
            data = json.loads(args.refresh.read_text(encoding="utf-8"))
            detail = get(client, f"/api/sprints/{data['sprint_id']}")
            data["artifacts"] = [artifact_summary(client, item) for item in detail["artifacts"]]
            sprint = detail["sprint"]
            data["stored_usage"] = {"calls": sprint.get("usage_calls"), "tokens": sprint.get("usage_tokens"),
                                    "estimated_cost_yuan": sprint.get("usage_cost")}
            data["usage"].pop("estimated_cost_yuan", None)
            for member in data["usage"]["members"]:
                member.pop("estimated_cost_yuan", None)
        args.refresh.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"REFRESHED {args.refresh}", flush=True)
        return
    output = args.output_root / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    # Ignore machine-wide HTTP proxies for this loopback-only test runner.
    with httpx.Client(base_url=args.base_url, timeout=45, trust_env=False) as client:
        state = get(client, "/api/state")
        if state.get("running"):
            raise SystemExit("A Sprint is already running; baseline must not interrupt it")
        results = []
        for case_id in args.case:
            results.append(run_case(client, case_id, output, args.timeout_s, args.execution_style, args.budget_yuan))
            if results[-1]["timed_out"]:
                print("A Sprint is still running; stopping batch to avoid overlap", flush=True)
                break
    index = {"created_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
             "cases": [{"case": item["case"], "sprint_id": item["sprint_id"],
                        "status": item["sprint_status"], "route": item["route"]} for item in results]}
    (output / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OUTPUT {output}", flush=True)


if __name__ == "__main__":
    main()
