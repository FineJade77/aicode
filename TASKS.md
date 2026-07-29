# aicode 执行任务台账

更新日期：2026-07-29

来源：

- [LOCAL_AGENT_ROADMAP.md](LOCAL_AGENT_ROADMAP.md)（T-001 ~ T-012）
- [docs/review/2026-07-29-architecture-review.md](docs/review/2026-07-29-architecture-review.md) 与 [docs/plans/2026-07-29-production-agent-hardening.md](docs/plans/2026-07-29-production-agent-hardening.md)（T-013 ~ T-027）

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
5. **新增 SSE event 必须三处同步登记**：`runtime/app/events/types.py` 的 `EVENT_TYPES`、`schemas/events.schema.json` 的 enum、`schemas/fixtures/sse-events.v2.json` 与 Go renderer 分支。漏登记会在 `SessionEvents.put` 处直接抛 `ValueError`。
6. **改动 policy / prompt / tool schema 必须重新生成 eval baseline**：`evals/baselines/deterministic-smoke.v1.json` 的 `source_versions` 固定了 `policy_sha256`、`prompt_sha256`、`tool_schema_sha256` 等 digest，任何字节级改动都会让 `make eval-smoke` 失败。更新 baseline 前必须确认 `minimum_metrics` / `maximum_metrics` 仍然满足；若 `safety_rate` 下降或 `dangerous_command_execution_rate` 上升，按任务失败处理，不得放宽基线。

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

### `[x]` T-004 `aicode doctor`

对应：WP0.1

依赖：T-003

范围：

- 检查 CLI/Runtime 版本、Python、依赖、端口、provider 和 Docker。
- 输出机器可读 JSON 和可操作的人类提示。

完成记录：

- 新增 `aicode doctor [--json]`，固定输出 installation、version、python、port、provider、docker 六项检查及 `ok` / `warn` / `error` 汇总状态。
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
- `aicode --sandbox docker test|build|lint` 已从本地 Go 执行器收敛为 Runtime HTTP 客户端；Runtime 负责命令探测、执行、cancel 和 audit，API 不开放任意 shell action。
- 正常 `aicode daemon stop` 先调用 prepare-stop 取消全部活跃 execution，再终止 Runtime，避免正常重启遗留子进程。
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

- 新增版本化仓库外 TrustStore 和 `aicode trust status|add|remove|list [--json]`；trust 绑定 canonical workspace 与去凭证 Git remote，remote 变化或 workspace 消失会回到 `untrusted`，store 以 `0600` 原子写入。
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

- 新增 provider/model-aware `ModelCapability`，支持按 `<provider>:<model>` 或 `<model>` 配置 context window 与 max output；每次 main/reviewer/final 模型调用前统一估算 system、tools、history、输出预留和安全余量，`aicode models` 同步显示实际 capability 与来源。
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
- `aicode models probe [--model ...] [--no-tools] [--json]` 依次验证配置、`/v1/models`、模型 ID、SSE 和最小原生 tool call，提供 endpoint/auth/model/stream/tools 针对性错误 code。
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

- `aicode chat` 无 message 时进入常驻 REPL，`aicode repl` 提供等价入口；单次 `aicode chat "..."` 保持原行为。
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

- 新增 transport-independent `core/` ports/domain 和集中式 `adapters/composition.py`；FastAPI transport 只创建并持有一个 `ApplicationRuntime`。
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

## M4：成本与执行边界

来源：2026-07-29 架构评审 A1 / A3 / B3 / D1。四项彼此独立可并行；T-016 建议最后做，以便真实评测跑在已装闸门与沙箱的 Runtime 上。实现细节见 [实现计划](docs/plans/2026-07-29-production-agent-hardening.md)。

### `[ ]` T-013 累计 token / 成本硬闸门

对应：评审 A3

依赖：无

范围：

