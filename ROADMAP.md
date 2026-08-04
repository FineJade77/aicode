# aicode Roadmap

更新日期：2026-08-02

本文是**能力状态清单**，回答三个问题：

1. 哪些核心能力已经完成。
2. 哪些能力有 MVP，但还值得升级。
3. 剩下的事情按什么顺序做，以及**凭什么判断该不该做**。

具体到任务粒度的执行顺序、依赖和完成记录见 [TASKS.md](TASKS.md)。本文不重复那些细节，只维护状态。

状态标记：

- `[x]` 已完成：代码已落地，有测试覆盖。
- `[~]` 部分完成：有可用 MVP，但还需增强体验、边界或测试。
- `[ ]` 待实现：尚未落地，或只存在设计意图。

## 1. 路线图原则

- **本地优先**：默认在用户本机 workspace 内工作。
- **CLI-first**：Go CLI 是主要用户入口，Python Runtime 是唯一 Agent 大脑。
- **安全前置**：写入、shell、项目规则和模型输出都必须经过边界约束；边界不可由被检查的仓库自行放宽。
- **可恢复**：session、事件、approval 和 usage 尽量持久化，避免 daemon 重启后状态丢失。
- **模型驱动**：Agent Loop 由模型通过工具调用探索和执行，规则层只做安全、裁剪和确定性辅助。
- **不做假成功**：任何"看起来在工作但实际没有"的降级路径都按缺陷处理。
- **证据先于投入**：需要靠猜才能判断价值的能力（索引、subagent、记忆检索），必须先由评测产出失败归因才启动。
- **文档跟实现同步**：README、ARCHITECTURE、ROADMAP 反映当前代码，而不是旧设计愿景。

## 2. 当前总览

### 2.1 基础设施

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| CLI + daemon | `[x]` | Go CLI 自动启动/停止/查询 Python Runtime，命令按五分类组织。 |
| HTTP + SSE | `[x]` | 本机 API、SSE event stream、daemon token 鉴权（fail-closed）。 |
| 可嵌入 Runtime 分层 | `[x]` | Application contract v2、Agent Core ports、adapters composition root；两条架构守卫锁定 import 无副作用与同进程多 Runtime。 |
| stdio JSONL RPC（SDK） | `[x]` | `app/sdk/`，protocol v1，与 HTTP 共用同一个 ApplicationRuntime。 |
| 版本化本地安装 | `[x]` | CLI、Runtime、venv 和 manifest 一体安装，clean-home E2E 已接入 CI。 |
| 安装诊断 | `[x]` | `aicode runtime doctor [--json]` 检查安装、版本、Python/依赖、端口、provider 和 Docker。 |
| 依赖管理 | `[x]` | Python runtime/dev extra、Go Makefile 入口收敛，锁文件提供可复现安装。 |
| 静态检查 | `[x]` | 仓库根 `ruff.toml`（覆盖 runtime 与 evals），`make lint-python` 已接入 CI。 |

### 2.2 Agent 能力

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| Agent Loop | `[x]` | 原生 function calling，模型自主调用工具；单一历史来源，无内存 transcript 副本。 |
| 双 Provider | `[x]` | OpenAI-compatible 与 Anthropic，含 jitter 退避与 `Retry-After`；可选 fallback（默认关闭，切换必然可见）。 |
| 工具系统 | `[x]` | 12 个内置工具；`ToolSpec` 单一声明，policy 与 loop 不再各持名单。 |
| 只读工具并发 | `[x]` | 连续只读调用成组并发（上限 8），结果按调用顺序写回。 |
| 计划状态 | `[x]` | `update_plan` + `plan.updated`，跨 daemon 重启可见，随 fork 带走。 |
| 批量编辑 | `[x]` | 同文件多处替换一次审批；任一不匹配整体失败，不半应用。 |
| 后台与长时命令 | `[x]` | `bash(background=true)` + `read_output` / `stop_command`，进程组级清理。 |
| 中途提问 | `[x]` | `ask_user` 复用 approval broker；超时与拒绝明确区分。 |
| 五类单轮闸门 | `[x]` | 步数 / token / 成本 / 验证轮次 / 重复动作，共用同一条收尾路径。 |
| 三级上下文管理 | `[x]` | 写入截断 → 折叠旧工具输出 → 结构化摘要；`pending` 由代码续接。 |
| 失效读取检测 | `[x]` | 按读取当时的 hash 比对，过期内容不以事实形态进入摘要。 |
| Session fork | `[x]` | 从任意消息派生新 session；历史复制而非共享，compaction 边界重映射。 |
| MCP 外部工具 | `[x]` | stdio 与 Streamable HTTP 两种 transport；trust 门控、强制审批、按 workspace 缓存。 |
| 上下文索引 | `[~]` | `related_files` + `glob` 启发式已完成；符号 / import / test mapping 未做，见 §4.1。 |

