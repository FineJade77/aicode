# aicode Architecture

本文描述当前代码库的运行架构和设计边界，重点是 CLI、Python Runtime、Agent Loop、工具权限、配置、审计和 Docker Sandbox 的真实实现状态。

## 1. Product Boundary

`aicode` 是一个本地优先的 AI Coding Agent：

- 用户通过 Go CLI 发起对话、代码审查、diff 分析、测试修复、提交信息生成等任务。
- Python Runtime 作为本机 daemon 提供会话、模型调用、工具执行、审批、SSE 事件和持久化能力。
- Agent 默认在用户的本地 workspace 内工作，读写都经过策略层和审批层。
- Docker Sandbox 是 CLI 侧的本地命令隔离能力，用于安全运行 `test`、`build`、`lint`。

非目标：

- 不提供 IDE 插件、Web UI 或桌面图形界面。
- 不提供云端托管执行环境。
- 不自动跨仓库写入或部署生产系统。
- 不把项目规则视为高于系统安全策略的指令。

## 2. System Overview

```text
User
  |
  v
Go CLI
  |-- chat / review / diff / test / explain / commit-message
  |-- config / sessions / usage / models / review-rules
  |-- --sandbox docker test|build|lint
  |
  | HTTP + SSE, localhost token auth
  v
Python Runtime
  |-- FastAPI server
  |-- Session store, messages, events, approval state
  |-- Agent Loop
  |-- Model router
  |-- Tool registry
  |-- Policy engine
  |-- Edit approval and patch apply
  |-- Audit logger and usage tracker
  |
  v
Workspace / SQLite / Model Providers
```

Docker Sandbox 不经过 Runtime：

```text
Go CLI --sandbox docker <command>
  |
  v
Docker container
  |-- workspace read-only mount
  |-- network disabled by default
  |-- .env not passed through
  |-- limited allowlist of cache env
  |-- resource limits
  v
local audit event
```

## 3. Main Components

### 3.1 Go CLI

CLI 负责用户入口、daemon 生命周期、命令参数解析、本地配置读写、SSE 输出和 Docker Sandbox。

主要命令：

- `chat`: 与 Agent 对话，可复用或创建 session。
- `review`: 对当前改动做代码审查，Runtime 使用 reviewer 路由和只读工具。
- `diff`: 分析当前 diff。
- `test`: 根据测试失败信息让 Agent 辅助修复。
- `explain`: 解释代码或项目行为。
- `commit-message`: 基于 staged diff 或 working tree diff 生成提交信息。
- `sessions`: 查看和恢复历史 session。
- `usage`: 查看 token 和成本估算。
- `models`: 查看模型路由。
- `review-rules`: 查看当前审查规则和保护路径。
- `config`: 查看或修改用户配置。
- `daemon`: 管理 Runtime daemon。
- `--sandbox docker test|build|lint`: 在 Docker 隔离环境运行项目命令。

CLI 在普通 Agent 命令中会自动确保 daemon 已启动；如果本机已有 Runtime，也会复用现有服务。

### 3.2 Python Runtime

Runtime 是 Agent 的核心执行层，职责包括：

- 提供 HTTP API 和 SSE 事件流。
- 管理 session、message、event、approval 状态。
- 构造 prompt、调用模型、解析工具调用。
- 执行工具前做 policy 校验。
- 对写操作和风险命令发起审批。
- 应用 patch 并检测文件漂移。
- 记录审计日志和用量统计。

Runtime 使用 FastAPI + Uvicorn，持久化默认落在 `.aicode/state/` 下。

## 4. Process Model

典型执行过程：

1. CLI 读取用户配置和项目配置。
2. CLI 确保 Runtime daemon 可用。
3. CLI 调用 `POST /v1/sessions` 创建或复用 session。
4. CLI 调用 `POST /v1/sessions/{id}/messages` 发起一次 Agent run。
5. Runtime 立即返回 `run_id`。
6. CLI 通过 `GET /v1/sessions/{id}/events` 订阅 SSE。
7. Runtime 持续写入 event，CLI 实时展示。
8. 如遇 approval，CLI 调用 approve 或 reject API。
9. Agent 完成后，Runtime 持久化消息、事件、用量和审计记录。

