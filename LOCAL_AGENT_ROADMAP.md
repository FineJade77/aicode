# aicode 本地 Coding Agent 优化执行路线

状态：Accepted

更新日期：2026-07-26

执行周期：建议按 4 个里程碑推进，不绑定固定自然周

关联文档：[ARCHITECTURE.md](ARCHITECTURE.md) · [ROADMAP.md](ROADMAP.md) · [TASKS.md](TASKS.md)

## 1. 目标

把 aicode 从“已经能运行的 Agent Loop MVP”推进为“可安装、可信任、可长时间使用、可持续评估的本地 Coding Agent”。

本路线聚焦四个结果：

1. 用户在任意 Git 仓库安装后即可运行，不依赖 aicode 源码目录。
2. 模型发起的所有命令和文件访问统一经过 Runtime 的执行、策略、隔离和审计边界。
3. 长会话不会因上下文溢出或压缩状态丢失而失效。
4. 每次架构或模型调整都能通过 Agent 任务集量化收益，而不是只依赖单元测试和主观体验。

完成 P0 和 P1 后，aicode 应达到“本地日常可用”的定义；P2 用于把稳定内核开放给 IDE、脚本和插件生态。

## 2. 决策摘要

### 2.1 保留 Go CLI + Python Runtime

当前双进程切分职责清晰：

- Go CLI 负责命令行、daemon 生命周期、流式展示和用户输入。
- Python Runtime 负责 Agent Loop、模型、session、工具、策略、审批和审计。

近期不重写语言或合并进程。优先修复分发、执行边界和 Runtime 内部分层；只有在性能数据或维护成本证明双进程成为主要瓶颈时，才重新评估。

### 2.2 参考 PI，但不复制 PI

