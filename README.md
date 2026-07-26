# aicode

`aicode` 是一个本地优先、CLI-first、默认中文交互的 AI Coding Agent。Go CLI 负责命令行交互、daemon 管理、SSE 渲染和审批输入；Python Runtime 是唯一的 agent 大脑，负责模型调用、工具执行、安全策略、审计、session 持久化和 usage 统计。

当前项目已经完成模型驱动 Agent Loop：模型通过 OpenAI-compatible `tools` 或 Anthropic `tool_use` 自主调用工具探索代码、运行安全命令、提出编辑；所有文件写入都必须经过 inline diff 确认。

## 目录

```text
cli/        Go CLI
runtime/    Python FastAPI Runtime daemon
schemas/    配置、工具、事件 schema
scripts/    安装器和 clean-home E2E
docs/       设计与开发计划
```

核心文档：

- [ARCHITECTURE.md](ARCHITECTURE.md)：当前架构和关键设计
- [ROADMAP.md](ROADMAP.md)：能力状态和剩余计划
- [LOCAL_AGENT_ROADMAP.md](LOCAL_AGENT_ROADMAP.md)：从当前 MVP 到本地日用 Agent 的 P0/P1/P2 工作包、依赖和验收标准
- [TASKS.md](TASKS.md)：按依赖执行的任务台账、当前状态和完成记录
- [schemas/config.schema.json](schemas/config.schema.json)：项目级 `.aicode/config.json` schema
- [schemas/execution.schema.json](schemas/execution.schema.json)：Host/Docker 共用 execution request/result contract
- [schemas/project-trust.schema.json](schemas/project-trust.schema.json)：仓库外 Project Trust store contract

## 已具备能力

- CLI 自动启动、停止和查询 Runtime daemon。
- HTTP + SSE 事件流，支持 `assistant.delta` 流式输出。
- 原生 function calling Agent Loop，支持 OpenAI-compatible provider 和 Anthropic provider。
- 工具集：`read_file`、`search`、`list_files`、`related_files`、`bash`、`edit_file`、`review_diff`。
- `edit_file` 逐次展示 unified diff，支持 `y` 单次应用、`a` 本 session 后续自动应用、其它输入拒绝。
- 仓库外 Project Trust、shell 路径风险分析、mandatory protected paths、stale 文件检测和非 UTF-8 文件拒绝编辑。
- Policy Engine 三态闸门：`allow` / `ask` / `deny`。
- Host 子进程使用最小环境变量 allowlist 和隔离 HOME，不继承 provider/runtime secret。
- review 模式只读；commit-message 模式无工具，只根据 CLI 提供的 diff 生成提交信息。
- SQLite session/message 持久化，支持 resume。
- 同一 session 的 run 串行排队；支持查看当前阶段并取消卡住的 run。
- 本地 JSONL 审计日志，`edit.applied` 记录 `diff_bytes` 与 `patch_hash`，不记录完整 diff。
- token/cost 本地统计，支持按天、session、purpose/model/provider 查看。
- 多仓库只读分析。
- Docker sandbox 可运行 `test` / `build` / `lint`，默认禁网、只读挂载、遮蔽 `.env*`、带资源限制。

## 快速开始

依赖：

- Go 1.22+
- Python 3.11+
- Git
- ripgrep (`rg`)，用于快速搜索
- Make，用于构建、安装和测试快捷命令
- Docker，可选，仅 `--sandbox docker` 需要

源码开发或直接运行 `./bin/aicode` 时，先安装 Runtime Python 依赖；推荐使用锁定版本以保证可复现：

```bash
python3 -m pip install -r runtime/requirements.lock.txt
python3 -m pip install -e ./runtime --no-deps
```

不需要精确锁定版本时，也可以直接安装 `pyproject.toml` 中声明的范围：

```bash
python3 -m pip install -e ./runtime
```

开发环境建议安装 Python 测试依赖（同样锁定版本），并整理 Go module：

```bash
make deps
```