SSE event 使用递增 `event_id`，CLI 可以用 `Last-Event-ID` 恢复事件流。

## 5. Agent Loop

Agent Loop 是一次 run 的核心状态机。

```text
load session history
  |
  v
build system prompt + project prompt + recent messages
  |
  v
select model route and tool schemas by mode
  |
  v
stream model response
  |
  +--> plain assistant text
  |
  +--> tool calls
          |
          v
       policy check
          |
          +--> allow: execute tool
          +--> ask: emit approval.requested and wait
          +--> deny: return denial as tool result
          |
          v
       persist tool result and continue
  |
  v
compact history when context budget requires it
  |
  v
emit run.completed or run.failed
```

模型 purpose：

- `review` 使用 `reviewer` 路由。
- 历史压缩使用 `summarizer` 路由。
- 其他模式使用 `main` 路由。

当前不会在 provider 未配置时降级到 stub 模型；缺少 API key 会直接报错。

## 6. Modes

| Mode | 入口 | 工具能力 | 说明 |
| --- | --- | --- | --- |
| `default` | `chat` | 全量工具 | 常规编码 Agent。 |
| `review` | `review` | 只读工具 | 专注发现问题，不修改文件。 |
| `diff` | `diff` | 全量工具 | 分析 diff，可结合用户后续要求执行修复。 |
| `test` | `test` | 全量工具 | 基于测试输出定位和修复问题。 |
| `explain` | `explain` | 只读工具 | 硬只读 mode：不暴露 `bash` / `edit_file`，Policy 层同时拒绝对应工具调用。 |
| `commit_message` | `commit-message` | 无工具 | CLI 注入 diff，模型只返回提交信息。 |

## 7. Tool Registry

Runtime 当前注册的工具：

| Tool | 类型 | 说明 |
| --- | --- | --- |
| `read_file` | 只读 | 读取 workspace 内文件。 |
| `search` | 只读 | 基于文本搜索文件内容。 |
| `list_files` | 只读 | 列出文件。 |
| `related_files` | 只读 | 根据路径和内容启发式寻找相关文件。 |
| `review_diff` | 只读 | 生成或读取用于审查的 diff。 |
| `bash` | 风险可变 | 执行命令，经过 policy 分类和审批。 |
| `edit_file` | 写入 | 生成 patch proposal，经过 edit approval。 |

工具 schema 会按 mode 裁剪：

- `commit_message`: 不暴露任何工具。
- `review`: 只暴露只读工具。
- 其他模式：暴露完整工具集。

即使模型绕过 schema 直接请求工具，Policy 层仍会做二次校验。

## 8. Policy And Approval

Policy 层对每个工具调用做本地判定：

- 只读工具默认允许。
- `review`、`commit_message`、`explain` 这类只读 mode 中，非只读工具会被拒绝。
- `edit_file` 默认进入 edit approval。
- `bash` 根据命令风险分类为 allow、ask 或 deny。
- 命中保护路径、越界路径或危险命令时，会拒绝或要求审批。

审批事件通过 SSE 暴露给 CLI：

- `approval.requested`
- `approval.approved`
- `approval.rejected`
- `approval.expired`

Pending approval 的恢复策略是保守的：Runtime 重启或 session 恢复时，未完成审批会被标记为 expired/rejected，并写入对应事件，避免内存里的 `asyncio.Event` 丢失后造成悬挂状态。

## 9. Edit Path

文件修改不会直接由模型写入。当前写入路径是：