### 2.3 安全与可运维

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| Policy Engine | `[x]` | allow / ask / deny 三态；bash 按 shell 语句边界切分后最严者胜。 |
| Edit approval | `[x]` | inline diff、单次批准、session accept-all、read-before-write、stale 检测；多文件一次审批可分文件选择，拒绝可附带修改指导。 |
| Project Trust | `[x]` | 默认 untrusted；仓库外 store 绑定 canonical 路径与 credential-free remote。 |
| Shell/secret 边界 | `[x]` | 路径风险、mandatory protected paths、symlink 防逃逸、env allowlist、secret 脱敏。 |
| 统一执行后端 | `[x]` | Host / Docker / OS 沙箱共用 execution contract、终态、取消、资源策略和 audit。 |
| OS 级沙箱 | `[x]` | macOS seatbelt；Linux 明确不做（见 §5.3），非 macOS 选 `os` 直接失败而非假装生效。 |
| Docker Sandbox | `[x]` | test/build/lint 完成；`--artifacts` 提供 workspace 之外的受控可写目录与元信息导出。 |
| 项目 hooks | `[x]` | `post_edit` / `pre_bash`，与 Agent 命令共用 policy 与审计路径；untrusted 不执行。 |
| Session store | `[x]` | SQLite 持久化 + WAL 复用连接 + 迁移 ladder + 分页 + 保留策略（默认关闭）。 |
| Pending approval 恢复 | `[x]` | 重启后未决 approval 标记 expired/rejected 并补事件。 |
| 审计可靠性 | `[x]` | 队列满降级同步写入而非丢弃；按大小轮转；健康度经 `runtime status` 暴露。 |
| 分布式追踪 | `[x]` | 可选 OTLP，`run → tool.call → execution`；JSONL 仍是真相来源。 |
| Usage | `[x]` | token/cost 本地统计，按 session/day/purpose/model/provider 查看。 |
| Prompt caching | `[~]` | Anthropic 断点、cache token 与分档计价已实现，**量化对照数据未产出**。 |
| Review rules | `[x]` | 18 条确定性规则，支持配置禁用和阈值。 |

### 2.4 评测

| 档次 | 状态 | 它回答什么 |
| --- | --- | --- |
| `smoke`（scripted，5 任务） | `[x]` | Agent Loop 实现是否正确。CI 门禁，零成本零抖动。 |
| `live`（28 任务） | `[x]` | Agent 能否完成常规真实任务。**已满分，无区分度。** |
| `live_hard`（8 任务） | `[x]` | 因与果分离时还能不能定位。**设计假设被证伪，仍无区分度。** |
| `live_scale`（2 任务） | `[x]` | 规模本身构成难度吗。通过率满分，但检索成本出现 4× 信号。 |
| `live_scale_curve`（4 任务） | `[x]` | 检索成本随规模怎么长。**前两轮因出题破绽作废；正式曲线次线性（0.35）且在 100 模块处走平。** |
| 参考解校验 | `[x]` | 每道题都有经校验的可行解，"难"和"无解"能分开。 |

## 3. 已完成能力

按交付批次组织。逐项的背景、取舍与完成记录见 [TASKS.md](TASKS.md) 对应任务号。

### 3.1 基础运行链路（M0–M3）

