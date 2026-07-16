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
- 结构化工具系统
- Policy Engine v1
- `list_files`
- `read_file`
- `search_text`
- `git_status`
- `git_diff`
- `git_show`
- `run_shell`
- `aicode test` 自动执行低风险测试命令
- append 写入场景的 inline diff 确认链路
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

也可以手动启动 Runtime：

```bash
cd runtime
python3 -m uvicorn app.server.main:app --host 127.0.0.1 --port 8765
```

然后在另一个终端运行：

```bash
go run ./cli daemon status
go run ./cli chat "你好"
go run ./cli test
go run ./cli usage
go run ./cli usage --today
go run ./cli usage --session <session_id>
go run ./cli "append README.md 一行新内容"
```

所有写入都会先展示 unified diff。只有输入 `y` 确认后，Runtime 才会应用 patch；其它输入会拒绝修改。

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

可用命令：

```bash
go run ./cli sessions
go run ./cli resume --last
go run ./cli resume <session_id>
```

## 配置

用户级配置默认路径：

```text
~/.aicode/config.toml
```

初始化：

```bash
go run ./cli config init
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

示例：

```json
{
  "commands": {
    "test": "python3 -m pytest tests/unit"
  },
  "protectedPaths": [
    ".env",
    "secrets/**",
    "infra/prod/**"
  ]
}
```

## 模型配置

Runtime 已接入 OpenAI-compatible provider 和 Model Router。没有 API key 时会自动回退到 stub provider，方便本地开发。

常用环境变量：

```bash
export OPENAI_API_KEY="..."
export AICODE_OPENAI_BASE_URL="https://api.openai.com/v1"
export AICODE_MODEL_PLANNER="gpt-5-high"
export AICODE_MODEL_CODER="gpt-5"
export AICODE_MODEL_REVIEWER="gpt-5"
export AICODE_MODEL_SUMMARIZER="gpt-5-mini"
```

也可以直接设置：

```bash
export AICODE_OPENAI_API_KEY="..."
```

## 验证

```bash
go test ./cli/...
python3 -m compileall runtime/app
cd runtime && python3 -m pytest
```