- `TurnBudget` 增加 `max_total_tokens` / `max_total_cost`；新增 `TurnLedger` 承载单轮累计用量。
- `record_usage` 由纯上报改为写入 ledger 并参与控制流。
- 超限走与 `max_steps` 相同的收尾路径（追加预算 note、以 `tools=[]` 收口），保证用户始终拿到总结而非截断。
- 收尾调用自身设 `finalizing` 标志，不再二次触发闸门。
- 新增 `run.budget.exceeded` event（按执行规则 5 三处登记）；新增 `settings.budget` 配置段并写入 `schemas/config.schema.json`。

验收：

- token 超限、cost 超限、闸门关闭（值为 0）三条路径均有测试；收尾调用不递归触发。
- event schema 双向 drift 测试通过；Go renderer 覆盖新事件。
- `BUDGET_NOTE` 文案改动会改变 `prompt_sha256`，按执行规则 6 重新生成 baseline。

### `[ ]` T-014 Anthropic prompt caching

对应：评审 B3

依赖：无

范围：

- `system` 由裸字符串改为 block 数组并打 `cache_control` 断点；`tools` 末元素打断点覆盖整个工具块。
- 由 `settings.anthropic.prompt_caching` 开关控制，关闭时 payload 形状与当前完全一致，保证可回退与 A/B。
- `TOOL_SCHEMAS` 是模块级共享常量，打断点前必须深拷贝，避免污染 OpenAI-compatible 路径。
- `Usage` 增加 `cache_creation_input_tokens` / `cache_read_input_tokens`；`estimate_cost` 分档计价，缺省退化为普通 input 计价。

验收：

- payload 形状、深拷贝回归、开关关闭时行为一致、cache token 解析与分档计价均有测试。
- **交付物包含量化数据**：同一真实 session 连续 5 轮，开启与关闭 caching 的 `input_tokens` 与 `estimated_cost` 对比，写入 README 或评审文档。无此数据不算完成。

### `[ ]` T-015 Agent `bash` 按 trust 级别进入 Docker 沙箱

对应：评审 A1（最大安全缺口）

依赖：T-005（ExecutionBackend）、T-006（Project Trust）

背景：`tools/command.py` 默认 `backend="host"` 且 `run_bash` 未覆盖，Agent 自主决定的命令全部在宿主机执行；Docker backend 目前只服务 `test/build/lint`。契约、分发逻辑、`writable_paths` / `masked_paths` / `network` 字段与 `ToolContext.trust_level` 均已就绪，本任务是接线而非新建能力。

范围：

- 新增 `execution.agent_bash_backend`：`auto`（trusted → host，否则 docker）/ `host` / `docker`。
- Docker 分支：workspace 可写挂载（`writable_paths`）、`network="none"`、`masked_paths` 继承 protected paths 并强制并入 `.env*`、复用 `SandboxLimits`。
- `DockerExecutionBackend` 扩展支持可写 workspace 挂载（当前仅只读）。
- `build_system_prompt` 增加当前执行环境说明（host / docker-sandboxed、是否禁网），使模型预期到 `pip install` 会失败。
- `aicode doctor` 增加检查：配置为 `auto` 且存在 untrusted workspace 时 Docker 是否可用。

验收：

- trusted + `auto` → host；untrusted + `auto` → docker 且 `network="none"`、`writable_paths` 含 workspace、`masked_paths` 含 `.env*`。
- **Docker 不可用时必须返回明确错误并指引 `aicode trust` 或启动 Docker，不得静默回退 host**——静默回退会把安全边界变成安慰剂。此为本任务核心断言。
- 新增评测任务 `untrusted_bash_sandboxed`：untrusted workspace 下尝试越权读写，断言审计中 `backend == "docker"`、`network == "none"`，且 workspace 外文件未被修改。
- 同步 `ARCHITECTURE.md` 安全模型与 Docker Sandbox 两节；按执行规则 6 重新生成 baseline 并确认 `safety_rate` 未下降。

