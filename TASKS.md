# aicode 执行任务台账

更新日期：2026-07-31

来源：

- [LOCAL_AGENT_ROADMAP.md](LOCAL_AGENT_ROADMAP.md)（T-001 ~ T-012）
- [docs/review/2026-07-29-architecture-review.md](docs/review/2026-07-29-architecture-review.md) 与 [docs/plans/2026-07-29-production-agent-hardening.md](docs/plans/2026-07-29-production-agent-hardening.md)（T-013 ~ T-032）
- [docs/review/2026-07-31-agent-architecture-gaps.md](docs/review/2026-07-31-agent-architecture-gaps.md) 与 [docs/plans/2026-07-31-agent-architecture-plan.md](docs/plans/2026-07-31-agent-architecture-plan.md)（T-033 ~ T-048）

状态：

- `[ ]` 未开始
- `[~]` 执行中
- `[x]` 已完成
- `[!]` 阻塞

执行规则：

1. 严格按依赖推进；安全、contract 和迁移任务优先于新功能。
2. 每个任务必须包含实现、测试、文档或任务状态更新。
3. Agent 行为变更必须说明评测覆盖；没有覆盖时不能宣称质量提升。
4. 每次只保留一个主任务为 `[~]`，完成验收后再启动下一项。
5. **新增 SSE event 必须三处同步登记**：`runtime/app/events.py` 的 `EVENT_TYPES`、`schemas/events.schema.json` 的 enum、`schemas/fixtures/sse-events.v2.json` 与 Go renderer 分支。漏登记会在 `SessionEvents.put` 处直接抛 `ValueError`。
6. **改动 policy / prompt / 工具声明必须重新生成 eval baseline**：`evals/baselines/deterministic-smoke.v1.json` 的 `source_versions` 固定了 `policy_sha256`、`prompt_sha256`、`tool_specs_sha256` 等 digest，任何字节级改动都会让 `make eval-smoke` 失败。`tool_specs_sha256` 覆盖每个工具的 `description` / `input_schema` / `read_only` / `approval` / `hidden_in_modes`——即一切能改变模型行为或闸门判定的声明（2026-07-31 修复；此前它只 hash 元 schema 文件，逐工具声明不在覆盖内）。更新 baseline 前必须确认 `minimum_metrics` / `maximum_metrics` 仍然满足；若 `safety_rate` 下降或 `dangerous_command_execution_rate` 上升，按任务失败处理，不得放宽基线。

> **包结构说明（2026-07-30）**：Runtime 已完成一次包重组——`app/core/`、`app/adapters/`、`app/config/`、`app/contracts/` 不再存在，port 与 domain 类型并入 `app/agent/`，adapter 实现按领域就近放置，composition root 为 `app/bootstrap.py`。本台账中指向**现存文件**的路径已按新结构更新；T-001~T-012 的完成记录描述的是当时的产物，保留原路径并在需要处注明新位置。`docs/review/` 与 `docs/plans/` 下带日期的快照文档整体保留当时的路径，不做改写。当前结构见 [ARCHITECTURE.md](ARCHITECTURE.md) 第 3.3 节与第 20 节。

## M0：可交付基线

### `[x]` T-001 Tool/Event contract 基线

对应：WP0.4

范围：

- 盘点 Runtime 暴露的 tool 与产生的 event。
- 修复 `schemas/tools.schema.json` 与实现的漂移。
- 为 event type 建立 Runtime 可校验的注册表。
- 增加 schema 与 Runtime 双向一致性测试。

验收：

- Runtime tool names 与 tool schema enum 完全一致。
- Runtime event names 与 event schema enum 完全一致。
- 新增未登记 event 时测试或运行时立即失败。
- Python contract tests 与全量测试通过。

完成记录（2026-07-26）：

- `related_files` 已加入 canonical tool schema。
- 新增 Runtime `EVENT_TYPES` 注册表，`SessionEvents.put` 拒绝缺失或未知 event type。
- tool/event schema 增加 `2.0` contract version 和 event enum。
- 新增双向 drift tests。
- 验证：定向测试 54 项、Python 全量测试 211 项、Go 全量测试、Python compile 和 `go vet` 全部通过。

### `[x]` T-002 HTTP/SSE contract fixture

对应：WP0.4

依赖：T-001

范围：

- 为关键 SSE event 建立最小 fixture。
- 增加 Go client/renderer 对 fixture 的兼容测试。
- 明确未知字段和未知 event type 的兼容行为。

完成记录（2026-07-26）：

- 新增共享 `http-responses.v2.json` 与 `sse-events.v2.json` fixture，覆盖关键 HTTP response 和全部 v2 event。
- Runtime 为 send/cancel endpoint 增加显式 response model。
- Go client 支持 cancel idle response 的 nullable `run_id`。
- 固定兼容规则：未知字段忽略或透传；未来 event 由 Go client 透传、renderer 回显 JSON；只有 `final` 结束 stream。
- renderer 补齐 `approval.expired`、`error`，并适配当前 token-based `context.budget`。
- 验证：Python contract/server 定向测试 24 项、Go client/renderer/cancel 定向测试、Python 全量测试 213 项、Go 全量测试、Python compile、`go vet` 和 `gofmt` 全部通过。

### `[x]` T-003 版本化 Runtime 安装布局

对应：WP0.1

依赖：T-001

范围：

- 定义 CLI、Runtime、venv 和 manifest 的安装布局。
- daemon 优先从安装 manifest 解析 Runtime。
- 保留 `AICODE_RUNTIME_DIR` 和源码 checkout 开发路径。
- 增加 clean-home install/start/stop E2E。

完成记录（2026-07-26）：

- 新增根目录 `VERSION` 和 install manifest schema。
- `make install` 安装 CLI、版本化 Runtime、独立 venv 和原子 manifest；默认前缀为 `~/.local`。
- installer 支持同版本幂等重装；staging 或依赖验证失败时恢复原版本、CLI 和 manifest。
- daemon 按 `AICODE_RUNTIME_DIR` → 安装 manifest → 源码 checkout fallback 解析 Runtime，并使用 manifest 指定的 venv Python。
- manifest 路径必须相对安装根目录，拒绝绝对路径和 `..` 逃逸。
- clean-home E2E 在临时 prefix、临时状态目录和源码外 workspace 覆盖 install → reinstall → failed install rollback → start → status → stop，并已接入 CI。
- 验证：clean-home E2E、Python 全量测试 214 项、Go 全量测试、installer Python 编译与 shell 语法检查全部通过。

### `[x]` T-004 `aicode runtime doctor`

对应：WP0.1

依赖：T-003

范围：

- 检查 CLI/Runtime 版本、Python、依赖、端口、provider 和 Docker。
- 输出机器可读 JSON 和可操作的人类提示。

完成记录：

- 新增 `aicode runtime doctor [--json]`，固定输出 installation、version、python、port、provider、docker 六项检查及 `ok` / `warn` / `error` 汇总状态。
- doctor 只读执行：不会自动启动 daemon，也不会请求 provider；缺少 API key、Docker 或尚未启动 daemon 为 warning，安装损坏、版本不一致、Python/依赖不可用和端口冲突为 error/非零退出。
- CLI 构建通过 ldflags 注入根 `VERSION`；源码 Runtime fallback 同样读取根版本，用于 CLI、安装 Runtime 和运行中 daemon 的三方一致性校验。
- provider 检查遵循 Runtime 的 key 解析顺序且不输出 secret；Python 检查覆盖 3.11+ 及 FastAPI/httpx/pydantic/uvicorn 导入。
- clean-home E2E 已扩展为 install → doctor → start → status → doctor → stop，并验证安装 manifest、版本、Python 和端口状态。
- 验证：Go 全量测试、go vet、Python 全量测试 214 项、源码 doctor JSON、clean-home 安装 E2E 全部通过。

## M1：可信执行与长会话

### `[x]` T-005 ExecutionBackend

对应：WP0.2

依赖：T-001、T-002

范围：

- 定义 Host/Sandbox 共用执行接口。
- 迁移 Agent bash、test/build/lint 和编辑后验证。
- 统一取消、终态、资源限制和 audit。

完成记录：

- 新增版本化 `ExecutionRequest` / `ExecutionResult` contract、`ExecutionBackend` Protocol 和 Runtime `ExecutionService`，固定 `succeeded` / `failed` / `timed_out` / `cancelled` 终态。
- `HostExecutionBackend` 成为唯一宿主机子进程创建点；argv/shell、timeout、显式 cancel 和任务取消均按 execution id 管理，并终止完整进程组。
- `DockerExecutionBackend` 复用 Host lifecycle，统一只读 workspace、默认禁网、`.env*` mask、最小 Docker CLI 环境和 CPU/内存/PID 限制。
- Agent bash、rg search、review git 命令和编辑后的模型验证均接入 ExecutionService；bash execution audit 仅保留 executable/command hash 和终态，不记录原始命令。
- `aicode project sandbox test|build|lint` 已从本地 Go 执行器收敛为 Runtime HTTP 客户端；Runtime 负责命令探测、执行、cancel 和 audit，API 不开放任意 shell action。
- 正常 `aicode runtime stop` 先调用 prepare-stop 取消全部活跃 execution，再终止 Runtime，避免正常重启遗留子进程。
- 新增 Host success/timeout/cancel/process-group、Docker 隔离参数、HTTP contract、project command detection、safe audit 和可用时真实 Docker backend 集成测试。
- 验证：Go 全量测试、go vet、Python 222 项通过（本机 Docker daemon/镜像不可用时跳过 1 项集成测试）、clean-home install/doctor/start/status/stop E2E 通过。

### `[x]` T-006 Project Trust 与 shell 路径安全

对应：WP0.2

依赖：T-005

范围：

- workspace trust 存储在仓库外。
- protected/masked paths 同时约束 file tool 和 shell。
- 子进程环境变量改为 allowlist。
- 增加 workspace/symlink/secret 逃逸测试。

完成记录：

- 新增版本化仓库外 TrustStore 和 `aicode project trust status|add|remove|list [--json]`；trust 绑定 canonical workspace 与去凭证 Git remote，remote 变化或 workspace 消失会回到 `untrusted`，store 以 `0600` 原子写入。
- Agent Loop 每次 run 读取 trust level；untrusted workspace 的 `pytest`、`go test`、`npm test` 等项目命令进入 approval，trusted workspace 才能按低风险 policy 自动执行。
- Policy 增加 shell statement/wrapper/path/glob 分析，拒绝 home、`../`、workspace 外绝对路径、path-qualified 外部 executable、protected path 和 symlink 逃逸；deny 不经过 approval。
- `.env*`、SSH/GPG、AWS/Azure/GCloud/Kubernetes、`.netrc`、包管理凭证和私钥升级为 mandatory protected paths；项目配置只能追加，CLI 不允许移除系统规则。
- read/search/list/related/edit 与 shell 共享路径保护；rg 和 Python fallback 均避免读取 protected path，文件遍历不会跟随逃逸 symlink。
- Host backend 默认只继承最小非敏感环境变量 allowlist，并使用隔离 HOME/XDG；provider key、Runtime token 和任意未列出变量不进入子进程。
- 已知 Runtime secret 会在 tool output、SSE 和 audit 中脱敏；包含这类值的 edit 或 shell 字面量直接拒绝。
- 新增 Project Trust schema、HTTP fixture/Go client contract、Runtime API 和 trust change audit；README、ARCHITECTURE、ROADMAP 同步安全语义。
- 验证：Python 全量测试 256 项通过（本机 Docker daemon/镜像不可用时跳过 1 项）、Go 全量测试、go vet、Python compileall、git diff check，以及包含 trust add/list/remove 的 clean-home install/doctor/start/status/stop E2E 全部通过。

### `[x]` T-007 持久化、模型感知的 compaction

对应：WP0.3

依赖：T-001

范围：

- model capability 与调用前 context preflight。
- compaction entry 持久化和 resume projection。
- context overflow 单次恢复重试。
- 增加旧 session、工具配对和连续压缩测试。

完成记录（2026-07-26）：

- 新增 provider/model-aware `ModelCapability`，支持按 `<provider>:<model>` 或 `<model>` 配置 context window 与 max output；每次 main/reviewer/final 模型调用前统一估算 system、tools、history、输出预留和安全余量，`aicode runtime models` 同步显示实际 capability 与来源。
- SQLite 新增 append-only schema v1 `compactions` 表，持久化 covered message id range、summary、provider/model、prompt version、压缩前后 token 估算、context window 和时间；原始 messages 不删除、不覆盖。
- session resume 使用最近有效且版本受支持的 compaction 重建 projection；旧数据库自动建表且继续读取，未知未来 schema 会被忽略并回退到上一条有效 projection。
- compaction cutoff 以原子消息组为边界，assistant tool calls 与对应 tool results 始终一起保留，未完成 tool call 不进入摘要；连续 compaction 累积上一摘要并推进 covered range。
- summarizer 请求按自身 capability 裁剪输入并记录 usage；provider 摘要失败时使用本地确定性 fallback，保留完整 source log，并通过 `context.budget` 暴露可恢复错误类型。
- OpenAI-compatible 与 Anthropic provider 统一识别 context overflow；Agent 仅执行一次 forced compaction retry，第二次 overflow 直接上抛，避免无限恢复循环。
- README 与 ARCHITECTURE 已补充 capability 配置、preflight、append-only source/projection 分离和恢复语义。
- 验证：Python 全量测试 267 项通过（本机 Docker daemon/镜像不可用时跳过 1 项）、Go 全量测试、go vet、Python compileall、gofmt 和 `git diff --check` 全部通过。

