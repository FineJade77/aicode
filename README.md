# aicode

`aicode` 是一个本地优先、CLI-first、默认中文交互的 Coding Agent。

当前仓库已完成 Agent Loop 模型驱动开发：

- Go CLI: `cli/`
- Python Runtime: `runtime/`
- Runtime 协议和配置 schema: `schemas/`
- 架构文档: `ARCHITECTURE.md`
- 路线图: `ROADMAP.md`

已具备：

- CLI 自动启动/停止 Runtime daemon
- HTTP + SSE 事件流，流式输出（`assistant.delta`）
- 模型驱动工具循环：主循环由模型通过原生 function calling 驱动（OpenAI `tools` + Anthropic `tool_use`），loop 本身保持极简，安全由执行点的统一 Policy 闸门保证
- 工具集：`read_file` / `search` / `list_files` / `related_files` / `bash` / `edit_file` / `review_diff`
- `edit_file` 逐次展示 inline diff 确认，支持会话级"全部允许"（accept-all）降噪（protected paths 除外），单文件 stale 检测，拒绝编辑非 UTF-8 文件
- OpenAI 兼容 + Anthropic 双 provider，三角色模型路由（`main` / `reviewer` / `summarizer`）
- Policy Engine 三态分级闸门（allow/ask/deny），并修复了 `sed -i`、`git push` 等历史分级漏洞
- 对话 history 作为唯一状态并跨消息持久化，支持多轮修正（如"不对，改成 X"）
- `aicode test` 自动执行低风险测试命令
- `aicode review` 对当前 git diff 执行只读规则审查，并用模型（reviewer 角色）汇总结果
- `aicode review-rules` 查看 review 规则和项目配置后的生效状态
- 本地 JSONL 审计日志
- SQLite session/message 持久化
- 多仓库只读分析

## 本地运行

确认依赖：

```bash
go version
python3 --version
python3 -c 'import fastapi, uvicorn'
```

运行 CLI：

```bash
go run ./cli "解释当前目录"
```

CLI 会自动启动 Python Runtime daemon，并通过 SSE 接收事件。
Runtime 的主循环由模型通过原生 function calling 驱动：模型自主决定调用 `read_file` / `search` / `list_files` / `related_files` / `bash` 探索代码库，需要修改代码时调用 `edit_file`；每次 `edit_file` 调用都会先展示 inline diff，等待用户确认（`y` 应用一次、`a` 应用并对本会话后续编辑自动放行、其它任意输入拒绝）。
应用编辑后，如果模型准备结束当前轮次，Runtime 会插入一条提示要求模型运行相关测试或命令验证改动；验证失败时模型会继续修复，连续 3 次修复失败会停止并汇报现状。
多轮对话中可以直接说"不对，改成 X"：对话 history 作为唯一状态跨消息持久化，模型会基于上一轮上下文继续修改。
发给模型的工具观测会经过三层上下文预算控制：单条大输出头尾保留并标记压缩，历史超预算时优先压缩低价值输出，仍超预算时由 summarizer 模型对早期历史做摘要兜底，避免 prompt 成本失控。
如果没有配置任何模型 provider（未设置 `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY`，或 `provider.anthropic.api_key_env` 指向的变量），Runtime 会直接报错并提示配置方式，不再回退到 stub。

也可以手动启动 Runtime：

```bash
cd runtime
python3 -m uvicorn app.server.main:app --host 127.0.0.1 --port 8765
```

然后在另一个终端运行：

```bash
go run ./cli daemon status
go run ./cli chat "你好"
go run ./cli review
go run ./cli review-rules
go run ./cli models
go run ./cli test
go run ./cli usage
go run ./cli usage --today
go run ./cli usage --session <session_id>
go run ./cli usage --json
go run ./cli "修复 pytest 失败"
```

修改代码不再有单独的直写命令：直接用自然语言描述任务，模型会自主决定读取/搜索哪些文件，并通过 `edit_file` 提出修改，走上面的 inline diff 确认链路。
所有写入都会先展示 unified diff，只有输入 `y`（或 `a` 全部允许）后 Runtime 才会应用；其它输入会拒绝修改。Runtime 会记录生成 diff 时的文件内容基线，应用前再次校验；如果目标文件在确认期间被外部修改、删除或创建，会标记为 stale 并把错误返回给模型，模型可以重新 `read_file` 后再次发起编辑。
CLI 会在实时事件流中展示紧凑 workflow 状态，包括工具调用、diff 确认、验证提示，以及模型 prompt 前发生的上下文预算压缩。
`bash` 工具调用会先经过 Policy Engine：低风险命令（如常见测试/构建命令）可自动执行，中风险命令会要求 CLI 确认，`rm`、破坏性 git、危险控制符等高风险命令不会执行。