- [x] Go CLI 五分类命令与旧扁平命令兼容重写。
- [x] Python FastAPI Runtime，CLI 自动启动 daemon。
- [x] Runtime 本机 token 鉴权，**fail-closed**：未配置 token 即拒绝而非放行。
- [x] `make install` 安装版本化 Runtime、独立 venv 和原子 manifest；三级 Runtime 解析顺序。
- [x] `aicode runtime doctor [--json]` 只读、可操作、可机器解析的本地诊断。
- [x] clean-home install/reinstall/rollback/doctor/start/status/stop E2E。
- [x] Session / run / SSE 完整链路，`assistant.delta` 流式输出，`Last-Event-ID` 恢复。
- [x] SSE 终止保证：按 run 过滤的流必定抵达终态事件，含 keep-alive 心跳。
- [x] 当前 run 取消、阶段与最后进度观测，取消后继续消费队列。
- [x] `aicode chat` 常驻 REPL：同 session follow-up、safe-boundary steer、手动 compaction。
- [x] Application contract v2 与 Agent Core ports 分层，`bootstrap.py` 为唯一 composition root。
- [x] stdio JSONL RPC（SDK），强制握手、有界队列真背压、关停先排空再取消 writer。

### 3.2 模型与配置

- [x] OpenAI-compatible provider 与 Anthropic provider。
- [x] `main` / `reviewer` / `summarizer` 三角色模型路由。
- [x] 版本化 Provider Profile：auth mode、capability、context window、tokenizer 策略。
- [x] `aicode runtime models [--json]` 查看路由与 capability 来源；`models probe` 分阶段探测。
- [x] `tool_calling=false` / `streaming=false` 在请求前快速失败，禁止从正文猜 tool JSON。
- [x] 可重试状态码使用 equal jitter 退避，优先采用 `Retry-After`（60 秒上限）。
- [x] 本地 pricing 配置与成本估算；`unpriced_model_calls` 区分"免费"与"没配价格"。
- [x] 遗留 `models.default/planner/coder` 从文档和推荐配置移除，仅保留读取兼容。
- [~] 旧配置键尚无正式迁移提示。

### 3.3 工具与安全策略

- [x] 12 个内置工具：`read_file` `search` `glob` `list_files` `related_files` `review_diff` `bash` `edit_file` `read_output` `stop_command` `ask_user` `update_plan`。
- [x] `ToolSpec` 单一声明：模型看 schema、policy 读 `read_only`、loop 读 `approval`，三者不可漂移。
- [x] Policy Engine 三态；bash 按 shell 语句边界切分后逐条分类，最严者胜。
- [x] 写入工具必须经过 approval；`deny` 不可由 approval 覆盖。
- [x] read-before-write 强制；非 UTF-8 文件拒绝编辑；stale patch 三种情形分别给出可执行提示。
- [x] protected paths：mandatory 清单不可移除，file/search/glob/list/related/edit 与 shell 共享边界。
- [x] Project Trust 存储在仓库外；仓库配置、rules、memory 均不能自行提升 trust。
- [x] untrusted workspace：bash 进沙箱、项目命令需审批、hooks 完全不执行。
- [x] Host 子进程环境变量 allowlist 与隔离 HOME；Runtime 内部 `git`/`rg` 拒绝 PATH hijack。
- [x] 已知 Runtime secret 不进入 tool output、SSE 或 audit，也不能写文件或拼入 shell。
- [x] review / commit-message / explain 三种只读 mode 的 schema 裁剪与 policy 硬拒绝分离实现。
- [x] MCP 外部工具强制 `approval="gate"`，命名空间隔离，环境 allowlist，故障隔离。

### 3.4 Agent Loop 正确性

- [x] 单一历史来源：不维护内存 transcript，每轮从 session 重建。
- [x] 连续只读调用并发（上限 8），结果按调用顺序写回，`ToolContext` 独立副本。
- [x] 五类单轮闸门共用同一条收尾路径，收尾调用不再过闸，不递归。
- [x] 验证闸门机制化：`VerifyTracker` 记账，达上限产出"改了什么、为何仍失败"。
- [x] 打转检测：同时跟"相同调用"与"相同失败"两条计数，警告每 episode 只发一次。
- [x] 三级上下文管理与结构化摘要，`pending` / `open_failures` 由代码续接。
- [x] 失效读取检测，按读取当时的 hash 而非滚动记录比对。

### 3.5 持久化与可运维