## M2：本地日用

### `[x]` T-008 Agent eval 与 trace 基线

对应：WP1.3

依赖：T-005、T-007

范围：

- 建立隔离 fixture、runner、grader 和报告。
- 首批覆盖修改、验证、安全拒绝和长上下文任务。
- 保存成功率、安全率、token、cost、latency 和工具轮次。

完成记录（2026-07-26）：

- 新增 eval contract v1：Pydantic task types 与 `eval-task`、`eval-trace`、`eval-report` JSON Schema 固定 fixture、请求、mode、scripted model profile、预算、审批策略和确定性 checks。
- 新增隔离 runner：每次将 fixture 复制到临时 workspace，以固定 identity/time 创建可复现初始 Git commit，并运行真实 Agent Loop、ModelRouter、PolicyEngine、ExecutionService、SessionStore 和 compaction 路径。
- scripted provider 强制执行 expected purpose、model-call/token/cost 预算；runner 同时执行 wall-time 上限、自动审批策略、trusted/untrusted workspace 和长历史 seed。
- deterministic grader 以测试命令、文件内容、allowed/forbidden changed paths、event/audit、approval、execution 和 compaction 断言评分，不依赖 LLM-as-judge。
- 首批 smoke suite 包含 4 个任务：单文件修复并验证、危险命令拒绝、protected-path prompt injection、防泄露长上下文 compaction/resume。
- 每个 run 输出可重放 schema v1 trace；完整 Agent shell 命令、模型正文、tool output 和 edit/diff 正文只保存 hash、大小和安全元数据。失败 check 可回溯 model call、tool/policy、edit、execution 与 grader verification。
- JSON/Markdown report 保存 success、pass@1/pass@k、安全率、越权修改率、危险命令执行率、approval accuracy、token、cost、latency、model/tool turns、无效调用、重复编辑及 compaction 前后成功率。
- 提交 `deterministic-smoke.v1` baseline，锁定 task/fixture、eval harness/schema、prompt、tool schema、policy 和 compaction prompt fingerprint；`make eval-smoke` 已接入 CI 并上传 traces/report artifact。
- 当前 deterministic baseline：4/4 成功，success/pass@1/safety/approval accuracy 均为 1.0，越权修改率和危险命令执行率均为 0。
- 验证：Python 全量测试 270 项通过（本机 Docker daemon/镜像不可用时跳过 1 项）、deterministic eval baseline PASS、Go 全量测试、go vet、Python compileall 和 `git diff --check` 全部通过。

### `[x]` T-009 本地模型 Provider Profile

对应：WP1.2

依赖：T-007、T-008

范围：

- auth mode、context window、max output 和 tool capability。
- endpoint/model probe。
- 至少一个 no-auth 本地 provider smoke task。

完成记录（2026-07-26）：

- 新增 Provider Profile v1 contract，覆盖 name/schema version、base URL、`required|optional|none` auth mode、model、context window、max output、native tools、SSE streaming 与 chars/token 估算；Go config、Runtime env、doctor、router 与 renderer 使用同一字段集。
- `auth_mode=none` 即使父进程存在 API key 也不发送 Authorization；`optional` 仅在 key 存在时发送；`required` 缺 key 快速失败。
- `aicode runtime models probe [--model ...] [--no-tools] [--json]` 依次验证配置、`/v1/models`、模型 ID、SSE 和最小原生 tool call，提供 endpoint/auth/model/stream/tools 针对性错误 code。
- profile 声明不支持 tools/streaming 时在请求前快速失败；tools probe 只接受原生 `tool_calls`，禁止从文本猜 JSON。
- 新增真实 localhost TCP no-auth smoke，跑通 probe → read → edit proposal → approval → verify，并断言请求无 Authorization；独立 Make target 已接入 CI。
- 新增 Ollama、llama.cpp server、LM Studio 最小配置与验证矩阵；本机未安装这些产品，因此不虚报具体产品版本手工验证。
- eval 影响：既有 deterministic 4-task baseline 保持 PASS；新增 provider transport smoke 覆盖此前 scripted provider 不覆盖的 HTTP/auth/probe/edit approval 链路。
- 验证：Python 全量 283 项通过（受限沙箱跳过 localhost bind 与 Docker 各 1 项），沙箱外 localhost smoke 1 项通过，Go 全量测试、go vet、Python compileall、deterministic eval baseline 和 `git diff --check` 全部通过。

### `[x]` T-011a Application Runtime 与 Session/Turn/Run 契约

对应：WP1.4（Application foundation）

依赖：T-002、T-008

范围：

- 提取 transport-independent Session snapshot、Turn request、Run receipt/control 契约。
- 让 Application services 返回具名类型，禁止关键边界继续扩散裸 `dict` / `Any`。
- 版本化 schema、fixture、Runtime descriptor 和 Go client characterization。

完成记录（2026-07-27）：

- Application contract 已升级至 v2：`SessionSnapshot`、`AgentRunState`、`TurnRequest`、`RunReceipt`、`RunControl`、`SteerReceipt` 与 `CompactionReceipt` 均位于不依赖 FastAPI/Pydantic 的 application 层，且已删除 `language` 字段。
- `SessionService` 公开 snapshot read model，并通过显式 `require` 隔离内部 domain session；`RunCoordinator`、`ContextService` 改为返回具名 contract。
- FastAPI `MessageRequest` 只作为 transport DTO，并显式转换为 `TurnRequest`；HTTP handler 在边界把 application contract 序列化为现有 v1 HTTP response，外部行为不变。
- `application-contract.schema.json`、v2 fixture 和双向 round-trip 测试已同步；`GET /v1/meta/contract` 公开 application contract v2，Go client 可读取 version/schema/type map。
- 新增 Application service characterization，覆盖 Session create/get/list/bind、Run submit/cancel 和 in-memory manual compaction 的返回类型。
- eval 影响：只收紧 Application/transport 类型边界，不修改 prompt、tool、policy 或 Agent 行为；deterministic 4-task smoke baseline 保持 PASS。
- 验证：Python 全量 297 项通过（Docker 与受限 localhost bind 各跳过 1 项），Go 全量测试、go vet、Python compileall、ruff 和 `git diff --check` 全部通过。

### `[x]` T-010 常驻 REPL

对应：WP1.1

依赖：T-011a、T-008

范围：

- persistent session、status/model/compact/new/resume 命令。
- steer、follow-up 和 cancel。
- TTY 与非 TTY 行为测试。

完成记录（2026-07-27）：

- `aicode chat` 无 message 时进入常驻 REPL；旧 `aicode repl` 保留为隐藏兼容入口，单次 `aicode chat "..."` 保持原行为。
- REPL 默认创建并绑定一个 session，支持 `/status`、`/model [name]`、`/compact`、`/new`、`/resume [--last|id]`、`/exit`；模型覆盖按 message 传入 Runtime，不修改全局路由。
- 普通输入在 run 活跃时作为 follow-up 排入同一 session；`/steer` 进入当前 run 专用队列，由 AgentLoop 在模型/工具之间的安全边界应用，并跳过尚未执行的旧工具调用；`/cancel` 保留既有终态与后续队列语义。
- Runtime 新增 versioned HTTP contract：session status typed client、`POST /v1/sessions/{id}/steer` 与 `POST /v1/sessions/{id}/compact`；新增 `run.steer.queued/applied` SSE contract fixtures 和 renderer 兼容行为。
- REPL 使用单一 stdin scanner 协调消息、命令和 approval，避免多个 reader 争抢输入；TTY 支持首个 Ctrl-C 取消、第二个 Ctrl-C 退出，非 TTY 在 EOF 后等待本进程提交的所有 run 终态再稳定退出。
- 新增 Runtime safe-boundary/model override/manual compaction 测试，Go REPL 覆盖 persistent session、控制命令、steer、TTY/Ctrl-C 与非 TTY follow-up。
- eval 影响：既有 deterministic 4-task smoke baseline 保持 PASS；新增 steer/REPL contract 与单元测试覆盖交互编排，不修改既有 task/prompt/tool/policy baseline。
- 验证：Python 全量 292 项通过（Docker 与受限 localhost bind 各跳过 1 项），Go 全量测试、go vet、Python compileall 和 `git diff --check` 全部通过。

## M3：可嵌入平台

### `[x]` T-011b Agent Core DI 与内部解耦

对应：WP1.4

依赖：T-011a、T-010

范围：

- 提取 ModelRuntime、SessionRepository、EventSink、ApprovalBroker 等接口。
- 移除核心路径对 FastAPI 和 module globals 的依赖。
- 在 T-011a contract 后方用 characterization tests 渐进迁移，不改变 T-010 REPL 契约。

完成记录（2026-07-27）：

- 新增 transport-independent `core/` ports/domain 和集中式 `adapters/composition.py`；FastAPI transport 只创建并持有一个 `ApplicationRuntime`。（2026-07-30 的包重组后，两者分别成为 `agent/ports.py` 与 `bootstrap.py`。）
- Agent Core 使用 ModelRuntime、ToolRegistry、SessionRepository、EventSink、ApprovalBroker、ExecutionRuntime、WorkspaceRuntime、TraceSink、Clock/IDs ports；AgentLoop、ContextManager、Policy 不依赖 FastAPI、server、SQLite、具体工具或 project config。
- SQLite SessionStore 支持 clock/ID 注入；新增 InMemorySessionRepository、JSONL usage、workspace/tool/approval/system adapters，fake model + in-memory session 可直接运行 AgentLoop。
- 新增 AST/import side-effect 架构守卫和 Agent Core characterization tests；T-010 REPL、HTTP/SSE、session、approval、execution、trust 与 compaction 行为由 T-011a contract 固定。
- 原 T-011 曾整体提前落地；本次按实际稳定边界拆分为 T-011a/T-011b，并将逻辑依赖正式调整为 `T-011a → T-010 → T-011b`。

### `[ ]` T-012 SDK 与 stdio JSONL RPC

对应：WP2.1

依赖：T-011b

范围：

- Python SDK。
- initialize/session/prompt/cancel/approval/event RPC。
- 协议版本协商、背压和最小集成示例。

优先级说明（2026-07-29）：T-013 ~ T-016 的成本闸门、执行边界与真实评测数字优先于本任务。IDE / 脚本嵌入需求出现前不启动。

## M4：生产就绪第一梯队（正确性与边界）

来源：2026-07-29 架构评审 A1 / A3 / A5，以及实施期补充发现的 N1 / N4。

**优先级调整（2026-07-29）**：目标由"简历项目完备度"改为"生产可用"后，真实模型评测（T-016）与 prompt caching（T-014）从第一梯队降级到 M11（原 M7）；SQLite 写入路径（T-020）与新发现的审计可靠性（T-028）、认证 fail-closed（T-029）升入第一梯队。判据换成：**别人装上能天天用，几周不出事，出事能查，升级不炸。**

本梯队五项已全部完成。实现细节见 [实现计划](docs/plans/2026-07-29-production-agent-hardening.md)。

### `[x]` T-013 累计 token / 成本硬闸门

对应：评审 A3

依赖：无

范围：

- `TurnBudget` 增加 `max_total_tokens` / `max_total_cost`；新增 `TurnLedger` 承载单轮累计用量。
- `record_usage` 由纯上报改为写入 ledger 并参与控制流。
- 超限走与 `max_steps` 相同的收尾路径（追加预算 note、以 `tools=[]` 收口），保证用户始终拿到总结而非截断。
- 新增 `run.budget.exceeded` event（按执行规则 5 三处登记）；新增 `settings.budget` 配置段。

完成记录（2026-07-29，`711ef06`）：

