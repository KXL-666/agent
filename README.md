# AI 敏捷开发办公室

一个把「多成员 AI 团队协作开发」可视化的网页项目：办公室场景里 6 名 AI 员工（产品、前端、后端、测试、技术主管、敏捷教练）围绕同一需求跑完 Sprint，并把最终产物落成真实文件（Word / PPT / Excel / 代码包）。

## 快速开始

```powershell
# 1. 安装前端依赖
npm install

# 2. 安装后端依赖
python -m pip install -r backend/requirements.txt

# 3. 一键启动（后端 :8000 + 前端 :5173）
npm run start
```

打开 http://localhost:5173 ，在「智能体 → ⚙ 设置」为至少一名成员填入 API Key，然后新建 Sprint。

> 也可以分开启动：`python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload`（在 `backend/` 下）与 `npm run dev`。

## 功能概览

- **多成员独立模型**：每个成员单独配置 Base URL / API Key / 模型，互不影响。
- **完整 Sprint 流程**：需求拆解 → 任务分派 → 并行开发 → 测试验证 → 技术评审 → 缺陷修复 → 交付成品 → 复盘。
- **真实交付物**：Word / PPT / Excel 直接生成到 `backend/deliverables/`，产物中心可下载、重试、继续修改（生成新版本）。
- **历史留存**：每轮 Sprint 的产物、任务、代码都写入 SQLite，刷新/重启/重连后可恢复，不再覆盖上一轮。
- **失败可见**：内容来自兜底/Mock 或真实文件生成失败时，产物带「兜底生成」徽标与原因，可单条重试。
- **进度与成本**：顶部显示当前阶段、总用时、本阶段耗时与本次估算花费；新建 Sprint 可勾选「极速模式」跳过演出动画。
- **指定保存位置**：在需求里写「放在 D:\资料」「保存到桌面的周报文件夹」，成品会自动复制到对应位置（Windows 下按系统 Known Folder 解析，兼容 OneDrive 重定向）。

## 目录结构

```text
backend/
  main.py          FastAPI 入口、REST API、WebSocket 快照
  graph.py         LangGraph Sprint 工作流（各角色节点）
  llm.py           OpenAI 兼容调用、重试、限流退避、用量统计
  forge.py         真实文件生成（docx/pptx/xlsx/代码）、路径解析、代码检查
  storage.py       SQLite 持久化（sprints / tasks / artifacts）
  artifacts.py     产物内存快照 + 入库 + 事件推送
  events.py        事件总线、任务落库、阶段进度
  uploads.py       附件保存与文本提取（docx/xlsx/pdf/txt）
  test_core.py     后端核心逻辑测试
  deliverables/    生成的真实文件（运行期创建）
src/
  components/      办公室场景 UI、各视图、智能体配置
  services/        WebSocket 客户端、动作分发
  store/           任务/产物/聊天等轻量状态
```

## 后端 API（常用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/sprint/start` | `{requirement, upload_ids, fast}` 发起 Sprint |
| POST | `/api/sprint/stop` | 手动结束当前 Sprint |
| GET | `/api/state` | 当前 Sprint 快照（任务/产物/阶段/花费/历史） |
| GET | `/api/sprints` | 历史 Sprint 列表 |
| GET | `/api/sprints/{id}` | 指定 Sprint 的任务与产物 |
| POST | `/api/artifacts/generate` | 直接生成成品（Word/PPT/Excel） |
| POST | `/api/artifacts/{id}/retry` | 重新生成单个产物 |
| POST | `/api/artifacts/{id}/revise` | 基于该产物生成修订版 |
| GET | `/api/artifacts/{id}/download` | 下载产物真实文件 |
| GET | `/api/usage` | 各成员调用次数、tokens、估算花费 |
| WS | `/ws/office` | 实时事件；`hello` 携带完整快照 |

## 测试

```powershell
cd backend
python -m unittest test_core -v
```

前端类型检查：`npx tsc --noEmit -p tsconfig.app.json`

## 数据与隐私

- 历史数据库：`backend/office.db`（首次启动自动创建；旧的 `artifacts_data.json` 会迁移为历史记录）
- 生成文件：`backend/deliverables/`
- 成员配置：`backend/agent_config.json`（含 API Key，请勿提交到公开仓库）
- 接口不会把真实 API Key 回传给前端，只返回掩码。
- 本项目只建议在本机自用；生成的 Python 脚本在启动前会做危险调用拦截，但不是完整安全沙箱，请勿直接用于生产环境。

## 常见问题

- **启动报端口占用**：已有旧的 uvicorn 在跑，先结束占用 8000 端口的进程再启动。
- **点击生成提示未配置 API**：到「智能体 → ⚙ 设置」为至少一名成员填入完整 Key（不要粘贴带 `*` 的掩码值）。
- **产物显示「兜底生成」**：该次模型调用失败或文件生成失败，鼠标悬停/展开可看原因，点「重新生成」重试。
- **指定路径没找到文件**：产物卡片会明确提示保存失败；可先下载，或用「打开所在文件夹」定位。