- [x] SQLite session/message/event/approval/compaction 持久化。
- [x] WAL 单连接复用（写入 5.2ms → 1.1ms）；schema 迁移 ladder 与版本上界拒绝。
- [x] 会话列表分页（69.4ms → 1.2ms）与保留策略（默认关闭，活跃 session 永不删除）。
- [x] Session fork：历史复制、compaction 边界重映射、非法 message id 直接报错。
- [x] 审计日志不静默丢弃、按大小轮转、健康度可查询。
- [x] 可选 OTLP 追踪，装饰器模式叠加在 JSONL 之上。
- [x] approval 四态终结（accepted / rejected / timed_out / missing），超时不读作拒绝。

### 3.6 执行环境

- [x] Host / Docker / OS 沙箱统一 ExecutionBackend，共享终态、取消、进程组终止与 audit。
- [x] Agent bash 按 trust 级别进入 Docker 沙箱；`auto|host|docker|os` 四种配置。
- [x] OS 级沙箱（macOS seatbelt）：allow-default + 定点拒绝，保证工作区外不可写与禁网。
- [x] 沙箱不可用时**明确失败**，不回退宿主机；镜像缺失不隐式拉取。
- [x] 后台进程管理挂在 ExecutionService 上，复用 lifespan 清理路径。
- [x] 项目 hooks 与 Agent 命令共用执行、policy 与审计路径。

### 3.7 评测与文档

- [x] scripted smoke suite 进 CI，baseline 固定 policy/prompt/tool-spec digest。
- [x] live harness：`LiveEvalProvider` 与 scripted 表面完全一致，无 mode 分支。
- [x] mutation check：新增测试类任务必须抓到蓄意缺陷。
- [x] 参考解校验：每题可解，且每条 mutation 被两份不同写法的解抓到。
- [x] 失败归因确定性推导，无 LLM-as-judge。
- [x] 四档 live 任务集与对应 Makefile target（含 OpenAI-compatible 重定向）。
- [x] README、ARCHITECTURE 按当前实现重写（2026-08-02）。

## 4. 待定：由证据触发

这一节的能力**都不缺设计，缺的是该不该做的证据**。它们共用一条纪律：只由评测产出的失败归因触发，不按直觉排期。

### 4.1 repo map / 符号索引（T-047）

`related_files` 是启发式黑箱，模型无法理解它为何给出这些结果。业界更有效的是 repo map（tree-sitter 抽符号签名按引用关系排序注入）。

**书面触发条件**：失败归因中"定位失败"占比显著。**当前未满足**——四档 live 的功能性通过率全部满分，归因分布是空的。

但 `live_scale` 给出了一个非通过率的信号：工具调用从易档 6.8 涨到规模档 27.0（4×），而模型调用只从 6.1 涨到 9.5（1.6×）——**模型没多想，是在多找**。这是成本信号而非失败信号，是否据此启动需要显式决定，不能用"差不多满足"代替。

`live_scale_curve` 就是为把这个决定变成数字而建的：同一缺陷埋进 10/30/100/300 个模块，看效率随规模的增长指数。**前两轮曲线均因出题破绽作废**（先是缺陷模块含唯一 token 可一次 grep 命中，再是失败测试直接点名缺陷模块），两处捷径都已由测试挡住。

**曲线已跑出（2026-08-04，12 次运行）**：input tokens 指数 0.35、工具调用 0.12，且在 100 模块处走平——规模涨 30 倍，检索只贵 3.2 倍。**判定：不启动，本方向降级。** 归因分布仍是空集，成本曲线也次线性且已走平。

否掉的是"模块数量多"这根轴：曲线顶点合计仍只有约 137k input tokens，仓库整体没超出上下文窗口，走平还有一部分来自工具输出 2000 字符上限。**"仓库大到装不下"那根轴尚未被测过**，要重开本任务须先造出那种规模。

- [x] 跑出 `live_scale_curve` 的增长指数（0.35 / 0.12 / 0.22，100 处走平）。
- [x] 按曲线形状决定：次线性且走平 → 不做。
- [ ] tree-sitter 符号抽取（Python / TypeScript / Go）。
- [ ] 按引用关系排序注入。
- [ ] 评估 `related_files` 是否应被取代。