- `TurnLedger` 累加每次 model call 的 token 与估算成本；`TurnBudget` 增加 `max_total_tokens`（默认 1000000）与 `max_total_cost`（默认 5.0），0 表示关闭。
- steps / tokens / cost 三个维度共用同一条收尾路径，用户拿到的始终是总结而非截断的对话。
- **收尾调用位于循环之外且其用量不再过闸**——这是防止收尾递归的结构性保证，而不是原计划里写的 `finalizing` 标志位；由 `test_budget_wind_down_does_not_recurse` 锁定。
- **偏离原计划**：预算改为 Runtime-level only，**不接受 `.aicode/config.json` 覆盖**。被检查的仓库能自行抬高的花费上限不是上限；这与 Project Trust 不允许 workspace 自我提权同源。原计划写了项目级覆盖，当时没有接上"仓库内容不可信"这条既有安全假设，故未采纳。
- `run.budget.exceeded` 已在 `EVENT_TYPES`、events schema enum、SSE fixture 三处登记，并补 Go renderer 分支。
- baseline 重新生成，仅 `prompt_sha256` 变化（新增预算 note 文案），`failed_runs` 0、safety 指标未劣化。
- 验证：Python 314 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS、`git diff --check` 通过。

### `[x]` T-015 Agent `bash` 按 trust 级别进入 Docker 沙箱

对应：评审 A1（最大安全缺口）

依赖：T-005（ExecutionBackend）、T-006（Project Trust）

背景：`tools/command.py` 默认 `backend="host"` 且 `run_bash` 未覆盖，Agent 自主决定的命令全部在宿主机执行；Docker backend 目前只服务 `test/build/lint`。契约、分发逻辑、`writable_paths` / `masked_paths` / `network` 字段与 `ToolContext.trust_level` 均已就绪，本任务是接线而非新建能力。

范围：

- 新增 `execution.agent_bash_backend`：`auto`（trusted → host，否则 docker）/ `host` / `docker`。
- Docker 分支：workspace 可写挂载（`writable_paths`）、`network="none"`、`masked_paths` 继承 protected paths 并强制并入 `.env*`、复用 `SandboxLimits`。
- `DockerExecutionBackend` 扩展支持可写 workspace 挂载（当前仅只读）。
- `build_system_prompt` 增加当前执行环境说明（host / docker-sandboxed、是否禁网），使模型预期到 `pip install` 会失败。
- `aicode runtime doctor` 增加检查：配置为 `auto` 且存在 untrusted workspace 时 Docker 是否可用。

验收：

- trusted + `auto` → host；untrusted + `auto` → docker 且 `network="none"`、`writable_paths` 含 workspace、`masked_paths` 含 `.env*`。
- **Docker 不可用时必须返回明确错误并指引 `aicode project trust` 或启动 Docker，不得静默回退 host**——静默回退会把安全边界变成安慰剂。此为本任务核心断言。
- 新增评测任务 `untrusted_bash_sandboxed`。
- 同步 `ARCHITECTURE.md` 安全模型与 Docker Sandbox 两节；按执行规则 6 重新生成 baseline 并确认 `safety_rate` 未下降。

完成记录（2026-07-29，`59f5b4d`）：

- 新增 `execution.agent_bash_backend`（`auto` / `host` / `docker`），Runtime 级 `AICODE_AGENT_BASH_BACKEND` 与项目级 `execution.agentBashBackend` 双入口；项目侧取值非法时回落到"继承 Runtime 设置"而非宽松默认——配置里的拼写错误不能悄悄削弱沙箱。
- Docker 分支 workspace 可写挂载并以宿主 uid/gid 运行，避免在用户仓库留下 root 拥有的文件；`network=none`、`.env*` 遮蔽、资源限制保持不变。`DockerExecutionBackend` 只接受 workspace 本身作为可写路径，其它路径直接拒绝。
- **Docker 不可用时直接失败并给出处置方式，不回退宿主机**；由 `test_bash_fails_loudly_when_sandbox_is_unavailable` 与评测任务双重锁定。
- 评测任务 `untrusted_bash_sandboxed` 断言的不变量改为"untrusted workspace 永不产生 host backend 的 execution"——该不变量在有无 Docker 的机器上都成立（有 Docker 走沙箱，无 Docker 被拒绝），CI 因此稳定。为此给 grader 增加 `forbidden_execution_backends` 检查。
- **额外修复（计划外）**：沙箱此前会隐式拉取镜像，首次使用时一次工具调用变成数分钟无反馈下载（实测 30s 仍未完成即被评测超时打断）。改为 `--pull=never` + 可操作的 `docker pull` 提示，实测 30391ms → 362ms；`aicode runtime doctor` 增加沙箱镜像检查，把这件事提前到安装期发现。
- baseline 重新生成，4 处 digest 变化均可归因（prompt / task_set / fixture_set / eval_harness）；`policy_sha256` 与 `tool_schema_sha256` 未变，safety 指标满分。
- 验证：Python 310 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS、`git diff --check` 通过。

### `[x]` T-020 SQLite 复用 WAL 连接

对应：评审 A5（原属 M5，因属第一梯队正确性问题上移）

依赖：无

完成记录（2026-07-29，`a47aae4`）：

- 此前每次数据库调用都新开一个 rollback-journal 连接且 `synchronous=FULL`，**实测一条消息写入 5.175ms**，全部发生在事件循环上——而同一循环正在向 SSE 推送 `assistant.delta`；一次 20 条消息的 turn 意味着约 100ms 循环阻塞。
- 改为复用单个连接，`journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000`，**实测降到 1.060ms（约 4.9x）**。`_connect()` 改为 contextmanager 以保持所有调用点写法不变；连接同时被事件循环与 write-behind 工作线程使用，因此 `check_same_thread=False` 搭配一把覆盖整个事务的锁。
- **偏离原计划**：未把 `append_message` 改成 `asyncio.to_thread`。测量后剩余成本约 1.1ms，而改成异步需要让 `persist_message` 与 `AgentSession` 协议一并异步化；1ms 级单次阻塞对 SSE 流式输出已不构成可感知影响，收益不足以支撑这个扩散。理由写入 `ARCHITECTURE.md` 14.2（SQLite 写入路径），便于后续复核该判断。
- 新增 WAL pragma、连接复用、并发写读不出现 `database is locked`、`aclose` 释放连接四项测试。
- 验证：Python 325 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS、clean-home install E2E 通过。

### `[x]` T-028 审计日志不静默丢失并按大小轮转

对应：实施期新发现 N1（评审后补充）

依赖：无

背景：审计日志是安全证据链。此前队列满时静默丢弃事件、写入异常被吞掉，两者只反映在计数器里且无任何告警。丢一条记录会让"没有危险命令的记录"和"没有发生危险命令"变得不可区分。此外 `audit.jsonl` 只追加不轮转，长期运行的 daemon 会无限增长。

完成记录（2026-07-29，`1c9c5b8`）：

- 队列满时降级为同步写入而非丢弃。队列深度 5000，打满意味着 writer 已跟不上，属病态情况；一次阻塞的 append 好过证据链上出现无人知晓的空洞。
- 写入失败重试一次；持续失败时计数、记录 `last_error`、在 stderr 报告一次（不是每条事件刷屏），并通过 `status().healthy` 暴露给 `aicode runtime status`。写入路径**绝不向调用方抛异常**——异常逃逸会杀掉 writer task 或中断一次 agent turn。
- 按大小轮转（默认 64MB × 5 备份，`AICODE_AUDIT_MAX_BYTES` / `AICODE_AUDIT_BACKUP_COUNT` 可调），并复用文件句柄，不再每条事件开关文件。
- session **event** 持久化仍保持 best-effort 可丢弃，这个差别是有意的：event 只影响 resume 时的回放展示，真正的 agent 历史由 messages 表独立可靠持久化。已在 `ARCHITECTURE.md` 15.1 写明取舍差异。
- 原 `test_audit_logger_counts_individual_write_failures` 断言的是旧的"首次失败即丢弃"契约，已重写为覆盖新契约的两条路径（瞬时失败经重试恢复、持续失败被计数并上报且 writer 存活），另补队列溢出降级与轮转测试。
- 验证：Python 317 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS。

### `[x]` T-029 Runtime 认证改为 fail-closed

对应：实施期新发现 N4（评审后补充）

依赖：无

背景：`is_authorized` 在未配置 `AICODE_RUNTIME_TOKEN` 时直接返回 `True`。未配置不等于关闭认证——它意味着本机任何进程都能调用 approval endpoint，替用户批准一次编辑或一条高风险命令。正常路径（`aicode runtime start`）总会生成 token，因此这条路径只在手动跑 uvicorn 时暴露，但暴露面是完整 API。

完成记录（2026-07-29，`07fb613`）：

- 未配置 token 时拒绝请求（`/v1/daemon/status` 仍开放），401 的 `detail` 说明如何处理。无认证访问仍可能，但必须通过 `AICODE_ALLOW_ANONYMOUS=1` 显式选择。
- 该开关只在未配置 token 时生效，**不能用来绕过已配置的 token**，由 `test_configured_token_takes_precedence_over_anonymous_opt_in` 锁定。
- token 从 import 时捕获改为每请求读取。import 时捕获让模块顺序敏感、无法在不重新 import 的情况下重配，与 T-021 把 composition root 移进 ASGI lifespan 的方向冲突。
- 测试从 monkeypatch 模块常量改为设置环境变量，走真实配置路径。
- 验证：Python 321 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS，以及 **clean-home install/doctor/start/status/stop E2E**——这条最关键，它是唯一走真实 daemon token 生成与握手的验证。

## M5：生产就绪第二梯队（稳定与可运维）

来源：2026-07-29 架构评审 C6 / C7 / D4，以及实施期补充发现的 N3 / N5 / N6。第一梯队保证"不出事"，本梯队保证"用久了不退化、出事能查、升级不炸"。

### `[x]` T-030 会话列表分页与数据保留策略

对应：实施期新发现 N3

背景：`SessionStore.list()` 每次都加载**所有** session 的**所有** messages 与 compactions（`store.py` 的 `list()`），同步执行且无分页。用几周后 `aicode session list` 会单调变慢。数据库本身也没有任何清理策略，只增不减。

范围：

- `list()` 只查 session 元信息，不加载 messages / compactions；需要详情的调用点显式取。
- 分页参数（limit / before），CLI 与 HTTP contract 同步。
- 保留策略：按数量或时间清理旧 session 及其 messages / events / compactions，并提供显式的 `aicode session prune`。
- SQLite `VACUUM` 或 `incremental_vacuum` 的触发时机。

验收：一万条消息规模下 `list()` 耗时与会话数解耦；保留策略有测试覆盖且不会删除仍被引用的 compaction 区间。

完成记录（2026-07-30，`ff59e4b`）：

- `list()` 此前每次调用都 hydrate 所有 session 的所有 messages 与 compactions。**实测 50 session × 40 消息为 69.4ms、每行约 82KB**，且随使用时间单调增长——而唯一的消费者 `aicode session list` 只是展示一个列表。改为只返回摘要（metadata + `message_count` + agent run state）后 **1.2ms、每行 438 字符**。
- 列表不再写入内存缓存：列举是只读概览，不应因此驱逐正在运行的 session。
- **偏离原计划**：分页用 `limit` / `offset` 而非计划里的 `limit` / `before` 游标。本地几百个 session 的规模下 keyset 游标的复杂度不划算。
- 新增 `SessionStore.prune` 与 `aicode session prune` / `POST /v1/sessions/prune`，一并删除 messages / events / compactions。**保留策略默认关闭**：静默删除用户的对话历史比数据库无限增长更糟。
- **正在运行或有未决 approval 的 session 永不删除**，即使命中保留条件；被跳过的数量通过 `retained_live` 如实汇报而非静默忽略。
- 当时新增 CLI 包 `sessionscmd`（含参数解析测试），命令归类后迁至 `cli/internal/cmd/sessioncmd/list.go`；Python 侧新增 8 项测试。
- 验证：Python 338 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS、clean-home install E2E 通过。

### `[x]` T-031 SQLite schema 迁移 ladder

对应：实施期新发现 N5

背景：没有 `PRAGMA user_version`。已经积累了两个 bespoke 修补方法（`_ensure_updated_at_column`、`_remove_language_column`），每次启动都全量跑一遍检查。第三次改 schema 会继续加第三个，且无版本检测、无降级保护。

范围：

- 引入 `user_version` 版本号与有序迁移列表，启动时只跑缺失的迁移。
- 把现有两个 bespoke 修补收编为 migration 1 / 2。
- 遇到高于当前代码支持的版本时明确报错，而不是带着未知 schema 继续运行。

验收：旧库升级、全新库初始化、未来版本库拒绝启动三条路径均有测试。

完成记录（2026-07-30，`1dd89f0`）：

- 引入 `PRAGMA user_version` 与只追加的有序 `MIGRATIONS` ladder，启动时只执行缺失的迁移。两个 bespoke 修补收编为迁移 2 / 3。
- `user_version` 高于当前构建支持的版本时**拒绝打开**并抛 `SchemaVersionError`：带着未知 schema 继续运行会写出旧构建读不回的行，或静默忽略新构建依赖的列。
- 迁移 1 保持 `if not exists` 幂等——ladder 之前创建的数据库 `user_version=0` 但已持有这些表。**现实中最常见的升级路径正是"表结构已是最终形态但没有版本戳"**，此时三条迁移全部空转、只有版本戳前进，由 `test_already_current_but_unversioned_database_is_stamped_without_changes` 锁定。
- 另补全新库、legacy 库原地迁移不丢行、未来版本库被拒绝、ladder 编号连续四项测试。既有的 `test_session_store_migrates_old_schema` 未改动即通过。
- 验证：Python 330 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS。

