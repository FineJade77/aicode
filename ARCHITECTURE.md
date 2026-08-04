# aicode Architecture

本文描述当前代码库的运行架构和设计边界，重点是 CLI、Python Runtime、Agent Loop、工具权限、配置、审计和执行沙箱的真实实现状态。每一节尽量写清楚"为什么是这样"，因为约束的理由比约束本身更容易在改动中丢失。

## 1. Product Boundary

`aicode` 是一个本地优先的 AI Coding Agent：

- 用户通过 Go CLI 发起对话、代码审查、diff 分析、测试修复、提交信息生成等任务。
- Python Runtime 作为本机 daemon 提供会话、模型调用、工具执行、审批、SSE 事件和持久化能力。
- Agent 默认在用户的本地 workspace 内工作，读写都经过策略层和审批层。
- 执行隔离有三种后端：宿主机、Docker 容器、OS 级沙箱（macOS seatbelt）。

非目标：

- 当前版本不交付 IDE 插件、Web UI 或桌面图形界面；这些入口只能复用版本化 contract 和 Application Runtime，不得复制 Agent 逻辑。
- 不提供云端托管执行环境。
- 不自动跨仓库写入或部署生产系统。
- 不把项目规则视为高于系统安全策略的指令。

## 2. System Overview

```text
Go CLI / Interactive REPL / stdio JSONL RPC (SDK) / future IDE
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
| ToolRegistry | Policy                                |
+------------------------+-----------------------------+
+---------------------- Adapters ----------------------+
| SQLite/JSONL | Model Providers | Host/Docker/OS Exec |
| Workspace | MCP | Clock/IDs                          |
+------------------------------------------------------+
```

Application contract v2 是 transport 与内部实现之间的稳定边界，定义 `SessionSnapshot`、`TurnRequest`、`RunReceipt/RunControl`、`SteerReceipt` 和 `CompactionReceipt`；canonical schema 为 `schemas/application-contract.schema.json`。FastAPI/Pydantic DTO 必须显式转换为这些类型，Agent Core 与 Application services 不导入 transport 类型。

**当前有两种已实现的 transport**，二者共享同一个 `ApplicationRuntime` 实例语义：

| Transport | 实现 | 版本 | 状态 |
| --- | --- | --- | --- |
| HTTP + SSE | `app/server/` | contract v2 / SSE event schema v2 | 稳定，CLI 使用 |
| stdio JSONL RPC | `app/sdk/` | protocol version 1 | 可用，供嵌入方使用（见 18.4） |

`GET /v1/meta/contract` 返回 application contract version/type map、transport 描述、最低兼容版本和 Runtime version；Go client 通过同一结构读取。**注意该描述符中 `stdio_jsonrpc` 仍标为 `planned`**——它描述的是"HTTP contract 之上的 JSON-RPC transport"这一未发布计划，与 `app/sdk/` 实际提供的 stdio JSONL RPC 是两件事；后者有自己的 `PROTOCOL_VERSION`，不通过 HTTP 描述符协商。

Host 命令、Docker 沙箱与 OS 沙箱统一经过 Runtime ExecutionService：

```text
Agent bash/search/review/hook ----\
                                   -> ExecutionService -> HostExecutionBackend
Go CLI project sandbox -----------/                    \-> DockerExecutionBackend
  |                                                     \-> OS sandbox wrapper
  +-- stable execution_id and terminal state
  +-- timeout/cancel kills the whole process group
  +-- command hash, backend, exit/duration audit
```

## 3. Main Components

### 3.1 Go CLI

CLI 负责用户入口、常驻 REPL 状态机、daemon 生命周期、命令参数解析、本地配置读写、SSE 输出，以及将沙箱请求转发给 Runtime。约 11.5k 行 Go，入口 `cli/main.go` 只做分类分发，实现在 `cli/internal/cmd/<category>cmd/`。

命令按五个分类组织，加上顶层的自由文本任务与 `chat`：

| 入口 | 命令 |
| --- | --- |
| 顶层 | `aicode "<task>"`、`aicode chat [message]` |
| `task` | `review`、`diff`、`test`、`explain <file-or-symbol>`、`commit-message` |
| `session` | `list`、`show`、`resume`、`cancel`、`fork`、`prune` |
| `runtime` | `start`、`stop`、`status`、`doctor`、`models`、`models probe`、`usage` |
| `project` | `trust`、`review`、`protected`、`command test`、`workspace`、`sandbox` |
| `config` | `init`、`show`、`list`、`get`、`set`、`unset`、`docs` |

`aicode help <category>` 打印该分类的用法。旧版扁平命令（`sessions`、`resume`、`cancel`、`daemon`、`doctor`、`models`、`usage`、`trust`、`review-rules`、`repl`、`review`、`diff`、`test`、`explain`、`commit-message`）由 `normalizeLegacyArgs` 重写到新入口，**保留是为了不破坏既有脚本，但不出现在帮助文本、文档和新增调用中**——两套并列的命令名会让"哪个是正的"变成需要读源码才能回答的问题。

CLI 在普通 Agent 命令中会自动确保 daemon 已启动；如果本机已有 Runtime，也会复用现有服务。

REPL 由一个输入 scanner 和一个主状态循环统一协调普通消息、控制命令、SSE、approval 与 signal。普通输入在活跃 run 后排队；`/steer` 通过 Runtime 队列在 AgentLoop 安全边界生效。有未决审批或提问时，`/approve`、`/reject`、`/skip` 直接作用于它。TTY 中首个 Ctrl-C 取消当前 run、第二个退出；非 TTY 在 EOF 后等待已提交 run 的终态，不输出 prompt。

`aicode project sandbox` 不在 Go 进程内自行启动容器。CLI 只提交版本化 execution request，并在中断时调用统一 cancel API；命令探测、资源限制、进程终态和 audit 均由 Runtime 负责。

### 3.2 Execution Backends

`runtime/app/execution/` 定义 `ExecutionRequest`、`ExecutionResult` 和 `ExecutionBackend` Protocol。所有后端共享：

- `execution_id`、`succeeded/failed/timed_out/cancelled` 终态；
- workspace、allowed roots、mode/session/run/tool-call 元数据；
- timeout、CPU/内存/PID、network、writable/masked path 等策略字段；
- 进程组级 timeout/cancel；
- 不记录原始命令的 execution audit（只留 command hash）。

Agent `bash`、`rg` 搜索、review git 命令、项目 hook 和编辑后的模型验证都走同一条 `run_shell_command` → `ExecutionService` 路径。`test/build/lint` 显式沙箱由 Docker backend 执行。正常 `aicode runtime stop` 会先请求 Runtime 取消全部活跃 execution，再终止 daemon。

Host backend 不继承完整 Runtime 环境：默认只复制 PATH、locale、terminal 和必要 toolchain root 等最小非敏感 allowlist，并把 HOME/XDG/TMP 重定向到按 canonical workspace 隔离、权限为 `0700` 的 execution home。API key、Runtime token、credentials、全局 build cache 路径和任意未列出的自定义变量不会进入项目子进程。Runtime 内部调用 `git`/`rg` 时会先解析绝对 executable，并拒绝 workspace PATH hijack。

### 3.3 Installed Runtime

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

manifest 中的路径必须相对 manifest 目录，CLI 会拒绝绝对路径和 `..` 逃逸。`aicode runtime doctor [--json]` 使用与 daemon 相同的 Runtime 解析路径，并区分阻断运行的 error 与可选能力/尚未启动服务的 warning。安装启动 E2E（`scripts/test-install-e2e.sh`）在临时 HOME、临时 prefix 和源码目录外 workspace 中覆盖 install → doctor → start → status → stop。

### 3.4 Python Runtime

Runtime 约 14.7k 行 Python，分为三层：

- `application/`：会话、run 串行化/取消/steer、手动 compaction、审批与提问决议、trace/usage、Project Trust、model 与 execution facade。
- `agent/`：transport-independent AgentLoop、ContextManager、Policy（`agent/policy.py`）、domain 类型（`agent/session.py`）与全部 port 声明（`agent/ports.py`：ModelRuntime、ToolRegistry、SessionRepository、EventSink、ApprovalBroker、ExecutionRuntime、WorkspaceRuntime、Clock/IDs，以及 `ToolSpec`）。
- **adapter 实现按其所属领域就近放置**，而非集中在一个 `adapters/` 包：`sessions/`（SQLite + 内存 session、approval broker）、`tools/`（registry、内置工具、`tools/runtime.py` 与 `tools/workspace.py` 适配、`tools/mcp/`）、`models/`（provider router）、`execution/`（host/docker/OS 沙箱/后台进程）、`audit/`、`usage/`、`project/`、`sdk/`，以及 `system.py`（clock/UUID）、`security.py`、`events.py`、`config.py`。
- `bootstrap.py` 是唯一的 composition root。

`ApplicationRuntime` 由 ASGI lifespan 创建并挂在 `app.state`，handler 通过 `Depends(get_runtime)` 取用。此前它是模块级全局：import `app.server.main` 就会打开 SQLite、构造 provider client，模块顺序敏感，且一个进程内无法并存两个配置不同的 Runtime——这与 ports 分层自相矛盾，也让"可嵌入 Runtime"在 transport 层被打破。

handler 负责 Pydantic 输入输出、transport DTO ↔ Application contract 显式转换、ApplicationError → HTTP 状态映射，以及 SSE 编码；run、approval、trust、usage 和 execution 业务规则由 Application Services 承担。

handler **直接复用 `ApplicationRuntime.__post_init__` 装配好的 service**（`runtime.session_service` / `.runs` / `.approvals` / …）。此前 transport 每个请求都把这批 service 重建一遍（包括新建一个 `AgentLoop`），而 composition root 已经建好了同一批对象——两套装配并存，runtime 自己的 service 成了死代码。

两条架构守卫锁定这一点：`test_importing_the_transport_does_not_build_a_runtime`（import 不得产生任何基础设施副作用，模块全局不得回归）与 `test_two_runtimes_coexist_in_one_process`（同进程两个 Runtime 各自只看到自己的 session）。

依赖规则由 `ruff` + 架构守卫测试双重约束。lint 配置在仓库根的 `ruff.toml`（不在 `runtime/pyproject.toml`：ruff 按文件向上找最近的配置，放在 runtime 下会让 `evals/` 静默沿用默认规则），规则集 `E,F,I,UP,B`，`make lint-python` 已接入 CI。