### 4.2 检索式项目记忆（T-023）

`.aicode/memory/*.md` 按主题拆分的带 frontmatter 小文件取代当前全量注入的单一 `memory.md`，按当前任务关键词/路径检索注入。

**触发条件**：评测显示跨会话重复解释同一项目约定造成可观测的 token 浪费。**当前未满足。**

### 4.3 subagent（T-048）

**触发条件**：评测显示主上下文被探索过程显著污染。**当前未满足。**

任何 subagent 必须复用预算、Policy、ExecutionBackend、approval 与 trace——否则它就是一条绕过所有闸门的旁路。

### 4.4 模型横向对比与上下文消融（T-026）

同一 live 任务集跑多个模型产出成本–成功率曲线；消融 `compact_threshold` 与 `related_files`。指标已实现，只需喂真实数据。

**部分已做**：`live_hard` 换弱模型（`deepseek-v4-flash`）跑出与强模型**完全一致**的结果，这排除了"任务集有区分度只是对强模型太易"。剩下的横向对比需要更有区分度的任务集才有意义。

## 5. 待定：不依赖证据

这一节是已知缺失的机制或收尾工作，不需要评测数据就能判断该做。

### 5.1 prompt caching 的量化数据（T-014）

代码与测试已完成，任务仍未关闭——**验收要求的是数字，不是代码**：同一真实 session 连续 5 轮，开启与关闭 caching 的 `input_tokens` 与 `estimated_cost` 对比。

阻塞原因是环境而非实现：本机未配置 `ANTHROPIC_API_KEY`，且当前 provider（DeepSeek）走自动上下文缓存、不认 `cache_control`，这条路径拿不到对照数据。

- [ ] 配置 Anthropic key 后跑 5 轮 × 开/关两组，把对照写进 README。

### 5.2 Docker Sandbox 收尾

- [x] 可控写入目录 + artifact 导出（两条本是同一套机制，一并交付）。`aicode project sandbox <action> --artifacts` 把一个宿主临时目录挂到容器 `/artifacts` 并经 `AICODE_ARTIFACTS` 告知命令；workspace 仍是 `readonly`，容器以宿主 uid/gid 运行。默认不开——产物就是退出码的命令不需要任何可写路径。
- 导出只记元信息（相对路径 / 字节数 / sha256），进 audit 也进 CLI；内容不进日志（构建输出无界，且它打印的东西会留在比运行活得更久的日志里）。
- 读取该目录按**不可信输入**处理：符号链接一律不跟随（容器以调用者身份运行，跟随即任意文件读取）、数量与体积设上限（64 / 8 MiB / 32 MiB）。跳过的条目由 `artifacts_truncated` 明说，不静默丢弃。
- **端到端未在本机验证**：本机没有 Docker daemon，Docker 集成测试照例跳过。已覆盖的是 docker 参数构造与收集逻辑（后者是纯文件系统操作，不依赖 Docker）。

### 5.3 Linux OS 沙箱

- **不做**（2026-08-04，用户决定）。当前非 macOS 上选 `os` 直接失败，这个行为保留：声称一条并未生效的边界比明说不支持更糟。Linux 用户使用 `docker` 后端。若日后重开，前置仍是能在 Linux 上真实验证 landlock / bubblewrap 的环境——这条不能靠"尽力而为"实现。

### 5.4 MCP HTTP transport

**先修了一个前提（2026-08-04）：stdio transport 此前根本没有接线。** `McpManager` 在 `app/` 里没有任何消费者（只有测试引用），registry 从不注册 MCP 工具，项目配置里的 `mcp_servers` 解析完无人读取，`events.py` 里登记的 `mcp.server.started` / `failed` 从不发出——一套完整实现、有单元测试、但从未连线的子系统。文档与本文件当时都写着"已接入"。给未接线的子系统加第二种 transport 不会产生任何可观测行为，因此先接线。