## 审计日志

Runtime 会把 session、tool call、edit、usage 等事件记录到本地 JSONL：

```text
~/.aicode/audit.jsonl
```

开发测试时如果设置了 `AICODE_HOME`，审计文件会写入：

```text
$AICODE_HOME/audit.jsonl
```

## Session 持久化

Runtime 会把 session 和 message 写入 SQLite：

```text
~/.aicode/sessions.sqlite
```

开发测试时如果设置了 `AICODE_HOME`，session 数据会写入：

```text
$AICODE_HOME/sessions.sqlite
```

`resume --last` 使用最近活动的 session；继续旧会话后，它会成为新的 last session。

Session 事件流默认每个 session 保留最近 2000 条事件，可通过 `AICODE_SESSION_EVENT_LIMIT` 调整。

可用命令：

```bash
go run ./cli sessions
go run ./cli resume --last
go run ./cli resume --last "继续刚才的任务"
go run ./cli resume <session_id>
go run ./cli resume <session_id> "继续这个会话"
```

## Runtime 认证

`aicode daemon start` 会在 `~/.aicode/runtime.token`（0600 权限）生成一个随机 token，并传给 Runtime 子进程；CLI 之后的每次请求都会带上 `Authorization: Bearer <token>`。`aicode daemon stop` 会清理这个 token 文件。这道认证防止同一台机器上的其它进程未经确认就调用 `/approve` 之类的接口。

手动启动 Runtime（`cd runtime && python3 -m uvicorn ...`，不经过 `aicode daemon start`）时不会设置 `AICODE_RUNTIME_TOKEN`，此时 API 保持不认证，方便本地调试；如果需要给手动启动的 Runtime 也加上认证，自行 `export AICODE_RUNTIME_TOKEN=...` 后启动即可，但对应的 CLI 请求也需要一致的 token 才能通过。

如果 CLI 返回 `401 Unauthorized`，通常是旧 daemon 或手动启动的 uvicorn 仍占用 `8765`，导致当前 `runtime.token` 与正在运行的进程不一致。先运行 `go run ./cli daemon stop`，再用 `lsof -nP -iTCP:8765 -sTCP:LISTEN` 确认没有旧进程后重新 `go run ./cli daemon start`。

## 配置

用户级配置默认路径：

```text
~/.aicode/config.toml
```

初始化：

```bash
go run ./cli config init
go run ./cli config list
go run ./cli config docs
go run ./cli config get models.reviewer
go run ./cli config set models.reviewer gpt-5
go run ./cli config unset models.reviewer
go run ./cli config protected add secrets/local/**
go run ./cli config protected list
go run ./cli config protected remove secrets/local/**
go run ./cli config protected reset
go run ./cli config review disable large_diff
go run ./cli config review enable large_diff
go run ./cli config review set largeDiffThreshold 1200
go run ./cli config review unset largeDiffThreshold
go run ./cli config review list
go run ./cli config review docs
go run ./cli config review prune
go run ./cli config test set python3 -m pytest
go run ./cli config test auto
go run ./cli config test show
go run ./cli config test unset
go run ./cli config workspace add api ../api
go run ./cli config workspace list
go run ./cli config workspace remove api
```

切换为英文交互：

```bash
go run ./cli config set ui.language en-US
```

开发和测试时可以使用 `AICODE_HOME` 避免写入真实 home：

```bash
AICODE_HOME=/tmp/aicode-dev go run ./cli "解释当前目录"
```

项目级配置路径：

```text
.aicode/config.json
```

当前已生效的字段：

- `protectedPaths`: `read_file`、`search`、`list_files`、`related_files`、`review_diff` 和 `edit_file` 都会跳过或拦截这些路径。
- `defaultLanguage`: 项目级交互语言偏好；创建 session 时优先于用户级 `ui.language`。
- `commands.*`: 项目常用命令记忆，会注入 prompt 供模型选择；`commands.test/build/lint` 会被 Docker sandbox 使用，`commands.test` 还会被 `aicode test` 使用；值设置为 `auto` 时自动探测。
- `workspaces`: 声明额外只读仓库，供 `list_files`、`search`、`read_file`、`related_files` 分析使用（这些只读工具支持 `workspace` 参数；`bash`、`edit_file`、`review_diff` 始终只作用于主 workspace）。
- `review.disabledRules`: 关闭指定 review 规则，例如 `large_diff`、`debug_output`。
- `review.largeDiffThreshold`: 调整大 diff 提醒阈值，默认 `500`。
- `review.maxFindings`: 限制 review 输出的问题数量，默认 `50`。

