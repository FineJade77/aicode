# aicode Roadmap

更新日期：2026-07-21

本文是当前项目的状态路线图，用来回答三个问题：

1. 哪些核心能力已经完成。
2. 哪些能力已经有 MVP，但还值得升级。
3. 后续继续做时，哪些任务优先级最高。

状态标记：

- `[x]` 已完成：当前代码已经落地，可运行或已有测试覆盖。
- `[~]` 部分完成：已有可用 MVP，但还需要增强体验、边界或测试。
- `[ ]` 待实现：尚未落地，或只存在设计意图。

## 1. 路线图原则

- 本地优先：默认在用户本机 workspace 内工作。
- CLI-first：Go CLI 是主要用户入口，Python Runtime 是唯一 Agent 大脑。
- 安全前置：写入、shell、项目规则和模型输出都必须经过边界约束。
- 可恢复：session、事件、approval 和 usage 尽量持久化，避免 daemon 重启后状态丢失。
- 模型驱动：Agent Loop 由模型通过工具调用探索和执行，规则层只做安全、裁剪和确定性辅助。
- 文档跟实现同步：README、ARCHITECTURE、ROADMAP 应反映当前代码，而不是旧设计愿景。

## 2. 当前总览

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| CLI + daemon | `[x]` | Go CLI 自动启动/停止/查询 Python Runtime。 |
| HTTP + SSE | `[x]` | 本机 API、SSE event stream、daemon token auth。 |
| Agent Loop v2 | `[x]` | 原生 function calling，模型自主调用工具。 |
| 双 Provider | `[x]` | OpenAI-compatible 与 Anthropic。 |
| 工具系统 | `[x]` | 读文件、搜索、列文件、相关文件、bash、edit、review_diff。 |
| Edit approval | `[x]` | inline diff、单次批准、session accept-all、stale 检测。 |
| Policy Engine | `[x]` | allow / ask / deny 三态，review/commit-message 只读防线。 |
| Session store | `[x]` | SQLite session/message/event/approval 持久化。 |
| Pending approval 恢复 | `[x]` | 重启后未决 approval 标记 expired/rejected 并补事件。 |
| Usage | `[x]` | token/cost 本地统计，按 session/day/model 查看。 |
| Review rules | `[x]` | 确定性 review finding，支持配置禁用和阈值。 |
| Docker Sandbox | `[~]` | test/build/lint MVP 已完成，仍缺写入挂载和 artifact 导出。 |
| Prompt 国际化 | `[~]` | Runtime prompt 支持中英文；CLI 固定文案仍以中文为主。 |
| 配置收敛 | `[~]` | 推荐 `main/reviewer/summarizer`，遗留键仍需迁移期兼容。 |
| 依赖管理 | `[x]` | Python runtime/dev extra 和 Go Makefile 入口已收敛，`requirements(-dev).lock.txt` 提供可复现安装。 |
| 上下文索引 | `[~]` | `related_files` 启发式已完成；符号/import/test mapping 尚未做。 |
| CLI 高级体验 | `[~]` | 基础可用，仍可做分文件审批、折叠展示、PR 描述等。 |

## 3. 已完成能力

### 3.1 基础运行链路

- [x] Go CLI 根命令和常用子命令。
- [x] Python FastAPI Runtime。
- [x] CLI 自动启动 Runtime daemon。
- [x] `daemon start/status/stop`。
- [x] Runtime 本机 token 鉴权。
- [x] `POST /v1/sessions` 创建 session。
- [x] `POST /v1/sessions/{id}/messages` 发起 run。
- [x] `GET /v1/sessions/{id}/events` SSE 事件流。
- [x] `assistant.delta` 流式输出。
- [x] `run.started`、`tool.started`、`tool.output`、`approval.*`、`edit.*`、`usage.recorded`、`final` 等事件。

### 3.2 Agent Loop v2

- [x] 使用模型原生 tools / tool_use 驱动主循环。
- [x] 历史消息作为主要状态，支持多轮继续。
- [x] 工具 schema 按 mode 裁剪。
- [x] 工具调用结果写回 history 后继续推理。
- [x] 编辑后自动提示模型验证。
- [x] 历史压缩和 summarizer 路由。
- [x] 未配置 provider 时明确报错，不回退 stub 模型。

### 3.3 模型和配置

- [x] OpenAI-compatible provider。
- [x] Anthropic provider。
- [x] `main` / `reviewer` / `summarizer` 三角色模型路由。
- [x] provider timeout / retry 配置。
- [x] 本地 pricing 配置。
- [x] `aicode models` 查看当前路由。
- [x] 遗留 `models.default/planner/coder` 从用户文档和推荐配置中移除。
- [~] 旧配置键仍保留读取兼容，后续需要正式迁移提示或清理策略。

### 3.4 工具和安全策略