接线内容：`McpToolProvider` 按 workspace 惰性启动并缓存服务器，`DefaultToolRuntime.prepare` 在每个 run 开始时调用，工具进入该 run 的工具集，`ApplicationRuntime.aclose` 负责停止。**trust 门控**：只在 `trusted` workspace 启动——清单来自被检查仓库自己的 `.aicode/config.json`，与 hooks 在 untrusted 下不执行同一条规则。已用真实 `bootstrap` + 临时仓库端到端验证：untrusted 为空、trusted 出现两个工具、内置工具不受影响、审批姿态强制为 `read_only=False / approval=gate`、关停干净。

- [x] HTTP transport（Streamable HTTP）。`command` 与 `url` 二选一，管理器之上的一切与 transport 无关。安全侧：不跟随重定向（否则 `Authorization` 会被重发到对端指定的主机）、凭据只写环境变量名、响应 8 MiB 上限且限定 content type。**按本条要求配了协议测试而不只是客户端代码**：JSON 与 SSE 两种应答形状、session id 回传、交错帧中按请求 id 取结果、流未作答、重定向、超大响应、错误状态码、错误 content type、JSON-RPC error、超时、非 http scheme、双 transport 声明——共 15 项；另用真实本地 HTTP 服务做过一次真实 socket 端到端验证。

### 5.5 配置与 provider 收尾

- [x] 遗留 `models.default/planner/coder` 的迁移提示。**发现它们此前根本没有生效**：解析进结构体字段后无人消费，只有 `models.main` 会注入 Runtime，用户写了旧键既不报错也不起作用。现在 `models.default` / `models.coder` 会在 `models.main` 缺席时顶上、在场时报告被忽略，`models.planner` 提示删除（没有对应路由）；每次调用在 stderr 提示做了什么，`runtime doctor` 有对应 `config` 检查项。
- [x] per-route health check：`aicode runtime models probe --routes` 逐条探测 main / reviewer / summarizer，各按自身 tool 能力探测，共用模型只探一次，总状态取最差。summarizer 是重点——它通常是另一个模型，且在 compaction 半路触发前无人碰它。
- [x] provider fallback（2026-08-04）。**默认关闭，必须同时配 `provider.fallback` 与 `provider.fallback_model` 才生效**——两者缺一即视为未配置，因为一个配了一半的 fallback 会恰好在最需要它的时刻失败。回退用它自己的模型：主 provider 的模型名对另一个 provider 毫无意义，直接沿用会失败成"fallback 坏了"的样子。

  **只在"够不着"时回退**：`ProviderError` 与 `ProviderNotConfigured` 会回退；`ProviderCapabilityError` 与 `ContextOverflowError` **不会**——前者说明请求本身不适合这个 provider，换一个能力声明不同的去回答等于悄悄改变模型能做什么；后者有自己的恢复路径，交给别人等于跳过它。

  **绝不静默**：新增 `provider.fallback` SSE 事件（按仓库规则四处同步登记：`events.py`、schema enum、fixture、Go renderer），CLI 直接打印切换；`route_status()` 报告已配置的 fallback，`aicode runtime models` 的 `fallback:` 行因此重新变成真的——它此前打印的是 Runtime 从不发送的字段。用量记录里 provider/model 本就是回答者的名字，所以计价不会张冠李戴。

  以下是决定做它之前记的顾虑，保留作为设计依据：
  - 它不是"收尾"，是新设计。当前配置只有**一个** provider（`provider.type` 单选），fallback 需要先设计第二 provider 的配置形态、路由归属与优先级——这些都还不存在。
  - 它有明确的"假成功"风险：静默换 provider 会同时改变**计价**（价格表按 provider:model 建键）、**能力**（tool_calling / streaming 逐 profile 声明）与**可复现性**。契约必须先定成"显式、可观测、绝不静默"，否则就是本文件第一条原则要按缺陷处理的那种降级路径。
  - **顺带修掉一处已经存在的假承诺**：`ModelRoutesTable` 一直在打印 `provider["fallback"]`，而 Runtime 从不产出该字段，真实输出是空的 `fallback: `——读起来像"有 fallback 但没配"，而不是"没有这个概念"。renderer 测试自己喂了 `"fallback": "stub"`，于是这行死代码看起来是有覆盖的。该行已删除，并加测试钉住不得重现。