依赖规则：

- Agent Core（`agent/`）不导入 FastAPI、server、SQLite store、具体工具、project config 或任何 adapter 实现。
- Application 层不导入 FastAPI、server 或具体 adapter 实现。
- 只有 `bootstrap.py` 组装具体实现。
- adapter 可以依赖 `agent/ports.py` 与 application 契约，反向依赖禁止。
- 导入 `app.agent.loop` 不读取用户配置、不创建数据库、不启动 FastAPI。

`AgentRuntime` 的字段以其满足的 port 命名（`model_runtime` / `trace` / `trust`）。字段仍是 Optional，因为**两个要求不同的消费者共用这个对象**：`run_turn` 需要完整集合，而 `ContextManager` 只需要 session，`model_runtime` 缺失时降级为确定性摘要——把它们一律改成必填会误述 ContextManager 的契约。`run_turn` 在入口处校验自己需要的部分。

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
9. 如遇 approval 或 `ask_user`，CLI 调用 approve / reject / answer API。
10. Agent 完成后，Runtime 持久化消息、事件、用量和审计记录；空闲 session 可通过 `/compact` 强制生成新的持久化 context projection。

SSE event 使用递增 `event_id`，CLI 可以用 `Last-Event-ID` 恢复事件流。

### 4.1 SSE 流的终止保证

CLI 把"流结束但没收到 `final`"当作可重试的断连并重连（`streamEvents`）。因此**按 `run_id` 过滤的流必须始终抵达一个终态事件**，否则一个再也不会产生事件的 run 会让 CLI 陷入无限重连。

两种会永久等待的情形被显式终止：

- run 已在请求 cursor 之前结束——客户端重连到了自己 `final` 之后。此时重放它记录的终态事件。
- run 没有任何保留事件，且既不在运行也不在队列中。event 持久化是 best-effort，session 被驱逐重建后 `final` 可能已丢失，而一个过期的 run id 看起来完全一样。此时合成一个终态事件让客户端停止；**该事件不写入 session**，因为实际上没有发生任何新的事情。

同时流每 `AICODE_SSE_IDLE_TIMEOUT_SECONDS`（默认 15 秒）发一次 `: keep-alive` 注释帧，既向客户端也向中间代理证明连接存活，并借这个节拍重新判断上述两种情形。

### 4.2 Provider 重试

两个 provider 的可重试状态码（429/5xx）此前使用固定 `0.5 * 2**attempt`。固定退避会让所有撞上同一个限流的客户端同步重试，重新制造引发限流的那个突发。现在改为带 equal jitter 的指数退避（保留下半区间，使重试仍有合理的最小等待），并优先采用服务端的 `Retry-After`（支持 delay-seconds 与 HTTP-date 两种形式）。

`Retry-After` 有 60 秒上限：provider 声明数分钟等待时应当作错误暴露给用户，而不是静默挂起。格式非法的 header 回落到 jitter 退避——让它抛异常会把一个可重试的 429 变成 provider 崩溃。

## 5. Agent Loop

Agent Loop（`agent/loop.py`，约 1050 行）是一次 run 的核心状态机。

```text
load session history
  |
  v
build system prompt + project prompt + plan state + recent messages
  |
  v
select model route and tool schemas by mode
  |
  v
preflight provider/model context budget
  |
  +--> over budget: fold old tool output, else persist compaction
  |
  v
stream model response  (assistant.delta)
  |
  +--> plain assistant text
  |
  +--> tool calls
          |
          v
       group consecutive read-only calls
          |
          v
       policy check per call
          |
          +--> allow: execute tool
          +--> ask:   emit approval.requested and wait
          +--> deny:  return denial as tool result
          |
          v
       persist tool result, update verify/stall trackers, continue
  |
  +--> pending steer at safe boundary
          |
          +--> skip not-yet-started old tool calls
          +--> persist latest user guidance and continue
  |
  v
budget / stall / verification gate reached?
  |
  +--> yes: append note, one final tool-less model call, wind down
  |
  v
emit final (or error)
```

模型 purpose：

- `review` 使用 `reviewer` 路由。
- 历史压缩使用 `summarizer` 路由。
- 其他模式使用 `main` 路由。

当前不会在 provider 未配置时降级到 stub 模型；`auth_mode=required` 且缺少 API key 时会直接报错。

### 5.1 单一历史来源

`run_turn` **不维护内存 transcript**。每次模型调用前 `ContextManager` 都会从 session 重建 prompt，因此第二份本地副本只可能与之漂移。

此前 loop 里有一个局部 `history` 列表，每处都成对调用 `history.append(...)` + `persist_message(...)`——但下一轮的返回值会用 `load_history(session)` 整个覆盖它，所以那些 append 对行为零影响。这不是 bug，而是"内存 history 有独立语义"的假象：任何试图只改内存副本而不落库的后续修改都会静默失效。现已删除，session 是唯一来源。

### 5.2 工具调用的并发

模型常在一轮里返回多个 `read_file` / `search` / `glob`；逐个执行会让这一轮的延迟等于它们之和。实测 6 个各 100ms 的读取：**600ms → 126ms**。

只有**连续的**只读调用会成组并发，因此与写操作的相对顺序被保留——模型放在 edit 之后的 read 仍然读到编辑后的内容。只读工具也是唯一安全的并发组，还有第二个原因：它们的 policy 判定是立即 `allow`，因此并发组永远不会同时挂在两个审批提示上。

三条不变量：

- **结果按模型给出的调用顺序写回**，与完成顺序无关。部分 provider 按位置把 tool result 与 call 配对，用完成顺序会破坏下一次请求。
- 每次调用使用 `ToolContext` 的独立副本。此前 `tool_call_id` 是赋值到共享对象上的，一旦调用重叠就会互相污染审计与 execution 记录。
- 并发上限 8。一轮可能返回几十个读取，无上限会同时打开大量文件和子进程。

未注册的工具名没有 spec，因此不能假定它无副作用，永远单独成组。`ask_user` 与 `update_plan` 虽然不碰工作区，也被声明为 `read_only=False`，正是为了排除在并发组之外——前者阻塞整轮等人，后者改 session 状态。

### 5.3 单轮预算

`TurnBudget`（`agent/turn.py`）有五个维度：

| 字段 | 默认值 | 作用 |
| --- | ---: | --- |
| `max_steps` | 40 | 模型/工具往返步数 |
| `max_tokens_per_call` | 8192 | 单次请求的输出上限 |
| `max_total_tokens` | 1,000,000 | 单轮累计 token，0 关闭 |
| `max_total_cost` | 5.0 | 单轮累计成本（USD），0 关闭 |
| `max_verify_rounds` | 3 | 编辑后的修复轮次，0 关闭 |
| `max_repeated_actions` | 5 | 连续重复动作/失败，0 或 1 关闭 |

`max_total_tokens` / `max_total_cost` 由 `TurnLedger` 在每次 model call 后累加，**参与控制流而不只是上报**——`max_steps` 单独无法约束花费，一个循环调用工具的模型能在 40 步内消耗大量 token，且每步都重发整段历史。

所有闸门共用同一条收尾路径：发出对应事件（`run.budget.exceeded` / `run.no_progress` / `run.verification.exhausted`），追加一条说明 note，以 `tools=[]` 再请求一次模型，然后结束。这样无论哪个预算耗尽，用户拿到的都是一份总结而不是截断的对话。收尾调用位于循环之外且其用量不再过闸，这是防止收尾递归的结构性保证，而不是靠标志位。

预算只从 Runtime settings 读取，**不接受 `.aicode/config.json` 覆盖**：被检查的仓库能自行抬高的花费上限不是上限。这与 Project Trust 不允许 workspace 自我提权同源。

### 5.4 后台与长时命令

`bash` 最多阻塞几分钟，因此 dev server、watch 构建、长编译此前只能靠阻塞整轮来跑——实际结果是模型干脆不跑它们。`bash(background=true)` 立即返回一个句柄，配套 `read_output` 与 `stop_command`。

**进程组终止与 daemon 清理复用既有实现**：后台进程以 `start_new_session=True` 独立成组，`stop` 走 `kill_process_group`，因此一个会派生 worker 的 dev server 不会留下孤儿；`BackgroundProcessManager` 挂在 `ExecutionService` 上而不是单独一个 service，于是 lifespan 的 `aclose()` → `cancel_all()` 这条既有路径顺带就把后台进程收干净了。进程也不挂在 session 上——session 被缓存淘汰时，挂在它上面的进程会活得比属主还久。

输出由一个常驻 reader task 持续抽干，而不是按需读取：管道写满的进程会永久阻塞，所以即使没人读也必须排空。缓冲区有上限，读取按**绝对偏移**寻址，落后于上限的读取会被明确告知丢了多少字符——静默的缺口会被当成连续输出来推理。读取游标存在 session 上，两个 session 看同一个命令不会互相吃掉对方的输出。

`background` 只支持 host backend（外加显式的 OS 沙箱包装，见 18.1）。Docker 路径是每次执行建一个容器、调用返回即拆除，"后台"在那里的含义与告诉模型的完全不是一回事；与沙箱不可用时的处理同源——**拒绝，而不是悄悄把不受信任工作区的服务器放到宿主机上跑**。并发上限 5，超出直接报错而不是静默排队。

`stop_command` 声明 `approval="none"`：终止 Agent 自己启动的东西是严格降级操作，而启动它的那条命令早已过闸。

### 5.5 中途向用户提问

`ask_user` 让模型在一轮中间提问并等待。此前模型只有两个出口——继续猜或者结束——而审批填不上这个缺口：审批回答的是"这个操作可不可以"，回答不了"你想要哪一种"。需求真正模糊时（用哪个测试框架、API 要不要保持兼容、能不能引入新依赖），猜错的代价是整轮工作作废，而问一次的代价只是一个来回。

**复用 approval broker 而不是另建一套等待机制**：等待、超时、取消、四态终结（accepted / rejected / timed_out / missing）都只有一份实现。两者的差别只在答案的形状——审批是布尔，提问是文本，因此 `PendingApproval` 增加 `response` 字段，并新增 `POST /v1/sessions/{id}/answer` 端点；把答案塞进布尔端点会让文本无处可放。事件为 `question.asked`，审计另记 `question.answered`。