- [x] `read_file`。
- [x] `search`。
- [x] `list_files`。
- [x] `related_files`。
- [x] `bash`。
- [x] `edit_file`。
- [x] `review_diff`。
- [x] Policy Engine 三态：`allow` / `ask` / `deny`。
- [x] 高风险 bash 命令识别。
- [x] 写入工具必须经过 approval。
- [x] protected paths。
- [x] 非 UTF-8 文件拒绝编辑。
- [x] stale patch 检测。
- [x] review mode 只暴露只读工具。
- [x] commit-message mode 不暴露工具。
- [x] explain mode 硬只读：不暴露 `bash` / `edit_file`，Policy 层同时拒绝对应工具调用。

### 3.5 Edit Approval

- [x] `edit_file` 生成 unified diff。
- [x] CLI 展示 inline diff。
- [x] `y` 批准单次编辑。
- [x] `a` 批准本 session 后续非 protected 编辑。
- [x] 其它输入拒绝编辑。
- [x] 应用前再次检查 `old_text`。
- [x] `edit.applied` 事件记录 `patch_hash`。
- [x] 审计日志不记录完整 diff。
- [x] daemon 重启后 pending approval 标记 expired/rejected。

### 3.6 Session、恢复和 Usage

- [x] SQLite session store。
- [x] messages 持久化。
- [x] events 持久化。
- [x] approval 状态持久化。
- [x] `aicode sessions`。
- [x] `aicode resume --last`。
- [x] `aicode resume <session_id>`。
- [x] SSE `Last-Event-ID` 恢复。
- [x] token usage 记录。
- [x] cost 本地估算。
- [x] `aicode usage`。
- [x] `aicode usage --today`。
- [x] `aicode usage --session <session_id>`。

### 3.7 Review

- [x] `aicode review`。
- [x] reviewer 模型路由。
- [x] review mode 只读工具。
- [x] `review_diff` 确定性规则。
- [x] secret、敏感路径、debug 输出、大 diff、TODO/FIXME、前端 XSS、反序列化、TLS/权限等规则。
- [x] `.aicode/config.yaml` 中配置 disabled rules、large diff threshold、max findings。
- [x] `aicode review-rules`。
- [x] `aicode config review list/docs/enable/disable/set/unset/prune`。

### 3.8 Docker Sandbox

- [x] `aicode --sandbox docker test`。
- [x] `aicode --sandbox docker build`。
- [x] `aicode --sandbox docker lint`。
- [x] 读取项目 `commands.test/build/lint`。
- [x] workspace 只读挂载。
- [x] 默认禁网。
- [x] 不传 `.env*`。
- [x] 遮蔽仓库根目录 `.env*`。
- [x] 允许少量 cache env。
- [x] CPU、内存、PID 限制。
- [x] sandbox audit event。
- [~] 还没有可选写入挂载。
- [~] 还没有 artifact 导出。

### 3.9 文档

- [x] README 已按当前实现重写。
- [x] ARCHITECTURE 已按当前实现重写。
- [x] ROADMAP 已改为当前状态路线图。

## 4. 近期优先级

这一组适合继续做“小而实用”的增量，风险小，收益直接。

### P0: 收敛安全语义

- [x] 把 `explain` 升级为硬只读 mode。
  - [x] Runtime 不向 `explain` 暴露 `bash` / `edit_file`（`tool_schemas_for_mode`）。
  - [x] Policy 把 `explain` 加入 read-only mode（`READ_ONLY_MODES`）。
  - [x] 文档同步说明（ARCHITECTURE.md Modes 表、Policy 小节、Current Gaps）。
  - [x] 增加测试覆盖（`test_registry.py`、`test_policy_gate.py`）。

- [ ] 为 pending approval 恢复补端到端测试。
  - 覆盖 daemon restart。
  - 覆盖 session store reload。
  - 覆盖 SSE 中 `approval.expired` + `tool.rejected` / `edit.rejected`。
  - 覆盖重复恢复不重复补事件。

- [ ] 为 prompt 安全层级增加回归测试。
  - `.aicode/rules.md` 不能覆盖系统安全策略。
  - 英文模式下也明确 project rules 的安全边界。
  - review/commit-message/explain mode 的 prompt 约束保持一致。

### P1: 收敛配置体验

- [ ] 给遗留 `models.default/planner/coder` 增加迁移提示。
  - `config show/list/docs` 不推荐旧键。
  - 如果检测到旧键，提示迁移到 `models.main/reviewer/summarizer`。
  - 保持短期兼容，避免破坏旧用户配置。

- [ ] 统一 provider 配置命名。
  - CLI 文档、README、Runtime env 注入保持一致。
  - Anthropic timeout/retry 配置在 CLI 中完整可见。
  - `aicode models --json` 能辅助排查 provider 配置。

### P1: 依赖管理