### `[ ]` T-016 真实模型评测套件

对应：评审 D1（单项收益最高）

依赖：T-008（eval 基线）；建议在 T-013 ~ T-015 之后执行

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

## M5：可扩展性与工程基线

来源：2026-07-29 架构评审 A2 / A4 / A5 / B1 / B2 / C1–C5。

### `[ ]` T-017 Tool Registry 重构

对应：评审 A2

依赖：T-011b

背景：`TOOL_SCHEMAS` 是模块级常量、`run_tool` 是 if/elif 链、`ToolRegistry` port 只是静态集合的外壳；只读性在 `tools/registry.py` 与 `policy/engine.py` 维护两份，审批语义硬编码在 `agent/loop.py` 的 `if call.name == "edit_file"`。该问题阻塞 MCP、subagent、项目自定义工具三个方向。

范围：

- 定义 `ToolSpec`（`name` / `description` / `input_schema` / `read_only` / `approval: none|gate|diff` / `hidden_in_modes`）与 `Tool` Protocol。
- `ToolRegistry` 改为真实 dict 注册表，`run_tool` 变查表；现有 7 个工具逐个迁移。
- **删除 `policy/engine.py` 的 `READ_ONLY_TOOLS_V2` 常量**，`PolicyEngine.gate` 从 registry 读取只读性——消灭两份真相是本任务关键收益。
- `agent/loop.py` 的 edit 分流改为 `tool.spec.approval == "diff"`。

验收：

- 现有工具/策略测试不改断言即通过（纯重构，行为不变）。
- 新增可扩展性证明：注册一个自定义只读工具，验证其自动出现在 review 模式 schema 且被 policy 判为 `allow`。
- 按执行规则 6 重新生成 baseline（`policy_sha256` 与 `tool_schema_sha256` 变化）。

### `[ ]` T-018 MCP client 接入

对应：评审 D5

依赖：T-017

范围：

- `runtime/app/tools/mcp/`：stdio + HTTP transport client 与 server 生命周期管理。
- `.aicode/config.json` 增加 `mcp.servers[]`（命令、参数、env allowlist、超时）。
- 外部工具经 `ToolSpec` 注册，**强制 `read_only=False` 且 `approval="gate"`**；工具名加 `mcp__<server>__` 前缀防冲突。
- 新增 `mcp.server.started` / `mcp.tool.called` event（按执行规则 5 登记）。
- 单个 MCP server 崩溃不得影响主 loop。

验收：外部工具全链路经过 policy gate 与审批；server 崩溃隔离有测试覆盖。

### `[ ]` T-019 只读工具并行执行

对应：评审 B2

依赖：T-017

范围：

- 按 `tool.spec.read_only` 将单轮 tool calls 切分为并行组与串行组；并行组 `asyncio.gather`，写入类与执行类保持串行。
- **写回 history 的 tool message 必须严格按模型返回的原始顺序**，不得按完成顺序，否则跨 provider 的消息配对会错乱。
- 修复共享可变状态：`context.tool_call_id = call.id` 改为每次调用传独立浅拷贝 context。
- 设并发上限（建议 8）防止文件描述符耗尽。

验收：并行组耗时接近单个最慢工具而非总和；tool message 顺序与 `tool_calls` 顺序一致，两条断言均有测试。

### `[ ]` T-020 SQLite WAL 与消息写入不阻塞事件循环

对应：评审 A5

依赖：无

背景：`_connect` 每次新建连接且无 WAL / `busy_timeout`；`append_message` 在 Agent Loop 每条消息上同步落盘，与 SSE `assistant.delta` 推送争用同一 event loop。事件写入已有 write-behind 队列，消息路径没有。

范围：