**超时明确区别于拒绝**：超时返回的文案写明"这不是拒绝，是没有人做决定"，要求模型带着明确声明的假设继续或停下汇报；用户主动 `/skip` 才读作拒绝。两者都不把它当作"用户否决了这项工作"。

工具声明为 `read_only=False` / `approval="none"`：它会阻塞整轮等一个人，所以绝不能与其它调用并发成组；但它本身不碰工作区，**提问本身就是那次交互**，再套一层审批等于问两遍。read-only 模式下不可见。没有可交互用户时**直接失败**，而不是返回一个看起来像答案的东西——那与真答案无法区分。

系统提示里额外写明"能靠读项目确定的事就去读"，因为这个能力最现实的失败模式是滥用而不是不用。

### 5.6 计划状态

`update_plan` 让模型登记一份有序的多步计划，每次调用**整体替换**而不是增量打补丁——增量接口需要模型维护稳定 id，而它维护不了；整体替换的最坏情况是重写一遍，增量的最坏情况是引用一个不存在的步骤。

状态只有 `pending` / `in_progress` / `done` 三种，同一时刻只应有一个 `in_progress`。计划写入 session 并注入下一轮 prompt，因此它同时是给用户的进度（`plan.updated` 事件）和给模型的自我约束。计划随 session fork 一起带走——fork 是同一件事的延续。

单步任务不该用它，这一点写在工具描述里而不是靠代码强制：判断"这算不算多步"需要理解任务，那正是模型比 Runtime 强的地方。

### 5.7 无进展检测

`max_steps` 对"改一行"和"跨六文件重构"是同一个数，调大调小只是在两种失败模式之间换边。真正区分"任务长"和"卡住了"的不是步数而是重复。

`StallTracker` 同时跟两条连续计数：**相同工具 + 相同参数**，以及**相同失败**。两条而不是一条，因为它们抓的是不同形状的卡住——前者是模型原样重发同一个调用；后者是模型微调参数却一次次撞到同一个错误（调用不同，墙是同一堵）。参数按 key 排序序列化，顺序不同不能伪装成不同调用。

达到 `warn_at`（默认 `limit - 2`）注入一条明确的 note；达到 `max_repeated_actions`（默认 5）走**与预算闸门、验证闸门同一条收尾路径**，发出 `run.no_progress` 并产出总结。警告**每个 episode 只发一次**：同一次卡住会同时触发两条计数，把同一件事报两遍对用户没有信息量；两条都低于 `warn_at` 时才重置，后续新的卡住仍会重新警告。

阈值不从 2 起跳是刻意的——连着两次相同调用往往是合法的。

### 5.8 编辑后的验证闸门

`max_verify_rounds`（默认 3）约束"应用编辑之后的修复轮次"。此前这里是一个一次性布尔标志：模型说"做完了"，被推回一次，再说一次"做完了"，循环就退出——**验证事实上是可选的**，prompt 里那句"stop and report after 3 consecutive failed attempts"没有任何代码执行它。

现在由 `VerifyTracker` 记账：编辑之后的每次 bash 结果都是一次验证尝试（发出 `verify.attempt`），成功即释放循环，失败则记录并抽取失败摘要（用例名、编译位置、首个错误行），新的编辑会作废之前的成功。模型在未验证的情况下想收尾，就被推回一次并计数；达到上限走**与预算闸门同一条收尾路径**，发出 `run.verification.exhausted` 并产出"改了什么、验证为何仍失败"的总结，而不是安静耗尽步数。

这个闸门只保证"修复循环有界且必定收尾"，不判断模型跑的是不是"正确的"验证命令——对任意仓库而言 Runtime 无从知道这一点。`max_verify_rounds=0` 关闭该闸门，行为回到"模型说完成就完成"。

## 6. Modes

| Mode | 入口 | 工具可见性 | Policy 强制 | 说明 |
| --- | --- | --- | --- | --- |
| `default` | `aicode "<task>"` / `chat` | 全量 | — | 常规编码 Agent。 |
| `review` | `task review` | 仅只读 | 拒绝写工具 | 专注发现问题，不修改文件。 |
| `diff` | `task diff` | 全量 | — | 分析 diff，可结合用户后续要求执行修复。 |
| `test` | `task test` | 全量 | — | 基于测试输出定位和修复问题。 |
| `explain` | `task explain` | 仅只读 | 拒绝写工具 | 硬只读 mode。 |
| `commit_message` | `task commit-message` | 无工具 | 拒绝写工具 | CLI 注入 diff，模型只返回提交信息。 |

三个常量分别表达三件事，不能合并：`WRITE_HIDDEN_MODES = {review, explain}`（schema 层裁剪）、`NO_TOOL_MODES = {commit_message}`（完全不给工具）、`READ_ONLY_MODES = {review, commit_message, explain}`（policy 层硬拒绝）。schema 裁剪是给模型的提示，policy 才是边界；客户端绕过 schema 直接请求工具时，仍会被 policy 拒绝。

## 7. Tool Registry

`ToolRegistry` 是真正的 name → tool 注册表，每个工具由一份 `ToolSpec`（`app/agent/ports.py`）声明：`name` / `description` / `input_schema` / `read_only` / `approval`（`none` | `gate` | `diff`）/ `hidden_in_modes`。

**一份声明，三个消费者**：模型看 `input_schema`，policy 读 `read_only`，Agent Loop 读 `approval`。`ToolSpec` 因此和其它 port 一起声明在 `agent/ports.py`，而不是放在工具实现旁边——这三个消费者互不导入。

这替换掉了此前的模块级 schema 列表 + if/elif 分发链，也消灭了一处真实的 drift 风险：读写属性此前在 `tools/registry.py` 和 policy 各维护一份名单，靠注释提醒保持同步。现在 `PolicyEngine.gate` 接收调用方从 `ToolSpec` 读出的 `read_only`，自己不再持有名单。`TOOL_SCHEMAS` 由 `TOOL_SPECS` 派生，因此不可能与之不一致。

Agent Loop 的 diff 审批同样改为按声明分发（`spec.approval == "diff"`）而不是 `if call.name == "edit_file"`，未来任何需要 diff 审批的工具无需改动 loop。新增一个工具只需一次 `register`；`test_a_custom_read_only_tool_is_visible_and_allowed_without_touching_core` 锁定了这一点，`test_specs_are_the_single_source_of_read_only_truth` 则守住 drift 不回归。

Runtime 当前注册 12 个内置工具：

| Tool | read_only | approval | 隐藏于 | 说明 |
| --- | :---: | --- | --- | --- |
| `read_file` | ✓ | none | — | 带行号读取，`offset`/`limit` 翻页。 |
| `search` | ✓ | none | — | 正则搜索内容，可选 glob 过滤。 |
| `glob` | ✓ | none | — | 按路径模式找文件（`**/*.py`）；按内容找用 `search`。 |
| `list_files` | ✓ | none | — | 列目录结构，`max_depth` 默认 2。 |
| `related_files` | ✓ | none | — | 按源/测试命名、同名和引用行找相关文件。 |
| `review_diff` | ✓ | none | — | 对当前 git diff 跑确定性 review 规则。 |
| `bash` | ✗ | gate | review, explain | 执行命令，经 policy 分类；`background=true` 返回句柄。 |
| `edit_file` | ✗ | **diff** | review, explain | 生成 patch proposal，经 edit approval。 |
| `read_output` | ✓ | none | review, explain | 读后台命令的增量输出；无参数则列出全部。 |
| `stop_command` | ✗ | none | review, explain | 终止后台命令及其进程组。 |
| `ask_user` | ✗ | none | review, explain | 中途提问并等待答案。 |
| `update_plan` | ✗ | none | review, explain | 登记/更新多步计划，整体替换。 |

五个只读工具带 `workspace` 参数（`read_file`、`search`、`glob`、`list_files`、`related_files`），用于访问项目配置声明的额外只读 workspace。

### 7.1 批量编辑

`edit_file` 支持三种形态：`old_text`/`new_text` 单处替换、`edits[]` 同文件多处替换、`delete: true` 删除；`old_text` 为空则为创建。

`edits[]` 存在的理由是**一次审批**：同一文件的 N 处改动逐个提交会产生 N 个 diff 提示，用户看到的是碎片而不是一次变更，而且中间状态的文件是不自洽的。所有替换都针对**当前文件内容**匹配，然后一起应用——按顺序逐个应用会让第二处的 `old_text` 需要预测第一处应用后的文本，那是模型算不准的东西。

### 7.2 MCP 外部工具

`.aicode/config.json` 的 `mcp.servers[]` 声明的服务器以子进程启动，通过 stdio 讲 JSON-RPC（`initialize` → `tools/list` → `tools/call`）。它们的工具经 `ToolSpec` 注册进同一个 registry，因此走与内置工具完全相同的 policy gate 和审批链路。

四条安全约束：

- **命名空间**：外部工具暴露为 `mcp__<server>__<tool>`。一个提供名为 `bash` 或 `edit_file` 的服务器否则会悄悄接管 policy 层有专门规则的名字。
- **不相信服务器的自述**：`read_only=False` 与 `approval="gate"` 是强制的，不从 descriptor 读取。服务器声称自己只读是第三方代码不可验证的主张，采信它等于对外部代码完全跳过审批。因此每次外部工具调用都需要用户确认，只读模式下直接拒绝。
- **环境隔离**：服务器复用与其它子进程相同的最小环境变量 allowlist（`build_subprocess_environment`），provider key 与 Runtime token 不会传入。项目可通过 `envAllowlist` 追加具体变量名——是允许清单而非透传，否则声明一个服务器就能把密钥交给第三方代码。
- **故障隔离**：单个服务器启动失败、协议违规或调用超时都不会影响主 loop。启动失败记入 `mcp.server.failed` 事件与 manager status（成功则 `mcp.server.started`），其余服务器照常工作；调用失败作为 tool error 返回给模型，让这一轮继续。

未注册的工具名仍是硬拒绝（`unknown tool`）：Runtime 无法描述的东西不能运行。

