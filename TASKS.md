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

### `[ ]` T-005 ExecutionBackend

对应：WP0.2

依赖：T-001、T-002

范围：

- 定义 Host/Sandbox 共用执行接口。
- 迁移 Agent bash、test/build/lint 和编辑后验证。
- 统一取消、终态、资源限制和 audit。

### `[ ]` T-006 Project Trust 与 shell 路径安全

对应：WP0.2

依赖：T-005

范围：

- workspace trust 存储在仓库外。
- protected/masked paths 同时约束 file tool 和 shell。
- 子进程环境变量改为 allowlist。
- 增加 workspace/symlink/secret 逃逸测试。

### `[ ]` T-007 持久化、模型感知的 compaction

对应：WP0.3

依赖：T-001

范围：

- model capability 与调用前 context preflight。
- compaction entry 持久化和 resume projection。
- context overflow 单次恢复重试。
- 增加旧 session、工具配对和连续压缩测试。

## M2：本地日用

### `[ ]` T-008 Agent eval 与 trace 基线

对应：WP1.3

依赖：T-005、T-007

范围：

- 建立隔离 fixture、runner、grader 和报告。
- 首批覆盖修改、验证、安全拒绝和长上下文任务。
- 保存成功率、安全率、token、cost、latency 和工具轮次。

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
