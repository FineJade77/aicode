# aicode 执行任务台账

更新日期：2026-07-26

来源：[LOCAL_AGENT_ROADMAP.md](LOCAL_AGENT_ROADMAP.md)

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

### `[ ]` T-009 本地模型 Provider Profile

对应：WP1.2

依赖：T-007、T-008

范围：

- auth mode、context window、max output 和 tool capability。
- endpoint/model probe。
- 至少一个 no-auth 本地 provider smoke task。

### `[ ]` T-010 常驻 REPL

对应：WP1.1

依赖：T-002、T-008

范围：

- persistent session、status/model/compact/new/resume 命令。
- steer、follow-up 和 cancel。
- TTY 与非 TTY 行为测试。

## M3：可嵌入平台

### `[ ]` T-011 Agent Core 依赖注入

对应：WP1.4

依赖：T-008

范围：

- 提取 ModelRuntime、SessionRepository、EventSink、ApprovalBroker 等接口。
- 移除核心路径对 FastAPI 和 module globals 的依赖。
- 用 characterization tests 渐进迁移。

### `[ ]` T-012 SDK 与 stdio JSONL RPC

对应：WP2.1

依赖：T-011

范围：

- Python SDK。
- initialize/session/prompt/cancel/approval/event RPC。
- 协议版本协商、背压和最小集成示例。

## 后续候选

以下任务不进入当前关键路径，需由 eval 或用户需求触发：

- 受控扩展系统。
- Session branch/fork/export/import。
- symbol/import/test mapping。
- subagent。
- 全屏 TUI、Web UI、云端执行。