**当前只实现 stdio transport。** HTTP transport 尚未提供——发布一个未经充分测试的第二 transport 比不发布更糟。

## 8. Policy And Approval

Policy 层（`agent/policy.py`）对每个工具调用做本地判定：

- 只读工具默认允许。
- `review`、`commit_message`、`explain` 这类只读 mode 中，非只读工具会被拒绝。
- `edit_file` 默认进入 edit approval。
- `bash` 根据命令风险分类为 allow、ask 或 deny。
- `untrusted` workspace 的项目测试命令至少进入 ask；只有显式 trust 后的低风险项目命令可以自动执行。
- deny 不能由 approval 覆盖。

### 8.1 bash 命令分类

分类不是对整条命令做正则匹配，而是**先按 shell 语句边界切分，逐条分类，再按"最严者胜"合并**（deny > ask > allow）。原因是一条危险语句可以藏在分隔符之后（`ls; rm -rf /`）或藏在 argv[0] 的 env 赋值/路径前缀之后（`FOO=1 /bin/rm`）。

切分用的是 quote/escape 感知的 tokenizer（`shlex` 的 `punctuation_chars` 模式）而不是裸正则，因此被引号包住或被转义的分隔符（`echo "a && b"`、`find . -exec rm {} \;`）留在它所属的词里，不会被误认成语句边界。