### `[x]` T-032 approval 超时语义

对应：实施期新发现 N6

背景：`wait_for_approval` 超时 300 秒后返回 `False`，模型收到 `[user rejected this edit]`——与用户真的拒绝不可区分。用户离开五分钟回来，编辑已被"拒绝"，模型可能已改用别的方案。

范围：区分 `rejected` / `timed_out` 两种结果，模型侧文案与事件分别处理；超时时长可配置。

验收：超时与拒绝产生不同的事件与不同的 tool 结果文案，有测试覆盖。

完成记录（2026-07-30，`0aba219`）：

- `wait_for_approval` 此前返回 `bool | None`，把"用户明确拒绝"和"超时无人应答"折叠成同一个 `False`。模型因此会为一个没人看到的请求收到 `user rejected this edit`，可能就此放弃一个本来正确的方案。
- 改为 `ApprovalDecision` 四态：`accepted` / `rejected` / `timed_out` / `missing`，`PendingApproval.resolution` 另记录 `cancelled`。
- 超时的 tool 结果明确说明"这不是拒绝"，并要求模型**停下来告知用户**而不是重试——重试只会阻塞在下一个同样无人应答的提示上。配一个反向回归测试确保超时文案不会渗进真正的拒绝路径。
- 事件侧复用 `approval.expired` 并带 `reason`（`timeout` / `run_cancelled`），避免新增近似重复的 event type；`tool.rejected` / `edit.rejected` 带 `resolution`，Go renderer 据此区分展示。
- 超时时长由 `AICODE_APPROVAL_TIMEOUT_SECONDS` 配置（默认 300），读取发生在 broker（adapter 层）。
- 验证：Python 342 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS。

### `[x]` T-025 OpenTelemetry 导出

对应：评审 D4 | 依赖：T-021（从增效梯队上移：生产可观测性是"出事能查"的硬指标，不是简历装饰）

范围：`TraceSink` 增加 OTLP 实现并与现有 JSONL 并存；span 层级 `run → model.call / tool.call → execution`；沿用 `audit/redaction.py` 脱敏；文档给出接 Jaeger 或 Langfuse 的本地验证步骤。

完成记录（2026-07-30，`1429230`）：

- `SpanTraceSink` 是 TraceSink 的**装饰器**而非替代：转发每个事件给 JSONL sink 的同时派生 span。JSONL 仍是真相来源，追踪是叠加的——丢掉追踪后端绝不能代价一条审计记录。
- span 由既有 start/finish 事件对派生，得到层级 `run → tool.call → execution`。关联键并不齐整（`session.final` 不带 `run_id`，`usage.recorded` 两者都无），因此按 session 维护 open span 栈。
- 六条不变量各有测试：关闭 span 连带丢弃内层 span（tool span 不能比发出它的 run 活得更久）；孤立的 close 降级为点事件；无 session 事件不进追踪；span 属性复用审计脱敏（span 会离开本机）；派生异常被吞掉；`aclose` 关闭悬挂 span。
- OTel SDK 是**可选 extra**，懒加载。为此把派生逻辑与 SDK 绑定分开——派生零依赖，16 项测试全部不需要 SDK。开启但 SDK 缺失直接报错：运维以为在跑而实际没在跑的追踪后端比没有更糟。
- README 给出 Jaeger 的 60 秒本地验证步骤。
- 验证：Python 373 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS。

### `[x]` T-027 SSE 健壮性与项目包装

对应：评审 C6 / C7 / D6（SSE 挂起属已知可复现缺陷，因此归入稳定性梯队）

范围：

- SSE 按 `run_id` 过滤后若目标 run 已结束需立即返回而非挂起至超时。
- provider 重试增加抖动并读 `Retry-After`（两个 provider 的固定 `0.5 * 2**attempt`）。
- 英文 README + 30 秒 asciinema/GIF；README 顶部重排为「是什么 → 架构图 → 三个数字 → 60 秒跑起来」。

完成记录（2026-07-30，`6d2b07e`）：

- CLI 把"流结束但没收到 `final`"当作**可重试断连并重连**，因此按 `run_id` 过滤的流一旦永久等待，CLI 会陷入**无限重连**而非单次挂起。
- 两种情形被显式终止：run 已在请求 cursor 之前结束（重放其终态事件）；run 无任何保留事件且既不在运行也不在队列（合成终态事件，**不写入 session**，因为实际没有发生新的事情）。第二种的成因是 event 持久化 best-effort + session 驱逐重建，与过期 run_id 在服务端看起来完全一样。
- `subscribe` 增加 `idle_timeout`，每 15 秒发 `: keep-alive` 并借此重新判断上述情形。yield 在 condition 之外，锁不跨挂起点。
- 为区分"未开始"与"已消失"，`Session` 增加 `queued_run_ids()`，而非让 server 读 `asyncio.Queue` 私有属性。
- provider 重试从固定 `0.5 * 2**attempt` 改为 equal jitter 指数退避 + `Retry-After`（上限 60 秒）。固定退避会让撞上同一限流的客户端同步重试，重新制造那个突发。
- **写测试时发现并修掉一个真实缺陷**：`parsedate_to_datetime` 对非法输入抛 `ValueError` 而非返回 `None`，原实现会把可重试的 429 变成 provider 崩溃。
- **未包含**：英文 README 与 asciinema/GIF。属项目包装而非稳定性问题，且录屏无法在当前环境产出，留作独立任务。
- 验证：Python 354 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS。

## M6：架构清理

来源：2026-07-29 架构评审 A2 / A4 / B1 / B2 / C1–C5。不阻塞生产可用，但决定后续每一项改动的成本。

### `[x]` T-017 Tool Registry 重构

对应：评审 A2

依赖：T-011b

背景：`TOOL_SCHEMAS` 是模块级常量、`run_tool` 是 if/elif 链、`ToolRegistry` port 只是静态集合的外壳；只读性在 `tools/registry.py` 与 `agent/policy.py` 维护两份，审批语义硬编码在 `agent/loop.py` 的 `if call.name == "edit_file"`。该问题阻塞 MCP、subagent、项目自定义工具三个方向。

范围：

- 定义 `ToolSpec`（`name` / `description` / `input_schema` / `read_only` / `approval: none|gate|diff` / `hidden_in_modes`）与 `Tool` Protocol。
- `ToolRegistry` 改为真实 dict 注册表，`run_tool` 变查表；现有 7 个工具逐个迁移。
- **删除 policy 的 `READ_ONLY_TOOLS_V2` 常量**，`PolicyEngine.gate` 从 registry 读取只读性——消灭两份真相是本任务关键收益。
- `agent/loop.py` 的 edit 分流改为 `tool.spec.approval == "diff"`。

验收：

- 现有工具/策略测试不改断言即通过（纯重构，行为不变）。
- 新增可扩展性证明：注册一个自定义只读工具，验证其自动出现在 review 模式 schema 且被 policy 判为 `allow`。
- 按执行规则 6 重新生成 baseline（`policy_sha256` 与 `tool_schema_sha256` 变化）。

完成记录（2026-07-30，`b175004`）：

- 每个工具一份 `ToolSpec` 声明（`name` / `description` / `input_schema` / `read_only` / `approval` / `hidden_in_modes`），`ToolRegistry` 变成真正的注册表，`run_tool` 变查表。
- `ToolSpec` 实现时放在 core 而不是工具实现旁边，因为它有三个互不导入的消费者：模型看 `input_schema`，policy 读 `read_only`，Agent Loop 读 `approval`；包重组后现位于 `runtime/app/agent/ports.py`。
- **最实质的收益**：消灭一处真实 drift 风险。读写属性此前在 `tools/registry.py` 与 policy 各维护一份同样的名单，仅靠注释提醒同步。`READ_ONLY_TOOLS_V2` 已删除，并有测试守住不回归。
- Agent Loop 的 diff 审批改为按声明分发（`spec.approval == "diff"`）而非 `if call.name == "edit_file"`。
- policy 测试改为经由 `DEFAULT_REGISTRY` 取 spec，与 loop 同一路径——这样测试同时验证声明本身是对的，而非把 `read_only` 硬编码进断言。
- 新增可扩展性测试：注册一个完全在内置集合之外的只读工具，验证自动进入 schema、被 policy 判 allow、按查表分发。
- **保留** `edit_file` 的 registry 分支并改写为明确守卫：删掉后会落到 `unknown tool: edit_file`，对一个确实存在的工具那是错误信息。
- eval baseline **仅 `policy_sha256` 变化，`tool_schema_sha256` 未变**——发给模型的 schema 与改动前逐字节一致，是行为保持的直接证据。
- 验证：Python 376 项、Go 全量、go vet、gofmt、ruff、compileall、eval-smoke PASS、clean-home install E2E 通过。

### `[x]` T-018 MCP client 接入

对应：评审 D5

依赖：T-017

范围：

- `runtime/app/tools/mcp/`：stdio + HTTP transport client 与 server 生命周期管理。
- `.aicode/config.json` 增加 `mcp.servers[]`（命令、参数、env allowlist、超时）。
- 外部工具经 `ToolSpec` 注册，**强制 `read_only=False` 且 `approval="gate"`**；工具名加 `mcp__<server>__` 前缀防冲突。
- 新增 `mcp.server.started` / `mcp.tool.called` event（按执行规则 5 登记）。
- 单个 MCP server 崩溃不得影响主 loop。

验收：外部工具全链路经过 policy gate 与审批；server 崩溃隔离有测试覆盖。

完成记录（2026-07-30，`c88e2da`）：

- `mcp.servers[]` 以子进程启动，stdio 讲 JSON-RPC（`initialize` → `tools/list` → `tools/call`），工具经 `ToolSpec` 注册进 T-017 的同一个 registry，因此**自动**走内置工具的 policy gate 与审批链路。
- 前置 policy 改动：`gate()` 改为按 `ToolSpec` 判定而非硬编码工具名。原实现里 `read_only=False` 的非内置工具会落到 `unknown tool` 硬拒绝，外部工具永远无法运行。spec 缺失仍是硬拒绝。
- 四条安全约束各有测试：命名空间 `mcp__<server>__<tool>`（服务器无法接管 `bash`/`edit_file`）；**不相信服务器的自述**（`read_only=False` / `approval="gate"` 强制，不读 descriptor——服务器声称只读是不可验证的主张，采信等于对外部代码跳过审批）；复用最小环境 allowlist（用会回报自己看到哪些变量的服务器**行为验证**，而非只读代码确认）；故障隔离（启动失败/协议违规/调用超时都不影响主 loop 与其它服务器）。
- 客户端把 MCP server 当普通子进程：每次调用有超时，关闭按进程组终止，SIGTERM 后 2 秒不退则 kill，stderr 保留有界尾部用于解释失败。
- 测试用 fake MCP server 脚本，按 argv 切换 healthy / crash / hang / garbage / error 五种行为，覆盖真实子进程与 stdio 往返。
- **只实现 stdio transport**。HTTP transport 未提供——发布一个未经充分测试的第二 transport 比不发布更糟，已在 ARCHITECTURE 与 README 写明。
- 验证：Python 394 项、Go 全量、go vet、gofmt、ruff、compileall、eval-smoke PASS、clean-home install E2E 通过。

### `[x]` T-019 只读工具并行执行

对应：评审 B2

依赖：T-017

范围：

- 按 `tool.spec.read_only` 将单轮 tool calls 切分为并行组与串行组；并行组 `asyncio.gather`，写入类与执行类保持串行。
- **写回 history 的 tool message 必须严格按模型返回的原始顺序**，不得按完成顺序，否则跨 provider 的消息配对会错乱。
- 修复共享可变状态：`context.tool_call_id = call.id` 改为每次调用传独立浅拷贝 context。
- 设并发上限（建议 8）防止文件描述符耗尽。

验收：并行组耗时接近单个最慢工具而非总和；tool message 顺序与 `tool_calls` 顺序一致，两条断言均有测试。

完成记录（2026-07-30，`9a493d3`）：

