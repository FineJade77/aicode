# aicode

`aicode` 是一个本地优先、CLI-first、默认中文交互的 Coding Agent。

当前仓库处于 Phase 0 骨架阶段：

- Go CLI: `cli/`
- Python Runtime: `runtime/`
- Runtime 协议和配置 schema: `schemas/`
- 架构文档: `ARCHITECTURE.md`
- 路线图: `ROADMAP.md`

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

## 验证

```bash
go test ./cli/...
python3 -m compileall runtime/app
cd runtime && python3 -m pytest
```
