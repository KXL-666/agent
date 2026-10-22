# 🤖 AI 敏捷开发办公室 · AI Agile Office

> 🎓 **毕业设计项目** · 可视化多智能体协作 Sprint 系统

> 📦 本项目基于开源项目 [PixOffice](https://github.com/teejoo/PixOffice)（作者 teejoo，MIT 许可证）二次开发，在此感谢原作者。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19.2-61DAFB.svg?logo=react)](https://react.dev/)
[![Vite](https://img.shields.io/badge/Vite-8-646CFF.svg?logo=vite)](https://vitejs.dev/)
[![PixiJS](https://img.shields.io/badge/PixiJS-8-06B6D4.svg)](https://pixijs.com/)

一个把「**多成员 AI 团队协作开发**」可视化的网页项目：办公室场景里 6 名 AI 员工（产品、前端、后端、测试、技术主管、敏捷教练）围绕同一需求跑完完整 Sprint，并把最终产物落成 **真实可下载的文件**（Word / PPT / Excel / 代码包）。

> A web-based visualization of a multi-agent AI team collaborating on a full Scrum Sprint. Six AI roles (PM, Frontend, Backend, QA, Tech Lead, Scrum Master) work together in a pixel-art office, producing real deliverables (Word / PPT / Excel / code bundles).

![preview](docs/page-preview.jpeg)

---

## ✨ 核心亮点

### 🎬 可视化协作
- **像素办公室场景**：6 个工位，AI 员工实时走动、工位对话、头顶状态牌，PixiJS + Spine 动画驱动
- **实时事件流**：WebSocket 推送每个节点的任务开始/完成/失败，可随时暂停、继续或终止 Sprint
- **桌面预览 & 代码沙箱**：生成的 HTML 代码产物直接在 iframe 沙箱里跑，Word/PPT/Excel 站内预览

### 🏗️ 完整 Sprint 流水线
```
需求拆解 → 任务分派 → 并行开发(≤3) → 测试验证 → 技术评审 → 缺陷修复 → 交付成品 → 复盘
```
后端用 **LangGraph** 编排工作流，每个节点都是独立可重试的 LLM 调用。

### 📦 真实交付物
- Word / PPT / Excel 直接生成到 `backend/deliverables/`，**可下载、可重试、可继续修改生成新版本**
- 支持**自然语言指定保存位置**：「放在 D:\资料」「保存到桌面的周报文件夹」，自动解析 Windows Known Folder 和相对路径
- 代码类产物自动打包：生成 README + 依赖清单 + 启动说明，启动前做语法校验和危险调用拦截

### 🔑 每个成员独立 LLM
- 6 名角色**各自独立配置** Base URL / API Key / 模型，互不影响
- OpenAI 兼容协议，接入任何服务商；自动兼容特殊参数限制（如 Kimi K2.6 强制 temperature=1）
- 主 API 失败自动切换备用 API；429 限流自动退避重试；JSON 截断自动重试加倍 max_tokens

### 💾 历史留存 & 版本链
- SQLite 持久化：每轮 Sprint 的任务、产物、事件、聊天记录全部入库
- 刷新/重连后 **完整恢复**，新 Sprint 不覆盖历史
- 产物支持**版本链**：基于已有产物继续修改，保留全历史版本，可前后对比差异

### 💰 成本可控
- 新建 Sprint 设置预算上限，达到后自动收尾并提示
- 实时显示各成员调用次数、tokens 消耗、估算花费，支持导出 CSV

### 🎁 零门槛体验
- **内置演示回放**：不配置 API 也能完整走一遍办公室动画和任务流转
- **5 个预置示例需求**：一键发起试跑，秒级进入状态
- 主流服务商预设 + 批量应用，首次配置只需填一次 Key

### 🌓 暗色主题 & 移动端
- 全局暗色主题，办公室场景实现**夜晚开灯效果**（台灯/主灯/月光柔和渐变衰减）
- 移动端自适应只读看板，手机端可正常查看任务和产物

---

## 🛠 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React 19 · TypeScript 6 · Vite 8 |
| 渲染 | PixiJS 8 · Spine 动画 |
| 状态 | Zustand (轻量 store) |
| 后端 | FastAPI · Uvicorn · WebSocket |
| 工作流 | LangGraph (LangChain) |
| LLM 调用 | httpx + OpenAI 兼容协议 |
| 文件生成 | python-docx · python-pptx · openpyxl |
| 持久化 | SQLite (标准库) |

---

## 🚀 快速开始

### 1. 安装依赖
```powershell
# 前端
npm install

# 后端
python -m pip install -r backend/requirements.txt
```

### 2. 一键启动
```powershell
npm run start
```
后端跑在 `:8000`，前端跑在 `:5173`。打开 http://localhost:5173 。

> 分开启动：
> ```powershell
> # backend/ 下
> python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
> # 项目根目录
> npm run dev
> ```

### 3. 配置 & 跑 Sprint
1. 进入「智能体 → ⚙ 设置」，为至少一名成员填入 API Key（不要粘贴带 `*` 的掩码）
2. 新建 Sprint，输入需求，可选附件（docx/xlsx/pdf/txt）
3. 观看 AI 团队协作动画，等待产物落地到「产物中心」

> 不配置 API 也能体验演示回放：首页点「内置演示」即可。

---

## 📁 目录结构

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
  test_core.py     后端核心逻辑单元测试
  .env.example     环境变量模板（复制为 .env 后填入，不要提交 .env）
  deliverables/    运行期生成的真实文件（不入库）
  uploads/         上传的附件（不入库）
src/
  components/      办公室场景 UI、各视图、智能体配置
  scene/           PixiJS 场景（角色、导航、动画系统、Spine）
  services/        WebSocket 客户端、动作分发
  store/           任务/产物/聊天等轻量 Zustand 状态
docs/              README 用的截图素材
scripts/           一键启动 / action-gateway 脚本
```

---

## 🔌 后端 API（常用）

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

---

## 🧪 测试

```powershell
# 后端
cd backend
python -m unittest test_core -v

# 前端类型检查
npx tsc --noEmit -p tsconfig.app.json
```

---

## 🔒 数据与隐私

- **历史数据库**：`backend/office.db`（首次启动自动创建）
- **生成文件**：`backend/deliverables/`
- **成员配置**：`backend/agent_config.json`（含 API Key，**已纳入 .gitignore，不会入库**）
- 接口**不会把真实 API Key 回传给前端**，只返回掩码
- CORS 收紧至本地域名，不允许跨域无鉴权访问
- 本项目仅建议在本机自用；代码产物启动前会做危险调用拦截，但**不是完整安全沙箱，请勿直接用于生产环境**

---

## ⚠️ 常见问题

- **启动报端口占用**：已有旧的 uvicorn 在跑，先结束占用 8000 端口的进程再启动
- **点击生成提示未配置 API**：到「智能体 → ⚙ 设置」为至少一名成员填入完整 Key（不要粘贴带 `*` 的掩码值）
- **产物显示「兜底生成」**：该次模型调用失败或文件生成失败，鼠标悬停/展开可看原因，点「重新生成」重试
- **指定路径没找到文件**：产物卡片会明确提示保存失败；可先下载，或用「打开所在文件夹」定位

---

## 📄 License

本项目基于 MIT License 开源。Copyright (c) 2026 teejoo (原作者), KXL-666 (二次开发)。

所有敏感配置文件（agent_config.json、office.db、.env、deliverables/、uploads/ 等）已纳入 `.gitignore`，不会提交到公开仓库。