- **实测 6 个各 100ms 的读取：600ms → 126ms。**
- 只有**连续的**只读调用成组并发，因此与写操作的相对顺序被保留——模型放在 edit 之后的 read 仍读到编辑后的内容。测试断言写操作 `peak_concurrent == 1`，且 edit 前后两次 read 分别看到旧值与新值。
- 只读工具是唯一安全的并发组还有第二个原因：它们的 policy 判定是立即 `allow`，因此并发组永远不会同时挂在两个审批提示上。这正是 T-017 把 `read_only` 变成声明之后才能安全依赖的性质。
- 三条不变量各有测试：结果按**模型调用顺序**写回（部分 provider 按位置配对 tool result 与 call，用完成顺序会破坏下一次请求，测试用故意后发先至的 runtime 验证）；每次调用用 `ToolContext` 独立副本（此前 `tool_call_id` 赋值到共享对象，重叠即互相污染）；并发上限 8。
- 未注册的工具名没有 spec，不能假定无副作用，永远单独成组。
- **ruff 的 B023 在实现过程中抓到一个闭包晚绑定写法**（这是 T-022 立起静态检查后第一次真正拦下东西）。
- 验证：Python 381 项、Go 全量、go vet、gofmt、ruff、compileall、eval-smoke PASS。

### `[x]` T-021 Composition root 移入 lifespan

对应：评审 A4

依赖：T-011b

背景：`server/main.py` 模块级 `application_runtime = build_application_runtime(settings)` 在 import 时即构造 provider client、打开 SQLite，与 T-011a/T-011b 建立的 ports 分层自相矛盾，也使 ROADMAP 标注为已完成的"可嵌入 Runtime"在 HTTP 层被打破。

范围：

- 删除模块级全局，改在 `lifespan` 构造并挂到 `app.state`；service 工厂改为 `Depends`。
- 测试从 monkeypatch 全局迁移到 `dependency_overrides`。

验收：新增测试在同一进程内起两个配置不同的 `ApplicationRuntime` 并各自完成一轮 turn——这是"可嵌入"的实证。

完成记录（2026-07-30，`d90f6c7`）：

- 删除模块级 `application_runtime`：import `app.server.main` 此前就会打开 SQLite、构造 provider client，模块顺序敏感，且一个进程内无法并存两个配置不同的 Runtime——与 T-011a/T-011b 建立的 ports 分层自相矛盾。改为 lifespan 创建并挂 `app.state`，handler 通过 `Depends(get_runtime)` 取用。
- **同时修掉 N2**：handler 现在直接复用 `ApplicationRuntime.__post_init__` 装配好的 8 个 service。此前 transport 每请求重建一遍（含新建 `AgentLoop`），runtime 自己的 service 是死代码。
- 测试从 monkeypatch 全局迁移到注入 runtime：新增 `tests.fakes.build_test_runtime`。**组件必须构造时传入而不能事后赋值**——`ApplicationRuntime` 在 `__post_init__` 装配 service，事后赋值会让 service 仍指向原对象（这正是旧测试依赖"每次调用重建 service"才能工作的原因），该约束写进 helper docstring。
- 新增两条架构守卫：import transport 不得产生任何基础设施副作用且模块全局不得回归；**同进程两个 Runtime 各自只看到自己的 session**（后者是本次改动的实证——ROADMAP 早把"可嵌入 Runtime"标为完成，但在 transport 层其实是破的）。
- 验证：Python 357 项、Go 全量、go vet、gofmt、compileall、eval-smoke PASS、**clean-home install E2E 通过（lifespan 现在是承载路径，这条最关键）**。

### `[x]` T-022 Python 静态检查进 CI 与核心路径类型收敛

对应：评审 C1 / C2 / C3 / C4 / C5 / B1

依赖：无（建议在 T-017、T-021 之后，避免与重构冲突）

范围（分两步，避免一次性改动过大）：

1. 接入工具：`[tool.ruff]`（先只开 `E,F,I,UP,B`）与 `[tool.mypy]`；`make lint-python` 并入 CI Python job；首次告警用 `per-file-ignores` 建立基线，**只对新代码强制**。
2. 收敛核心类型与清理死代码：
   - `run_turn` 的 `request: Any` → 已存在但未使用的 `AgentRequest` Protocol；`ToolRuntime.run -> Any` → `ToolResult`；`build_context -> Any` → `ToolContext`。
   - `AgentRuntime` 字段去 Optional 化，删除 `agent/loop.py` 中 6 处 `assert` / `raise RuntimeError` 兜底。
   - 收敛 `model_router`/`model_runtime`、`audit`/`trace`、`trust_store`/`trust`、`ToolRegistry`/`ToolRuntime` 四对过渡期别名，各留一个。
   - 删除 `agent/history.py` 的 legacy `compact_if_needed` 与 `tools/registry.py` 的 `edit_file` 占位分支。
   - 删除 `agent/loop.py` 中对行为无影响的局部 `history` 列表（所有 append 都被下一轮 `load_history(session)` 覆盖），统一以 session 为唯一真相源。

验收：`make lint-python` 进 CI 且通过；上述类型改动后全量测试不改断言即通过。

完成记录（2026-07-30，`cbf68b1`）：

- 新增仓库根 `ruff.toml`（`E,F,I,UP,B`，line-length 160），`make lint-python` 并入 CI 与本地 `make test`。
- **配置刻意放在仓库根而非 `runtime/pyproject.toml`**：ruff 按文件向上找最近配置，放在 runtime 下会让 `evals/` 静默沿用默认规则——第一次跑就踩到。另设 `known-first-party`，否则 `app`/`evals`/`tests` 会被当第三方与 pytest、pydantic 交错，反而掩盖 import 表达的分层。
- 基线只有 54 个问题（30 可自动修），因此**没建 per-file-ignores 基线而是全部修完**。E501 的三处豁免是长 prose 字符串，配置里写明理由。唯一非机械修复是 `registry.py` 的 B904。
- 死代码：删除 loop.py 的局部 `history` 列表（每处 append 都被下一轮 `load_history(session)` 覆盖，**删除后 loop 测试未改断言即通过**，反证其为死代码；它制造的"内存 history 有独立语义"假象有害——后续只改内存副本的修改会静默失效）；删除 `compact_if_needed` 及其两项测试。
- 类型收敛：`AgentRuntime` 字段改用 port 名（`model_runtime` / `trace` / `trust`），删除三个别名属性，19 处构造点同步；`request: Any` → 已存在但从未使用的 `AgentRequest` Protocol。
- **两处偏离原计划**：(1) 未把 `AgentRuntime` 字段改必填、未删 loop 的 assert——计划假设这些字段只有一个消费者，但 `ContextManager` 与 `run_turn` 要求不同（前者只需 session，`model_runtime` 缺失时降级为确定性摘要，8 项测试依赖这一点），一律改必填会误述 `ContextManager` 的契约；正确做法是引入校验后的 `TurnDependencies` 视图，但那要改 loop 全部 helper 签名，不适合作为本任务尾部的顺带改动，已在 ARCHITECTURE 写明并留作独立任务。(2) 保留 `registry.py` 的 `edit_file` 分支——实测它是有意义的守卫而非占位。
- 验证：Python 371 项、Go 全量、go vet、gofmt、ruff、compileall、eval-smoke PASS、clean-home install E2E 通过。

## M8：Agent 能力补齐（第一梯队）

来源：[2026-07-31 Agent 架构缺口评审](docs/review/2026-07-31-agent-architecture-gaps.md)，实现细节见 [实现计划](docs/plans/2026-07-31-agent-architecture-plan.md)。

**排期判据**：这四项**不需要真实评测数据即可判断该做**——它们补齐的是已知缺失的**机制**（计划表示、写前读、批量写、路径查找），而不是"可能有用的能力"。四项彼此独立；T-034 与 T-035 同改 `tools/edit.py`，建议相邻落地。

四项都会改动 `TOOL_SPECS`，因此 `tool_schema_sha256` 必变，按执行规则 6 重新生成 baseline。

### `[x]` T-033 显式计划状态

对应：评审 L1

背景：`run_turn` 是纯线性 `for _step in range(max_steps)`，模型意图在 Runtime 内**没有任何外部表示**。`mark_agent_progress` 只能回答"正在调用哪个工具"，回答不了"它认为自己在做什么、还剩几步"。显式计划有两个作用：用户可见性，以及模型对照计划自纠（外部化工作记忆）。

范围：

- 新增 `update_plan` 工具，`read_only=False` / `approval="none"`（只写 session 状态，不碰文件系统与网络）。
- 计划随 session 持久化并进入 `SessionSnapshot`；新增 `plan.updated` 事件（按规则 5 三处登记 + Go renderer 渲染为进度清单）。
- 采用**整体替换**而非增量修改：增量接口要求模型维护稳定 id，实践中出错率高于收益。

验收：计划跨 daemon 重启可见；非法 status 被拒；多步任务中 `plan.updated` 次数 ≥ 计划条目数（机制正确性先在脚本 eval 验，可见性效果留待 T-016）。

完成记录（2026-07-31）：

- 新增 `update_plan` 工具与 `PlanItem`（`app/agent/session.py`），计划随 session 持久化并进入 `SessionSnapshot`；`plan.updated` 按规则 5 三处登记 + Go renderer 渲染为进度清单。
- 采纳整体替换而非增量：增量接口要求模型维护稳定 id，出错率高于收益。非法 status 与超过 `MAX_PLAN_ITEMS` 均被拒。
- `approval="none"` 这条路径需要在 policy 里新开一个分支，**位置有讲究**：它放在 read-only-mode 检查之后，否则一个 `read_only=False` 的工具会因为声明了 `approval="none"` 而在 review/explain/commit_message 模式下获得执行权。顺序本身就是约束，注释已写明。
- 只有内置 registry 能声明 `approval="none"`，MCP 工具一律强制 `approval="gate"`——外部服务器不能自我豁免审批。

### `[x]` T-034 read-before-write 强制

对应：评审 T3（正确性）

背景：`base_hash` 防的是"读过之后文件被外部改了"，**防不住"根本没读就编辑"**。模型可以凭空捏造 `old_text`，只有匹配失败才发现——而那个报错对模型而言与"文件内容不同"无法区分。

范围：session 维护已读文件集合（path → 内容 hash）；`replace` / `delete` 要求目标已读，未读即拒绝并指明先 `read_file`；`create` 不受限（文件不存在时无可读）；编辑成功后更新记录 hash，连续编辑无需重读。

验收：未读即 replace 被拒且文案可操作；读→编辑→再编辑无需重读；既有 stale 检测测试不改断言即通过。

完成记录（2026-07-31）：

- session 维护 path → 内容 hash 的已读集合；`replace` / `delete` 要求目标已读，`create` 豁免（文件不存在时无可读）；编辑成功后更新记录，连续编辑无需重读；删除文件会清除其记录。
- **这是正确性问题而非效率问题**：`base_hash` 只能发现"读过之后被外部改了"，发现不了"根本没读就编辑"。模型可以凭空编造 `old_text`，而那个不匹配报错对它而言与"文件内容确实不同"无法区分——强制先读把猜测变成一条明确的"去看一眼"指令。
- 仅在存在 session 时强制：Agent Loop 总会提供，而直接驱动编辑工具的嵌入方没有 session 级状态可依据。这条豁免写在 `require_prior_read` 的 docstring 里。
- `EditPort.build_edit_proposal` 签名由 `(workspace, arguments, protected_paths)` 改为 `(context, arguments)`——三者本就同源，而已读记录也在 context 上。

### `[x]` T-035 批量编辑

对应：评审 T2

背景：改同一文件三处 = 三轮模型调用 + 三次审批往返。T-019 解决了只读工具并发，**写入侧的往返成本没有动过**，这是当前延迟结构里最大的一块。

范围：`edit_file` 增加 `edits: [{old_text, new_text}]`，与单次形式并存；全部替换在同一份原文上按序应用；**任一处不匹配则整体失败，不允许部分应用**——半应用的编辑比失败更难恢复；一次 diff、一次审批。

验收：多处替换一次审批成功；任一不匹配则文件字节不变；重叠区间被拒；既有单次编辑测试不改断言即通过。

完成记录（2026-07-31）：

- `edit_file` 增加 `edits: [{old_text, new_text}]`，与单次形式并存；`resolve_edits` 把单次形式归一化为长度 1 的列表，管线下游只处理列表。
- **全部替换都对同一份原文定位**，不是在逐步更新的文本上连续 `str.replace`：后者会让靠后的 `old_text` 匹配到由前一次替换产生的、模型从未见过的内容，可能静默改错地方。实现改为先在原文中定位全部区间、检查重叠、再一次性拼接。
- 任一处不匹配即整体失败，落盘前抛出，绝不半应用——半应用的编辑比被拒绝的编辑更难恢复。
- 写测试时自己踩了一次坑：最初断言长度为 1 的批量形式会带 `edit 1:` 前缀，但该标签只在 `len(edits) > 1` 时出现。改成断言真实契约——长度 1 的批量与单次形式报错完全一致。

### `[x]` T-036 `glob` 工具

对应：评审 T1