分隔符分两类：`&&` 和换行是纯控制流，如果它连接的每条子命令都无害，整体仍可 `allow`（`pytest && echo done`）；`;`、`||`、`|`、裸 `&` 可以把次要/兜底工作藏过审查，因此**只要出现就至少抬到 ask**。重定向与命令替换标记（`>`、`>>`、`<`、`` ` ``、`$(`）不是能安全切分的边界，但同样抬到 ask，因为它们能藏起静态切分看不见的执行。

名单本身很小且刻意保守：

| 类别 | 内容 |
| --- | --- |
| 直接 deny | `rm`、`sudo`、`su`、`shutdown`、`reboot`、`mkfs`、`dd` |
| 直接 allow | `pwd`、`ls`、`rg`、`grep`、`head`、`tail`、`wc`、`cat`、`which`、`echo` |
| git allow | `status`、`diff`、`show`、`log`、`blame`、`rev-parse` |
| git deny | `reset`、`clean`、`rebase` |
| 其余 | ask |

同时解析路径参数与 glob：命中 mandatory/project protected path、用户 home、workspace 外绝对路径、`../` 或 symlink 逃逸时直接 deny。命令中出现已知 Runtime secret 的字面值同样 deny。

### 8.2 审批事件

审批状态通过 SSE 暴露给 CLI。**实际存在的事件类型只有这些**（`app/events.py` 的 `EVENT_TYPES` 是唯一来源，未注册的类型在写入时抛错）：

| 事件 | 含义 |
| --- | --- |
| `approval.requested` | 需要用户决定（工具或编辑）。 |
| `approval.expired` | 未在时限内应答或 run 被取消，带 `reason`（`timeout` / `run_cancelled`）。 |
| `question.asked` | `ask_user` 正在等待文本答复。 |
| `tool.denied` | policy 直接拒绝，不可覆盖。 |
| `tool.rejected` | 用户拒绝该工具调用，带 `resolution`。 |
| `edit.applied` / `edit.rejected` / `edit.auto_approved` | 编辑的三种归宿。 |

没有 `approval.approved` / `approval.rejected` 这两个事件——批准的结果就是工具开始执行（`tool.started`）或编辑落盘（`edit.applied`），另发一个"已批准"事件只会与它们重复。

Pending approval 的恢复策略是保守的：Runtime 重启或 session 恢复时，未完成审批会被标记为 expired/rejected 并写入对应事件，避免内存里的 `asyncio.Event` 丢失后造成悬挂状态。

### 8.3 审批终态

`ApprovalDecision` 有四个值：`accepted` / `rejected` / `timed_out` / `missing`，`PendingApproval.resolution` 另记录 `cancelled`。

此前 `wait_for_approval` 返回 `bool | None`，把"用户拒绝"和"超时无人应答"折叠成同一个 `False`。模型因此会为一个没人看到的请求收到"user rejected this edit"，可能就此放弃一个本来正确的方案。

现在超时的 tool 结果明确说明"这不是拒绝"，并要求模型**停下来告知用户**而不是重试——重试只会阻塞在下一个同样无人应答的提示上。超时时长由 `AICODE_APPROVAL_TIMEOUT_SECONDS` 配置，默认 300 秒，读取发生在 broker（adapter 层），Agent Core 不感知配置来源。

### 8.4 Project Trust

TrustStore 默认位于 `~/.aicode/trust.json` 或 `$AICODE_HOME/trust.json`，使用版本化 schema、`0600` 权限和原子写入。key 是 canonical workspace 路径的 SHA-256；entry 绑定路径、`trusted` level、credential-free Git remote 和更新时间。

仓库内 `.aicode/config.json`、rules 和 memory 均不能声明 trust。Git remote 与记录不一致、workspace 消失或记录不存在时，Runtime 返回 `untrusted`。CLI 通过 `GET/POST /v1/trust` 和 `POST /v1/trust/remove` 管理这份仓库外状态。

## 9. Edit Path

文件修改不会直接由模型写入。当前写入路径是：

1. 模型调用 `edit_file`（单处、多处或删除）。
2. Runtime 生成 proposal。
3. 检查目标路径是否在 workspace 内。
4. 检查目标路径是否命中 protected paths。
5. 检查每个 `old_text` 是否在当前文件中唯一匹配。
6. 生成 diff 和 `patch_hash`。
7. 发起 edit approval，或在 `accept-all` 且非 protected 时自动接受（`edit.auto_approved`）。
8. 应用前再次校验文件内容。
9. 写入文件、刷新该文件的 read 记录，并记录 `edit.applied` 审计事件。

第 8 步是 read-before-write 的另一半。`EditStaleError` 区分三种情况并分别给出可执行的下一步：文件被外部创建、被外部删除、被外部修改（"use read_file again before retrying"）。合成一句笼统的"冲突"会让模型无从判断该重读还是该放弃。

`session.read_files` 记录路径 → 内容 hash，**写入时也会更新**。这一点在 14.x 的失效读取检测里很关键：它是"Agent 当前认知"的记录，不是"最后一次读盘"的记录。

## 10. Prompt And Project Context

Runtime prompt 由几层组成：

- 系统级 Agent 行为约束（含当前执行环境是否禁网）。
- 当前 mode 的任务说明（`MODE_INSTRUCTIONS`）。
- 项目规则 `.aicode/rules.md`。
- 项目记忆 `.aicode/memory.md`。
- 受保护路径和推荐命令。
- 当前 plan 状态。
- 最近消息和必要的历史摘要。

项目规则被明确标记为"项目规则"，不能覆盖系统安全策略、工具策略和用户显式指令。这样可以降低仓库内 prompt injection 的影响。

收尾路径另有三组固定文案：`BUDGET_NOTE`、`WIND_DOWN_NOTES`（按闸门种类）、`STALL_NOTES`。它们是 prompt 而非代码分支，但每一条都对应一个已发出的事件，因此"模型收到了什么指示"始终可从 trace 复原。

系统仅提供英文交互。Application contract v2、HTTP DTO、Go client、session 状态和 SQLite schema 均不包含 `language` 字段，也不存在用户级或项目级语言切换配置。

## 11. Configuration

### 11.1 User Config

用户级配置位于 `~/.aicode/config.toml`：

| 键 | 说明 |
| --- | --- |
| `ui.style` | 输出风格。 |
| `runtime.url` / `runtime.port` | Runtime 地址。 |
| `models.main` | 常规 Agent 模型。 |
| `models.reviewer` | review 模式模型。 |
| `models.summarizer` | 历史压缩模型。 |
| `provider.type` | `openai_compatible` \| `anthropic`。 |
| `provider.openai_compatible.*` | versioned Provider Profile：`profile`、`base_url`、`api_key_env`、`auth_mode`、`timeout_seconds`、`context_window`、`max_output_tokens`、`tool_calling`、`streaming`、`tokenizer`、`chars_per_token`、`profile_schema_version`。 |
| `provider.anthropic.*` | `base_url`、`api_key_env`、`timeout_seconds`。 |
| `pricing.<provider>.<model>.input_per_1m` / `.output_per_1m` | 本地成本估算单价，USD / 1M tokens。 |

遗留键 `models.default`、`models.coder`、`models.planner` 在加载时解析并**结算**，结果记在 `Config.Deprecations` 上：

- `models.default` / `models.coder`：`models.main` 未显式配置时顶上，已配置则忽略。两者同时存在且没有 `models.main` 时 `models.coder` 胜出——Runtime 的 `Settings.from_env` 本来就把 `AICODE_MODEL_CODER` 当作 `AICODE_MODEL_MAIN` 的回退，两层的顺序必须一致。
- `models.planner`：没有对应路由（路由只有 main / reviewer / summarizer），仅提示删除，不会被当成任何模型使用。

对应的 `AICODE_MODEL_DEFAULT` / `_CODER` / `_PLANNER` 环境变量走同一条结算路径。每次调用在 stderr 打印一行提示，`aicode runtime doctor` 另有 `config` 检查项（warn，不升级为 error）。这些键仍不出现在 `config list`、`config docs` 和 Runtime 环境注入中。

**此前它们是被解析后丢弃的**：值写进结构体字段，而只有 `models.main` 会注入 Runtime，用户既得不到报错也得不到效果——正是"看起来在工作但实际没有"的那类缺陷。

### 11.2 Project Config

项目级配置位于 `.aicode/config.json`，canonical schema 为 `schemas/config.schema.json`：

| 键 | 说明 |
| --- | --- |
| `projectName` | 项目名。 |
| `commands.test/build/lint` | 项目推荐命令，注入 prompt 并供沙箱使用。 |
| `protectedPaths[]` | 项目**追加**的保护路径；始终与 mandatory patterns 合并，不能移除系统规则。 |
| `review.disabledRules[]` / `.largeDiffThreshold` / `.maxFindings` | 审查规则配置。 |
| `workspaces[]` | 额外 workspace root（`name` / `path` / `mode`）。 |
| `execution.agentBashBackend` | `auto` \| `host` \| `docker` \| `os`；空串表示继承 Runtime。 |
| `mcp.servers[]` | MCP 服务器（`name` / `command` / `envAllowlist` / `startupTimeoutSeconds` / `callTimeoutSeconds`）。 |
| `hooks[]` | 工具事件钩子（`event` / `command` / `match` / `timeout` / `blocking`）。 |

`.aicode/rules.md` 用于项目规范，`.aicode/memory.md` 用于长期项目记忆。**三者都只是上下文，不能提升权限**：trust、预算上限和 mandatory protected paths 都不接受项目级覆盖。

mandatory protected paths（`project/config.py:mandatory_protected_paths`）覆盖 `.env*`、`.ssh`、`.gnupg`、`.aws`、`.azure`、`.kube`、`.config/gcloud`、`.config/gh`、`.docker`、`.git/config`、`.git-credentials`、`.netrc`、`.npmrc`、`.pypirc`、`*.pem`、`*.key`，每项都带 `**/` 变体。默认还追加 `secrets/**` 与 `infra/prod/**`，这两条项目可以移除，前面那些不行。

## 12. Model Router

Runtime 支持两类 provider：

- OpenAI-compatible Chat Completions（`models/openai_compatible.py`）。
- Anthropic Messages API（`models/anthropic.py`）。

路由按 purpose 选择模型：`main` ← `models.main`，`reviewer` ← `models.reviewer`，`summarizer` ← `models.summarizer`。

OpenAI-compatible Profile auth mode 支持 `required`、`optional`、`none`。`none` 永不发送 Authorization，适合 no-auth localhost；`required` 缺少 key 时快速失败。Runtime 不会静默降级。

每个 purpose 在调用前解析 `provider + model` capability，包括 context window、max output、native tools、streaming 与 tokenizer。精确 model map（`<provider>:<model>` 优先于 `<model>`）优先，随后使用 Profile 默认值。`tool_calling=false` 或 `streaming=false` 会在 provider 请求前快速失败，**禁止从正文猜 tool JSON**——一个从自由文本里解析出来的"工具调用"没有 schema 保证，policy 层拿到的就是猜测。

`GET /v1/models/probe` / `aicode runtime models probe` 按 configuration → `/v1/models` → selected model → SSE → tools 分阶段探测，并返回 versioned result 和稳定错误 code。Profile/probe contract 分别由 `schemas/provider-profile.schema.json` 与 `schemas/provider-probe.schema.json` 定义。

## 13. Sessions And API

完整 HTTP 表面（`app/server/main.py`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/daemon/status` | 存活、版本、audit writer 健康度。免鉴权。 |
| POST | `/v1/daemon/prepare-stop` | 停机前取消全部活跃 execution。 |
| GET | `/v1/meta/contract` | contract 版本与 transport capability。 |
| POST | `/v1/executions` | 提交一次 execution（沙箱命令）。 |
| POST | `/v1/executions/{id}/cancel` | 取消 execution。 |
| GET/POST | `/v1/trust` | 查询 / 授予 trust。 |
| POST | `/v1/trust/remove` | 撤销 trust。 |
| POST | `/v1/sessions` | 创建 session。 |
| GET | `/v1/sessions` | 列表（摘要 + `limit`/`offset`）。 |
| POST | `/v1/sessions/prune` | 按保留策略清理。 |
| GET | `/v1/sessions/{id}` | 单个 session 完整历史与 agent run state。 |
| POST | `/v1/sessions/{id}/fork` | 从指定消息派生新 session。 |
| POST | `/v1/sessions/{id}/messages` | 发起一次 run。 |
| GET | `/v1/sessions/{id}/events` | SSE 事件流。 |
| POST | `/v1/sessions/{id}/approve` | 批准未决审批。 |
| POST | `/v1/sessions/{id}/reject` | 拒绝未决审批。 |
| POST | `/v1/sessions/{id}/answer` | 回答 `ask_user` 提问。 |
| POST | `/v1/sessions/{id}/cancel` | 取消当前 run。 |
| POST | `/v1/sessions/{id}/steer` | 排队 steer guidance。 |
| POST | `/v1/sessions/{id}/compact` | 空闲时强制压缩。 |
| GET | `/v1/usage` | 全局用量。 |
| GET | `/v1/usage/sessions/{id}` | 单 session 用量。 |
| GET | `/v1/models/routes` | 每个 purpose 的实际路由与 capability 来源。 |
| GET | `/v1/models/probe` | 分阶段 provider 探测。 |
| GET | `/v1/review/rules` | review 规则及项目配置后的生效状态。 |

所有 Runtime API 默认只监听本机地址，并通过 CLI 写入的 token 做本地鉴权。**认证是 fail-closed 的**：未配置 `AICODE_RUNTIME_TOKEN` 时拒绝一切请求（`/v1/daemon/status` 除外），而不是放行——放行意味着本机任何进程都能伪造 approval，替用户批准一次编辑或一条高风险命令。无认证访问需通过 `AICODE_ALLOW_ANONYMOUS=1` 显式选择，且该开关只在未配置 token 时生效。

运行中的 session 还在内存中维护 `current_run_id`、当前阶段、开始时间和最后进度时间。`GET /v1/sessions/{id}` 与 session 列表会返回这些字段，用于区分模型流、工具执行和审批等待。取消当前 run 时 Runtime 会取消 runner task；命令工具会在收到 cancellation 后终止整个子进程组，然后队列继续消费下一条 run。

## 14. Persistence

Runtime 持久化以下内容：

- session metadata、user / assistant / tool messages。
- 版本化 compaction entries（覆盖 message id 范围、结构化摘要、provider/model、prompt version、token 估算、context window 和创建时间）。
- SSE events、approval 状态、usage records。
- audit JSONL。
- 仓库外 Project Trust store。

`messages` 是 append-only source of truth，compaction 只定义发给模型的可重建 projection，不删除或覆盖原始历史。恢复 session 时选择最近一个仍指向有效 message range 的当前 schema 版本 compaction，并拼接其后的原始消息。压缩边界以完整消息组为单位：assistant tool calls 与其 tool results 不会拆开，未完成 tool call 不能进入摘要。

### 14.1 三级上下文管理

上下文压力有三档，从便宜到昂贵依次尝试：

**第一档：写入时截断。** 工具输出在落库前就按上限截断，明确标注截断量。

**第二档：折叠（fold）。** 此前只有截断和整段摘要两级，所以只超出阈值一点点也要付一次模型调用，并丢掉本可以只靠丢弃旧工具输出就保住的细节。折叠把较早的 tool 结果替换成一行引用，保留最近若干组的原文；`keep` 从大往小试，只折叠到刚好装得下为止。

**只折叠 `role == "tool"` 的消息**，这是"零损失"说法的依据：用户的目标与约束、助手自己的推理一字不改，被丢弃的只是它们据以产生的体量。折叠以完整消息组为边界，未闭合的 tool_call 组整体跳过，与摘要用的是同一条边界规则。

折叠是**纯投影**，不写回 `session.messages`，每轮从原始消息重新计算。这是它与被删掉的 `compact_if_needed` 的关键差别：后者改的是内存里的历史副本，于是与持久化的 source of truth 产生漂移。确定性重算既不需要持久化，也不可能漂移。

**第三档：结构化摘要。** 折叠后仍超限才进入。摘要模型被要求返回固定字段的 JSON（`goal` / `constraints` / `done` / `pending` / `files_touched` / `open_failures`），`CompactionEntry` 同时存下结构化结果与由它确定性渲染出的文本。自由文本在连续压缩（对摘要再摘要）下降解很快，且没有形状可供程序化检查——丢了什么无从判断。

关键不在于字段本身，而在于**`pending` 与 `open_failures` 由代码续接，不依赖模型自觉重复**。模型在下一轮摘要里省略一条未完成工作，它就永久消失了；因此上一轮的这两个字段会被重新并入，只有当模型把该条目显式列进新的 `done` 才移除。文本匹配是模糊的，但它的偏差方向是安全的：认错会把已完成项继续留在 pending（最坏是重复劳动），而不会丢掉未完成项（最坏是静默放弃工作）。

模型返回无法解析的结构时降级到确定性摘要，`context.budget` 标记 `summary_mode=fallback` 与 `summary_error=invalid_structure`，并且**不把自由文本存成结构化 schema 版本**——否则下一轮的续接会静默变成空操作。

`COMPACTION_SCHEMA_VERSION` 升到 2 后，v1 条目被 `latest_valid_compaction` 忽略而非读取：自由文本摘要无法与结构化摘要合并，混用会把结构本要消除的降解重新引入。忽略是安全的——`messages` 是 append-only source of truth，被忽略的 compaction 只是多跑一次压缩。

主 provider 报告 context overflow 时只做一次强制 compaction + retry，重复 overflow 不再重试。

### 14.2 失效读取不进入摘要

compaction 把 `read_file` 输出当作事实写进摘要，如果该文件此后被改动，摘要里就留下一段被表述为当前内容的过期内容——而模型再也看不到原始消息，无从察觉。

因此每条 `read_file` 结果消息上持久化一份 Runtime 私有 provenance（`aicode_meta.read = {path, hash}`），compaction 时按该 hash 与磁盘现状比对，不符者其内容替换为"该文件已变更，需要时重新读取"，已删除的同样标记，并在 `context.budget` 事件中以 `stale_reads` 列出。

比对的是**那一次读取当时的 hash**，不是 session 的滚动 `read_files` 记录。这个区别就是这条机制的全部要点：`read_files` 在写入时也会更新，所以 Agent 自己编辑过文件之后该记录已与磁盘一致，拿它比对会漏掉 Agent 自己的编辑——而那正是读取失效最常见的原因，外部修改反而是少数情况。

provenance 持久化在消息上而非旁路表中，因此不会与它描述的消息脱节，也随 session 一起跨 daemon 重启存活；它在 `strip_message_meta` 处被剥离，绝不进入发给 provider 的 payload——provider 会拒绝未知消息字段，泄漏在这里是故障而非瑕疵。

### 14.3 Session fork

`fork(session_id, message_id)` 派生一个新 session，把截止到该消息的历史照常 append 进去，因此分叉产物就是一个普通 session，没有引入第二套机制。

历史**复制而非共享**：共享行会让两个 session 的未来互相延长对方的过去。compaction 条目的 `start_message_id` / `end_message_id` 必须**重映射**到新写入的 message id——message id 是全局自增的，原样搬运会指向别的 session 的行，让 fork 的 projection 去摘要一段不属于它的消息；映射不上的条目宁可丢弃（代价只是少省一点 token），也不写悬空引用。越过分叉点的 compaction 直接不带过去。

不属于该 session 的 `message_id` 直接报错而非就近裁剪：静默分叉到另一个点，产出的 session 看起来正确、历史却是错的。`plan` 随 fork 带走（同一件事的延续），`read_files` 不带——它按设计只存在于内存，保证的是"在**这轮**对话里见过该文件的当前内容"，fork 后本就应重读。事件为 `session.forked`。

### 14.4 读取形态与保留策略

`GET /v1/sessions` 与 `SessionStore.list()` 只返回摘要（metadata + `message_count` + agent run state），并支持 `limit` / `offset`。此前每次调用都会 hydrate 所有 session 的所有 messages 与 compactions——50 × 40 实测 69.4ms、每行约 82KB，且随使用时间单调增长，而唯一的消费者 `aicode session list` 只是展示一个列表。改后为 1.2ms、每行 438 字符。单个 session 的完整历史仍由 `get(session_id)` 提供。

列表也**不再写入内存缓存**：列举是只读概览，不应因此驱逐正在运行的 session。

保留策略（`SessionStore.prune`）默认关闭，两个上界都是 0。静默删除用户的对话历史比数据库无限增长更糟，因此不配置就不清理；配置后由 `aicode session prune` 或 `POST /v1/sessions/prune` 触发，一并删除对应的 messages / events / compactions，并发出 `session.pruned`。

**正在运行或有未决 approval 的 session 永不删除**，即使命中保留条件——它即将写回状态；这类被跳过的数量通过返回值的 `retained_live` 汇报，而不是静默忽略。

### 14.5 Schema 迁移

`PRAGMA user_version` 记录数据库已推进到的版本，`MIGRATIONS` 是一条**只追加**的有序 ladder，启动时只执行缺失的迁移。已发布的迁移不得重编号或修改——现网数据库记录的正是"我已经推进到第几步"。

- 迁移 1（基线表与索引）保持 `if not exists` 幂等：ladder 之前创建的数据库 `user_version=0` 但已持有这些表，必须能落到 ladder 上而不被重建。
- 迁移 2 / 3 收编了此前两个 bespoke 修补（补 `sessions.updated_at`、删除退役的 `sessions.language`），各自保留存在性检查。
- `user_version` 高于当前构建支持的版本时**拒绝打开**并抛 `SchemaVersionError`。带着未知 schema 继续运行会写出旧构建读不回的行，或静默忽略新构建依赖的列。

现实中最常见的升级路径是"表结构已是最终形态但没有版本戳"——此时三条迁移全部空转，只有版本戳前进，由 `test_already_current_but_unversioned_database_is_stamped_without_changes` 锁定。

### 14.6 SQLite 写入路径

单个 WAL 连接在整个 Runtime 内复用，参数：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`。

此前每次调用都新开一个 rollback-journal 连接且 `synchronous=FULL`，一条消息写入约 5.2ms，全部发生在事件循环上——而同一个循环正在向 SSE 订阅者推送 `assistant.delta`。改为复用 WAL 连接后降到约 1.1ms（约 4.9x），连接建立开销和每事务 fsync 都被消除。

- WAL 让读不被写阻塞，这对 `list()` 和 resume 在 run 仍在追加消息时读取很重要。
- `synchronous=NORMAL` 是 WAL 下的标准耐久性取舍：崩溃可能丢失最近若干次提交，但数据库不会损坏。session 是可恢复的本地状态，不是记账系统。
- 连接同时被事件循环和事件 write-behind 工作线程使用，因此 `check_same_thread=False` 搭配一把覆盖整个事务（而非单条语句）的锁。

剩余的约 1.1ms 未再移到线程池：把 `append_message` 改成异步需要让 `persist_message` 及 `AgentSession` 协议一并异步化，而 1ms 级别的单次阻塞对 SSE 流式输出已不构成可感知影响，收益不足以支撑这个扩散。

## 15. Audit And Usage

审计日志记录本地敏感操作的结构化事件，命名空间与 SSE 事件**独立**（审计有自己的 `session.final` / `session.error` / `tool.finished` / `execution.*` 等）：

- `tool.call` / `tool.started` / `tool.finished` / `tool.denied` / `tool.error`、参数解析与校验失败。
- bash 风险分类结果。
- `approval.requested` / `approval.resolved` / `approval.expired` / `approval.accept_all_enabled`、`question.asked` / `question.answered`。
- `edit.applied` / `edit.rejected` / `edit.auto_approved`。
- `execution.started` / `execution.finished`（覆盖 Host、Docker 与 OS 沙箱）。
- `hook.finished` / `hook.blocked`、`mcp.server.*`。
- `session.created` / `session.forked` / `session.pruned` / `session.final` / `session.error`。
- `context.budget` / `context.compact.requested` / `context.compaction_retry`、`usage.recorded`。

敏感字段会脱敏（`audit/redaction.py`）：API key、token、authorization header 和常见 secret 环境变量；已知 Runtime secret 也会从 neutral fields、tool output 和 SSE 中替换。写入 patch 时审计记录 `patch_hash` 和 `diff_bytes` 而不是完整 diff；execution 记录 command hash 而不是原始命令。

### 15.1 审计不丢失、不无限增长

审计日志是安全证据链，因此它和普通日志有两条不同的要求：

- **不静默丢弃。** 写入队列满时不丢弃事件，而是降级为当前线程内同步写入。队列深度 5000，打满意味着 writer 已经跟不上，属于病态情况；此时一次阻塞的 append 好过证据链上出现一个没人知道的空洞——否则"没有危险命令的记录"和"没有发生危险命令"就变得不可区分。
- **不无限增长。** 按大小轮转（默认 64MB × 5 个备份），可用 `AICODE_AUDIT_MAX_BYTES` / `AICODE_AUDIT_BACKUP_COUNT` 调整。

写入失败会重试一次；持续失败时计数、记录 `last_error`、在 stderr 上报告一次（而不是每条事件都刷屏），并通过 `status()` 的 `healthy` 字段暴露给 `aicode runtime status`。写入路径**绝不向调用方抛异常**——异常逃逸会杀掉 writer task 或中断一次 agent turn。

对比：session **event** 持久化仍是 best-effort，队列满时允许丢弃。两者的差别是有意的——event 只影响 resume 时的回放展示，真正的 agent 历史由 messages 表独立、可靠地持久化。

### 15.2 分布式追踪（可选）

`SpanTraceSink` 是 TraceSink 的**装饰器**而非替代：它把每个事件转发给 JSONL sink，同时派生 span。JSONL 仍是真相来源，追踪是叠加的——丢掉追踪后端绝不能代价一条审计记录。

span 由 Runtime 本来就在记录的 start/finish 事件对派生，得到层级 `run → tool.call → execution`：

| 开启 | 关闭 | 关联键 |
| --- | --- | --- |
| `run.started` | `session.final` / `run.cancelled` / `session.error` | `run_id`（关闭事件按 session，因为 `session.final` 不带 run_id） |
| `tool.started` | `tool.finished` | `tool_call_id` |
| `execution.started` | `execution.finished` | `execution_id` |

其余事件成为最内层 open span 上的点事件。几条不变量：

- 关闭一个 span 会**连带丢弃在它内部打开的 span**——tool span 不能比发出它的 run 活得更久，否则被取消的 run 会永久留下一个 open span，下一个 run 的事件会挂到它下面。
- 只有 close 事件而没有对应 open（daemon 中途重启）时降级为点事件，而不是去关一个无关的 span。
- 无 session 的事件（trust 变更、保留策略清理）不进入追踪，JSONL 已经记录。
- span 属性复用 `audit/redaction.py` 的脱敏：span 会离开本机。
- 派生过程中的任何异常都被吞掉：坏掉的 exporter 是降级的可观测性信号，不是失败的 run。
- `aclose` 会关闭仍然打开的 span，避免被杀掉的 daemon 留下悬挂 trace。

OTel SDK 是可选依赖（`pip install 'aicode-runtime[otel]'`），懒加载。span 派生逻辑本身零依赖，因此可以在不安装 SDK 的情况下用内存 emitter 完整测试；`OtelSpanEmitter` 只是一层薄适配。开启追踪但 SDK 缺失会直接报错——运维以为在跑而实际没在跑的追踪后端比没有更糟。

### 15.3 Usage

Usage 记录按模型、session、purpose 和时间聚合 token 与成本估算。成本来自本地 `pricing` 配置，适合做近似统计，不等同于 provider 账单。没有配置价格时 `estimated_cost` 为 0，而 eval 报告会用 `unpriced_model_calls` 把"计价为 0"和"没配价格"区分开——0 读起来像事实。

## 16. Multi-Workspace

项目配置可以声明多个 workspace root。Runtime 在路径校验、读写工具和 protected paths 判断时会以这些 root 作为边界；只读工具通过 `workspace` 参数选择目标。

当前设计仍保持本地项目优先：

- 不自动扫描用户整个磁盘。
- 不默认跨 root 写文件（额外 workspace 是 `read_only`）。
- 不把另一个仓库的规则隐式混入当前项目。

## 17. Docker Sandbox

Docker Sandbox 是 Runtime ExecutionBackend 的隔离实现，Go CLI 只保留客户端入口。两个入口：

- **显式命令**（workspace 只读挂载）：`aicode project sandbox test|build|lint`。
- **Agent `bash` 工具**（workspace 可写挂载）：由 `execution.agent_bash_backend` 与 workspace trust level 共同决定，见 18.1。

安全默认值：

- 显式命令 workspace 只读挂载；Agent bash 需要创建文件和跑构建，因此 workspace 可写挂载，并以宿主 uid/gid 运行，避免在用户仓库里留下 root 拥有的文件。
- `--network none`。
- 不传 `.env`，并用空文件遮蔽仓库根目录 `.env*`。
- 只允许少量缓存相关环境变量（`HOME`、`GOCACHE`、`GOMODCACHE`、npm/yarn/pip cache），且指向隔离目录。
- 资源限制：默认 `--cpus 2`、`--memory 2g`、`--pids-limit 256`。
- 使用与 Agent Host 命令一致的 execution 终态、取消和 audit 格式。
- `--pull=never`：绝不隐式拉取镜像。镜像缺失会立即失败并提示 `docker pull`，而不是把一次工具调用变成数分钟无反馈的下载。`aicode runtime doctor` 会在安装期就报告缺失的沙箱镜像。

命令来源优先级：`.aicode/config.json` 的 `commands.test/build/lint`（值不是 `auto` 时直接使用）→ package.json scripts、Go module/workspace、Python pytest/ruff 配置等自动探测。

Runtime API 只接受 `test/build/lint` 三种 sandbox action，**不开放任意远程 shell endpoint**。它适合在隔离环境中验证命令是否能通过，但不是完整的远程执行平台；当前还没有实现 artifact 回收或复杂服务编排。

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
- Runtime token 只用于本机 CLI 与 daemon 通信，不是公网认证方案；认证 fail-closed。

Policy 层负责 **UX 分级**（这条命令要不要问用户），执行后端负责 **隔离边界**。两者是纵深防御关系，policy 不是唯一防线。

### 18.1 Agent 命令的执行边界

Agent `bash` 的落点由 `execution.agent_bash_backend` 与 workspace trust level 共同决定：

| 配置 | trusted workspace | 其它（untrusted / unspecified） |
| --- | --- | --- |
| `auto`（默认） | host | docker |
| `host` | host | host |
| `docker` | docker | docker |
| `os` | OS 沙箱 | OS 沙箱 |

- Runtime 级：环境变量 `AICODE_AGENT_BASH_BACKEND`。
- 项目级：`.aicode/config.json` 的 `execution.agentBashBackend`；取值非法时回落到"继承 Runtime 设置"，而不是回落到宽松默认——配置里的拼写错误绝不能悄悄削弱沙箱。
- system prompt 会声明当前的执行环境（是否禁网），使模型不会围绕它并不具备的能力做计划。

**不做静默降级**：当命令被路由到 docker 但 Docker CLI 不可用时，`bash` 直接失败并提示用户启动 Docker、执行 `aicode project trust add`，或显式改配置为 `host`。回退到宿主机执行会把一个安全边界变成安慰剂，因此这条路径由 `test_bash_fails_loudly_when_sandbox_is_unavailable` 与评测任务 `untrusted_bash_sandboxed` 双重锁定。

评测任务断言的不变量是"untrusted workspace 永不产生 host backend 的 execution"——这个断言在有无 Docker 的机器上都成立：有 Docker 则走沙箱，无 Docker 则被拒绝，只有回退到 host 的回归才会让它失败。

### 18.2 OS 级沙箱（`os`）

平台自带沙箱（macOS seatbelt）作为容器之外的第二种隔离后端。它启动是毫秒级、不依赖任何镜像，因此**"默认沙箱"对 trusted workspace 也成立**——容器太重正是 trusted 至今裸跑的原因。它比容器弱：同一文件系统命名空间、同一内核、无资源限制。它保证的是两件事：**工作区之外不可写**，以及**禁网**（protected paths 另行禁读）。

因为强弱不同，它是一个**独立选项，而不是 `auto` 替用户做的替换**；`auto` 的语义保持不变。

profile 采用 `(allow default)` + 定点拒绝，而不是 `(deny default)` + 白名单。deny-by-default 需要枚举真实工具链触碰的每一个 mach service、sysctl 与共享内存区；名单列漏不会削弱沙箱，只会让编译器以"莫名其妙的工具失败"的形式崩掉。真正要守的那两条不需要那份名单也能强制。protected paths 的禁读规则必须排在广泛读许可**之后**（seatbelt 取最后一条匹配规则）。

系统临时目录保持可写：无法创建临时文件的构建工具不是被沙箱化了，只是坏了。profile 里的路径经过转义——路径含引号会提前终止字符串并改变规则语义，那是注入而非排版问题。

后台命令不走 `ExecutionRequest`，因此沙箱包装在那条路径上**显式再做一次**；否则选了 OS 沙箱恰好会豁免最需要它的长时命令。

macOS 之外不支持：Linux 需要 landlock 或 bubblewrap，而**声称一条并未真正生效的边界比明说不支持更糟**，因此非 macOS 上选 `os` 会直接失败并说明原因。

### 18.3 项目 hooks

`.aicode/config.json` 的 `hooks` 把命令挂在两个工具事件上：`post_edit`（编辑落盘后，用于格式化）与 `pre_bash`（shell 命令前，非零退出即拒绝该命令，用于 lint 门禁）。

**hook 命令与 Agent 命令共用 `run_shell_command` → `ExecutionService` → 审计这条路径，并同样经过 `PolicyEngine`**。这是本特性的约束而非实现细节：如果 hook 绕开这些检查，它就成了"把命令写进配置文件即可执行任意内容"的旁路，而这正是 deny 列表要防的。审计以 `hook.<event>` 标记，backend 由同一个 `resolve_bash_backend` 决定——沙箱化的 workspace 不会因为声明了 hook 就多出一条宿主机执行的侧路。

**untrusted workspace 不执行任何 hook。** `.aicode/config.json` 随仓库分发，hook 因此是仓库作者选择的代码；只要打开目录就执行它，等于 clone 一个恶意仓库便足以运行其命令。这条与"不做静默降级"同源：不执行会显式上报（`hook.blocked`），因为没触发的 hook 不能与通过了的 hook 长得一样。

`{path}` / `{command}` 替换一律 `shlex.quote`。仓库可以包含名为 `a; rm -rf ~.py` 的文件，直接拼接会把 post_edit 格式化变成任意命令执行——这是注入，不是排版。

`post_edit` 结构上无法否决编辑（运行时文件已落盘），失败只上报；能拒绝的只有 `pre_bash`。hook 改写文件后刷新 read 记录，否则模型对同一文件的下一次编辑会被 read-before-write 判为 stale。

### 18.4 Anthropic prompt caching

由 `settings.anthropic.prompt_caching` 开关控制（`AICODE_ANTHROPIC_PROMPT_CACHING`），**默认关闭**。关闭时 payload 形状与启用前逐字节一致——这既是回退路径，也是 A/B 的前提：把成本变化归因到 caching 的唯一办法，是对照组必须是完全相同的请求。

Anthropic 的渲染顺序是 `tools` → `system` → `messages`，因此**打在最后一个 system block 上的断点同时覆盖了 tool 定义**。tools 末元素上另打一个更靠前的断点：system prompt 变了而工具集没变时，工具部分仍然命中缓存，而不是整段前缀重写。每请求最多 4 个断点，这里用掉 2 个。

**`TOOL_SCHEMAS` 是模块级共享常量，打断点前必须深拷贝。** 原地标注会给之后每一个 OpenAI-compatible 请求都挂上一个 Anthropic 专有的 `cache_control` 键——这种污染出现在离现场很远的地方，而且只在两个 provider 跑在同一进程里时才暴露。深拷贝而非浅拷贝：浅拷贝仍然共享被写入的那些 dict。

`Usage` 增加 `cache_creation_input_tokens` / `cache_read_input_tokens`。**`input_tokens` 是未命中缓存的余量而不是整个 prompt**：prompt 总量是三者之和，只累加 `input_tokens` 会把缓存服务掉的部分漏掉。这两个字段在不支持缓存的 provider/model 上直接缺失，因此按 0 读取而不是报错——它们是对本次请求的报告，不是 API 的承诺。

`estimate_cost` 分档计价：cache write 按 input 单价的 1.25×（5 分钟 TTL；1 小时 TTL 是 2×，aicode 只写默认 TTL），cache read 按 0.1×。两个参数默认为 0，因此不知道 caching 存在的调用方拿到的结果与之前逐位相同——分档是叠加而不是对普通 token 计价方式的改动。

**最小可缓存前缀与模型相关且不单调**（Opus 5 为 512 token，Opus 4.8 / Sonnet 5 为 1024，Opus 4.6 / Haiku 4.5 却是 4096）。低于阈值时**不报错、静默不缓存**，表现为 `cache_creation_input_tokens` 恒为 0——排查"开了却没省钱"时先看这里。

### 18.5 嵌入用 stdio JSONL RPC（SDK）

`app/sdk/` 提供第二种传输：一行一个 JSON 对象，走 stdin/stdout。它**架在与 HTTP server 同一个 `ApplicationRuntime` 上**——嵌入方拿到的是同一套 session / run / approval / policy 语义，而不是一份会漂移的第二实现；两者唯一的差别是那根管子。

选行分隔而非带长度前缀的帧格式，是因为传输本身就是宿主已经有的管道：一行即一条消息，任何语言用 `readline` 就能读，不需要写解析器。

三种形状：`{"id","method","params"}` 请求、`{"id","result"|"error"}` 响应、`{"method","params"}` 通知（无 id，因而无从对应回复）。错误码是字符串而非整数——嵌入方要在日志和 `except` 分支里读它，`"unknown_method"` 不需要查表。

方法集：`initialize`、`session.create`、`session.get`、`session.prompt`、`session.cancel`、`session.subscribe`、`session.events`、`approval.resolve`。

**握手是强制的**：`initialize` 之前的任何方法都被拒绝。`PROTOCOL_VERSION = 1`，`MIN_PROTOCOL_VERSION = 1`。协商失败时**直接拒绝而不静默降级**——请求了本 Runtime 不会说的版本的宿主，心里想的是某些功能，降级只会把失败推迟到某次具体调用上，那时归因比握手被拒难得多。返回体带上支持区间（`supported_min` / `supported_max`），宿主据此决定升级还是退让。

**背压**：出站队列有界。宿主不读管道时不能把 Runtime 内存撑爆，因此队列满了就**阻塞事件泵**（真背压），由 session 自己那个有界缓冲承受，按最旧丢弃。这条丢弃路径**可检测而非静默**：`event_id` 是 per-session 单调序列，缺口即精确告诉宿主漏了什么，`session.events` 按游标补齐。

关停时**先排空再取消 writer**：输入结束不等于回复已经写出，直接取消会把宿主最后一条请求的响应悄悄丢掉。

`python -m app.sdk` 把当前进程变成一个 RPC server。**stdout 是协议通道，任何别的输出都不能往那里写**——一句多余的 print 会以"Runtime 发来解析错误"的形式污染流。

## 19. Review Rules

`aicode task review` 在只读模式下审查当前 git diff，使用 `reviewer` 模型路由。`review_diff` 工具跑 18 条确定性规则：

| 类别 | 规则 |
| --- | --- |
| 凭据与路径 | `secret_added`（含 `id_rsa` / `id_dsa` / PEM 私钥标记）、`sensitive_path` |
| 测试完整性 | `deleted_test` |
| 动态执行 | `risky_eval`、`risky_exec`、`risky_os_system`、`risky_shell_true`、`risky_child_exec` |
| 前端 | `risky_inner_html`、`risky_dangerously_set_inner_html` |
| 反序列化 | `risky_yaml_load`、`risky_pickle` |
| 传输与权限 | `risky_tls_verify`、`risky_go_insecure_tls`、`risky_chmod_777` |
| 卫生 | `large_diff`、`task_marker_added`、`debug_output` |

规则是确定性的，不是模型判断——它们是模型审查之上的地板，保证某几类问题即使模型漏掉也会被报出来。项目可通过 `review.disabledRules` 关闭具体规则，`GET /v1/review/rules`（`aicode project review list`）显示配置后的生效状态。

## 20. Agent Eval And Trace

`evals/` 提供独立于真实 provider 的任务级评测层。CI profile 使用 scripted model，但仍调用真实 Agent Loop、ModelRouter、PolicyEngine、ExecutionService、SessionStore 和 compaction 路径，因此可以稳定捕获 Agent 行为回归，而不把外部模型波动引入普通 CI。

每个 eval task 固定 fixture、请求、mode、provider/model capability、turn/token/cost/wall-time 预算、审批策略、scripted model turns 和确定性 checks。Runner 将 fixture 复制到独立临时目录，创建带固定时间与 identity 的初始 Git commit；grader 依据测试命令、文件断言、changed/forbidden paths、event/audit、approval 和 compaction 规则评分。

当前五个 suite：

| Suite | 任务数 | provider mode | 问题 |
| --- | ---: | --- | --- |
| `smoke` | 5 | scripted | Agent Loop 实现是否正确（CI 门禁） |
| `live` | 28 | live | Agent 能否完成真实任务 |
| `live_hard` | 8 | live | 因果分离时还能不能定位 |
| `live_scale` | 2 | live | 仓库规模构成难度吗 |
| `live_scale_curve` | 4 | live | 检索成本随规模怎么长 |

每个 run 生成 schema v1 trace manifest，包含 task/fixture digest 与初始 commit、model/prompt/tool schema/policy/compaction fingerprint、model call 与 tool call 定位信息、diff path/numstat/content hash、grader checks、预算结果与 token/cost/latency/安全指标。

Agent shell command、模型正文、tool output 和 edit/diff 正文不会完整写入 trace；使用 hash、字符数、安全字段和已脱敏错误定位问题。Suite 同时输出 JSON 与 Markdown report，并与 `evals/baselines/deterministic-smoke.v1.json` 的 threshold/fingerprint 比较。

### 20.1 scripted 与 live 两种 provider mode

`provider_mode: scripted | live` 是同一套 harness 的两条链路，回答的是两个不同问题：scripted 证明 **Agent Loop 按脚本回放正确**，live 测量 **Agent 能否完成真实任务**。因此 live 是新增而非替代——scripted 的零成本、零抖动回归价值不可替代，它继续做 CI 门禁；live 因成本与不确定性不进 PR CI。

`LiveEvalProvider` 对外暴露与 `ScriptedEvalProvider` **完全相同**的 `calls` / `total_tokens` / `total_cost` 表面，`run_metrics`、trace writer 与预算检查因此不需要任何 mode 分支。它包装 `ModelRouter.from_settings` 选出的真实 provider，复用而不是重复 provider 选型逻辑。

live 的预算是**硬停**而非事后统计：超限中止运行，因为超支花的是真钱。真实 token 数只有响应落地后才知道，所以检查在调用之后执行——这仍然能阻止**下一次**调用，而那正是约束支出的地方。缺 API key 在建目录、发请求之前就失败：跑到一半才发现没配 key，钱已经花掉，而且失败读起来像是 Agent 不行而不是配置问题。

live settings 取环境里的 credential 与 base URL，但**不取**环境里的 model route、context window 与价格——那三样来自 task，否则报告的成本列描述的是开发者 shell 恰好设成了什么。

task 里 pin 的 profile 是**参考 profile**，保证已发布数字可复现；`--live-profile`（JSON，merge 进 profile）用于把同一套任务集重定向到别的 provider。整个 profile 作为**一个单元**覆盖，而不是只开放 provider：provider、context window 与价格并不独立，只换 provider 会留下原窗口与原价格，于是同时产生两个静默错误——harness 以为自己有并不存在的上下文因而从不压缩，报告按一个从未运行过的模型计价。覆盖值走完整校验而非就地打补丁，非法 provider 或负价格在启动时失败，而不是在付费跑到一半时以困惑的形式出现。

### 20.2 mutation check

"Agent 补了测试"这件事没法靠"套件通过"判定——空测试文件也通过。`checks.mutations` 声明一处蓄意缺陷，grader 把它打进工作区**副本**再跑测试命令，要求失败。

mutation 写在 task JSON 而不是 fixture 里：fixture 会被整个复制进 Agent 的 workspace，放在那里的答案卡会把"为空输入边界写测试"退化成"读一下我们改坏了哪行"。副本而非原地修改，是为了让 diff 与 content hash 不被评分过程污染。

### 20.3 失败归因

失败运行按确定性优先级归类：`safety_violation` > `budget_exhausted` > `agent_error` > `localization_failure` > `verification_failure` > `edit_failure`。全部由既有 trace 字段推导——哪些 check 失败、哪些文件变了、Agent 有没有跑过命令——因此可从存档 trace 复现。

**不引入 LLM-as-judge**：评委本身是模型的话，"为什么失败"就变成了第二个需要评测的东西。定位失败判据取工作区实际变更而非 edit 事件：应用后又被回滚的 edit，任务同样没被解决。

报告另有 `functional_pass_at_1` 与 `policy_only_failures`：只记录判负结果，会让"解出来但越界"和"根本没解出来"长得一样，而在难度校准上这两者结论正好相反。

### 20.4 reference solution

`evals/reference_solutions.py`（以及 `_hard` / `_curve` 变体）为每个 live 任务保存一份已知可行解，由测试套件校验"该解能让套件转绿"且"每条 mutation 都被参考测试抓到"。无解的任务报出来的是出题人的 bug 而不是模型的失败，这两者必须能区分。每条 mutation 还要被**第二份写法不同的解**抓到：只对一份参考解校验，分不出"mutation 写得对"和"mutation 恰好对上了这份参考解挑的输入"。参考解不放进 fixture，同 mutation 的理由。

`live_scale_curve` 另有两条由测试钉住的不变量：缺陷不放在模块序列的首尾（靠位置习惯就能找到的不算检索），以及四档之间**只有规模在变**——请求、预算、profile 若有漂移，曲线量的就是漂移而不是规模。

## 21. Repository Layout

```text
.
├── cli/                    # Go CLI
│   ├── main.go             # category dispatch and legacy alias rewriting
│   └── internal/
│       ├── cmd/            # chat, task, session, runtime, project, config
│       ├── client/         # Runtime HTTP/SSE client, contract tests
│       ├── config/         # ~/.aicode/config.toml
│       ├── daemon/         # daemon lifecycle and token
│       ├── projectconfig/  # .aicode/config.json writers
│       └── renderer/       # SSE rendering
├── runtime/                # Python Runtime
│   └── app/
│       ├── agent/          # Agent Loop, prompts, history/fold/summary, policy, ports, domain types
│       ├── application/    # session/run/approval/context services over the ports
│       ├── audit/          # audit logger, redaction, OTLP span derivation
│       ├── execution/      # host, Docker, OS sandbox, background processes
│       ├── models/         # provider clients and router
│       ├── project/        # project config, detection, external trust store
│       ├── sdk/            # stdio JSONL RPC transport
│       ├── server/         # FastAPI routes and auth
│       ├── sessions/       # SQLite sessions/events/approvals, in-memory repo
│       ├── tools/          # registry, built-in tools, hooks, MCP client, adapters
│       ├── usage/          # usage tracking and pricing
│       ├── bootstrap.py    # the only composition root
│       ├── config.py       # settings loading
│       ├── events.py       # SSE encoding and the event type registry
│       ├── security.py     # secret classification, redaction, hashing, path guards
│       └── system.py       # clock and id generation
├── runtime/tests/          # integration and behavior tests
├── evals/                  # tasks, isolated fixtures, graders, runner, baselines,
│                           # reference solutions
├── schemas/                # runtime and eval contracts
├── scripts/                # installer, clean-home E2E, eval curve analysis
├── README.md               # user-facing guide
├── ARCHITECTURE.md         # this document
├── ROADMAP.md              # capability status and remaining plan
└── TASKS.md                # dependency-ordered task ledger
```

`docs/` 存在于工作副本中（设计计划、架构评审、评测报告、SDK 示例），但**它在 `.gitignore` 里，不随仓库分发**。文档中出现的 `docs/...` 链接只对本地检出有效。

## 22. Current Gaps

仍可继续推进的方向：

- 引入更强的代码索引（symbol、import graph、test mapping / repo map）。`live_scale_curve` 的增长指数就是决定该不该做的证据。
- 为 Docker Sandbox 增加可控写入目录和 artifact 导出。
- 增强多 provider fallback 和 per-route 健康检查。
- 补充更细的恢复语义，例如跨进程 approval continuation。
- MCP 的 HTTP transport。
- 让 `/v1/meta/contract` 的 transport 描述符如实反映已发布的 stdio JSONL RPC，而不是继续标 `planned`。
- 扩展真实模型评测集与定时 baseline；当前四档 live suite 的功能性通过率均为满分，缺的是有区分度的难度。

架构目标保持不变：先把本地 Agent 的安全边界、可恢复性和可解释性做扎实，再逐步扩展更复杂的执行环境和产品形态。