1. 模型调用 `edit_file`。
2. Runtime 根据 `old_text` / `new_text` 生成 proposal。
3. 检查目标路径是否在 workspace 内。
4. 检查目标路径是否命中 protected paths。
5. 检查 `old_text` 是否唯一匹配。
6. 生成 diff 和 `patch_hash`。
7. 发起 edit approval，或在允许策略下自动接受。
8. 应用前再次校验文件内容，防止 stale patch。
9. 写入文件并记录 `edit.applied` 审计事件。

这样可以避免模型直接覆盖用户本地文件，也便于 CLI 和审计日志展示将要发生的变更。

## 10. Prompt And Project Context

Runtime prompt 由几层组成：

- 系统级 Agent 行为约束。
- 当前 mode 的任务说明。
- 项目规则 `.aicode/rules.md`。
- 项目记忆 `.aicode/memory.md`。
- 受保护路径、推荐命令和默认语言。
- 最近消息和必要的历史摘要。

项目规则被明确标记为“项目规则”，不能覆盖系统安全策略、工具策略和用户显式指令。这样可以降低仓库内 prompt injection 的影响。

默认输出语言由项目配置 `defaultLanguage` 控制；当前 prompt 支持中文和英文模式，但 CLI 的部分固定提示仍以中文为主。

## 11. Configuration

### 11.1 User Config

用户级配置通常位于 `~/.aicode/config.yaml`，包含：

- `ui`: 默认语言、输出风格等交互设置。
- `runtime`: host、port、state 目录、audit 目录、HTTP timeout。
- `models.main`: 常规 Agent 模型。
- `models.reviewer`: review 模式模型。
- `models.summarizer`: 历史压缩模型。
- `providers.openai_compatible`: OpenAI-compatible endpoint、API key env、timeout、retry 等。
- `providers.anthropic`: Anthropic endpoint、API key env、timeout、retry 等。
- `pricing`: 本地成本估算所需单价。

遗留的 `models.default`、`models.planner`、`models.coder` 已不再作为推荐配置入口，保留主要是为了向后兼容和迁移期容错。

### 11.2 Project Config

项目级配置位于 `.aicode/config.yaml`，包含：

- `defaultLanguage`: 项目默认输出语言。
- `commands.test/build/lint`: 项目推荐命令。
- `protectedPaths`: 需要写入保护的路径。
- `review`: 审查规则和严重级别设置。
- `workspaces`: 多 workspace root。

`.aicode/rules.md` 用于项目规范，`.aicode/memory.md` 用于长期项目记忆。

## 12. Model Router

Runtime 支持两类 provider：

- OpenAI-compatible Chat Completions。
- Anthropic Messages API。

路由按 purpose 选择模型：

| Purpose | 来源 |
| --- | --- |
| `main` | `models.main` |
| `reviewer` | `models.reviewer` |
| `summarizer` | `models.summarizer` |

Provider 配置要求明确的 API key env。未配置时，Runtime 会返回清晰错误，而不是静默降级。

## 13. Sessions And API

主要 API：

- `GET /v1/daemon/status`
- `POST /v1/sessions`
- `GET /v1/sessions`
- `GET /v1/sessions/{session_id}`
- `POST /v1/sessions/{session_id}/messages`
- `GET /v1/sessions/{session_id}/events`
- `POST /v1/sessions/{session_id}/cancel`
- `POST /v1/sessions/{session_id}/approve`
- `POST /v1/sessions/{session_id}/reject`
- `GET /v1/usage`
- `GET /v1/usage/sessions/{session_id}`
- `GET /v1/models/routes`
- `GET /v1/review/rules`

所有 Runtime API 默认只监听本机地址，并通过 CLI 写入的 token 做本地鉴权。401 通常表示 CLI 读取的 daemon token 与当前 Runtime 不一致。

运行中的 session 还在内存中维护 `current_run_id`、当前阶段、开始时间和最后进度时间。`GET /v1/sessions/{id}` 与 session 列表会返回这些字段，用于区分模型流、工具执行和审批等待。取消当前 run 时 Runtime 会取消 runner task；命令工具会在收到 cancellation 后终止整个子进程组，然后队列继续消费下一条 run。

