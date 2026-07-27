# aicode Architecture

本文描述当前代码库的运行架构和设计边界，重点是 CLI、Python Runtime、Agent Loop、工具权限、配置、审计和 Docker Sandbox 的真实实现状态。

## 1. Product Boundary

`aicode` 是一个本地优先的 AI Coding Agent：

- 用户通过 Go CLI 发起对话、代码审查、diff 分析、测试修复、提交信息生成等任务。
- Python Runtime 作为本机 daemon 提供会话、模型调用、工具执行、审批、SSE 事件和持久化能力。
- Agent 默认在用户的本地 workspace 内工作，读写都经过策略层和审批层。
- Docker Sandbox 是 Runtime ExecutionBackend 的本地隔离能力，用于安全运行 `test`、`build`、`lint`。

非目标：

- 当前版本不交付 IDE 插件、Web UI 或桌面图形界面；这些入口只能复用版本化 contract 和 Application Runtime，不得复制 Agent 逻辑。
- 不提供云端托管执行环境。
- 不自动跨仓库写入或部署生产系统。
- 不把项目规则视为高于系统安全策略的指令。

## 2. System Overview

```text
Go CLI / Interactive REPL / future IDE / stdio JSON-RPC
                         |
                 versioned API contract
                         |
+---------------- Application Runtime ----------------+
| SessionService | RunCoordinator | ApprovalService   |
| ContextService | TraceService | ProjectTrustService |
+------------------------+-----------------------------+
                         |
+-------------------- Agent Core ----------------------+
| AgentLoop | ContextManager | ModelRuntime            |
| ToolRegistry | Policy                               |
+------------------------+-----------------------------+
                         |
+---------------------- Adapters ----------------------+
| SQLite/JSONL | Model Providers | Host/Sandbox Exec   |
| Workspace | Clock/IDs                                |
+------------------------------------------------------+
```

Application contract v2 是 transport 与内部实现之间的稳定边界，定义 `SessionSnapshot`、`TurnRequest`、`RunReceipt/RunControl`、`SteerReceipt` 和 `CompactionReceipt`；canonical schema 为 `schemas/application-contract.schema.json`。FastAPI/Pydantic DTO 必须显式转换为这些类型，Agent Core 与 Application services 不导入 transport 类型。

HTTP/SSE 是当前稳定 transport。`GET /v1/meta/contract` 返回 application contract version/type map、HTTP/SSE contract version、最低兼容版本、Runtime version 和 transport capability；Go client 通过同一结构读取。stdio JSON-RPC 只预留 capability，尚未作为已实现功能发布。

Host 命令与 Docker Sandbox 统一经过 Runtime ExecutionService：

```text
Agent bash/search/review ----\
                              -> ExecutionService -> HostExecutionBackend
Go CLI --sandbox docker -----/                   \-> DockerExecutionBackend
  |
  +-- stable execution_id and terminal state
  +-- timeout/cancel kills the whole process group
  +-- command hash, backend, exit/duration audit
```

## 3. Main Components

### 3.1 Go CLI

CLI 负责用户入口、常驻 REPL 状态机、daemon 生命周期、命令参数解析、本地配置读写、SSE 输出，以及将 Docker Sandbox 请求转发给 Runtime。

主要命令：

- `chat`: 带 message 时执行单次对话；不带 message 时进入常驻 REPL，在同一 session 中支持 follow-up、steer、cancel、status/model/compact/new/resume。
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
- `doctor`: 只读检查安装、版本、Python/依赖、端口、provider 和 Docker；不启动 daemon，不请求外部 provider。
- `trust`: 查看、授予或移除仓库外 Project Trust。
- `--sandbox docker test|build|lint`: 在 Docker 隔离环境运行项目命令。

CLI 在普通 Agent 命令中会自动确保 daemon 已启动；如果本机已有 Runtime，也会复用现有服务。

REPL 由一个输入 scanner 和一个主状态循环统一协调普通消息、控制命令、SSE、approval 与 signal。普通输入在活跃 run 后排队；`/steer` 通过 Runtime 队列在 AgentLoop 安全边界生效。TTY 中首个 Ctrl-C 取消当前 run、第二个退出；非 TTY 在 EOF 后等待已提交 run 的终态，不输出 prompt。