背景：`search(query, glob)` 是"内容正则 + 路径过滤"，而"**找出名字符合某模式的文件**"是独立且高频的需求，现在只能用 `list_files` 配深度或拿正则撞路径。业界（Claude Code、Aider）都把 Grep 与 Glob 分开，因为检索意图不同。T-017 之后新增工具只需一次 `register`，边际成本近乎为零。

验收：模式匹配正确；protected path 不出现在结果；越界模式被拒；结果有上限。

完成记录（2026-07-31）：

- 新增 `app/tools/glob.py`。与 `search` 分开而不是合并：`search` 回答"哪些文件含这段文本"，`glob` 回答"哪些文件叫这个名字"，把后者表达成路径正则对模型别扭且更慢。
- 越界模式做**语法前置拒绝**（绝对路径、盘符、`~`、`..`），因为 `Path.glob` 对这类模式会直接枚举到工作区之外；但语法检查不足以保证安全，树内的 symlink 仍可指向外部，所以每个候选再做一次 `is_within_workspace` 实检。
- protected path 与 `IGNORED_DIRS` 从结果中剔除；结果有上限并在截断时提示收窄模式。

## M9：循环与上下文正确性（第二梯队）

来源：同上。需要设计，但**仍不依赖评测数据**。其中 T-038 是正确性问题，其余是效率与保真度。

T-038 → T-039 → T-040 有真实依赖：文件失效判定要先存在，摘要 schema 才能表达"哪些文件内容已作废"；中间层的折叠策略又复用同一套失效判定。

### `[x]` T-037 验证循环机制化

对应：评审 L2

背景：`VERIFY_NOTE` 文案里写着"stop and report after 3 consecutive failed attempts"，但**没有任何代码执行这个 3**——它是一句 prompt 里的话，不是机制。当前验证是一次性提示后完全交给模型自觉。

范围：Loop 跟踪验证尝试次数与结果；达上限后走既有收尾路径（与预算闸门同一条）产出总结；失败输出结构化提取（失败用例名、首个错误行）避免整段 stderr 挤占上下文；新增 `verify.attempt` 事件。

验收：连续失败达上限必定收尾且 `final` 非空；一次成功即退出；上限为 0 时行为回到当前实现。

完成记录（2026-07-31）：

- 新增 `app/agent/verify.py`：`VerifyTracker` 记账 +（`summarize_failure`）失败摘要抽取。`max_verify_rounds` 作为 `TurnBudget` 第四维（默认 3，`AICODE_BUDGET_MAX_VERIFY_ROUNDS`），与其余预算同源、同样不可被 `.aicode/config.json` 覆盖。
- **原实现比"没有执行那个 3"更弱**：`verify_note_sent` 是一次性布尔，模型说"做完了"→被推回一次→再说一次"做完了"，循环就退出。也就是说**验证在原实现里事实上是可选的**，不是"上限没被执行"，而是"根本没有上限"。`test_claiming_done_does_not_satisfy_verification` 专门钉住这一点。
- 同时修掉一个反向浪费：原实现只看"有没有发过 note"，**模型明明已经跑过验证也照样被推回一轮**，每个成功的编辑轮都白花一次 model call。两个既有测试（`test_e2e_smoke`、`test_local_provider_profile`）的期望值因此下降一次调用——不是回归，是少做一次无意义往返。
- `execute_gated` 的返回值由 `tuple[str, int]` 换成 `ToolCallResult`（带 `ok` 与 `exit_code`）。**第一版是从输出文本里正则抓 `exit=`，是错的**：失败路径会包上 `[error] ` 前缀，锚定失效；而且那是给模型看的渲染格式，拿来做控制流判断等于猜。退出码现在从 `result.data` 直接携带。
- 判定口径写在代码注释里并在此重申：编辑之后的任何 bash 都算一次验证尝试。Runtime **无法**对任意仓库判断模型跑的是不是"正确的"验证命令，因此闸门只保证"修复循环有界且必定收尾"，不冒充它保证不了的东西。
- 事件按规则 5 三处注册 + Go 渲染：`verify.attempt`、`run.verification.exhausted`。`budget_note` → `wind_down_note`，因为这条收尾路径现在服务两类原因。
- **eval 按规则 6 处理**：首跑 `success_rate` 1.0 → 0.8，安全指标未动。查因是 `single_file_fix` 的第 5 条脚本（"Verification complete"）只为应答旧的一次性 note 而存在，模型其实已经跑过 `unittest` 并通过。**没有下调断言**：删掉那条脚本，并把 `final_contains` 换成实质内容、加 `required_events: verify.attempt` 与 `forbidden_events: run.verification.exhausted`——比原先"断言一句脚本台词出现过"更强。重跑全部指标回到 1.0/0.0 后才只 rebase `prompt_sha256` 与 `task_set_sha256` 两个 digest。
- 测试策略：`without_verification()` 让主题不是验证的测试（审批流、批量编辑、并行）显式关闭该闸门，而不是给每个编辑测试都补一条通过命令。写测试时踩到一个自己的坑：用 `exit 1` 当失败命令会因不在 allow list 而**卡在审批上永久挂起**，改用 `cat missing_file.txt`。
- 验证：Python 451 项 + 1 skip、Go 全量、gofmt、go vet、ruff、compileall、eval-smoke PASS、clean-home install E2E 通过。

### `[x]` T-038 文件状态跟踪与压缩失效

对应：评审 C1（正确性）

背景：压缩把 `read_file` 输出**当作事实**写进摘要，而该文件可能已被编辑。摘要里于是留下**过期内容被表述为当前内容**，模型据此推理且无从知道它已失效。`base_hash` 只在 apply 时检测外部修改，防不住这条路径。

范围：复用 T-034 的已读记录；压缩时比对当前 hash，不符者其读取内容**不进入摘要**，代之以"该文件已变更，需要时重新读取"；已删除文件同样标记失效。

验收：读→编辑→压缩后摘要不含旧内容且含重读提示；未变更文件正常进入摘要。

完成记录（2026-07-31）：

- 每条 `read_file` 结果消息上持久化 Runtime 私有 provenance（`aicode_meta.read = {path, hash}`）；compaction 前 `invalidate_stale_reads` 按该 hash 与磁盘比对，不符者内容替换为重读提示，已删除的单独标记，并在 `context.budget` 事件以 `stale_reads` 列出、CLI 渲染出来。
- **任务原定的做法（"复用 T-034 的已读记录，压缩时比对当前 hash"）不成立，已改**：`session.read_files` 在写入时也由 `record_written_file` 更新，所以 Agent 自己编辑过文件之后该记录与磁盘一致，用它比对只能发现**外部**修改，恰好漏掉背景里写的"该文件可能已被编辑"这一主因。必须比对的是**那一次读取当时**的 hash，因此改为按读取记录 provenance 而不是复用滚动记录。`test_the_agents_own_edit_counts_as_a_change` 专门钉这条。
- provenance 放在消息上而不是旁路表：不会与它描述的消息脱节，且随 session 持久化跨 daemon 重启存活。
- **实现中踩到一个真实的顺序错误**：最初把剥离放在 `_normalized_messages`，但那个函数同时喂给 compaction 和 provider，于是 compaction 拿到的消息已经没有 provenance，失效判定完全不生效（集成测试直接抓到，单元测试全绿）。改为保留到管线末端，由 `strip_message_meta` 在交付给 provider 的边界统一剥离。
- 无法覆盖的一种情况已知并留给 T-039：连续压缩时上一轮摘要是自由文本，其中的过期内容无法再判定——这正是 T-039 结构化摘要要解决的问题。
- 验证：Python 474 项 + 1 skip、Go 全量、gofmt、go vet、ruff、eval-smoke PASS（无 baseline 变更）。

### `[x]` T-039 结构化压缩摘要

对应：评审 C3

背景：摘要要求 `Return concise Markdown bullets`。连续压缩（对压缩结果再压缩）时自由文本降解很快，且无法程序化检查完整性。

范围：改为结构化 schema（`goal` / `constraints` / `done` / `pending` / `files_touched` / `open_failures`）；`CompactionEntry` 存结构化结果并升 `COMPACTION_SCHEMA_VERSION`，旧条目仍可读（既有版本校验已支持忽略不兼容版本）。

验收：连续两次压缩后 `pending` 与 `open_failures` 不丢失（当前自由文本无法断言这一点）；模型返回非法结构时降级到确定性摘要并标注 `summary_mode=fallback`。

完成记录（2026-07-31）：

- 新增 `app/agent/summary.py`：六字段 schema、容错解析（去围栏、从散文中抽 JSON、单字符串归一为列表、去重与上限）、确定性渲染。`CompactionEntry` 增加 `structured` 列（migration 005），`COMPACTION_SCHEMA_VERSION` 升到 2。
- **验收里的"不丢失"靠代码保证，不靠模型自觉**：`pending` 与 `open_failures` 由 `merge_summaries` 程序化续接，模型只有把条目显式列进新的 `done` 才能移除它。匹配是文本比对，确实模糊，但偏差方向是安全的——认错只会让已完成项多留一轮（重复劳动），不会丢掉未完成项（静默放弃工作）。`test_pending_survives_two_consecutive_compactions` 里第二次摘要故意完全不提这两个字段，正是自由文本降解的样子。
- 非法结构降级到确定性摘要并标 `summary_mode=fallback` / `summary_error=invalid_structure`，同时**不把自由文本存成 structured**——否则下一轮续接会静默变成空操作。
- 任务描述里"旧条目仍可读"不准确：版本校验是**忽略**不兼容版本，不是读取它。升级后 v1 条目不再用于投影，会退回原始历史重新压缩一次。这是安全的（messages 是 source of truth），但代价不是零，已在 ARCHITECTURE 写明。SQL 行本身不受影响，`test_a_v4_database_gains_the_structured_column_without_losing_compactions` 覆盖。
- **顺手补掉一个 eval 覆盖漏洞**：`COMPACTION_SYSTEM_PROMPT` 此前只由手工维护的 `compaction_prompt_version` 字符串"覆盖"，而 `prompt_sha256` 只哈希 `prompts.py`——改了压缩提示词却不改版本号，没有任何闸门会发现。新增 `compaction_prompt_sha256` 直接哈希提示词文本，并实测验证：改一个词即 FAIL，改回即 PASS。这与之前 `tool_schema_sha256` 是同一类问题。
- 验证：Python 505 项 + 1 skip、Go 全量、gofmt、go vet、ruff、eval-smoke PASS、clean-home install E2E 通过。

### `[x]` T-040 中间压缩层

对应：评审 C2

背景：当前只有两档——写入时按工具截断，以及触发阈值后的整组有损摘要。中间缺一层，导致过早触发有损压缩。被删除的 `compact_if_needed` 粗糙地做过这件事，方向对、实现不对（无组边界意识、无持久化）。

范围：保留最近 K 组 tool 输出原文，更早的折叠为一行引用；以完整消息组为边界（复用 `_message_groups`），不跨越未闭合的 tool_call 组。

验收：达中间阈值时只折叠不摘要，token 下降且 `pending` 类信息零损失；仅当折叠后仍超限才触发全量摘要。

完成记录（2026-07-31）：

- `fold_old_tool_output` 把较早的 tool 结果换成一行引用（含工具名与原字符数），`_fold_to_fit` 让 `keep` 从 `FOLD_KEEP_RECENT_GROUPS` 往下试，只折叠到刚好装得下为止——只超出一点点的历史几乎全部保留原文。
- "零损失"的依据是**只折叠 `role == "tool"`**：用户约束与助手推理一字不改。`test_only_tool_messages_are_folded` 直接钉这一条。边界复用 `_message_groups`，未闭合的 tool_call 组整体跳过。
- 折叠是**纯投影**，不写回 `session.messages`，每轮从原始消息确定性重算。这正是与被删掉的 `compact_if_needed` 的差别：后者改内存副本，与持久化的 source of truth 漂移。确定性重算既不需要持久化，也不可能漂移——所以任务背景里提的"无持久化"问题在这个实现下不存在，而不是被绕过。
- 短输出不折叠（引用比原文还长时得不偿失）。`context.budget` 增加 `reason="folded"` 与 `folded_tool_outputs`，CLI 渲染为"折叠了几条、token 从多少降到多少、无需摘要"。
- **改了一个既有测试而不是删它**：`test_compaction_boundary_keeps_tool_call_and_result_together` 原先靠 8 组 40KB 的 tool 输出触发摘要，现在被折叠吸收了。把体量移到 assistant 消息上——折叠不碰这一类——于是它仍然走到摘要、仍然在验证边界规则。
- fold 变体没有进 SSE fixture：该 fixture 契约要求每种事件类型恰好一条，重复的 `context.budget` 会破坏它；变体改由 Go 单测覆盖。
- 验证：Python 515 项 + 1 skip、Go 全量、gofmt、go vet、ruff、eval-smoke PASS、clean-home install E2E 通过。

### `[x]` T-041 打转检测

对应：评审 L4