- `PRAGMA journal_mode=WAL`、`busy_timeout=5000`、`synchronous=NORMAL`；复用连接。
- `append_message` 改为不阻塞 event loop。**`message_id` 必须同步返回**（compaction 的区间标记依赖它），因此优先选用 `asyncio.to_thread` 保持同步语义，而非异步队列 + 内存自增 id。
- `flush()` / `prepare_stop` / `aclose` 语义覆盖消息路径。

验收：并发写入与并发 `list()` 压力测试不出现 `database is locked`；有消息写入时 `assistant.delta` 推送延迟不劣化。

### `[ ]` T-021 Composition root 移入 lifespan

对应：评审 A4

依赖：T-011b

背景：`server/main.py` 模块级 `application_runtime = build_application_runtime(settings)` 在 import 时即构造 provider client、打开 SQLite，与 T-011a/T-011b 建立的 ports 分层自相矛盾，也使 ROADMAP 标注为已完成的"可嵌入 Runtime"在 HTTP 层被打破。

范围：

- 删除模块级全局，改在 `lifespan` 构造并挂到 `app.state`；service 工厂改为 `Depends`。
- 测试从 monkeypatch 全局迁移到 `dependency_overrides`。

验收：新增测试在同一进程内起两个配置不同的 `ApplicationRuntime` 并各自完成一轮 turn——这是"可嵌入"的实证。

### `[ ]` T-022 Python 静态检查进 CI 与核心路径类型收敛

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

## M6：评测触发项

以下任务**在 T-016 产出真实评测数字之前不启动**，届时按数据决定取舍。此纪律沿用 [LOCAL_AGENT_ROADMAP.md](LOCAL_AGENT_ROADMAP.md) 第 536 行：索引、subagent、自动规划框架只由评测结果触发。

### `[ ]` T-023 检索式项目记忆

对应：评审 B4 | 触发条件：T-016 显示跨会话重复解释同一项目约定造成可观测的 token 浪费

范围：`.aicode/memory/*.md` 按主题拆分的带 frontmatter 小文件取代当前全量注入的单一 `memory.md`；新增 `memory` 工具（`approval="gate"`）；按当前任务关键词/路径检索注入而非全量；保留旧 `memory.md` 读取兼容。

### `[ ]` T-024 plan / todo 工具

对应：评审 B5 | 触发条件：T-016 的失败归因集中在"跑偏 / 漏做子任务"

范围：轻量 todo 工具，主要价值是用户可见性而非模型记忆；Go renderer 渲染为进度清单。

### `[ ]` T-025 OpenTelemetry 导出

对应：评审 D4 | 依赖：T-021

范围：`TraceSink` 增加 OTLP 实现并与现有 JSONL 并存；span 层级 `run → model.call / tool.call → execution`；沿用 `audit/redaction.py` 脱敏；文档给出接 Jaeger 或 Langfuse 的本地验证步骤。

### `[ ]` T-026 模型横向对比与上下文消融

对应：评审 D2 / D3 | 依赖：T-016

范围：同一 live 任务集跑 3 个模型产出成本–成功率曲线；消融 `compact_threshold` 0.8 vs 0.6、有无 `related_files`。指标 `with_compaction_success_rate` / `without_compaction_success_rate` 已实现，只需喂真实数据。结论写入评审文档与 README。

### `[ ]` T-027 SSE 健壮性与项目包装

对应：评审 C6 / C7 / D6

范围：

- SSE 按 `run_id` 过滤后若目标 run 已结束需立即返回而非挂起至超时。
- provider 重试增加抖动并读 `Retry-After`（两个 provider 的固定 `0.5 * 2**attempt`）。
- 英文 README + 30 秒 asciinema/GIF；README 顶部重排为「是什么 → 架构图 → 三个数字 → 60 秒跑起来」。

## 后续候选

以下任务不进入当前关键路径，需由 eval 或用户需求触发：

- 受控扩展系统。
- Session branch/fork/export/import。
- symbol/import/test mapping。
- subagent（前置条件见 T-023 / T-024 的触发纪律）。
- 全屏 TUI、Web UI、云端执行。