`--sandbox docker` 不再在 Go 进程内自行启动容器。CLI 只提交版本化 execution request，并在中断时调用统一 cancel API；命令探测、资源限制、进程终态和 audit 均由 Runtime 负责。

### 3.1.1 Execution Backends

`runtime/app/execution/` 定义 `ExecutionRequest`、`ExecutionResult` 和 `ExecutionBackend` Protocol。Host 与 Docker 共享：

- `execution_id`、`succeeded/failed/timed_out/cancelled` 终态；
- workspace、allowed roots、mode/session/run/tool-call 元数据；
- timeout、CPU/内存/PID、network、writable/masked path 等策略字段；
- 进程组级 timeout/cancel；
- 不记录原始命令的 execution audit。

Agent `bash`、`rg` 搜索、review git 命令和编辑后的模型验证都走 Host backend。`test/build/lint` sandbox 由 Docker backend 执行，保持 workspace 只读、默认禁网、`.env*` 遮蔽和资源限制。正常 `daemon stop` 会先请求 Runtime 取消全部活跃 execution，再终止 daemon。

Host backend 不继承完整 Runtime 环境：默认只复制 PATH、locale、terminal 和必要 toolchain root 等最小非敏感 allowlist，并把 HOME/XDG/TMP 重定向到按 canonical workspace 隔离、权限为 `0700` 的 execution home。API key、Runtime token、credentials、全局 build cache 路径和任意未列出的自定义变量不会进入项目子进程。Runtime 内部调用 `git`/`rg` 时会先解析绝对 executable，并拒绝 workspace PATH hijack。

### 3.2 Installed Runtime

`make install` 使用根目录 `VERSION` 作为发布版本，安装布局为：

```text
<prefix>/bin/aicode
<prefix>/lib/aicode/manifest.json
<prefix>/lib/aicode/<version>/runtime/
<prefix>/lib/aicode/<version>/venv/
```

installer 先在 `<prefix>/lib/aicode` 下的 staging 目录复制 Runtime、创建 venv 并验证依赖，成功后才原子切换版本目录、CLI 和 manifest。同一版本可重复安装；失败时恢复原版本。

daemon 解析 Runtime 的顺序：

1. `AICODE_RUNTIME_DIR`，配合可选 `AICODE_RUNTIME_PYTHON`，用于开发和诊断。
2. 根据当前 CLI 可执行文件定位 `<prefix>/lib/aicode/manifest.json`，使用其中版本化 Runtime 和 venv Python。
3. 从当前目录向父目录查找源码 checkout 的 `runtime/`，仅作为开发 fallback。

manifest 中的路径必须相对 manifest 目录，CLI 会拒绝绝对路径和 `..` 逃逸。`aicode doctor [--json]` 使用与 daemon 相同的 Runtime 解析路径，并区分阻断运行的 error 与可选能力/尚未启动服务的 warning。安装启动 E2E 在临时 HOME、临时 prefix 和源码目录外 workspace 中覆盖 install → doctor → start → status → stop。

### 3.3 Python Runtime

Runtime 分为三层：

- `application/`：会话、run 串行化/取消/steer、手动 compaction、审批决议、trace/usage、Project Trust、model 与 execution facade。
- `agent/` + `core/`：transport-independent AgentLoop、ContextManager、Policy，以及 ModelRuntime、ToolRegistry、SessionRepository、EventSink、ApprovalBroker、ExecutionRuntime、WorkspaceRuntime、Clock/IDs ports。
- `adapters/`：composition root、SQLite/内存 session、JSONL usage、provider router、host/Docker execution、workspace/project config、tool registry、approval broker 与系统 clock/UUID。

`server/main.py` 只创建一个 `ApplicationRuntime`。handler 负责 Pydantic 输入输出、transport DTO ↔ Application contract 显式转换、ApplicationError → HTTP 状态映射，以及 SSE 编码；run、approval、trust、usage 和 execution 业务规则由 Application Services 承担。

依赖规则：

- Agent Core 不导入 FastAPI、server、SQLite store、具体工具、project config 或 adapter。
- Application 层不导入 FastAPI、server 或具体 adapter。
- 只有 `adapters/composition.py` 组装具体实现。
- adapter 可以依赖 core/application port，反向依赖禁止。
- 导入 `app.agent.loop` 不读取用户配置、不创建数据库、不启动 FastAPI。

Runtime 使用 FastAPI + Uvicorn，持久化默认落在 `.aicode/state/` 下。