参考 [earendil-works/pi coding-agent](https://github.com/earendil-works/pi/tree/main/packages/coding-agent) 的以下做法：

- core、session、interactive modes 分层；
- append-only session 与可派生视图；
- 模型感知、可持久化的上下文压缩；
- SDK / RPC 入口；
- 小型核心工具集和显式扩展点。

不直接照搬以下选择：

- 默认完全信任宿主机用户权限；
- 一开始支持大量 provider、TUI 能力和扩展类型；
- 在没有评测证据前增加复杂索引、subagent 或自动规划框架。

### 2.3 安全边界先于功能数量

任何能读取敏感文件、启动进程、访问网络或改变 workspace 的能力，都必须服从同一套：

```text
Tool Call
  -> Policy
  -> Project Trust
  -> Execution Backend
  -> Approval
  -> Audit
  -> Result
```

不能继续让 CLI sandbox、Runtime bash、验证命令形成彼此独立的执行路径。

## 3. 当前基线与主要缺口

当前已经具备原生 function calling、edit diff 审批、三态 Policy、SQLite session、SSE、usage、review 和 Docker sandbox MVP。以下问题阻碍其成为可靠的本地 Agent：

| 缺口 | 当前表现 | 风险 |
| --- | --- | --- |
| 安装不完整 | `make install` 只复制 Go 二进制，daemon 仍从当前目录向上寻找 `runtime/app/server/main.py` | 离开源码仓库后可能无法启动 |
| 执行边界分裂 | Docker sandbox 在 CLI 侧，Agent `bash` 和编辑后验证在 Runtime 宿主机执行 | 同一命令因入口不同得到不同安全语义 |
| shell 路径保护不足 | protected paths 主要约束文件工具；低风险白名单命令可能读取敏感路径 | 模型可通过 shell 绕过文件工具边界 |
| 压缩不持久 | 历史压缩只改变当前内存列表，并在模型调用后才检查 | resume 后重复膨胀，首个请求可能先溢出 |
| 上下文预算固定 | 当前预算没有按模型能力计算 | 本地小模型和大上下文模型都无法合理利用 |
| 协议漂移 | Go、Python、JSON schema 和文档存在重复定义 | 客户端、Runtime 与文档逐步不一致 |
| 本地模型配置不顺畅 | OpenAI-compatible 路径假定存在 API key，缺少能力描述与健康检查 | Ollama、llama.cpp、LM Studio 等 no-auth endpoint 接入困难 |
| 交互模型偏单次命令 | 普通运行创建新 session，缺少常驻 REPL、steer/follow-up | 日常多轮修改成本高 |
| Agent 缺少任务级评测 | 测试主要证明代码正确，没有证明 Agent 能完成代码任务 | 无法判断模型、prompt、工具或压缩改动是否真实提升 |

这些缺口按风险和依赖顺序拆为 P0、P1、P2。

## 4. 目标架构

```text
Go CLI / Interactive REPL / future IDE / stdio JSON-RPC
                         │
                 versioned API contract
                         │
┌──────────────── Application Runtime ────────────────┐
│ SessionService · RunCoordinator · ApprovalService   │
│ TraceService · ProjectTrustService                  │
└────────────────────────┬────────────────────────────┘
                         │
┌──────────────────── Agent Core ─────────────────────┐
│ AgentLoop · ContextManager · ModelRuntime           │
│ ToolRegistry · Policy                               │
└────────────────────────┬────────────────────────────┘
                         │
┌────────────────────── Adapters ─────────────────────┐
│ SQLite/JSONL · Model Providers · Host/Sandbox Exec  │
│ Workspace · Clock/IDs                               │
└─────────────────────────────────────────────────────┘
```

约束：

- Agent Core 不直接依赖 FastAPI、全局 session store 或具体模型 SDK。
- Application Runtime 负责并发、恢复、审批、事件和 trace。
- Host 与 sandbox 是同一 `ExecutionBackend` 接口的不同实现。
- API、事件和工具 schema 有版本号与单一事实来源。
- 完整 session log 是事实；上下文窗口只是从 log 构建的可丢弃投影。

## 5. 里程碑

| 里程碑 | 结果 | 包含工作包 |
| --- | --- | --- |
| M0：可交付基线 | 安装后能在干净环境启动，协议不再静默漂移 | WP0.1、WP0.4 |
| M1：可信执行 | 所有 Agent 命令统一执行边界，长会话可靠 | WP0.2、WP0.3 |
| M2：本地日用 | 常驻交互、本地模型 profile、任务级评测 | WP1.1、WP1.2、WP1.3 |
| M3：可嵌入平台 | Core 可测试/嵌入，开放 RPC、扩展和 session 分支 | WP1.4、P2 |

M0 和 M1 是发布阻塞项。M2 完成后再把项目定位为“日常可用”；M3 不阻塞本地 CLI 使用。

## 6. P0：可靠性与安全基础

### WP0.1 可安装、可诊断的分发包

目标：用户无需 clone aicode 源码，即可从任意仓库启动 CLI 和 Runtime。

主要改动：

- 定义安装布局，例如：

  ```text
  ~/.local/bin/aicode
  ~/.local/lib/aicode/<version>/runtime/
  ~/.local/lib/aicode/<version>/venv/
  ~/.local/share/aicode/
  ```

- 构建产物携带版本化 Runtime、Python lock file 和安装 manifest。
- daemon 按明确优先级解析 Runtime：
  1. `AICODE_RUNTIME_DIR`，仅用于开发和诊断；
  2. 当前二进制关联的版本化安装目录；
  3. 源码 checkout fallback，仅用于开发。
- 提供 `aicode doctor`，至少检查：
  - CLI 与 Runtime 版本是否兼容；
  - Python 版本、虚拟环境和依赖；
  - Runtime 路径、状态目录和端口；
  - provider endpoint、认证变量和模型路由；
  - Docker 可用性（如果启用 sandbox）。
- 安装、升级失败时保留上一版本，版本切换使用原子更新的 manifest 或 symlink。

涉及代码：

- `Makefile`
- `cli/internal/daemon/daemon.go`
- `cli/internal/cmd/daemoncmd/`
- 新增 `cli/internal/cmd/doctorcmd/`
- 新增分发脚本或 release workflow

验收标准：

- 在不包含 aicode 源码的临时目录执行安装后的 `aicode daemon start` 成功。
- 修改当前工作目录不会改变 Runtime 解析结果。
- CLI/Runtime 版本不匹配时快速失败，并给出可执行修复提示。
- 重复安装同一版本是幂等的；升级失败不会破坏已安装版本。
- CI 在隔离的临时 home 中跑通 install → doctor → daemon start → smoke request → stop。

### WP0.2 统一执行、信任与审计边界

目标：Agent 发起的所有命令通过统一接口执行，且不能通过 bash 绕过 workspace、protected paths 或 secret 约束。

设计：

```python
class ExecutionBackend(Protocol):
    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...
    async def cancel(self, execution_id: str) -> None: ...
```

`ExecutionRequest` 至少包含：

- argv 或明确标记的 shell command；
- workspace 与允许访问的 roots；
- mode、run/session/tool-call id；
- timeout、网络策略、资源限制；
- writable paths、masked paths；
- 环境变量 allowlist；
- project trust level。

主要改动：

- 将 CLI Docker sandbox 下沉为 Runtime 的 `SandboxExecutionBackend`。
- `bash`、测试、构建、lint 和编辑后验证都通过 `ExecutionBackend`。
- 增加项目 trust：
  - `untrusted`：默认不执行项目命令；需要审批或 sandbox；
  - `trusted`：可按 policy 自动执行低风险命令；
  - trust 记录绑定规范化 workspace 路径和可选 Git remote，不由仓库内配置自行提升。
- 对 shell 命令同时执行“命令风险”和“路径风险”分析。
- `.env*`、凭证目录、SSH/GPG/cloud config 等敏感路径默认 mask/deny；项目 `protectedPaths` 同时作用于 file tools 和 shell。
- 宿主机执行只继承最小环境变量 allowlist；provider key 不传给子进程。
- audit 默认记录命令类别、可执行文件、cwd、exit code、duration 和参数 hash；原始命令仅在用户显式开启 debug trace 时写入受保护本地文件。
- 保持“deny 不可由 approval 覆盖；ask 必须由用户确认；allow 才可自动执行”。

涉及代码：

- `runtime/app/tools/command.py`
- `runtime/app/tools/registry.py`
- `runtime/app/policy/engine.py`
- `runtime/app/project/`
- `runtime/app/audit/`
- `cli/internal/cmd/sandboxcmd/`（迁移后只保留客户端入口）
- `schemas/config.schema.json`
- `schemas/events.schema.json`

验收标准：

- `cat .env`、`sed -n ... ~/.ssh/config`、符号链接逃逸和 `../` 逃逸被拒绝或遮蔽。
- `pytest`、`npm test` 等项目命令在 untrusted workspace 不会直接落到宿主机。
- CLI sandbox 命令与 Agent `bash` 使用相同 policy、事件和 audit 格式。
- cancel 会终止完整进程组，daemon 重启后无孤儿执行任务。
- secret 不出现在 tool output、SSE、Runtime log 或 audit 中。
- Linux 与 macOS 都有 Host backend 测试；Docker backend 在 CI 可用时运行集成测试。

### WP0.3 模型感知、可持久化的上下文管理

目标：长会话可恢复、可预测，不因单次请求超出 context window 而直接失败。

数据模型：

```text
SessionEntry (append-only source of truth)
  user | assistant | tool_call | tool_result | approval
  compaction | branch | metadata

ContextProjection (ephemeral)
  system prompt + selected entries + active summary + current turn
```

主要改动：

- 用具名类型替换 Agent history 中的宽泛 `dict[str, Any]`。
- 每次模型调用前执行 preflight：
  1. 根据 provider/model capabilities 取得 context window；
  2. 估算 system、tools、history、当前 turn 和保留输出的 token；
  3. 必要时先压缩，再调用模型。
- compaction 作为 session entry 持久化，记录：
  - 被覆盖的 entry range；
  - summary、模型、prompt/version；
  - token 估算与创建时间。
- resume 时从最近有效 compaction 重建 projection，而不是重新加载全部历史。
- 工具调用与工具结果必须成对保留；未完成调用不能被压缩掉。
- 保留最近用户目标、未完成任务、最近 edits、验证结果和审批状态。
- provider 返回 context overflow 时允许一次强制压缩重试；相同请求不无限重试。
- 为 compaction 定义版本与迁移策略，旧 session 仍可读取。

涉及代码：

- `runtime/app/agent/history.py`
- `runtime/app/agent/types.py`
- `runtime/app/agent/loop.py`
- `runtime/app/sessions/store.py`
- `runtime/app/models/provider.py`
- `runtime/app/models/router.py`

验收标准：

- 人工构造超过模型窗口的 session，首次模型请求前即完成压缩。
- daemon 重启和 `resume` 后复用已持久化的 compaction。
- 本地 8K 模型与远端大窗口模型使用不同预算。
- summary 失败时保留原始 log，并给出可恢复错误。
- 在压缩前后，Agent 能继续回答当前目标、已改文件、失败验证和下一步。
- 增加 golden tests，覆盖 tool-call 配对、连续压缩、旧 session 迁移和 overflow 单次重试。

### WP0.4 协议与文档单一事实来源

目标：Go CLI、Python Runtime、schema 和文档在 CI 中自动保持一致。

主要改动：

- 为 HTTP、SSE event、tool 和 config contract 增加显式版本。
- 选择一个 canonical source：
  - HTTP API 推荐 OpenAPI；
  - event/tool/config 推荐 JSON Schema。
- 短期先增加双向 contract tests；稳定后再评估生成 Go/Python 类型。
- 修正 `schemas/tools.schema.json` 缺少已实现工具的问题。
- 对未知 event/tool 采用明确兼容策略：可忽略的扩展字段与必须报错的未知类型分开。
- 增加文档链接检查和配置示例解析测试。
- 发布时校验 CLI、Runtime 和 schema version compatibility。

涉及代码：

- `schemas/`
- `runtime/app/server/main.py`
- `runtime/app/events/`
- `runtime/app/tools/registry.py`
- `cli/internal/client/`
- `cli/internal/renderer/`
- `.github/workflows/ci.yml`

验收标准：

- Runtime 暴露的每个 tool 都存在于 schema，schema 中的每个 tool 都有实现。
- 所有 SSE event fixture 可被 Go client 解码，Go fixture 也可通过 schema 校验。
- 文档中的 `.aicode/config.json` 示例能通过 schema。
- contract 漂移会让 CI 失败，而不是在发布后才被发现。

## 7. P1：本地日常使用体验

### WP1.1 常驻交互模式

目标：把“每条命令一次新 session”升级为可持续工作的本地对话。

前置条件：先完成 WP1.4a 的 Application Runtime 与 Session/Turn/Run contract；REPL 只消费该稳定边界，不直接依赖 Agent Core 内部对象。

第一版命令：

```text
aicode chat
  /status
  /model [name]
  /compact
  /steer <guidance>
  /follow-up <message>
  /cancel
  /new
  /resume [--last|session]
  /exit
```

实现状态（2026-07-27）：第一版已完成。Go REPL 与单次命令复用 versioned Go client 和 renderer；Runtime 提供 steer/manual compact contract，AgentLoop 在安全边界应用 steer。TTY、Ctrl-C 和非 TTY 脚本路径均有测试。

行为要求：

- REPL 默认绑定一个 session，多条输入按 turn 追加。
- 模型输出期间的新输入可选择：
  - `steer`：注入当前 run 的下一个安全边界；
  - `follow-up`：排到当前 run 之后；
  - `cancel`：取消当前 run 后作为新 turn。
- Ctrl-C 第一次取消当前 run，第二次退出；任何取消都必须有终态事件。
- 非交互命令和 REPL 复用同一客户端与事件 renderer。
- terminal 能显示当前阶段、tool、approval、token、elapsed 和 queued 状态，但不在 P1 重写全屏 TUI。

涉及代码：

- `cli/internal/cmd/agentrun/`
- `cli/internal/cmd/runtimeio/`
- `cli/internal/renderer/`
- Runtime run queue 与 event API

验收标准：

- 单个 REPL 中连续完成“解释 → 修改 → 测试 → 追问”，session id 不变。
- 流式输出期间可 cancel、follow-up；无重复消息或丢失终态。
- 非 TTY 输入仍支持脚本化，并有稳定 exit code。

### WP1.2 本地模型 Provider Profile

目标：让 Ollama、llama.cpp server、LM Studio 等 OpenAI-compatible endpoint 成为一等本地路径，而不是 API provider 的特殊配置。

主要改动：

- provider auth mode 支持 `required`、`optional`、`none`。
- profile 包含 base URL、模型、context window、max output、tool calling、streaming 和 tokenizer/估算策略。
- `aicode models probe` 执行健康检查、模型发现和最小 tools smoke test。
- 允许配置一个主 profile 和显式 fallback，但不在 P1 构建复杂多 provider 自动路由。
- tool calling 不可用时快速失败；不要静默退化为从文本猜 JSON。
- 文档提供 Ollama、llama.cpp、LM Studio 各一个最小示例，并注明经 CI/手工验证的版本。

涉及代码：

- `runtime/app/config/settings.py`
- `runtime/app/models/openai_compatible.py`
- `runtime/app/models/provider.py`
- `runtime/app/models/router.py`
- `cli/internal/config/`
- `cli/internal/cmd/modelscmd/`

验收标准：

- no-auth localhost endpoint 不要求伪 API key。
- endpoint 不可达、模型不存在、无 tools 能力时给出针对性错误。
- 至少一个真实本地 provider 跑通 read → edit proposal → approval → verify 的 smoke task。

当前落地（2026-07-26）：

- Provider Profile v1 已由 JSON Schema 固化，并贯通 Go config、Runtime env、doctor、router、`models` renderer。
- no-auth 模式永不发送 Authorization；optional/required 模式分别实现按需认证与缺 key 快速失败。
- `aicode models probe` 已覆盖配置、模型发现、SSE 和原生 tools，失败返回稳定分类；Agent 不支持文本 JSON tools fallback。
- 真实 localhost TCP fixture 已跑通 probe → read → edit proposal → approval → verify，独立 smoke target 接入 CI。
- Ollama、llama.cpp server、LM Studio 提供最小配置模板与官方文档链接；当前自动化验证的是 OpenAI-compatible 协议与 no-auth 全链路，不宣称未安装产品的具体版本已验证。
- 验证：Python 283 passed / 2 skipped（受限沙箱 localhost bind、Docker），沙箱外 localhost smoke 1 passed；Go test/vet、compileall、deterministic eval baseline 与 diff check 通过。

### WP1.3 Agent 任务级评测与 Trace

目标：用稳定指标回答“这次改动是否让 Agent 更可靠”。

第一版任务集保持 20–50 个小任务，覆盖：

- 定位并修复单文件 bug；
- 跨文件 API 修改；
- 补边界测试；
- 只读解释与 review；
- protected path、prompt injection 和危险命令拒绝；
- 长上下文恢复；
- 失败测试后的二次修复。

每个 task 固定：

- 输入仓库 fixture 和初始 commit；
- 用户请求、mode、模型 profile 和预算；
- 必须通过的测试与静态断言；
- 禁止修改的路径；
- 最大 turns、tokens、cost 和 wall time。

记录指标：

- task success / pass@1 / pass@k；
- 越权修改率、危险命令执行率、approval 正确率；
- token、cost、latency、model/tool turns；
- 无效工具调用、重复编辑、验证后回归；
- compaction 前后成功率。

实现原则：

- 每个 run 输出可重放的 trace manifest，但不保存 secret 或完整敏感命令。
- grader 以测试、diff 和确定性规则为主；LLM-as-judge 只做补充。
- 基线结果与模型、prompt、tool schema、policy 版本一起保存。
- CI 跑小型 deterministic smoke suite；完整真实模型评测手动或定时运行。

建议位置：

```text
evals/
  tasks/
  fixtures/
  graders/
  baselines/
  runner/
```

验收标准：

- 一条命令可在隔离 worktree 中运行任务并生成 JSON + Markdown 报告。
- 失败报告能定位到 model call、tool call、policy decision、edit 和验证结果。
- 合并 Agent 行为改动时至少提供 smoke suite 结果；发布前保存完整基线对比。

首批落地（2026-07-26）：

- `evals/` 已提供 contract v1、隔离临时 Git workspace runner、scripted CI provider、确定性 grader、trace/report 与 baseline 比较。
- smoke baseline 首批 4 个任务覆盖 edit+verification、危险命令拒绝、protected-path prompt injection 和长上下文 compaction/resume；后续真实模型完整集继续扩展到 20–50 个任务。
- CI 执行 `make eval-smoke` 并上传 JSON、Markdown 和 redacted trace artifacts；baseline 同时锁定行为阈值与 task/fixture/prompt/tool/policy/compaction fingerprint。

### WP1.4 提取可嵌入 Agent Core

目标：Agent Loop 可在 HTTP server 之外独立测试和嵌入，为 P2 SDK/RPC 做准备。

实施拆分：

1. **WP1.4a / T-011a**：先提取 Application Runtime 及版本化 Session/Turn/Run contract。
2. **WP1.1 / T-010**：以常驻 REPL 作为第一个长期消费者，验证 follow-up、steer、cancel、approval 和 compaction 边界。
3. **WP1.4b / T-011b**：在 contract 后方完成 Agent Core DI 与内部解耦，禁止内部重构反向改变 REPL/transport。

建议接口：

```python
class ModelRuntime(Protocol): ...
class ToolRegistry(Protocol): ...
class SessionRepository(Protocol): ...
class EventSink(Protocol): ...
class ApprovalBroker(Protocol): ...
class ExecutionBackend(Protocol): ...
```

主要改动：

- 建立 application factory，显式注入依赖，减少 server module globals。
- 将 `loop.py` 拆为 loop orchestration、turn execution、context、tool dispatch。
- 用稳定 dataclass/Pydantic 类型替换关键路径中的 `Any`。
- Agent Core 只发出 domain events，不知道 SSE、HTTP 或 terminal renderer。
- 保持现有 API 行为，通过 characterization tests 渐进迁移，不做大爆炸重写。

验收标准：

- 测试可用 fake model + in-memory session + fake execution backend 直接运行 Agent Core。
- 导入 Agent Core 不会启动 FastAPI、读用户配置或创建全局数据库连接。
- HTTP API、CLI 事件和现有 session 可兼容迁移。

首批落地（2026-07-27）：

- 新增 `core/` ports/domain、`application/` services/runtime 和 `adapters/` composition root；FastAPI 只持有单一 `ApplicationRuntime`。
- Application contract v1 使用具名 `SessionSnapshot`、`TurnRequest`、`RunReceipt/RunControl` 与 control receipts，配套 JSON Schema、fixture、Runtime descriptor 和 Go reader；FastAPI/Pydantic 只负责 transport 映射。
- AgentLoop 通过 ModelRuntime、ToolRegistry、WorkspaceRuntime、ApprovalBroker、TraceSink、Clock 和 ProjectTrust ports 工作；ContextManager 负责模型感知、持久化 compaction。
- SessionStore 明确为 SQLite adapter，并支持 Clock/ID 注入；新增 InMemorySessionRepository、JSONL usage、workspace/tool/approval/system adapters。
- 新增 `/v1/meta/contract` 与 Go client contract reader，现有 HTTP/SSE contract 保持 v2，stdio JSON-RPC 标记为 planned。
- 架构守卫验证 Agent Core 无 FastAPI/server/adapter/session/tool/project import；fake model + in-memory session 可直接运行 AgentLoop。

## 8. P2：可嵌入与扩展

P2 只在 M2 指标稳定后启动。

### WP2.1 SDK 与 stdio JSONL RPC

- Python SDK 直接调用 Agent Core/Application Runtime。
- stdio JSONL RPC 支持 initialize、session、prompt、cancel、approval 和 event subscription。
- transport 不复制业务逻辑，只做序列化、认证与背压。
- 协议进行版本协商，并提供最小 IDE/脚本示例。

### WP2.2 受控扩展

- 第一版只提供 tool、prompt/context hook、event subscriber。
- 扩展 manifest 声明所需权限、命令、网络和 workspace 范围。
- 默认禁用第三方扩展，启用时记录用户信任决策。
- 扩展与核心 tool 使用相同 Policy、ExecutionBackend 和 audit。
- 不允许扩展绕过 approval 或直接获得 provider secret。

### WP2.3 Session 分支与可移植性

- append-only entry 支持 parent/branch 指针。
- 提供 fork、list branches、export、import。
- compaction 是分支内投影，不改写共享历史。
- 导出时默认脱敏，并包含 schema/version metadata。

### WP2.4 按评测证据增加上下文能力

只有 eval 明确显示文本搜索成为主要失败原因时，再引入：

- symbol/import/test mapping；
- tree-sitter 或 language-server adapter；
- 按路径和任务检索项目记忆。

只有 eval 显示主上下文被探索过程显著污染时，再评估 subagent。任何 subagent 都必须复用预算、Policy、ExecutionBackend、approval 和 trace。

## 9. 依赖关系与推荐顺序

```text
WP0.4 contract baseline ─> WP1.4a Application contracts ─> WP1.1 REPL ─> WP1.4b Core DI ─> P2
WP0.2 execution boundary ─> security eval ─> WP1.3 Eval ────────────────────┘
WP0.3 context persistence ─> long-run eval ─> WP1.3 Eval
WP0.1 distribution ─> install E2E
WP0.3 model capabilities ─> WP1.2 local provider profiles
```

实际执行建议：

1. 先做 WP0.4 的 contract baseline，冻结可观察行为。
2. 并行推进 WP0.1 和 WP0.2；二者完成后才能宣称“可安全安装”。
3. 完成 WP0.3，并用长 session fixture 验证。
4. 建立 WP1.3 最小评测骨架，并先完成 WP1.4a Application contract。
5. 基于该 contract 完成 WP1.1，再用 characterization tests 渐进完成 WP1.4b；WP1.2 可在此期间独立推进。
6. 达到质量门槛后再进入 P2。

## 10. 质量门槛

### M0 发布门槛

- 干净 home、任意 workspace 安装启动通过。
- contract test、Go test、Python test、vet、format、compile 全部通过。
- 安装产物可回滚，CLI/Runtime 版本错误可诊断。

### M1 安全与可靠性门槛

- 安全回归集中的敏感路径读取和 workspace 逃逸为 0。
- untrusted 项目命令不会未经确认在宿主机运行。
- 所有进程执行都有 run/tool-call id、终态、可取消。
- 超窗口 session 可压缩、重启、resume，并完成后续任务。

### M2 日用门槛

- 本地 profile smoke task 稳定通过。
- REPL 多轮、cancel、follow-up 无消息丢失或 run 卡死。
- Agent eval 相比冻结基线不退化；安全指标不得用成功率换取。
- 典型小任务的失败能通过 trace 定位到具体模型、工具、policy 或验证步骤。

## 11. 兼容、迁移与回滚

- CLI 与 Runtime 在一个发布周期内至少兼容当前和前一 contract minor version。
- session schema 只做向前迁移；升级前备份 SQLite，迁移失败继续使用旧版本。
- 新 ExecutionBackend 先以 feature flag 对照运行，再切为默认；Host fallback 必须显式可见。
- compaction 新格式上线前保留旧历史读取路径；任何摘要失败不得删除原始 entry。
- project trust 默认 `untrusted`，不能因升级自动扩大权限。
- provider profile 从现有配置迁移时保留原键读取，并输出一次性迁移提示。

## 12. 暂不实施

以下项目在 P0/P1 中明确不做：

- 全屏 TUI 重写；
- 同时支持大量专有模型 provider；
- 自动多 Agent 编排或通用 planner；
- 默认联网搜索和浏览器工具；
- 向量数据库、全量代码 embedding 或复杂 symbol index；
- 云端账号、团队同步、远程执行和自动部署；
- 可绕过核心 Policy 的任意插件 API。

这些功能不是永久拒绝，而是必须由 eval、用户需求和清晰安全模型触发。

## 13. Issue 拆分建议

每个工作包拆成“设计/contract → 实现 → E2E/文档”三个 Issue，避免单个长期分支同时改动所有层。

推荐首批 Issue：

1. `contract: inventory runtime tools/events and add drift tests`
2. `distribution: define versioned runtime layout and manifest`
3. `doctor: add install/runtime/provider diagnostics`
4. `execution: introduce ExecutionBackend and migrate bash`
5. `security: apply protected/masked paths to shell execution`
6. `trust: persist per-workspace trust outside repository config`
7. `context: add model capability registry and preflight budgeting`
8. `session: persist compaction entries and resume projection`
9. `eval: add isolated runner and first deterministic tasks`
10. `provider: support no-auth local OpenAI-compatible profiles`
11. `repl: persistent chat with cancel and follow-up`
12. `core: inject model/session/tool/event dependencies`

每个 Issue 必须包含：

- 用户可观察结果；
- 不变量与安全边界；
- 受影响 contract；
- 单元/集成/E2E 测试；
- trace 或指标变化；
- 迁移和回滚说明。

## 14. 完成定义

本路线的“完成”不是功能清单全部打勾，而是满足以下结果：

- 从 release artifact 安装后，在任意本地仓库可运行；
- 不可信仓库无法借 Agent 获取超出声明范围的主机权限或 secret；
- session 能跨 daemon 重启继续，长上下文会在请求前安全压缩；
- 本地 OpenAI-compatible 模型不需要伪造凭证即可使用；
- 用户可在一个交互 session 中 steer、follow-up、cancel 和恢复；
- 核心行为有版本化 contract、任务级 eval 和可诊断 trace；
- IDE、SDK、插件等未来入口复用同一 Core、Policy 和执行边界。

达到这些条件后，再根据真实使用数据决定是否投入复杂索引、subagent 和更广的 provider/客户端生态。

## 15. 参考依据

PI：

- [Coding Agent README](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)：最小核心工具、interactive/print/RPC/SDK 多入口、message queue 和 project trust。
- [Session format](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)：append-only JSONL、parent/branch 关系和可恢复 session。
- [Compaction](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)：主动压缩、overflow recovery，以及保留完整历史与使用摘要投影的分离。
- [RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md) 与 [SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)：同一 Agent 能力的进程集成和嵌入入口。
- [Extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md) 与 [Containerization](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/containerization.md)：扩展面和隔离部署的取舍。

业界实践：

- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)：优先简单、可组合的 Agent 模式，并使用环境反馈和停止条件保持可控。
- [Anthropic: Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)：把 context 视为有限资源，持续选择高信号信息。
- [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)：组合 outcome、transcript 和行为 grader，持续维护任务级评测。
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)：使用可验证增量、结构化交接和干净工程状态支撑长任务。

这些资料用于提炼边界和验证方向，不构成逐项复制清单；aicode 的实现顺序仍由本地优先、安全审批和当前代码基线决定。