## 14. Persistence

Runtime 持久化以下内容：

- session metadata。
- user / assistant / tool messages。
- SSE events。
- approval 状态。
- usage records。
- audit JSONL。

历史消息会在接近上下文预算时被压缩，压缩结果作为摘要继续参与后续 prompt。

## 15. Audit And Usage

审计日志记录本地敏感操作的结构化事件：

- 工具调用。
- bash 风险分类。
- approval requested / approved / rejected / expired。
- edit proposal / applied。
- sandbox run。

敏感字段会尽量脱敏，例如 API key、token、authorization header 和常见 secret 环境变量。写入 patch 时，审计记录使用 `patch_hash` 等摘要信息辅助追踪，避免不必要地扩散完整敏感内容。

Usage 记录按模型、session 和时间聚合 token 与成本估算。成本来自本地 `pricing` 配置，适合做近似统计，不等同于 provider 账单。

## 16. Multi-Workspace

项目配置可以声明多个 workspace root。Runtime 在路径校验、读写工具和 protected paths 判断时会以这些 root 作为边界。

当前设计仍保持本地项目优先：

- 不自动扫描用户整个磁盘。
- 不默认跨 root 写文件。
- 不把另一个仓库的规则隐式混入当前项目。

## 17. Docker Sandbox

Docker Sandbox 是 CLI 侧能力，当前支持：

- `aicode --sandbox docker test`
- `aicode --sandbox docker build`
- `aicode --sandbox docker lint`

安全默认值：

- workspace 只读挂载。
- 默认禁网。
- 不传 `.env`。
- 只允许少量缓存相关环境变量。
- 设置 CPU、内存、进程数等资源限制。
- 记录 sandbox audit 事件。

它适合在隔离环境中验证命令是否能通过，但不是完整的远程执行平台。当前还没有实现可配置写入挂载、完整 artifact 回收或复杂服务编排。

## 18. Security Model

核心安全假设：

- 仓库内容不可信，尤其是 prompt、文档和脚本。
- 模型输出不可信，必须经过工具 schema、policy 和 approval。
- 写入必须限制在 workspace root 内。
- 项目规则不能覆盖系统安全策略。
- 风险 bash 命令必须可审计、可拒绝。
- Runtime token 只用于本机 CLI 与 daemon 通信，不是公网认证方案。

这套模型的目标是降低本地 Agent 的误操作风险，而不是提供强沙箱级隔离。强隔离任务应优先使用 Docker Sandbox 或未来更完整的 sandbox 后端。

## 19. Repository Layout

```text
.
├── cli/                    # Go CLI
├── runtime/                # Python Runtime
│   └── app/
│       ├── agent/          # Agent Loop, prompts, history compression
│       ├── audit/          # audit logger and redaction
│       ├── config/         # user/project config loading
│       ├── models/         # provider clients and router
│       ├── policy/         # tool policy engine
│       ├── server/         # FastAPI routes
│       ├── sessions/       # SQLite-backed sessions/events/approvals
│       ├── tools/          # tool registry and implementations
│       └── usage/          # usage tracking
├── tests/                  # integration and behavior tests
├── README.md               # user-facing guide
└── ARCHITECTURE.md         # this document
```

## 20. Current Gaps

仍可继续推进的方向：

- 为 Docker Sandbox 增加可控写入目录和 artifact 导出。
- 引入更强的代码索引，例如 symbol、import graph、test mapping。
- 增强多 provider fallback 和 per-route 健康检查。
- 补充更细的恢复语义，例如跨进程 approval continuation。
- 提供更完整的英文 CLI 文案。
- 增加端到端测试覆盖 daemon restart、SSE resume、approval recovery 和 sandbox audit。

架构目标保持不变：先把本地 Agent 的安全边界、可恢复性和可解释性做扎实，再逐步扩展更复杂的执行环境和产品形态。
