# aicode

`aicode` 是一个本地优先、CLI-first、默认中文交互的 AI Coding Agent。Go CLI 负责命令行交互、daemon 管理、SSE 渲染和审批输入；Python Runtime 是唯一的 agent 大脑，负责模型调用、工具执行、安全策略、审计、session 持久化和 usage 统计。

当前项目已经完成模型驱动 Agent Loop：模型通过 OpenAI-compatible `tools` 或 Anthropic `tool_use` 自主调用工具探索代码、运行安全命令、提出编辑；所有文件写入都必须经过 inline diff 确认。

## 目录

```text
cli/        Go CLI
runtime/    Python FastAPI Runtime daemon
schemas/    配置、工具、事件 schema
docs/       设计与开发计划
```

核心文档：

- `ARCHITECTURE.md`: 当前架构和关键设计
- `ROADMAP.md`: 路线图和剩余计划
- `schemas/config.schema.json`: 项目级 `.aicode/config.json` schema

## 已具备能力

- CLI 自动启动、停止和查询 Runtime daemon。
- HTTP + SSE 事件流，支持 `assistant.delta` 流式输出。
- 原生 function calling Agent Loop，支持 OpenAI-compatible provider 和 Anthropic provider。
- 工具集：`read_file`、`search`、`list_files`、`related_files`、`bash`、`edit_file`、`review_diff`。
- `edit_file` 逐次展示 unified diff，支持 `y` 单次应用、`a` 本 session 后续自动应用、其它输入拒绝。
- protected paths、stale 文件检测、非 UTF-8 文件拒绝编辑。
- Policy Engine 三态闸门：`allow` / `ask` / `deny`。
- review 模式只读；commit-message 模式无工具，只根据 CLI 提供的 diff 生成提交信息。
- SQLite session/message 持久化，支持 resume。
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

安装 Runtime Python 依赖，推荐使用锁定版本以保证可复现：

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

安装到 `~/.local/bin/aicode`：

```bash
make install
export PATH="$HOME/.local/bin:$PATH"
```

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

审计日志会记录 session、tool call、approval、edit、usage、final、error、sandbox 等事件。敏感字段会脱敏；edit 审计记录 `patch_hash` 而不是完整 diff；sandbox 审计记录 command hash 而不是原始命令。

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
export AICODE_SESSION_EVENT_LIMIT="2000"
export AICODE_SESSION_CACHE_LIMIT="200"
export AICODE_MODEL_PRICES_JSON='{"openai_compatible/gpt-5":{"input_per_1m":1.25,"output_per_1m":10}}'
```

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
- `protectedPaths`: 受保护路径；读取、搜索、review 和编辑都会跳过或拦截。
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

Docker sandbox 是 CLI 本地能力，不需要 Runtime。支持：

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
- 写入本地 audit JSONL，记录 sandbox 配置、env mask 数量、退出码、耗时和 command hash

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