- [x] usage 中区分重试消耗与最终输出 —— **核查后改做了别的**。原命题不成立：provider 层重试只在尚未 yield 任何内容时发生，失败那次拿不到 `usage`、从不产生记录；context overflow 重试同理。`main` 里并不藏着一池"重试 token"，加字段只会得到恒为零的一列。真正的洞在隔壁且更严重——`record_usage` 只在 `CompletionResult` 构造后调用，而取消会让 `CancelledError` 从流循环穿出，**中途取消的 run 已消耗的 token 一条记录都不留**。现已改为：取消时写一条 `complete: false` 的用量记录（token/成本为 0，附 `streamed_chars`），汇总新增 `incomplete_calls` 把总数标记为下界。不做估算——编造的测量值比明说的缺口更糟。
- [x] 评估 Go 工具依赖 pinning（如 lint 工具的 `tools.go`）。**结论：不做。** `cli/go.mod` 零依赖（纯 stdlib，连 `go.sum` 都不存在），Go 侧也没有任何第三方工具——只用 toolchain 自带的 `gofmt` / `go vet` / `go test` / `go build`。`tools.go` 的作用是防止 `go mod tidy` 清掉只被工具引用的 import；没有工具也没有模块依赖时，它钉不住任何东西，只增加一处要维护的表面。**引入第三方 linter（如 golangci-lint）时再重开本条**，那时它就有意义了。

  评估中挖出的真问题已修：Go 的**唯一**外部依赖其实是 toolchain 版本本身，而它声明在三处（`go.work`、`cli/go.mod`、CI）且无人保证一致；同时 `make lint` 只跑 Python，CI 的 gofmt / vet 在本地根本没有入口。已新增 `make lint-go` 并让 CI 调用同一个 target（复制步骤正是当初漂移的原因），`make test` 现在跑完整 `lint`，另加 `TestGoVersionIsDeclaredConsistently` 钉住三处版本一致。

  **为什么这不是小题大做**：`go` 指令自 1.21 起只是*最低语言版本*，不是钉子。实测本机 1.25 下 `slices.Repeat`（Go 1.23 才有）在 `go 1.22` 的模块里编译通过——CI 的 1.22 会直接挂。`go vet` 的 stdversion 分析器会报这个错，但它本身自 Go 1.23 才有，且此前本地根本没有跑 vet 的入口。

### 5.6 测试补齐

- [x] pending approval 恢复的端到端测试（`runtime/tests/test_approval_recovery_sse.py`）：重启后重建 `ApplicationRuntime`、SSE 端点上读到 `approval.expired` + `tool.rejected` / `edit.rejected`、两次重启不重复补发、已解决的 approval 不被补发。**边界**：流是直接驱动端点的 body iterator 读的，不走 socket——恢复后的 session 没有 `final`、流不会结束，而 httpx 的 ASGI transport 会缓冲整个响应体，走 HTTP 只能死锁。因此传输层本身仍未覆盖，覆盖到的是 cursor、事件顺序与 SSE 帧格式。
- [x] prompt 安全层级回归测试（`runtime/tests/test_turn_prompts.py`）：敌意 `.aicode/rules.md` / `memory.md` 无法卸掉免责声明、无法撤销 approval 规则；项目文本必须排在约束它的系统段之后；`READ_ONLY_MODES` 里每个 mode 都必须在 prompt 里明说不得改文件（新增只读 mode 却漏配 prompt 会直接报错）。

### 5.7 CLI 体验