`.aicode/memory.md` 会作为项目记忆注入 prompt，用来放长期背景、架构约定、常见入口等信息；它和 `.aicode/rules.md` 一样只是项目级上下文，不能覆盖系统安全策略、审批要求或 protected paths。

当前内置规则覆盖疑似密钥、敏感路径、大 diff、调试残留、动态执行、前端 XSS、Python 反序列化/YAML 加载、Go TLS 跳过校验和过宽文件权限等常见风险。

可以用 CLI 管理项目保护路径。第一次新增规则时会基于默认保护列表追加，不会丢掉 `.env`、`secrets/**` 等默认安全项：

```bash
go run ./cli config protected add secrets/local/**
go run ./cli config protected list
go run ./cli config protected remove secrets/local/**
go run ./cli config protected reset
```

可以用 CLI 直接启用或禁用 review 规则：

```bash
go run ./cli config review disable large_diff
go run ./cli config review enable large_diff
go run ./cli config review set largeDiffThreshold 1200
go run ./cli config review set maxFindings 25
go run ./cli config review unset largeDiffThreshold
go run ./cli config review list
go run ./cli config review docs
go run ./cli config review prune
```

可以用 CLI 管理项目测试命令覆盖：

```bash
go run ./cli config test set python3 -m pytest tests/unit
go run ./cli config test auto
go run ./cli config test show
go run ./cli config test unset
```

也可以先用 Docker sandbox 在隔离环境里跑测试、构建或 lint。该模式默认禁网、只读挂载 workspace，给容器设置 CPU/内存/PID 限制，并用空文件遮住仓库根目录的 `.env*`：

```bash
go run ./cli --sandbox docker test
go run ./cli --sandbox docker build
go run ./cli --sandbox docker lint
```

默认镜像会按探测到的命令选择；如需自定义，可设置 `AICODE_SANDBOX_DOCKER_IMAGE`。资源限制默认是 `AICODE_SANDBOX_CPUS=2`、`AICODE_SANDBOX_MEMORY=2g`、`AICODE_SANDBOX_PIDS_LIMIT=256`，可用同名环境变量覆盖。每次 sandbox 运行会写入本地审计日志，只记录 action、镜像、隔离配置、资源限制、`.env*` mask 数量和 command hash，不记录原始命令。

也可以用 CLI 管理额外只读 workspace：

```bash
go run ./cli config workspace add api ../api
go run ./cli config workspace list
go run ./cli config workspace remove api
```

如果不确定 rule id，先运行：

```bash
go run ./cli review-rules
```

`config review list` 会用表格显示规则启用状态；`config review docs` 会输出 Markdown 规则说明；`review-rules` 会输出完整 JSON。
`review-rules` 也会在 `config_warnings` 中标出历史配置里已经不存在的 rule id。
可以运行 `config review prune` 自动移除这些未知 rule id。

示例：

```json
{
  "defaultLanguage": "zh-CN",
	  "commands": {
	    "test": "python3 -m pytest tests/unit",
	    "build": "npm run build",
	    "lint": "ruff check ."
	  },
  "review": {
    "disabledRules": ["large_diff"],
    "largeDiffThreshold": 1200,
    "maxFindings": 25
  },
  "protectedPaths": [
    ".env",
    "secrets/**",
    "infra/prod/**"
  ],
  "workspaces": [
    {
      "name": "api",
      "path": "../api",
      "mode": "read_only"
    }
  ]
}
```

多仓库 workspace 第一版只做只读分析。工具调用传入 `{"workspace":"api"}` 时，Runtime 会把路径限制在该配置仓库内；`edit_file`、`bash` 和测试命令仍只在主 workspace 内执行。

模型可以在调用 `read_file`、`search`、`list_files`、`related_files` 时传入 `workspace` 参数指向额外仓库，例如读取 `api` workspace 中的 `src/service.py`，或在 `api` workspace 内搜索 `login`；`bash`、`edit_file`、`review_diff` 始终只作用于主 workspace，跨仓库的 git diff 需要用户显式提供或改用只读工具分析。

## 模型配置

