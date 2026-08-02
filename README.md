# QunWork（群沃客）

> **Beta** - QunWork 是 OpenWorker 的二次开发版本，基于 [OpenWorker](https://github.com/andrewyng/openworker)（MIT License，作者 Andrew Ng）构建。

**让 AI 帮你把日常任务做完。** QunWork 是一个开源 AI 同事（coworker），常驻在你的桌面，交付**成品**而不只是聊天：一份精修过的文档、一条带数据的回复、一次更新好的日程、一封整理好的收件箱。

**产品愿景：多 Agent 协同、蜂群智能。** 当前版本聚焦于单人桌面的可靠执行；后续版本将逐步支持多个 Agent 协同分工，像蜂群一样把复杂任务拆解、并行、汇聚成最终成品。

**蜂群协同（多 Agent）**：内置 Orchestrator（planner 拆解 → executor 并行执行 → reviewer 校验 → 治理回路监控），支持工程化编程执行角色（Code persona）与并行 worker 池，进度/思维链/历史实时可见（🐝 面板）。

**技能市场**（🧩 面板）：SKILL.md 技能创建/编辑/删除、zip 导出导入、安装数与评分统计——让蜂群把重复性工作沉淀为可复用技能。

**知识文件库**（📚 面板）：工作区文档自动索引 + 本地文件夹导入 + 手动知识条目，分块向量化检索（中文开箱即用），蜂群与对话通过 `knowledge_search` 直接检索。

它运行在你的机器上，不锁定任何模型：自带 OpenAI、Anthropic、Google 或开源模型的 API Key，也可以完全本地运行（Ollama）。你的数据只会通过你自己选择的模型与集成离开机器。

## 下载

> 官方安装包渠道目前仍指向 OpenWorker 原版下载；QunWork 自己的发布渠道搭建中，当前请使用 [Run from source](#run-from-source) 本地运行。

[**⬇ macOS (Apple Silicon)**](https://download.openworker.com/mac)（原版 OpenWorker）
[**⬇ Windows 10/11 (x64)**](https://download.openworker.com/windows)（原版 OpenWorker）

## 工作原理

1. 告诉 QunWork 你想要的结果 —— "准备一份客户简报"、"理一下我的日程"、"起草一份报告"、"看看发布在 Jira 和 GitHub 上的进度"。
2. 它把任务拆成步骤，在你的桌面、文件和已连接的应用之间工作。
3. 在任何有后果的操作前 —— 发消息、改日程、执行命令 —— 它会先确认，由你批准或改向。
4. 你拿到的是完成的交付物，而不是一份待办清单。

架构：

```text
┌────────────────────────────────────────────────┐
│              QunWork desktop app               │  native shell + GUI
├────────────────────────────────────────────────┤
│           local agent server (Python)          │  engine · tools · connectors - built on aisuite
├───────────────┬────────────────┬───────────────┤
│  your files   │   your tools   │  your model   │  everything runs with your keys,
│  & terminal   │ 25+ connectors │  any provider │  on your machine
└───────────────┴────────────────┴───────────────┘
```

## 它能做什么

- **产出真实交付物** —— 文档、表格、报告、网页，落地为你可以打开和分享的文件。
- **从 Slack 干活** —— 在频道里 `@QunWork`，桌面端打开一个会话，工作在你的工具上完成，答案以 thread 回复返回。
- **使用你的日常工具** —— 25+ 集成，包括 GitHub、Slack、Jira、Notion、Linear、HubSpot、Outlook、monday.com、Gmail、Google Calendar，以及你的**终端和本地文件**。任何支持 [MCP](https://modelcontextprotocol.io/) 的工具都能接入，并支持按工具控制。
- **按计划运行** —— 面向重复工作的自动化：晨报、周报、对某个频道的持续关注。运行结果带完整记录落在应用里。
- **行动前先询问** —— 写入、发送、shell 命令都经过批准门控。无人值守的运行把待确认事项停在收件箱，而不是自作主张。

## 自带模型

模型访问权是你的：选一个提供商、粘贴你的 Key、随时切换。开箱支持：

**OpenAI · Anthropic · Google Gemini · Inkling (Thinking Machines) · GLM (Z.ai) · DeepSeek · Kimi (Moonshot) · Qwen · MiniMax · Mistral · Grok (xAI)** —— 外加通过 **Together** 和 **Fireworks** 的开源权重模型，以及通过 **Ollama** 的完全本地模型。

经过验证的模型清单会标注可用于工具调用。添加任意模型字符串需自担风险。

## 隐私

QunWork 本地优先。一切都在你的机器上：agent 循环、对话、连接器 token、模型 Key —— 全部在应用的本地 secret store 里。唯一的云组件是一个为连接器代理 OAuth 握手的小服务。你也可以完全不登录使用 —— 通过手动创建的凭据/API Key 使用连接器。

## 从源码运行

前置条件：Python 3.10+、Node 20+、以及（桌面壳需要）通过 [rustup](https://rustup.rs/) 安装 Rust 工具链。

```shell
git clone <你的 QunWork 仓库地址>
cd <repo>

# 1. 一次性引导 - 在 .venv 创建 Python 虚拟环境
#    (Windows 上请从 Git Bash 或 WSL 运行)
bash packaging/setup_dev_env.sh

# 2. 启动本地 agent 服务器
.venv/bin/qunwork-server --cwd ~/some/project --port 8765
#    (Windows: .venv\Scripts\qunwork-server.exe)

# 3. 另开一个终端启动 UI
cd surfaces/gui
npm install
npm run dev        # browser UI on the Vite dev port
```

独立服务器每次启动会在 `<state-dir>/qunwork-8765.token` 创建一个一次性 token；Vite 启动时读取这个用户专属文件。直接调用 API 时，把它的值放在 `X-QunWork-Token` 请求头里。桌面应用改用内存中的启动 token，从不落盘。

要运行完整桌面应用而非浏览器 UI，把第 3 步换成 `npm run tauri dev`（在 `surfaces/gui/` 下）—— Tauri 壳会启动窗口并自行监督服务器。

测试：`.venv/bin/pytest`（服务器）、`surfaces/gui` 下的 `npm test` 与 `npm run e2e`（GUI 单元 + 封闭端到端）。桌面安装包用 `packaging/build_dmg.sh` / `packaging/build_windows.ps1` 构建。

## 仓库结构

| 目录 | 内容 |
|---|---|
| `coworker/` | Python 后端 —— agent 引擎、模型提供商、连接器、MCP 客户端、记忆、自动化 |
| `surfaces/gui/` | 桌面应用 —— React UI + 监督服务器的 Tauri 壳 |
| `stt/` | 语音转文字侧车（Rust），用于语音输入 |
| `packaging/` | 安装包构建（macOS DMG、Windows）、自动更新清单、开发引导 |
| `docs/` | 设计文档与决策记录 |
| `tests/` | 后端测试套件 |

## 基于 aisuite 构建

QunWork 的引擎基于 [**aisuite**](https://github.com/andrewyng/aisuite)，一个轻量 Python 库，提供跨 LLM 提供商的统一 chat-completions API，以及带工具、工具包与 MCP 支持的 agents 层。如果你想构建自己的 agent harness 而不是用我们的，可以从那里开始；本仓库是 aisuite 能力的可运行参考。

## 与上游的关系

QunWork 是 [OpenWorker](https://github.com/andrewyng/openworker)（MIT License）的二次开发分支。OpenWorker 由 Andrew Ng 于 2024 年发布，原先是 aisuite 仓库的一部分，后独立成仓。感谢 aisuite 与 OpenWorker 的贡献者。合规细节见 [NOTICE.md](NOTICE.md)。

## License

MIT —— 保留上游原版权声明，详见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。

## 记忆系统（三套并存）

| 系统 | 位置 | 用途 |
|---|---|---|
| 结构化记忆 | `coworker/memory/`(SQLite `coworker.db`) | 事实/偏好,`remember`/`forget` 工具,scope 隔离(global/workspace/session) |
| 向量记忆(episodic) | `coworker/orchestrator/memory_store.py`(`.qunwork/memory.db`) | 蜂群跨会话任务经验,embedder 可注入、n-gram 兜底 |
| 知识文件库 | `coworker/knowledge/store.py`(`.coworker/knowledge.db`) | 工作区文档/导入文件/手动条目的分块向量检索,`knowledge_search` 工具 |

三者职责分离:记忆存"事实",向量记忆存"经验",知识库存"文档"。知识库是蜂群与对话的共享检索源(单一数据源,避免双库分裂)。
