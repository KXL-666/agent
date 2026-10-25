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
- 最终 Word / PPT / Excel / 代码包按北京时间归档到 `backend/deliverables/YYYY-MM-DD/项目编号/`，**可下载、可重试、可继续修改生成新版本**
- PRD、测试报告、评审、复盘和开发源码放在 `backend/workspace/`，系统内仍可查看与下载；首次启动自动整理旧目录，保留历史文件与旧下载标识
- 支持**自然语言指定保存位置**：「放在 D:\资料」「保存到桌面的周报文件夹」，自动解析 Windows Known Folder 和相对路径
- 代码类产物自动打包：生成 README + 依赖清单 + 启动说明，启动前做语法校验和危险调用拦截

### 成品制作与验收

- Word 根据文档用途组织正文，使用原生标题导航、原生编号和可跨页表格；仅明确要求时添加封面。
- Excel 支持原生公式、图表、格式、下拉验证和来源批注；LibreOffice 实际重算公式，并检查缓存结果与公式错误。
- PDF 优先与 Word 同源排版导出，支持中文可搜索文本、书签与实际分页；无 Office 引擎时使用 ReportLab 构建。
- 代码项目由规划与整合环节统一入口、接口、依赖、README 和测试，再根据实际错误修复。Python/Node 项目的运行测试需要可用的 Docker；其他语言保留源文件并明确标记未配置的运行验证。
- 每个成品执行实际文件检查；Office/PDF 渲染成页面，结合模型的内容与截图评审定点修订，默认最多三轮并保留较好版本。截图评审最多抽样六页，全部页面另做渲染与边界检查。
- 产物中心「验收详情」区分通过、未通过与未验证，并显示具体检查范围和真实页面；文件存在、语法正确或模型自述均不等于功能验收通过。
- 六人协作记录包含需求条款、角色贡献、交接、候选哈希、QA 证据和 LEAD 决定；缺少贡献、检查未完成或复核版本不一致时会阻断正式通过。代码任务按 BE→FE/QA 共享真实接口源码，QA 贡献限制为测试文件，冲突或越界贡献会阻断整合。模型服务故障可能由现有低价备用模型接管，实际模型与用量记入运行记录。

Windows 新环境可运行 `powershell -File scripts/setup-artifact-runtime.ps1` 准备 Python 与 Office/PDF 依赖。已有 Office 可用 `OFFICE_SOFFICE` 指定程序路径；Linux/macOS 安装 LibreOffice 后也可使用。依赖和渲染缓存放在 `backend/workspace/`，不进入 Git；备份不打包工具安装目录。

固定集的改造后协作运行仍有失败和未验证项，不能据此宣称协作成品质量已经普遍提升。

### 🔑 每个成员独立 LLM
- 对话按成员独立管理请求、中断和草稿，回复期间可以切换并向其他成员发送消息
- 对话注入当前需求、任务与产物状态；询问当前日期/时间直接读取北京时间，无需配置模型
- 6 名角色**各自独立配置** Base URL / API Key / 模型，互不影响
- OpenAI 兼容协议，接入任何服务商；自动兼容特殊参数限制（如 Kimi K2.6 强制 temperature=1）
- 主 API 失败自动切换备用 API；429 限流自动退避重试；JSON 截断自动重试加倍 max_tokens

### 💾 历史留存 & 版本链
- 迭代计划记录用户故事、P0/P1/P2 优先级、故事点和验收标准；任务中心展示状态看板和剩余故事点，详情可查看具体验收条件
- 测试通过进入「待评审」，技术评审通过才计入完成；返工超过上限仍未通过则阻止正式交付，界面提示「交付待修正」
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
- 移动端自适应看板，可查看任务和产物，并在手机上新建任务、核对执行计划、暂停或继续任务

### 📚 文件资料库与执行计划
- 上传的 txt、代码、Word、Excel、可复制文字的 PDF 会在本机分段建立索引；文件中心可搜索原文，结果标注文件、PDF 页码或 Excel 行号。扫描 PDF 和图片暂不支持本地 OCR。
- 任务会优先引用与需求匹配的附件片段；未命中时回退到有限长度的附件正文。来源片段帮助核对依据，不代表模型生成内容已自动完成事实核验。
- 新建任务先选择自动判断、文件成品或软件开发路线，再预览输入、流程、交付类型、保存位置、费用上限和数据去向；核对后才启动。
- 可选“仅本机模型 API”模式；只有所有已配置的主、备用 API 均指向本机回环地址时才能启用。此检查约束本项目发出的模型请求，无法保证第三方本机模型服务不会自行转发数据。

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

手机访问：先运行 `npm run build` 并保持 `npm run start` 运行，再在另一个 PowerShell 窗口设置至少 12 位的访问密码并启动受保护的局域网入口：

```powershell
$env:OFFICE_MOBILE_PASSWORD = '请替换为自己设置的长密码'
npm run mobile
```

手机与电脑处于可信的同一局域网时，打开 `http://电脑的局域网IP:5174`，用户名为 `office`，密码为上述值。此入口使用 HTTP Basic 和会话 Cookie，适合可信局域网演示；不要直接暴露到公网。后端和动作网关仍只监听本机地址。

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

API Key 不入库：仓库里只有空白模板 [backend/agent_config.example.json](backend/agent_config.example.json)，需要时复制为 `backend/agent_config.json` 再填入你自己的 Key（或直接在界面里配，界面保存时会用 Windows DPAPI 加密落盘，换台机器无法解密，所以也不需要共享）。

### 4. 配置成员技能

进入「技能中心」，新建技能或选择周报、代码审查、数据分析模板，填写适用场景与 Markdown 工作指令，勾选成员并启用后保存。也可以导入独立的 `SKILL.md`（支持 `name` / `description` frontmatter）或导出已有技能。

已分配且启用的技能会在成员下一次真实模型调用时加载，覆盖普通聊天、流式聊天、Sprint 节点和由该成员生成的产物。编辑、停用和删除无需重启；正在进行的模型请求不会被追溯修改。使用「预览已加载指令」检查分配，使用「调用模型试用」验证效果（需要配置成员 API，会产生用量）。导入默认停用、未分配，核对后再启用。

技能中心还可从 GitHub 仓库/技能目录、HTTPS ZIP 链接或本地 ZIP 导入完整技能包。系统保存整个技能目录，注册 `scripts/` 中的 Python/Node 脚本，安装 `requirements.txt` 或 `package.json` 中的依赖，并保留参考资料和附件。成员可通过模型工具调用搜索 GitHub 技能仓库、安装技能包、读取附件、运行已分配的脚本；脚本产生的文件可在技能中心下载。单独导入 `SKILL.md` 仍只有文字指令。依赖失败或遇到尚无适配器的脚本/清单时会标为「依赖未就绪」，可补齐环境后重试，不能当作可运行技能。脚本以本机用户权限运行；Python 虚拟环境和 Node 本地依赖仅隔离依赖，不隔离文件或网络访问。只应安装可信来源的技能包。

技能配置保存在 `backend/skills_data.json`，完整目录位于 `backend/workspace/skills/`。下方角色标签仍表示岗位职责。
数据备份包含技能目录与附件，不包含虚拟环境和 `node_modules`；导入备份后在技能中心点击「重试依赖安装」即可重建运行环境。

技能回归验证：`cd backend` 后运行 `python -m unittest test_skills test_skill_packages -v`，使用临时文件和模拟模型请求，不消耗 API 用量。

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