- [x] Python 运行依赖集中到 `runtime/pyproject.toml`。
- [x] Python 测试依赖集中到 `runtime[dev]` extra。
- [x] Go module 通过根目录 `go.work` 管理。
- [x] 根目录 `Makefile` 提供 `make deps`、`make test-go`、`make test-python`、`make test`。
- [x] Python 依赖锁文件：`runtime/requirements.lock.txt`（运行依赖）与 `runtime/requirements-dev.lock.txt`（含测试依赖），由 `make lock-python`（基于 `uv pip compile --universal`）生成，`make deps-python` 和 CI 均从锁文件安装。
- [ ] 评估是否需要 Go 工具依赖 pinning，例如 lint 工具的 `tools.go`。
- [x] CI 中固定依赖安装和 cache 路径：`.github/workflows/ci.yml` 通过 `make deps-python`（锁文件）安装，`setup-python` 启用 `cache: pip`。

### P1: 补齐 Docker Sandbox MVP

- [ ] 支持可控写入目录。
  - 默认仍只读。
  - 用户显式开启后挂载临时 writable workdir。
  - 不把 `.env*` 或 secret 写入容器。

- [ ] 支持 artifact 导出。
  - 允许导出测试报告、coverage、build output。
  - artifact 路径必须在受控目录内。
  - 审计日志记录 artifact 元信息。

- [ ] 增加 sandbox e2e 测试。
  - test/build/lint 命令选择。
  - 禁网参数。
  - `.env*` mask。
  - resource limit。
  - audit command hash。

## 5. 中期升级

### 5.1 上下文引擎

当前 `related_files` 已能解决一部分源码/测试同名、引用搜索和邻近文件问题。下一步不建议把复杂索引塞回 Agent Loop，而是作为可选只读工具逐步增强。

- [ ] SQLite workspace index。
- [ ] 文件 mtime/hash 缓存。
- [ ] symbol table。
- [ ] import/dependency graph。
- [ ] source/test mapping。
- [ ] TypeScript symbol extraction。
- [ ] Python symbol extraction。
- [ ] Go symbol extraction。
- [ ] `related_files` 使用索引结果增强排序。
- [ ] 大仓库下的增量更新策略。

验收：

```bash
aicode "解释登录流程，并指出前端和后端接口在哪里对应"
```

应能自动找到关键入口、调用链和测试文件，并保持只读。

### 5.2 Provider Reliability

- [ ] per-route health check。
- [ ] provider fallback。
- [ ] 模型不可用时给出可操作诊断。
- [ ] 429/5xx backoff 策略可配置。
- [ ] usage 里区分重试消耗和最终输出。

验收：

```bash
aicode models --json
```

应能看出每个 route 的 provider、model、配置来源和健康状态。

### 5.3 CLI UX

- [ ] 工具调用折叠展示。
- [ ] 分文件 approve / reject。
- [ ] 对同一 edit 增加“追加要求后重新生成”。
- [ ] 大改动前展示简短 plan 并请求确认。
- [ ] 失败原因摘要。
- [ ] `aicode pr-description`。
- [ ] `aicode explain <symbol>` 更精准。
- [ ] 英文 CLI 固定文案补齐。

验收：

```bash
aicode "重构这个模块，但保持行为一致"
```

应能先给出改动边界，分步骤提出 patch，每次写入前展示 diff，最后汇总验证结果。

## 6. 长期方向

这些方向有价值，但不应挤占当前本地 Agent 核心闭环。

- [ ] IDE 插件。
- [ ] Web UI。
- [ ] 远端企业审计控制台。
- [ ] SSO / workspace policy 管理。
- [ ] 云端隔离执行环境。
- [ ] embedding 检索。
- [ ] PR 自动评论。
- [ ] 跨仓库写入。
- [ ] 多 Agent 协作执行。

## 7. 暂不做

- [ ] 不做无确认的大规模文件删除。
- [ ] 不做默认联网 sandbox。
- [ ] 不做自动读取用户全磁盘。
- [ ] 不做项目规则覆盖系统安全策略。
- [ ] 不做 provider 未配置时的 stub 假成功。
- [ ] 不做自动部署生产环境。

## 8. Definition Of Done

新增功能完成时需要满足：

- 有最小测试覆盖。
- 有失败路径处理。
- 涉及写入时必须经过 approval flow。
- 涉及 shell 时必须经过 Policy Engine。
- 涉及模型调用时必须记录 usage。
- 涉及安全策略时必须记录 audit。
- 涉及用户可见行为时同步 README 或 ARCHITECTURE。
- 涉及路线图状态变化时同步本文件。

推荐验证命令：

```bash
make test-go
make test-python
```

文档变更至少运行：

```bash
git diff --check README.md ARCHITECTURE.md ROADMAP.md
```

## 9. 下一轮建议

最建议按这个顺序继续：

1. pending approval 恢复端到端测试。
2. prompt 安全层级回归测试。
3. Docker Sandbox artifact 导出设计和最小实现。
4. `related_files` 索引化增强。

这样可以继续强化当前项目最关键的三个优势：安全边界清楚、状态可恢复、上下文获取更省心。