`InMemorySessionRepository` 允许 SDK、测试或嵌入调用不启动 HTTP/SQLite；生产默认使用 `SessionStore` 的 SQLite message/event/compaction 持久化与 AuditLogger JSONL trace。

## 4. Process Model

典型执行过程：

1. CLI 读取用户配置和项目配置。
2. CLI 确保 Runtime daemon 可用。
3. CLI 调用 `POST /v1/sessions` 创建或复用 session。
4. CLI 调用 `POST /v1/sessions/{id}/messages` 发起一次 Agent run。
5. Runtime 立即返回 `run_id`。
6. CLI 通过 `GET /v1/sessions/{id}/events` 订阅 SSE。
7. Runtime 持续写入 event，CLI 实时展示。
8. 活跃 run 收到 `/steer` 时，Runtime 排队 guidance；AgentLoop 在下一个模型/工具安全边界写入 history，未开始的旧工具调用不会执行。
9. 如遇 approval，CLI 调用 approve 或 reject API。
10. Agent 完成后，Runtime 持久化消息、事件、用量和审计记录；空闲 session 可通过 `/compact` 强制生成新的持久化 context projection。

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
preflight provider/model context budget
  |
  +--> over budget: persist compaction and rebuild projection
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
  +--> pending steer at safe boundary
          |
          +--> skip not-yet-started old tool calls
          +--> persist latest user guidance and continue
  |
  v