`runtime/requirements.lock.txt` 和 `runtime/requirements-dev.lock.txt` 由 `make lock-python` 生成（需要 [uv](https://docs.astral.sh/uv/)），修改 `runtime/pyproject.toml` 的依赖后重新运行并提交锁文件。

构建 CLI 二进制：

```bash
make build
./bin/aicode "解释当前项目"
```

完整安装到 `~/.local`：

```bash
make install
export PATH="$HOME/.local/bin:$PATH"
```

`make install` 会构建 CLI，创建版本化 Runtime 和独立 Python venv，并从 `runtime/requirements.lock.txt` 安装依赖。默认布局：

```text
~/.local/bin/aicode
~/.local/lib/aicode/manifest.json
~/.local/lib/aicode/<version>/runtime/
~/.local/lib/aicode/<version>/venv/
```

可用 `INSTALL_PREFIX` 安装到其它前缀：

```bash
make install INSTALL_PREFIX=/path/to/prefix
```

安装过程先在 staging 目录完成 Runtime、venv 和依赖验证，再原子更新当前 manifest；失败不会提前切换当前 Runtime。daemon 的解析顺序是 `AICODE_RUNTIME_DIR`、安装 manifest、源码 checkout fallback。

安装后可先运行只读诊断。默认输出人类可读提示，CI 或安装脚本可使用 JSON：

```bash
aicode doctor
aicode doctor --json
```

doctor 检查 Runtime 安装、CLI/Runtime/daemon 版本、Python 3.11+ 与关键依赖、Runtime 端口、provider 配置/API key 环境变量，以及可选的 Docker daemon。它不会启动 Runtime，也不会向 provider 发送请求；未配置 API key、未安装 Docker 或 daemon 尚未启动会显示 warning，核心安装、版本、Python 或端口冲突才返回非零退出码。

下文默认 `aicode` 已经在 `PATH` 中。如果不安装，也可以用 `./bin/aicode` 替代。

配置模型 API key。默认 provider 是 OpenAI-compatible：

```bash
export OPENAI_API_KEY="..."
```

也可以用 aicode 专用变量覆盖：

```bash
export AICODE_OPENAI_API_KEY="..."
```

第一次运行：

```bash
aicode "解释当前项目"
```

CLI 会自动启动 Runtime daemon，并创建 session。没有配置可用 provider 时，Runtime 会直接报错并提示需要设置 API key，不会回退到 stub。

## 常用命令

```bash
aicode chat "你好"
aicode "修复 pytest 失败"
aicode review
aicode diff
aicode test
aicode explain runtime/app/server/main.py
aicode commit-message
```

辅助命令：

```bash
aicode daemon status
aicode daemon stop
aicode doctor
aicode doctor --json
aicode trust status
aicode trust add
aicode trust remove
aicode trust list
aicode sessions
aicode resume --last
aicode resume --last "继续刚才的任务"
aicode resume <session_id> "继续这个会话"
aicode models
aicode models --json
aicode usage
aicode usage --today
aicode usage --session <session_id>
aicode usage --json
aicode review-rules
```

`commit-message` 会优先读取 staged diff；如果没有 staged diff，则读取 tracked working tree diff。它不会包含 untracked 文件内容，除非文件已被 `git add`。

## 工作流

普通任务直接用自然语言描述即可：

```bash
aicode "给认证模块补一个边界测试"
```

Runtime 会把系统 prompt、项目配置、项目规则、项目记忆和对话历史发给模型。模型根据需要调用只读工具、运行低风险命令或提出 `edit_file`。一旦要写文件，CLI 会展示 diff 并等待确认：

- `y`: 应用这一次编辑。
- `a`: 应用这一次编辑，并允许本 session 后续非 protected 编辑自动通过。
- 其它输入: 拒绝该编辑。

应用编辑后，Runtime 会插入验证提示，要求模型运行相关测试或命令；验证失败时模型会继续尝试修复。

## Runtime daemon

自动启动是默认路径。也可以手动启动 Runtime：

```bash
cd runtime
python3 -m uvicorn app.server.main:app --host 127.0.0.1 --port 8765
```

源码调试时可显式覆盖 Runtime 和 Python：

```bash
export AICODE_RUNTIME_DIR="/path/to/aicode/runtime"
export AICODE_RUNTIME_PYTHON="python3"
```

手动启动时默认没有 `AICODE_RUNTIME_TOKEN`，API 不启用认证，便于本地调试。通过 `aicode daemon start` 启动时，CLI 会生成：

```text
~/.aicode/runtime.token
```

之后 CLI 请求会携带 `Authorization: Bearer <token>`。如果遇到 `401 Unauthorized`，通常是旧 daemon 或手动 uvicorn 仍占用 `8765`，先停止 daemon 并确认端口空闲：

```bash
aicode daemon stop
lsof -nP -iTCP:8765 -sTCP:LISTEN
aicode daemon start
```

## Session 和审计

默认数据位置：

```text
~/.aicode/sessions.sqlite
~/.aicode/audit.jsonl
~/.aicode/runtime.log
```

开发测试时可以用 `AICODE_HOME` 隔离本地状态：

```bash
AICODE_HOME=/tmp/aicode-dev aicode "解释当前项目"
```

如果 daemon 重启或 session 恢复时发现未决 approval，Runtime 会把这些 approval 标记为 expired/rejected，并发出对应事件，避免恢复后一直悬挂等待。

审计日志会记录 session、tool call、approval、edit、usage、final、error、execution 等事件。敏感字段会脱敏；edit 审计记录 `patch_hash` 而不是完整 diff；Host/Docker execution 都记录 command hash 而不是原始命令。

## Project Trust 与本地执行安全

workspace 默认是 `untrusted`。`pytest`、`go test`、`npm test` 等会运行仓库代码的项目命令不会直接自动落到 Host；用户需要逐次批准，或显式改用 Docker sandbox。确认仓库可信后可执行：

```bash
aicode trust status
aicode trust add
aicode trust list
aicode trust remove
```

Trust 不写入仓库，也不能通过 `.aicode/config.json`、rules 或 memory 自行提升。记录默认位于 `~/.aicode/trust.json`（设置 `AICODE_HOME` 时为 `$AICODE_HOME/trust.json`），绑定 canonical workspace 路径和可选的 credential-free Git remote；remote 变化后状态自动回到 `untrusted`。文件使用 `0600` 权限和原子替换。

Host shell 同时经过命令风险与路径风险检查：

- `../`、workspace 外绝对路径、用户 home、symlink 逃逸和敏感 glob 命中会被拒绝，`deny` 不能由 approval 覆盖。
- `.env*`、SSH/GPG、AWS/Azure/GCloud/Kubernetes 配置、`.netrc`、包管理凭证和私钥是 mandatory protected paths；仓库配置只能增加保护，不能移除这些系统规则。
- protected paths 同时约束 file/search/list/related/edit 工具和 shell；搜索、目录遍历也不会跟随逃逸 symlink。
- Host 子进程只继承非敏感 allowlist，并使用按 workspace 隔离、权限为 `0700` 的 `HOME` / XDG 目录；provider key、Runtime token 和任意自定义环境变量默认不传入，Runtime 内部 `git`/`rg` 也会拒绝 workspace PATH hijack。
- 已知 Runtime secret 会从 tool output、SSE 和 audit 中脱敏，也不能直接写入文件或作为 shell 字面值执行。

### 排队或疑似卡死时排查

同一 session 一次只执行一个 run，后续消息会排队。先查看当前 run 的状态：

```bash
aicode resume --last
# 或查看全部 session
aicode sessions
```

返回结果中的 `agent` 字段包含：

- `current_run_id`: 当前 run。
- `stage`: 当前阶段，例如 `model.request`、`model.stream`、`tool.bash`、`approval.edit`。
- `elapsed_seconds`: 当前 run 已运行多久。
- `stalled_seconds`: 距离最近一次进度更新多久。
- `queued`: 后面还有多少个 run。

取消当前 run：

```bash
aicode cancel --last
# 或
aicode cancel <session_id>
```

取消会向 Runtime 的当前任务发送 cancellation；如果正在运行 `bash`，其进程组也会被终止。当前 run 会写入 `run.cancelled` 和 `final` 事件，队列中的下一条任务随后自动开始。

实时看 Runtime 和结构化审计日志：

```bash
tail -f ~/.aicode/runtime.log
tail -f ~/.aicode/audit.jsonl
```

按 session 或 run 过滤审计事件：

```bash
tail -f ~/.aicode/audit.jsonl \
  | jq -c 'select(.session_id == "sess_xxx" or .data.run_id == "run_xxx")'
```

判断卡点时可以看最后一组事件：

- `run.started` 之后长期没有 `tool.started`：通常卡在模型请求或模型流。
- 有 `tool.started`、没有对应 `tool.finished`：卡在该工具；`bash` 受配置的命令超时限制。
- 最后是 `approval.requested`：CLI 正在等待确认，默认最多等待 300 秒。
- `runtime.log` 出现异常但没有 `final`：属于 Runtime 异常路径，应保留日志和相应 `session_id` / `run_id`。

shell 命令把服务放到后台时，shell 可能先退出，而后台进程继续持有 Runtime 捕获的 stdout/stderr 管道。Runtime 的超时和取消清理会始终按创建时的进程组 ID 终止整个进程组，并对管道排空设置二次超时，避免这类后台子进程让 run 永久悬挂。

旧版本尚未包含 `cancel` 时，可用下面的兜底方式终止整个 Runtime；这会中断所有 session 的当前 run：

```bash
aicode daemon stop
aicode daemon start
```

## 用户级配置

用户级配置路径：

```text
~/.aicode/config.toml
```

初始化和查看：

```bash
aicode config init
aicode config show
aicode config list
aicode config docs
aicode config get models.main
```

常用设置：

```bash
aicode config set ui.language en-US
aicode config set models.main gpt-5
aicode config set models.reviewer gpt-5
aicode config set models.summarizer gpt-5-mini
aicode config unset models.reviewer
```

provider 配置：

```bash
aicode config set provider.type openai_compatible
aicode config set provider.openai_compatible.base_url https://api.openai.com/v1
aicode config set provider.openai_compatible.api_key_env OPENAI_API_KEY
aicode config set provider.openai_compatible.timeout_seconds 60

aicode config set provider.type anthropic
aicode config set provider.anthropic.base_url https://api.anthropic.com
aicode config set provider.anthropic.api_key_env ANTHROPIC_API_KEY
aicode config set provider.anthropic.timeout_seconds 120
```

成本估算使用本地价格表，单位是 USD / 1M tokens：

```bash
aicode config set pricing.openai_compatible.gpt-5.input_per_1m 1.25
aicode config set pricing.openai_compatible.gpt-5.output_per_1m 10
aicode config unset pricing.openai_compatible.gpt-5.input_per_1m
```

`models.default`、`models.planner`、`models.coder` 已废弃。旧配置仍可被读取用于迁移，但 CLI 文档、配置列表和 Runtime 环境注入不再暴露这些键；请使用 `models.main`、`models.reviewer`、`models.summarizer`。

常用环境变量：

```bash
export AICODE_HOME="/tmp/aicode-dev"
export AICODE_DEFAULT_LANGUAGE="zh-CN"
export AICODE_PROVIDER_TYPE="openai_compatible"
export AICODE_OPENAI_API_KEY="..."
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export AICODE_OPENAI_BASE_URL="https://api.openai.com/v1"
export AICODE_OPENAI_API_KEY_ENV="OPENAI_API_KEY"
export AICODE_OPENAI_TIMEOUT_SECONDS="60"
export AICODE_ANTHROPIC_BASE_URL="https://api.anthropic.com"
export AICODE_ANTHROPIC_API_KEY_ENV="ANTHROPIC_API_KEY"
export AICODE_ANTHROPIC_TIMEOUT_SECONDS="120"
export AICODE_MODEL_MAIN="gpt-5"
export AICODE_MODEL_REVIEWER="gpt-5"
export AICODE_MODEL_SUMMARIZER="gpt-5-mini"
export AICODE_MODEL_CONTEXT_WINDOWS_JSON='{"openai_compatible:local-8k":8192,"gpt-5":200000}'
export AICODE_MODEL_MAX_OUTPUT_TOKENS_JSON='{"openai_compatible:local-8k":2048}'
export AICODE_SESSION_EVENT_LIMIT="2000"
export AICODE_SESSION_CACHE_LIMIT="200"
export AICODE_MODEL_PRICES_JSON='{"openai_compatible/gpt-5":{"input_per_1m":1.25,"output_per_1m":10}}'
```

context capability 的 key 优先使用 `<provider>:<model>`，也支持只写 `<model>`；未配置的模型保守使用 32768 context window 和 8192 max output。`aicode models` 会显示每条路由实际采用的 context、max output 及其来源。还可用 `AICODE_CONTEXT_DEFAULT_WINDOW`、`AICODE_CONTEXT_DEFAULT_MAX_OUTPUT_TOKENS`、`AICODE_CONTEXT_RESERVE_TOKENS`、`AICODE_CONTEXT_COMPACT_THRESHOLD` 和 `AICODE_CONTEXT_CHARS_PER_TOKEN` 调整默认预算与估算参数。

Runtime 会在每次模型请求前估算 system prompt、tool schema、session history 和预留输出所占 token。接近当前 provider/model 的窗口时，它先把旧历史压缩成版本化 compaction entry，再提交请求；原始 message log 保持追加且不会被摘要覆盖。恢复 session 时直接复用最近有效 compaction。若 provider 仍返回 context overflow，只允许一次强制压缩重试，第二次错误会原样结束本次 run，避免无限重试。

## 项目级配置

项目级配置路径：

```text
.aicode/config.json
.aicode/rules.md
.aicode/memory.md
```

`.aicode/rules.md` 是项目规则，`.aicode/memory.md` 是长期项目记忆。二者都会注入 prompt，但都只是项目级上下文，不能覆盖系统安全策略、工具策略、审批要求或 protected paths。

示例：

```json
{
  "defaultLanguage": "zh-CN",
  "commands": {
    "test": "python3 -m pytest tests/unit",
    "build": "npm run build",
    "lint": "ruff check ."
  },
  "protectedPaths": [
    ".env",
    "secrets/**",
    "infra/prod/**"
  ],
  "review": {
    "disabledRules": ["large_diff"],
    "largeDiffThreshold": 1200,
    "maxFindings": 25
  },
  "workspaces": [
    {
      "name": "api",
      "path": "../api",
      "mode": "read_only"
    }
  ]
}
```

字段说明：

- `defaultLanguage`: 项目级输出语言，创建 session 时优先于用户级 `ui.language`。
- `commands.*`: 常用项目命令，会注入 prompt；`commands.test/build/lint` 也会被 Docker sandbox 使用。
- `protectedPaths`: 项目追加的受保护路径；读取、搜索、list/related、review、编辑和 shell 都会跳过或拦截。系统 mandatory patterns 始终合并生效，不能移除。
- `review.disabledRules`: 禁用指定 review 规则。
- `review.largeDiffThreshold`: 大 diff 提醒阈值，默认 `500`。
- `review.maxFindings`: review finding 最大数量，默认 `50`。
- `workspaces`: 额外只读仓库；只读工具可通过 `workspace` 参数访问。

配置 protected paths：

```bash
aicode config protected add secrets/local/**
aicode config protected list
aicode config protected remove secrets/local/**
aicode config protected reset
```

配置 review 规则：

```bash
aicode config review list
aicode config review docs
aicode config review disable large_diff
aicode config review enable large_diff
aicode config review set largeDiffThreshold 1200
aicode config review set maxFindings 25
aicode config review unset largeDiffThreshold
aicode config review prune
```

配置测试命令：

```bash
aicode config test set python3 -m pytest tests/unit
aicode config test auto
aicode config test show
aicode config test unset
```

配置额外只读 workspace：

```bash
aicode config workspace add api ../api
aicode config workspace list
aicode config workspace remove api
```

## Review

`aicode review` 在只读模式下审查当前 git diff。Runtime 只暴露只读工具，并使用 `reviewer` 模型路由。内置规则覆盖：

- 疑似密钥
- 敏感路径
- 大 diff
- 删除测试
- 调试残留
- 动态执行
- 前端 XSS
- Python pickle/YAML 风险
- Go TLS 跳过校验和过宽文件权限

查看规则和项目配置后的生效状态：

```bash
aicode review-rules
```

## Docker sandbox

Docker sandbox 通过本地 Runtime 的统一 ExecutionBackend 执行；CLI 只负责提交、展示结果和中断时取消。支持：

```bash
aicode --sandbox docker test
aicode --sandbox docker build
aicode --sandbox docker lint
```

默认行为：

- workspace 只读挂载到 `/workspace`
- `--network none`
- 不传 `.env*`
- 用空文件遮蔽仓库根目录 `.env*`
- 设置隔离 cache 目录：`HOME`、`GOCACHE`、`GOMODCACHE`、npm/yarn/pip cache
- 资源限制：`--cpus 2`、`--memory 2g`、`--pids-limit 256`
- 与 Agent bash 共用 `execution_id`、超时/取消、终态和 audit
- 写入本地 audit JSONL，记录 backend、资源策略、退出码、耗时和 command hash

可覆盖：

```bash
export AICODE_SANDBOX_DOCKER_IMAGE="golang:1.22"
export AICODE_SANDBOX_CPUS="1.5"
export AICODE_SANDBOX_MEMORY="1g"
export AICODE_SANDBOX_PIDS_LIMIT="128"
```

命令来源优先级：

1. `.aicode/config.json` 的 `commands.test/build/lint`，值不是 `auto` 时直接使用。
2. package.json scripts、Go module/workspace、Python pytest/ruff 配置等自动探测。

Runtime API 只接受 `test/build/lint` 三种 sandbox action，不开放任意远程 shell endpoint。执行中按 `Ctrl-C` 时，CLI 会调用 execution cancel；正常 `aicode daemon stop` 也会先清理活跃进程组。

## 用量统计

```bash
aicode usage
aicode usage --today
aicode usage --session <session_id>
aicode usage --json
aicode usage --today --json
aicode usage --session <session_id> --json
```

输出包含 token、估算成本，以及按 purpose/model/provider 的汇总。没有配置价格时，`estimated_cost` 为 `0`。

## 开发验证

Go CLI：

```bash
make test-go
```

Python Runtime：

```bash
make test-python
make compile-python
```

完整验证：

```bash
make test
```

Go 依赖在 `cli/go.mod` 中维护，根目录 `go.work` 只注册 `./cli` 子模块。Python 运行依赖在 `runtime/pyproject.toml` 的 `[project.dependencies]` 中维护，测试依赖在 `runtime[dev]` extra 中维护。

在受限环境中如果默认 Go build cache 不可写，可以把 cache 放到 workspace 内：

```bash
GOCACHE=.cache/go-build GOMODCACHE=.cache/go-mod go test ./cli/...
```

## 当前边界

- 不支持跨仓库自动写入；额外 workspace 只读。
- 不默认访问互联网；网络相关动作需要经过策略和用户确认。
- 不做无监督自动上线。
- Docker sandbox 目前不支持可选写入挂载。
- tree-sitter 索引、持久化 symbol/import/test mapping 仍在路线图中。