背景：`max_steps = 40` 对"改一行"和"跨六文件重构"是同一个数。比调整这个数更有价值的是无进展检测。

范围：跟踪连续"相同工具 + 相同参数"或"相同失败"；达阈值注入明确 note；再达上限走收尾路径。

验收：构造反复调用同一失败命令的脚本模型，验证在耗尽 `max_steps` 之前被拦下并产出总结。

完成记录（2026-07-31）：

- 新增 `app/agent/progress.py`：`StallTracker` 跟两条连续计数——相同工具+相同参数、相同失败。两条而非一条，因为它们抓不同形状的卡住：前者是原样重发，后者是微调参数却撞同一堵墙（`test_varying_arguments_still_trip_on_the_identical_failure` 单独钉这条）。参数按 key 排序序列化，顺序不同不能伪装成不同调用；不可序列化的值退回 repr 而不是抛异常——签名算不出来应当降级为"不算重复"，不应该在 loop 里炸。
- `max_repeated_actions`（默认 5，`TurnBudget` 第五维）达上限走既有收尾路径，`warn_at = limit - 2` 先注入 note。为 0 或 1 关闭。
- **实现中改了一处自己的设计**：最初警告按 (reason, signature) 各记一次，结果同一次卡住同时触发两条计数、连发两条警告。改为**每个 episode 只警告一次**，两条计数都掉回 `warn_at` 以下才重置；同时适用时优先报 `repeated_failure`，因为它信息量更大。
- 阈值不从 2 起跳是刻意的：连着两次相同调用往往合法。`test_two_identical_calls_are_never_enough` 与 `test_varied_work_is_never_interrupted` 守的是误报方向。
- **改了一个既有测试而不是删它**：`test_max_steps_forces_summary` 原先用 40 次完全相同的 `read_file` 触发步数上限，现在会先被打转检测拦下。改成每次参数不同，于是它仍然在测步数预算本身。
- 验收测试用的是 `StuckProvider`（只要还给工具就一直重发同一失败命令），而不是定长脚本——脚本会在耗尽时"恰好"停下，证明不了是检测起的作用。
- 未做：没有把打转检测加进 eval 任务集。单测已覆盖到收尾与总结，加 eval task 会带来 task_set digest 变更，价值增量有限；若后续 T-016 显示打转是真实失败模式，再补。
- 验证：Python 531 项 + 1 skip、Go 全量、gofmt、go vet、ruff、eval-smoke PASS、clean-home install E2E 通过。

## M10：交互与执行环境（第三梯队）

来源：同上。彼此独立，按需推进。

### `[x]` T-042 `ask_user` 工具

对应：评审 L3。模型当前只有两个出口——继续猜或结束；审批只能回答"这个操作可不可以"，回答不了"你想要哪种方案"。范围：turn 中途提问并等待，复用 approval broker 的等待/超时/取消语义（含 T-032 的四态终结），超时按"未回答"处理并明确区别于"用户拒绝"。

完成记录（2026-07-31）：

- 新增 `app/tools/ask.py` 与 `ask_user` 工具，`SessionApprovalBroker.ask()` 复用同一套 pending 机制——等待、超时、取消、四态终结只有一份实现。差别只在答案形状：审批是布尔，提问是文本，因此 `PendingApproval` 加 `response`，并新增 `/v1/sessions/{id}/answer`；把答案塞进布尔端点会让文本无处可放。
- **超时与拒绝的文案严格分开**：超时明说"这不是拒绝，是没有人做决定"，要求带着明确声明的假设继续或停下汇报。`test_a_timeout_is_not_a_refusal` 连"declined 不得出现在超时文案里"都断言了。
- `read_only=False` / `approval="none"`：会阻塞整轮等人，绝不能并发成组；但提问本身就是那次交互，再套审批等于问两遍（`test_asking_needs_no_separate_approval`）。无可交互用户时直接失败，不返回看起来像答案的东西。
- **CLI 端做完了，否则这个能力端到端不可用**：`question.asked` 触发一个接受任意文本的 pending prompt。这里有个真实的坑——必须在 y/n 解析之前拦截，否则用户回答"no"会被当成拒绝审批。`TestAnswerNoIsAnAnswerNotARejection` 专门钉这条。`/skip` 表示不回答。
- 系统提示补一条"能靠读项目确定的事就去读"：这个能力最现实的失败模式是滥用而非不用。
- eval 按规则 6 处理：`tool_specs_sha256` 与 `prompt_sha256` 变更，5 个 run 全通过、指标无回退后才 rebase。
- 验证：Python 545 项 + 1 skip、Go 全量（含 4 项新增 REPL 测试）、gofmt、go vet、ruff、eval-smoke PASS、clean-home install E2E 通过。

### `[x]` T-043 后台与长时命令

对应：评审 T4。`bash` 最多阻塞 600 秒，dev server / watch / 长构建做不了。范围：background 模式返回句柄，配套读取输出与终止工具，复用 `ExecutionService` 的进程组终止与 daemon 停止清理。

完成记录（2026-07-31）：

- 新增 `app/execution/background.py`，`bash(background=true)` 返回句柄，配套 `read_output` / `stop_command`。`BackgroundProcessManager` 挂在 `ExecutionService` 上，于是 lifespan 的 `aclose()` → `cancel_all()` 这条既有路径顺带收干净后台进程——不是新建一条清理链路。进程组终止复用 `kill_process_group`。
- 进程**不挂在 session 上**：session 被缓存淘汰时挂在它上面的进程会活得比属主久。读取游标才挂在 session 上，两个 session 看同一命令不会互相吃掉输出。
- 输出由常驻 reader task 持续抽干，不是按需读取——管道写满的进程会永久阻塞。缓冲区有上限，读取按绝对偏移寻址，落后者被明确告知丢了多少字符；静默的缺口会被当成连续输出来推理。
- **Docker backend 下拒绝后台执行**：Docker 路径每次执行建容器、返回即拆除，"后台"在那里不是同一个意思。与沙箱不可用时同源——拒绝，而不是悄悄把不受信任工作区的服务器放到宿主机上跑。
- **实现中改了一处自己的判断**：`status()` 最初让 `exited` 优先于 `stopped`，于是被我们杀掉的进程报告成"自己退出了"——模型据此可能得出"服务器崩了"。改为 `stopped` 优先，退出码照常一并给出。
- 进程组终止用 T-035 时重写的 pid 断言（`process_helpers`）验证，不是靠 marker 计时推断；跑完全量套件后实测无残留进程。
- 验证：Python 561 项 + 1 skip、Go 全量、gofmt、go vet、ruff、eval-smoke PASS（仅 `tool_specs_sha256` 变更）、clean-home install E2E 通过。

### `[x]` T-044 OS 级沙箱

对应：评审 E1。Codex 用 seatbelt / landlock，启动毫秒级、无镜像依赖。当前 trusted workspace 只能裸跑，正是因为容器太重（T-015 因此要求显式预拉镜像）。OS 级沙箱能让"默认沙箱"对 trusted 也成立。契约已就绪，属新增 backend 而非改结构。

完成记录（2026-08-01）：

- 新增 `app/execution/sandbox_os.py`（macOS seatbelt）与 backend 取值 `os`，贯通 Runtime settings、`.aicode/config.json`、`schemas/config.schema.json`、README 与 ARCHITECTURE。包装 host backend 而非重写：进程组、超时、取消、输出抽干都已解决，唯一差别是启动 shell 的 argv。
- **profile 用 `(allow default)` + 定点拒绝，不是 `(deny default)` + 白名单**。deny-by-default 要枚举真实工具链触碰的每个 mach service / sysctl / 共享内存区；名单列漏不会削弱沙箱，只会让编译器以"莫名其妙的工具失败"崩掉。真正要守的两条——工作区外不可写、禁网——不需要那份名单也能强制。protected paths 另行禁读且必须排在广泛读许可**之后**（seatbelt 取最后一条匹配规则）。路径做转义：含引号的路径会提前终止字符串并改变规则语义，那是注入不是排版。
- **刻意没有改 `auto` 的语义**，尽管任务背景提到"让默认沙箱对 trusted 也成立"。翻默认值要么给 untrusted 悄悄降级隔离强度（seatbelt 弱于容器：同一文件系统命名空间、同一内核、无资源限制），要么给 trusted 加上禁网/受限写而打断现有工作流——两者都不该作为"新增一个 backend"的副作用发生。`os` 是显式选项，`auto` 行为不变，`test_auto_is_unchanged_by_adding_the_backend` 钉住这一点。是否翻默认值留给用户单独决定。
- macOS 之外直接失败并说明原因：Linux 需要 landlock 或 bubblewrap，**声称一条并未生效的边界比明说不支持更糟**。沙箱不可用时与 Docker 不可用同源——拒绝，绝不回退到裸跑。
- **实现中发现并堵上一个自己刚造的洞**：后台命令（T-043）不走 `ExecutionRequest`，因此选了 `os` 之后后台命令会**完全不受沙箱约束**——恰好豁免了最需要它的长时命令。改为在后台路径显式再包一层 seatbelt，profile 文件随进程结束清理。`test_background_commands_are_sandboxed_too` 覆盖。
- **第一版逃逸测试是空过的**：把逃逸目标放在 `tmp_path` 附近，而系统临时目录是刻意保持可写的（构建工具写不了临时文件不是被沙箱化，只是坏了），于是测试必然通过却什么都没证明。改为写 `~` 下的目标——那既在工作区和临时目录之外，也正是这条边界真正要保护的地方（`~/.ssh`、`~/.aws`、shell rc）。
- `ExecutionRequest.__post_init__` 的 backend 白名单同步放行 `os`（原先只认 host/docker，新 backend 会在构造期就被拒）。
- 验证：Python 580 项 + 1 skip（其中 19 项新增，非 macOS 上按平台跳过）、Go 全量、gofmt、go vet、ruff、eval-smoke PASS（无 baseline 变更）、clean-home install E2E 通过。

### `[x]` T-045 hooks

对应：评审 E2。范围：允许在工具事件上挂命令（写入后 format、提交前 lint 门禁）。**hook 命令必须与 Agent 命令走同一执行与审计路径**，否则等于开了一个绕过 policy 的旁路。

完成记录（2026-08-01）：

- 两个触发点：`post_edit`（编辑落盘后，格式化）与 `pre_bash`（shell 命令前，非零退出即拒绝该命令）。`.aicode/config.json` 的 `hooks` 数组声明，含 `match` glob、`timeout`、`blocking`。
- **hook 命令走 `run_shell_command` → `ExecutionService` → 审计，并同样经过 `PolicyEngine.gate_bash`**。这是任务的硬约束：绕开就等于"把命令写进配置文件即可绕过 deny 列表"。审计以 `hook.<event>` 标记；backend 由同一个 `resolve_bash_backend` 决定，沙箱化 workspace 不会因声明 hook 而多出宿主机执行的侧路。`test_a_hook_cannot_reach_past_policy` 与 `test_hook_execution_is_audited_like_an_agent_command` 覆盖。
- **untrusted workspace 完全不执行 hook**（范围外但必要）。`.aicode/config.json` 随仓库分发，hook 就是仓库作者选的代码——只要打开目录就执行它，等于 clone 一个恶意仓库便足以运行其命令。与"不做静默降级"同源：不执行会**显式上报**，因为没触发的 hook 不能与通过了的 hook 长得一样。
- **`{path}` / `{command}` 替换一律 `shlex.quote`**。仓库可以包含名为 `a; rm -rf ~.py` 的文件，原样拼接会把"格式化刚写的文件"变成任意命令执行——这是注入不是排版。`test_a_hostile_filename_cannot_run_a_second_command` 用真实文件验证。
- `post_edit` 结构上无法否决编辑（运行时文件已落盘），失败只上报；能拒绝的只有 `pre_bash`。**hook 改写文件后刷新 read 记录**，否则模型对同一文件的下一次编辑会被 read-before-write 判为 stale——这一条由端到端测试（真实 loop 跑一次编辑 + 格式化 hook）钉住。
- 未知 event 名直接丢弃而非落到默认值：拼错不能把命令悄悄挂到另一个触发点上。
- 新增事件 `hook.finished` / `hook.blocked` 同步进 `events.py` 允许表、`schemas/events.schema.json` 与 SSE fixture；Go renderer 给出可读输出而不是原始 JSON dump，跳过原因也会渲染。
- 验证：Python 670 项 + 1 skip（其中 20 项新增）、Go 全量、gofmt、go vet、ruff、eval-smoke PASS。

### `[x]` T-046 session fork

对应：评审 E3。"从这里换个方案试试"当前只能重跑整轮。范围：从指定 message id 派生新 session，复用既有 append-only messages + compaction projection 结构。

完成记录（2026-08-01）：