Runtime 接入 OpenAI 兼容 provider（原生 `tools` function calling）和 Anthropic provider（原生 `tool_use`），通过 `provider.type` 选择使用哪一个；模型路由收敛为 `main` / `reviewer` / `summarizer` 三角色。未配置对应 provider 的 API key 时，Runtime 会直接报错并提示配置方式，不再回退到 stub provider。

`aicode review` 在 review 模式下运行同一套模型驱动 loop：只开放只读工具（含 `review_diff` 规则引擎），由 `main` 模型总结审查结论；未配置 provider 时同样会直接报错。`reviewer` 角色路由已接入配置和 `aicode models`，供后续独立审查路径使用。
CLI 的用量事件会显示本次模型调用目的，目前实际会出现 `purpose=main` 或 `purpose=summarizer`（历史压缩兜底）。

查看当前 Runtime 生效的模型路由：

```bash
go run ./cli models
go run ./cli models --json
```

查看 CLI 本地生效配置：

```bash
go run ./cli config list
go run ./cli config docs
go run ./cli config get provider.openai_compatible.base_url
go run ./cli config get pricing.openai_compatible.gpt-5.input_per_1m
```

通过用户级配置设置路由（OpenAI 兼容 provider，默认）：

```bash
go run ./cli config set models.main gpt-5-high
go run ./cli config set models.reviewer gpt-5
go run ./cli config set models.summarizer gpt-5-mini
go run ./cli config set provider.openai_compatible.base_url https://api.openai.com/v1
go run ./cli config set provider.openai_compatible.api_key_env OPENAI_API_KEY
go run ./cli config set provider.openai_compatible.timeout_seconds 60
go run ./cli config set pricing.openai_compatible.gpt-5.input_per_1m 1.25
go run ./cli config set pricing.openai_compatible.gpt-5.output_per_1m 10
go run ./cli config unset pricing.openai_compatible.gpt-5.input_per_1m
```

切换到 Anthropic provider：

```bash
go run ./cli config set provider.type anthropic
go run ./cli config set provider.anthropic.base_url https://api.anthropic.com
go run ./cli config set provider.anthropic.api_key_env ANTHROPIC_API_KEY
go run ./cli config set provider.anthropic.timeout_seconds 120
```

`models.default` / `models.planner` / `models.coder` 已废弃。CLI 会兼容读取旧配置用于迁移，但 `config list`、`config docs`、`config set` 和 Runtime 环境注入不再暴露这些键；请改用 `models.main`。

成本估算只使用本地价格表，不内置也不自动更新官方价格。价格单位是 USD / 1M tokens。
如果没有为当前 provider/model 配置价格，`estimated_cost` 会保持 `0`。
`config unset` 会移除用户配置里的显式项，让它回到默认值或环境变量覆盖值。

常用环境变量：

```bash
export OPENAI_API_KEY="..."
export AICODE_OPENAI_BASE_URL="https://api.openai.com/v1"
export AICODE_OPENAI_API_KEY_ENV="OPENAI_API_KEY"
export AICODE_OPENAI_TIMEOUT_SECONDS="60"
export AICODE_MODEL_MAIN="gpt-5-high"
export AICODE_MODEL_REVIEWER="gpt-5"
export AICODE_MODEL_SUMMARIZER="gpt-5-mini"
export AICODE_PROVIDER_TYPE="openai_compatible"
export AICODE_ANTHROPIC_BASE_URL="https://api.anthropic.com"
export AICODE_ANTHROPIC_API_KEY_ENV="ANTHROPIC_API_KEY"
export AICODE_ANTHROPIC_TIMEOUT_SECONDS="120"
export AICODE_SESSION_EVENT_LIMIT="2000"
export AICODE_SESSION_CACHE_LIMIT="200"
export AICODE_MODEL_PRICES_JSON='{"openai_compatible/gpt-5":{"input_per_1m":1.25,"output_per_1m":10}}'
```

也可以直接设置：

```bash
export AICODE_OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
```

## 用量统计

`aicode usage` 默认输出可读表格，包含总 token、估算成本，以及按 purpose/model/provider 的分组汇总：

```bash
go run ./cli usage
go run ./cli usage --today
go run ./cli usage --session <session_id>
```

需要脚本处理时可切换为原始 JSON：

```bash
go run ./cli usage --json
go run ./cli usage --today --json
go run ./cli usage --session <session_id> --json
```

## 验证

```bash
go test ./cli/...
python3 -m compileall runtime/app
cd runtime && python3 -m pytest
```