- [x] 工具调用折叠展示。超过 16 行的工具输出在**显示上**折叠，保留头部（工具输出的信息在开头：文件的前几行、测试的第一个失败），并写明折叠了多少行**以及模型收到的是完整输出**——以为模型只看到头部的读者会误读它之后的每个决定。
- [x] 分文件 approve / reject。一轮里 ≥2 个 `edit_file` 且路径互不相同时，一次 approval 呈现全部 diff，CLI 支持 `y` / `a` / `r` / `1,3` / 其他=拒绝。同一文件的两次编辑仍串行——第二个 proposal 基于第一个的结果构建，提前展示会显示一份到执行时已失效的 diff。选择解析中越界索引**整体作废**而不是丢掉那一个：静默写入与用户所选不同的集合，比让他重打一次更糟。
- [x] 对同一 edit 追加要求后重新生成。新增 `revise` 结果：拒绝时可附带"应该怎么做"，Runtime 把它作为指令交回模型（"用户没有批准，并要求改成 X；据此修改后重新提出，不要重复同一方案"），而不是只报一个"被拒绝"。空指导会退化为普通拒绝——说"用户解释了"而他没有，是模型会照着做的谎话。
- [x] 失败原因摘要。`RunTracker` 收集本轮的拒绝、失败、预算终止、未应用的编辑与不可用的 MCP 服务器，在 `final` 时汇成一段机械账目。此前这些事件**在发生时**逐条打印，散落在几百行工具输出里；收尾的那句话由模型自己写，而它是唯一对答案有利害关系的叙述者。干净的运行保持沉默——总是打印的摘要会训练读者跳过它，然后在真正需要时它就不在了。已应用的编辑数会一并说明：失败发生在写入之后意味着工作区已被改动。
- [x] `aicode task pr-description [--base <ref>]`。未指定 base 时按 `origin/main` → `origin/master` → `main` → `master` 探测并**打印用了哪个**（猜错要看得见）。**log 用两点、diff 用三点**：`log base...branch` 是对称差，会把 base 自己的提交列成本 PR 的一部分——这个 bug 是测试抓出来的，不是设计时想到的。

## 6. 长期方向

有价值，但不应挤占当前本地 Agent 核心闭环：

- [ ] IDE 插件（必须复用 Application contract v2，不得复制 Agent 逻辑）。
- [ ] Web UI / 全屏 TUI。
- [ ] 远端企业审计控制台、SSO / workspace policy 管理。
- [ ] 云端隔离执行环境。
- [ ] embedding 检索。
- [ ] PR 自动评论。
- [ ] 跨仓库写入。
- [ ] 多 Agent 协作执行。

## 7. 明确不做

这些不是"以后再说"，是设计上的拒绝：

- 不做无确认的大规模文件删除。
- 不做默认联网 sandbox。
- 不做自动读取用户全磁盘。
- 不做项目规则覆盖系统安全策略。
- 不做 provider 未配置时的 stub 假成功。
- 不做沙箱不可用时回退宿主机执行。
- 不做自动部署生产环境。
- 不做 LLM-as-judge 评分。

## 8. Definition Of Done

新增功能完成时需要满足：

- 有最小测试覆盖，有失败路径处理。
- 涉及写入时必须经过 approval flow。
- 涉及进程执行时必须经过统一 ExecutionBackend 和 Policy。
- 涉及模型调用时必须记录 usage。
- 涉及安全策略时必须记录 audit。
- 涉及 Agent 行为时必须提供对应 eval，或明确说明尚缺的评测覆盖。
- 新增 SSE event 必须三处同步登记（`events.py`、`events.schema.json`、fixture + Go renderer）。
- 改动 policy / prompt / 工具声明必须重新生成 eval baseline，且不得为通过而放宽 `minimum_metrics`。
- 涉及用户可见行为时同步 README 或 ARCHITECTURE。
- 涉及路线图状态变化时同步本文件与 [TASKS.md](TASKS.md)。

推荐验证命令：

```bash
make test          # Go + Python
make lint-python
make eval-smoke
```

## 9. 下一轮

一句话：**曲线已跑出且是次线性的，索引方向降级；下一轮该换轴出题，不是加模块数。**

四档 live 的功能性通过率全部满分，通过率已经不携带信息；唯一在动的检索成本现在也有了数字——input tokens 指数 0.35、工具调用 0.12，在 100 模块处走平。因此：

- T-023 / T-026 / T-047 / T-048 的触发条件仍未满足，不启动。T-047 另因曲线次线性而降级。
- 优先做 §5 里不依赖证据的收尾项——它们不需要等任何数据。
- 出题方向从"规模"转向剩下三根还没测过的轴：**需要多处协同编辑**、**仓库内没有 oracle 的真模糊需求**、**长时程多步**。三轮出题的经验是同一条：先读 trace 确认模型走的是设计中的那条路径，再看报告数字——两轮平坦曲线在报告里都显示 PASS，破绽只有 trace 能看出来。
- 若要重开规模这根轴，前置是造出**整体超出上下文窗口**的仓库，而不是把模块数从 300 加到 1000。