- `SessionStore.fork(session_id, message_id=None)`：截止该消息的历史照常 append 进新 session，因此分叉产物就是一个普通 session，**没有引入第二套机制**。省略 `message_id` 即从当前末尾分叉。
- **历史复制而非共享**。共享行会让两个 session 的未来互相延长对方的过去——正是 fork 要避免的。`test_forked_history_is_copied_not_shared` 用双向追加验证。
- **compaction 边界必须重映射**。message id 是全局自增的，把 `start_message_id` / `end_message_id` 原样搬过去会指向**别的 session 的行**，让 fork 的 projection 去摘要一段不属于它的消息。映射不上的条目宁可丢弃（代价只是少省一点 token）也不写悬空引用；越过分叉点的 compaction 不带过去。
- **不属于该 session 的 `message_id` 直接报错，不做就近裁剪**：静默分叉到另一个点，产出的 session 看起来正确、历史却是错的。
- `plan` 随 fork 带走（同一件事的延续）；`read_files` **不带**——它按设计只存在于内存，保证的是"在这轮对话里见过该文件的当前内容"，fork 后本就应重读。这条差别由测试显式钉住，否则很容易被"顺手也复制一下"改错。
- 贯通 `SessionRepository` 协议、`InMemorySessionRepository`、`SessionService.fork`（记 `session.forked` trace，新 session 发 `session.created` 且带 `forked_from`）、`POST /v1/sessions/{id}/fork`、Go client `ForkSession` 与 `aicode session fork <id|--last> [--message N]`。
- 验证：Python 681 项 + 1 skip（其中 11 项新增）、Go 全量、gofmt、go vet、ruff、eval-smoke PASS。

## M11：增效与评测触发项

**T-014（prompt caching）与 T-016（真实模型评测）已从第一梯队降级至此**（2026-07-29 目标切换为生产可用）；两者都不阻塞生产可用。T-023 / T-024 / T-026 三项**在 T-016 产出真实评测数字之前不启动**，届时按数据决定取舍。此纪律沿用 [LOCAL_AGENT_ROADMAP.md](LOCAL_AGENT_ROADMAP.md) 第 536 行：索引、subagent、自动规划框架只由评测结果触发。

### `[ ]` T-014 Anthropic prompt caching

对应：评审 B3（2026-07-29 由第一梯队降级：属成本增效而非正确性问题，不阻塞生产可用）

依赖：无

范围：

- `system` 由裸字符串改为 block 数组并打 `cache_control` 断点；`tools` 末元素打断点覆盖整个工具块。
- 由 `settings.anthropic.prompt_caching` 开关控制，关闭时 payload 形状与当前完全一致，保证可回退与 A/B。
- `TOOL_SCHEMAS` 是模块级共享常量，打断点前必须深拷贝，避免污染 OpenAI-compatible 路径。
- `Usage` 增加 `cache_creation_input_tokens` / `cache_read_input_tokens`；`estimate_cost` 分档计价，缺省退化为普通 input 计价。

验收：

- payload 形状、深拷贝回归、开关关闭时行为一致、cache token 解析与分档计价均有测试。
- **交付物包含量化数据**：同一真实 session 连续 5 轮，开启与关闭 caching 的 `input_tokens` 与 `estimated_cost` 对比，写入 README 或评审文档。无此数据不算完成。

### `[ ]` T-016 真实模型评测套件

对应：评审 D1（2026-07-29 由第一梯队降级：价值在于能力证明，不阻塞生产就绪）

依赖：T-008（eval 基线）

背景：现有 `evals/` 仅有 `ScriptedEvalProvider`，证明的是 Agent Loop 实现正确，不是 Agent 能完成真实任务。

范围：

- **保留 scripted smoke suite 并继续留在 CI**，其零成本、零抖动的回归价值不可替代；live 套件是并行新增的第二条链路，因成本与不确定性不进 PR CI。
- 新增 `LiveEvalProvider`：包装 `ModelRouter`，对外暴露与 `ScriptedEvalProvider` 相同的 `calls` / `total_tokens` / `total_cost` 接口，使 `run_metrics` 无需分支。
- `EvalTask` 增加 `provider_mode: scripted | live` 与可选 `live_model`；live 模式跳过脚本相关断言。
- 任务集 20–30 个，四类分布：单文件缺陷修复 8、跨文件改动 6、边界/新增测试 6、失败后二次修复 4，另复用现有 4 个安全场景。
- 判定沿用确定性 grader（测试转绿 / 未授权修改 / 危险命令执行），**不引入 LLM-as-judge**。
- `make eval-live`（默认 `--repetitions 3`），可选接 nightly workflow。

验收（本任务交付物是数字，不是代码）：

- 在 `docs/review/` 产出评测报告：分类别的 pass@1 / pass@3、平均与总成本、平均与 p95 耗时、失败归因分类（定位失败 / 编辑失败 / 验证失败 / 预算耗尽）。
- 该数字写入 README 顶部。

进度（2026-08-01）：**harness 与任务集已完成，首轮真实运行已跑通，仍为 `[ ]`**——首轮的数字不可用于交付（原因见下），需在修复后重跑再落 `docs/review/` 与 README 顶部。

首轮运行（DeepSeek，OpenAI-compatible，`REPETITIONS=1`）：27/28 通过，p95 35.4s。这一轮的价值不在数字而在**它暴露的三个 bug，其中两个在产品里而不在评测里**：

- **policy 路径探测崩溃（产品 bug）**。`policy.py` 把 shell 命令的每个 token 都当候选路径试探，`Path.exists()` 吞 ENOENT 但不吞 ENAMETOOLONG。模型内联一段 579 字符的 `python3 -c` 脚本时，异常从 policy 抛出、**越过 tool 错误处理、终结整个 turn**（`session.final` 都没发出）——模型写一段长脚本的代价是丢掉整个会话，而不是拿回一次失败的工具调用。已改为不抛异常的探测；`path_like` token 仍照常按解析后的文本 gate，由 `cat ../aaa…` 仍然 deny 的测试钉住。回归测试先复现了生产报错再验证修复。
- **模型别名导致计价静默丢失（产品 bug）**。DeepSeek 把 `deepseek-chat` 以 `deepseek-v4-flash` 的名字返回；价格表按**配置的**模型名建键，计价却用**返回的**模型名，于是查不中、整轮计价为 $0.00。已改为回退到请求的模型名，并新增 `unpriced_model_calls` 指标让"计价为 0"无法再静默通过（该指标立刻在 scripted smoke 里也抓到 7 次）。**"免费"和"没配价格"必须长得不一样。**
- **approval 策略让评测测错了东西（出题 bug）**。原先 24 个作业任务是 `tool: reject`，模型**一次命令都跑不了**，代码是盲写的、验证是 grader 做的。于是 `retry_fix` 不再测"读失败再改"，`verification_failure` 永远不可能触发。已改为作业任务 `tool: accept`、安全任务保持 `reject`（拒绝本身是被测行为），并由测试钉住这个划分。policy 仍直接拒绝危险可执行文件与 protected path，accept 放宽的是可批准范围而非可执行范围。

另外两个出题 bug 在此前一轮已修（长上下文种子按窗口比例化、clamp mutation 由"单点 off-by-one"改为"整体移除文档行为"），本轮两项均通过。`reference_solutions.py` 新增第二份写法不同的解交叉校验 mutation——只校验一份参考解，分不出 mutation 写得对还是恰好对上了那份解挑的输入。

已完成部分：

- `LiveEvalProvider` 暴露与 `ScriptedEvalProvider` **完全相同**的 `calls` / `total_tokens` / `total_cost` 表面，`run_metrics`、trace writer、预算检查全部无 mode 分支。它包装 `ModelRouter.from_settings` 选出的真实 provider，复用而非重复 provider 选型。
- `EvalTask` 增加 `provider_mode` 与 `live_model`；两种 mode 的非法组合在 contract 层就被拒（live 带 script、scripted 缺 script、mode 与 profile.provider 不匹配）。**live 带 script 必须报错而不是忽略**：一条被静默忽略的断言读起来和一条在跑的断言一模一样。
- 任务集 28 个，分布与要求一致：单文件 8、跨文件 6、新增测试 6、二次修复 4，另 4 个安全场景的 live 变体。安全变体只保留与模型措辞无关的不变量（什么都没被破坏、凭据没被读到、没有 host 执行、compaction 确实发生），scripted 版那些精确措辞断言真实模型不会复现。
- **修复类任务把测试文件放进 `forbidden_changed_paths`**：把测试改成迎合坏实现是伪造通过最省事的方式。
- **新增 `checks.mutations`**。"新增测试"类任务光看套件通过毫无意义——空测试文件也通过。grader 把一处蓄意缺陷打进工作区**副本**再跑测试，要求它失败。mutation 写在 task JSON 而不是 fixture：fixture 会被整个复制进 Agent workspace，放那里就是答案卡。用副本是为了不污染 diff 与 content hash。
- **失败归因确定性推导**，优先级 `safety_violation` > `budget_exhausted` > `agent_error` > `localization_failure` > `verification_failure` > `edit_failure`，全部来自既有 trace 字段，可从存档 trace 复现。**没有 LLM-as-judge**：评委是模型的话，"为什么失败"就变成第二个要评测的东西。
- **新增 `evals/reference_solutions.py`**（范围外但必要）：每个 live 任务一份已知可行解，测试套件校验其可解且每条 mutation 都被抓到。无解任务报出来的是出题人的 bug 而不是模型的失败，两者必须能区分——已验证 24/24 可解、10/10 mutation 被抓。为控制这一步的耗时，参考解校验关掉了 pytest 插件自动加载（1.9s → 0.5s/次，fixture 只用核心 pytts 特性）；grader 本身刻意不这么做，它跑的必须和 Agent 跑的一致。
- live 预算**硬停**而非事后统计（超支花真钱）；缺 key 在建目录、发请求之前就失败——跑到第 12 个任务才发现没 key，钱已经花掉，而且失败读起来像 Agent 不行。
- `make eval-live` 默认 `--repetitions 3`，支持 `REPETITIONS=` 与 `LIVE_MODEL=`（后者为 T-026 的横向对比预留）；**不进 `make test`、不进 PR CI**。
- 报告新增分类别 pass@1 / pass@k、mean 与 p95 耗时、失败归因分布。p95 取 nearest-rank：插值出来的是没有任何一次运行真正花过的数字。
- 验证：Python 634 项 + 1 skip（其中 54 项新增）、Go 全量、gofmt、go vet、ruff、eval-smoke PASS（baseline 的 `eval_harness_sha256` / `eval_schema_sha256` / `task_set_sha256` 按预期更新，5/5 任务全通过、recorded_metrics 未变）。

### `[ ]` T-023 检索式项目记忆

对应：评审 B4 | 触发条件：T-016 显示跨会话重复解释同一项目约定造成可观测的 token 浪费

范围：`.aicode/memory/*.md` 按主题拆分的带 frontmatter 小文件取代当前全量注入的单一 `memory.md`；新增 `memory` 工具（`approval="gate"`）；按当前任务关键词/路径检索注入而非全量；保留旧 `memory.md` 读取兼容。

### `[ ]` T-024 plan / todo 工具

对应：评审 B5 | 触发条件：T-016 的失败归因集中在"跑偏 / 漏做子任务"

范围：轻量 todo 工具，主要价值是用户可见性而非模型记忆；Go renderer 渲染为进度清单。

### `[ ]` T-026 模型横向对比与上下文消融

对应：评审 D2 / D3 | 依赖：T-016

范围：同一 live 任务集跑 3 个模型产出成本–成功率曲线；消融 `compact_threshold` 0.8 vs 0.6、有无 `related_files`。指标 `with_compaction_success_rate` / `without_compaction_success_rate` 已实现，只需喂真实数据。结论写入评审文档与 README。

### `[ ]` T-047 repo map / 符号索引

对应：评审 T5 | 触发条件：T-016 的失败归因中"定位失败"占比显著

背景：`related_files` 是启发式黑箱（前三轮评审均提到），模型无法理解它为何给出这些结果。业界更有效的是 repo map（Aider：tree-sitter 抽符号签名按图排序注入），这是大仓库上下文效率最大的单项收益。**但值不值得取决于失败模式**——若失败集中在别处，投入索引是浪费。

范围：tree-sitter 抽符号签名按引用关系排序注入；同时评估 `related_files` 是否应被取代。

### `[ ]` T-048 subagent

触发条件：T-016 显示主上下文被探索过程显著污染。沿用 [LOCAL_AGENT_ROADMAP.md](LOCAL_AGENT_ROADMAP.md) 第 536 行纪律。任何 subagent 必须复用预算、Policy、ExecutionBackend、approval 与 trace。

## 后续候选

以下任务不进入当前关键路径，需由 eval 或用户需求触发：

- 受控扩展系统。
- Session branch/fork/export/import。
- symbol/import/test mapping。
- subagent（前置条件见 T-023 / T-024 的触发纪律）。
- 全屏 TUI、Web UI、云端执行。
