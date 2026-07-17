# aicode

`aicode` 是一个本地优先、CLI-first、默认中文交互的 Coding Agent。

当前仓库处于 Phase 1 工具系统起步阶段：

- Go CLI: `cli/`
- Python Runtime: `runtime/`
- Runtime 协议和配置 schema: `schemas/`
- 架构文档: `ARCHITECTURE.md`
- 路线图: `ROADMAP.md`

已具备：

- CLI 自动启动/停止 Runtime daemon
- HTTP + SSE 事件流
- Agent 工具循环：规则 planner 保底，模型 planner 在 provider 已配置时参与下一步工具选择
- 结构化工具系统
- Policy Engine v1
- `list_files`
- `find_files`
- `read_file`
- `search_text`
- `git_status`
- `git_diff`
- `git_show`
- `review_diff`
- `run_shell`
- `detect_project`
- `run_tests`
- `aicode test` 自动执行低风险测试命令
- 显式 `shell <command>` / `运行命令 <command>` 支持策略检查；中风险命令需用户确认，高风险命令直接拦截
- `aicode review` 对当前 git diff 执行只读规则审查，并用 reviewer model 汇总结果
- `aicode review-rules` 查看 review 规则和项目配置后的生效状态
- 显式 `create` / `append` / `replace` 写入场景的 inline diff 确认链路
- provider 已配置时，coder model 可提出结构化单文件或多文件 patch proposal，并复用 inline diff 确认链路
- 本地 JSONL 审计日志
- SQLite session/message 持久化

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
Runtime 会按计划执行工具循环：先收集工作区、项目和 git 状态，再根据请求选择只读分析、测试或 diff 工具；所有写入仍必须经过 inline diff 确认。
当请求只包含函数名、关键词或不确定位置的文件名时，Runtime 会先搜索/定位候选文件，并自动读取前几个相关文件作为 coder 上下文。
当已读取源码文件时，Runtime 会按 Python、Go、TypeScript/JavaScript 的常见命名规则定位相关测试文件，并把命中的测试文件也读入 coder 上下文。
Runtime 还会从已读取源码中启发式提取 Python、Go、TypeScript/JavaScript 的 import/require 依赖线索，并结合 `tsconfig.json` 的 `baseUrl/paths`、`go.mod` 的 module 名称、`pyproject.toml/setup.cfg` 的 Python package root 定位少量相关依赖文件，帮助 coder 获得入口附近的实现上下文。
发给 planner、coder、reviewer、summarizer 的工具观测会经过上下文预算层：单条大输出会头尾保留并标记压缩，总体超预算时优先压缩低价值搜索/状态类输出，避免 prompt 成本失控。
当模型 provider 已配置且任务带有修复、实现、更新、补测试等写作意图时，Runtime 会让 coder model 输出受限 JSON patch proposal；单次 proposal 可包含多个文件操作，支持 `schema_version: 1`、同一文件连续 replace/append、create、delete、rename。Runtime 会合并生成 unified diff，等待用户确认后才应用。
如果 patch 应用后的自动验证失败，Runtime 会基于失败分析最多生成一次后续修复 patch；后续修复同样只展示 diff，不会绕过用户确认。

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
go run ./cli "create TODO.md 第一条任务"
go run ./cli "append README.md 一行新内容"
go run ./cli "replace README.md old text => new text"
```

所有写入都会先展示 unified diff。只有输入 `y` 确认后，Runtime 才会应用 patch；其它输入会拒绝修改。多文件 proposal 会作为一次 diff 一次确认，确认后批量应用；diff 过大时会在确认前拒绝生成。Runtime 会记录生成 diff 时的文件内容基线，确认后应用前再次校验；如果文件已被外部修改、删除或创建，会拒绝 stale patch 并要求重新生成 diff。
CLI 会在实时事件流中展示上下文状态，包括已读取文件、测试映射、依赖映射、搜索/文件定位命中，以及模型 prompt 前发生的上下文预算压缩。
Patch 应用成功后，Runtime 会自动探测项目测试命令并交给 Policy Engine；低风险测试会自动运行，没有测试命令时会跳过验证。
验证失败时，Runtime 会提取失败摘要、失败用例和相关输出，供 coder model 尝试一次最小后续修复；修复 patch 仍然必须再次确认。
显式 shell 命令会先经过 Policy Engine：低风险测试命令可自动执行，中风险命令会要求 CLI 确认，`rm`、破坏性 git、危险控制符等高风险命令不会执行。

## 审计日志

Runtime 会把 session、message、tool call、approval、patch、usage 等事件记录到本地 JSONL：

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

- `protectedPaths`: 文件读取、搜索、列表和 patch 写入都会跳过或拦截这些路径。
- `commands.test`: 设置为具体命令时，`aicode test` 会优先使用该命令；设置为 `auto` 时自动探测。
- `workspaces`: 声明额外只读仓库，供 `list_files`、`find_files`、`read_file`、`search_text`、`git_status`、`git_diff`、`git_show` 分析使用。
- `review.disabledRules`: 关闭指定 review 规则，例如 `large_diff`、`debug_output`。
- `review.largeDiffThreshold`: 调整大 diff 提醒阈值，默认 `500`。
- `review.maxFindings`: 限制 review 输出的问题数量，默认 `50`。

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
  "commands": {
    "test": "python3 -m pytest tests/unit"
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

多仓库 workspace 第一版只做只读分析。工具调用传入 `{"workspace":"api"}` 时，Runtime 会把路径限制在该配置仓库内；patch、shell 和测试命令仍只在主 workspace 内执行。

Agent 也会识别明确的跨仓目标，例如 `api:src/service.py` 会读取 `api` workspace 中的文件，`查看 api diff` 会查看该只读 workspace 的 git diff，`在 api 搜索 login` 会在该 workspace 内搜索。

## 模型配置

Runtime 已接入 OpenAI-compatible provider 和 Model Router。没有 API key 时会自动回退到 stub provider，方便本地开发。

`aicode review` 会走 `reviewer` 模型路由；没有 API key 时仍会输出确定性规则审查结果。
CLI 的用量事件会显示本次模型调用目的，例如 `purpose=reviewer` 或 `purpose=summarizer`。

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

通过用户级配置设置路由：

```bash
go run ./cli config set models.planner gpt-5-high
go run ./cli config set models.reviewer gpt-5
go run ./cli config set models.summarizer gpt-5-mini
go run ./cli config set provider.openai_compatible.base_url https://api.openai.com/v1
go run ./cli config set provider.openai_compatible.api_key_env OPENAI_API_KEY
go run ./cli config set provider.openai_compatible.timeout_seconds 60
go run ./cli config set pricing.openai_compatible.gpt-5.input_per_1m 1.25
go run ./cli config set pricing.openai_compatible.gpt-5.output_per_1m 10
go run ./cli config unset pricing.openai_compatible.gpt-5.input_per_1m
```

成本估算只使用本地价格表，不内置也不自动更新官方价格。价格单位是 USD / 1M tokens。
如果没有为当前 provider/model 配置价格，`estimated_cost` 会保持 `0`。
`config unset` 会移除用户配置里的显式项，让它回到默认值或环境变量覆盖值。

常用环境变量：

```bash
export OPENAI_API_KEY="..."
export AICODE_OPENAI_BASE_URL="https://api.openai.com/v1"
export AICODE_OPENAI_API_KEY_ENV="OPENAI_API_KEY"
export AICODE_OPENAI_TIMEOUT_SECONDS="60"
export AICODE_MODEL_PLANNER="gpt-5-high"
export AICODE_MODEL_CODER="gpt-5"
export AICODE_MODEL_REVIEWER="gpt-5"
export AICODE_MODEL_SUMMARIZER="gpt-5-mini"
export AICODE_SESSION_EVENT_LIMIT="2000"
export AICODE_MODEL_PRICES_JSON='{"openai_compatible/gpt-5":{"input_per_1m":1.25,"output_per_1m":10}}'
```

也可以直接设置：

```bash
export AICODE_OPENAI_API_KEY="..."
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