emit run.completed or run.failed
```

模型 purpose：

- `review` 使用 `reviewer` 路由。
- 历史压缩使用 `summarizer` 路由。
- 其他模式使用 `main` 路由。

当前不会在 provider 未配置时降级到 stub 模型；`auth_mode=required` 且缺少 API key 时会直接报错。

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
- `bash` 同时解析 shell statement、wrapper、路径参数和 glob；命中 mandatory/project protected path、home、workspace/`../`/symlink 逃逸或危险命令时直接 deny。
- `untrusted` workspace 的项目测试命令至少进入 ask；只有显式 trust 后的低风险项目命令可以自动执行。
- deny 不能由 approval 覆盖。

审批事件通过 SSE 暴露给 CLI：

- `approval.requested`
- `approval.approved`
- `approval.rejected`
- `approval.expired`

Pending approval 的恢复策略是保守的：Runtime 重启或 session 恢复时，未完成审批会被标记为 expired/rejected，并写入对应事件，避免内存里的 `asyncio.Event` 丢失后造成悬挂状态。

### 8.1 Project Trust

TrustStore 默认位于 `~/.aicode/trust.json` 或 `$AICODE_HOME/trust.json`，使用版本化 schema、`0600` 权限和原子写入。key 是 canonical workspace 路径的 SHA-256；entry 绑定路径、`trusted` level、credential-free Git remote 和更新时间。

仓库内 `.aicode/config.json`、rules 和 memory 均不能声明 trust。Git remote 与记录不一致、workspace 消失或记录不存在时，Runtime 返回 `untrusted`。CLI 通过 `GET/POST /v1/trust` 和 `POST /v1/trust/remove` 管理这份仓库外状态。

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
- 受保护路径和推荐命令。
- 最近消息和必要的历史摘要。

项目规则被明确标记为“项目规则”，不能覆盖系统安全策略、工具策略和用户显式指令。这样可以降低仓库内 prompt injection 的影响。

系统仅提供英文交互。Application contract v2、HTTP DTO、Go client、session 状态和 SQLite schema 均不再包含 `language` 字段，也不存在用户级或项目级语言切换配置。

## 11. Configuration

### 11.1 User Config

用户级配置位于 `~/.aicode/config.toml`，包含：

- `ui`: 输出风格等交互设置。
- `runtime`: Runtime URL 和 port。
- `models.main`: 常规 Agent 模型。
- `models.reviewer`: review 模式模型。
- `models.summarizer`: 历史压缩模型。
- `provider.openai_compatible`: versioned Provider Profile，包含 endpoint、auth mode、模型能力、context/max output、streaming 与 token 估算策略。
- `provider.anthropic`: Anthropic endpoint、API key env、timeout。
- `pricing`: 本地成本估算所需单价。

遗留的 `models.default`、`models.planner`、`models.coder` 已不再作为推荐配置入口，保留主要是为了向后兼容和迁移期容错。

### 11.2 Project Config

项目级配置位于 `.aicode/config.json`，包含：

- `commands.test/build/lint`: 项目推荐命令。
- `protectedPaths`: 项目追加的保护路径；Runtime/CLI 始终与 `.env*`、SSH/GPG/cloud credentials、包管理凭证和私钥 mandatory patterns 合并，仓库配置不能移除系统规则。
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

OpenAI-compatible Profile auth mode 支持 `required`、`optional`、`none`。`none` 永不发送 Authorization，适合 no-auth localhost；`required` 缺少 key 时快速失败。Runtime 不会静默降级。

每个 purpose 在调用前解析 `provider + model` capability，包括 context window、max output、native tools、streaming 与 tokenizer。精确 model map 优先，随后使用 Profile 默认值。`tool_calling=false` 或 `streaming=false` 会在 provider 请求前快速失败，禁止从正文猜 tool JSON。

`GET /v1/models/probe` / `aicode models probe` 按 configuration → `/v1/models` → selected model → SSE → tools 分阶段探测，并返回 versioned result 和稳定错误 code。Profile/probe contract 分别由 `schemas/provider-profile.schema.json` 与 `schemas/provider-probe.schema.json` 定义。

## 13. Sessions And API

主要 API：

- `GET /v1/daemon/status`
- `GET /v1/trust`
- `POST /v1/trust`
- `POST /v1/trust/remove`
- `POST /v1/executions`
- `POST /v1/executions/{execution_id}/cancel`
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
- `GET /v1/models/probe`
- `GET /v1/review/rules`

所有 Runtime API 默认只监听本机地址，并通过 CLI 写入的 token 做本地鉴权。401 通常表示 CLI 读取的 daemon token 与当前 Runtime 不一致。

运行中的 session 还在内存中维护 `current_run_id`、当前阶段、开始时间和最后进度时间。`GET /v1/sessions/{id}` 与 session 列表会返回这些字段，用于区分模型流、工具执行和审批等待。取消当前 run 时 Runtime 会取消 runner task；命令工具会在收到 cancellation 后终止整个子进程组，然后队列继续消费下一条 run。

## 14. Persistence

Runtime 持久化以下内容：

- session metadata。
- user / assistant / tool messages。
- 版本化 compaction entries（覆盖 message id 范围、summary、provider/model、prompt version、token 估算、context window 和创建时间）。
- SSE events。
- approval 状态。
- usage records。
- audit JSONL。
- 仓库外 Project Trust store。

`messages` 是 append-only source of truth，compaction 只定义发给模型的可重建 projection，不删除或覆盖原始历史。恢复 session 时选择最近一个仍指向有效 message range 的 schema v1 compaction，并拼接其后的原始消息。压缩边界以完整消息组为单位：assistant tool calls 与其 tool results 不会拆开，未完成 tool call 不能进入摘要。

compaction 在每次模型调用前按该路由的 capability 主动发生。摘要优先使用 `summarizer` 路由，并对摘要请求本身做 context 上限裁剪；provider 失败时使用可审计的本地确定性摘要，保留全部原始消息，并在 `context.budget` 事件标记 fallback 类型。主 provider 报告 context overflow 时只做一次强制 compaction + retry，重复 overflow 不再重试。

## 15. Audit And Usage

审计日志记录本地敏感操作的结构化事件：

- 工具调用。
- bash 风险分类。
- approval requested / approved / rejected / expired。
- edit proposal / applied。
- `execution.started` / `execution.finished`（覆盖 Host 与 Docker）。

敏感字段会尽量脱敏，例如 API key、token、authorization header 和常见 secret 环境变量；已知 Runtime secret 也会从 neutral fields、tool output 和 SSE 中替换。写入 patch 时，审计记录使用 `patch_hash` 等摘要信息辅助追踪，避免不必要地扩散完整敏感内容。

Usage 记录按模型、session 和时间聚合 token 与成本估算。成本来自本地 `pricing` 配置，适合做近似统计，不等同于 provider 账单。

## 16. Multi-Workspace

项目配置可以声明多个 workspace root。Runtime 在路径校验、读写工具和 protected paths 判断时会以这些 root 作为边界。

当前设计仍保持本地项目优先：

- 不自动扫描用户整个磁盘。
- 不默认跨 root 写文件。
- 不把另一个仓库的规则隐式混入当前项目。

## 17. Docker Sandbox

Docker Sandbox 是 Runtime ExecutionBackend 的隔离实现，Go CLI 只保留客户端入口。当前支持：

- `aicode --sandbox docker test`
- `aicode --sandbox docker build`
- `aicode --sandbox docker lint`

安全默认值：

- workspace 只读挂载。
- 默认禁网。
- 不传 `.env`。
- 只允许少量缓存相关环境变量。
- 设置 CPU、内存、进程数等资源限制。
- 使用与 Agent Host 命令一致的 execution 终态、取消和 audit 格式。

它适合在隔离环境中验证命令是否能通过，但不是完整的远程执行平台。当前还没有实现可配置写入挂载、完整 artifact 回收或复杂服务编排。

## 18. Security Model

核心安全假设：

- 仓库内容不可信，尤其是 prompt、文档和脚本。
- 模型输出不可信，必须经过工具 schema、policy 和 approval。
- workspace 默认 untrusted；Project Trust 只保存在仓库外。
- 写入必须限制在 workspace root 内。
- file 与 shell 共享 mandatory/project protected path 和 symlink 边界。
- Host 子进程使用环境变量 allowlist，不能继承 provider/runtime secret。
- 项目规则不能覆盖系统安全策略。
- 风险 bash 命令必须可审计、可拒绝。
- Runtime token 只用于本机 CLI 与 daemon 通信，不是公网认证方案。

这套模型的目标是降低本地 Agent 的误操作风险，而不是提供强沙箱级隔离。强隔离任务应优先使用 Docker Sandbox 或未来更完整的 sandbox 后端。

## 19. Agent Eval And Trace

`evals/` 提供独立于真实 provider 的任务级评测层。CI profile 使用 scripted model，但仍调用真实 Agent Loop、ModelRouter、PolicyEngine、ExecutionService、SessionStore 和 compaction 路径，因此可以稳定捕获 Agent 行为回归，而不把外部模型波动引入普通 CI。

每个 eval task 固定 fixture、请求、mode、provider/model capability、turn/token/cost/wall-time 预算、审批策略、scripted model turns 和确定性 checks。Runner 将 fixture 复制到独立临时目录，创建带固定时间与 identity 的初始 Git commit；grader 依据测试命令、文件断言、changed/forbidden paths、event/audit、approval 和 compaction 规则评分。

每个 run 生成 schema v1 trace manifest，包含：

- task/fixture digest、初始 commit 和 replay task reference；
- model、prompt、tool schema、policy 和 compaction prompt fingerprint；
- model call、tool call、policy decision、edit、verification 和 usage 定位信息；
- diff path/numstat/content hash、grader checks 和预算结果；
- token、cost、latency、model/tool turns、安全与 approval 指标。

Agent shell command、模型正文、tool output 和 edit/diff 正文不会完整写入 trace；使用 hash、字符数、安全字段和已脱敏错误定位问题。Suite 同时输出 JSON 与 Markdown report，并与提交的 baseline threshold/fingerprint 比较。CI 跑 deterministic smoke，真实模型完整任务集保留为手动或定时 profile。

## 20. Repository Layout

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
│       ├── project/        # project config, detection and external trust store
│       ├── security/       # secret classification and output redaction
│       ├── server/         # FastAPI routes
│       ├── sessions/       # SQLite-backed sessions/events/approvals
│       ├── tools/          # tool registry and implementations
│       └── usage/          # usage tracking
├── evals/                  # tasks, isolated fixtures, graders, runner and baselines
├── schemas/                # runtime and eval contracts
├── runtime/tests/          # integration and behavior tests
├── README.md               # user-facing guide
└── ARCHITECTURE.md         # this document
```

## 21. Current Gaps

仍可继续推进的方向：

- 为 Docker Sandbox 增加可控写入目录和 artifact 导出。
- 引入更强的代码索引，例如 symbol、import graph、test mapping。
- 增强多 provider fallback 和 per-route 健康检查。
- 补充更细的恢复语义，例如跨进程 approval continuation。
- 提供更完整的英文 CLI 文案。
- 扩展真实模型完整评测集与定时 baseline，覆盖跨文件修改、边界测试和失败后二次修复。

架构目标保持不变：先把本地 Agent 的安全边界、可恢复性和可解释性做扎实，再逐步扩展更复杂的执行环境和产品形态。
