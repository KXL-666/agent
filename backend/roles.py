"""办公室名册：与前端 officeLayout.ts 的 AGENT_ROSTER 顺序一一对应（名册序号 1-6）"""

PO = 1   # 产品经理
DEV_BE = 2
DEV_FE = 3
QA = 4
LEAD = 5
SM = 6   # 敏捷教练

ROSTER = {
    PO: {"id": "marvis", "name": "王明", "role": "产品经理"},
    DEV_BE: {"id": "code-agent", "name": "李研", "role": "后端工程师"},
    DEV_FE: {"id": "file-agent", "name": "周理", "role": "前端工程师"},
    QA: {"id": "app-agent", "name": "陈书", "role": "测试工程师"},
    LEAD: {"id": "review-agent", "name": "刘市", "role": "技术主管"},
    SM: {"id": "data-agent", "name": "赵审", "role": "敏捷教练"},
}


def roster_name(no: int) -> str:
    entry = ROSTER.get(no)
    return entry["name"] if entry else f"#{no}"


def roster_label(no: int) -> str:
    entry = ROSTER.get(no)
    return f"{entry['role']}·{entry['name']}" if entry else f"#{no}"
