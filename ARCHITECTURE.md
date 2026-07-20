# aicode Architecture

> 本文档描述 **Agent Loop v2**（模型驱动重构后）的当前架构。历史 v1 设计（规则 planner + JSON patch proposal）已废弃，重构设计见 `docs/superpowers/specs/2026-07-17-agent-loop-redesign-design.md`。

## 1. 产品定位

`aicode` 是一个本地优先、CLI-first、默认中文交互的 Coding Agent。

它专注软件开发场景，核心能力是：

- 理解代码仓库
- 搜索和阅读代码
- 由模型自主决定探索路径并调用工具
- 生成代码修改（`edit_file`），展示 inline diff 并等待用户确认后应用
- 运行命令验证改动（低风险命令自动执行，其余经策略闸门）
- 审查代码变更
- 记录 token、成本和审计日志

`aicode` 不做通用聊天助手，不做无监督自动上线，不默认访问互联网，也不允许绕过权限策略修改敏感文件。

## 2. 已确认架构决策

| 主题 | 决策 |
| --- | --- |
| 产品形态 | 第一版只做 CLI |
| CLI 名称 | `aicode` |
| CLI 实现 | Go（薄客户端，只做交互与渲染） |
| Runtime 实现 | Python 3.11+（唯一智能体大脑） |
| 通信协议 | HTTP + Server-Sent Events，支持流式与断点续传 |
| 默认语言 | 中文；通过配置切换英文 |
| Agent 循环 | 模型通过原生 function calling 驱动的工具循环 |
| 模型接入 | OpenAI-compatible provider + Anthropic provider（原生 tool calling + 流式） |
| 模型扩展 | 通过 provider 抽象扩展其他模型 |
| 模型路由 | 三角色：`main`（主循环）/ `reviewer`（review 模式）/ `summarizer`（history 压缩） |
| 无模型运行 | 不支持：未配置 provider 直接报错，不回退 stub |
| token/cost | 本地统计 |
| 文件写入 | 统一走 `edit_file`，逐次 inline diff 确认（支持会话级 accept-all，protected 路径除外） |
| 低风险测试命令 | 允许自动执行 |
| review 模式 | 严格只读（工具集过滤 + policy deny 双保险） |
| 多仓库 workspace | 只读分析（`read_file`/`search`/`list_files` 支持 `workspace` 参数） |
| Docker sandbox | CLI MVP 已支持 `aicode --sandbox docker test`；完整资源限制/审计增强后续补齐 |
| 审计日志 | 从第一版开始记录，后续增强到企业级 |

## 3. 总体架构

```text
aicode CLI (Go)
  |
  | HTTP + SSE over localhost (127.0.0.1)
  v
Python 3.11+ Runtime Daemon
  |
  +-- Agent Loop (模型驱动的工具循环)
  +-- Model Router (main / reviewer / summarizer)
  +-- Provider 层 (OpenAI-compatible / Anthropic，流式 + tool calling)
  +-- Tool Registry (read_file/search/list_files/bash/edit_file/review_diff)
  +-- Policy Engine (allow / ask / deny 三态闸门)
  +-- Edit Approval (inline diff + accept-all + stale 检测)
  +-- History (加载 / 持久化 / 三层上下文压缩)
  +-- Audit Logger
  +-- Session Store (SQLite)
  |
  v
Workspace / Git / Shell / SQLite / Docker Sandbox (test MVP)
```

Go CLI 是用户交互层：命令解析、自动启停 daemon、SSE 事件消费与渲染、审批交互。它不调用模型、不做规划、不修改文件、不判断风险。

所有核心决策、工具调度、权限判断、会话状态都由 Python Runtime 管理。

## 4. 进程模型

本地 daemon + session 模式。

```text
User
  |
  v
aicode "修复这个测试失败"
  |
  v
Go CLI 检查 Runtime 状态
  +-- 未启动：启动 Python daemon（绑定 127.0.0.1）
  +-- 已启动：复用当前 daemon
  |
  v
创建或恢复 session
  |
  v
通过 SSE 流式接收事件
  |
  v
CLI 渲染 assistant 文本流、工具调用、inline diff、final summary
```

保留一次性运行模式（CI/调试）：`aicode --no-daemon "..."`。

## 5. Agent Loop（核心）

v2 的核心理念：**loop 本身极简，模型自主决定做什么，安全由执行点的统一策略闸门保证**。没有规则 planner、意图分类或固定工具序列。

```text
用户消息
  |
  v
load_history(session)  ← 唯一状态，跨消息持久化
  |
  v
┌─────────────────────────────────────────────┐
│  for step in range(max_steps):               │
│      result = model.stream_complete(          │
│          purpose, system, history, tools)     │  ← 文本增量转发为 assistant.delta
│      history.append(assistant_message)        │
│      if not result.tool_calls:                │
│          if 本轮有编辑且未提示验证:            │
│              注入验证提示，继续                │
│          else: break                          │
│      for call in result.tool_calls:           │
│          output = execute_gated(call)         │  ← 安全闸门在这里
│          history.append(tool_message)         │
│      history = compact_if_needed(history)     │
│  else:  # 达到步数上限                         │
│      注入收尾提示，最后一次 tools=[] 强制总结  │
└─────────────────────────────────────────────┘
  |
  v
emit final { summary }
```

要点：

- **history 是唯一状态**，持久化到 SQLite messages 表（role: user/assistant/tool），因此多轮修正（"不对，改成 X"）开箱即用。
- **验证闭环在循环内自然发生**：编辑应用成功后注入系统提示"请运行测试验证；连续 3 次失败请停止汇报"，模型自行跑测试→看失败→再改。
- **预算收尾**：`TurnBudget`（默认 40 步）耗尽时强制模型总结，保证每轮都以 `final` 事件结束。
- **模型失败**：provider 请求最终失败或未配置 → 该轮以 `error` + `final` 事件结束，不静默降级。
- **角色路由**：review 模式的模型调用走 `purpose="reviewer"`，其余走 `main`；history 层压缩走 `summarizer`。

## 6. 通信协议（HTTP + SSE）

### 6.1 API

```http
GET  /v1/daemon/status
POST /v1/sessions
GET  /v1/sessions
GET  /v1/sessions/{id}
POST /v1/sessions/{id}/messages       # provider 未配置时返回 400
GET  /v1/sessions/{id}/events         # SSE，支持 after / Last-Event-ID 续传
POST /v1/sessions/{id}/approve        # body: {approval_id, accept_all}
POST /v1/sessions/{id}/reject
GET  /v1/usage
GET  /v1/usage/sessions/{id}
GET  /v1/models/routes
GET  /v1/review/rules
```

### 6.2 SSE 事件

事件结构化，带 `event_id`（续传游标）和 `run_id`（区分同一 session 内多次运行）。CLI 只渲染。

```text
session.created / run.queued / run.started
assistant.delta      # 模型文本增量（流式）
tool.started / tool.output / tool.denied / tool.error / tool.rejected
approval.requested   # kind: "tool" | "edit"（edit 携带 diff）
edit.applied / edit.rejected / edit.auto_approved
usage.recorded       # model / provider / purpose / tokens / cost
context.budget       # history 压缩发生时
error / final
```

Schema 见 `schemas/events.schema.json`、`schemas/tools.schema.json`、`schemas/config.schema.json`。

## 7. Tool 系统

模型只能通过结构化工具操作系统。工具定义（`{name, description, input_schema}`）作为原生 function calling 的 tools 传给模型；Provider 层把它翻译成各自格式（OpenAI `tools` / Anthropic `tool_use`）。

内置工具（`runtime/app/tools/registry.py`）：

| 工具 | 说明 | 风险 |
| --- | --- | --- |
| `read_file` | 带行号读取，`offset`/`limit` 分页；支持 `workspace` | 只读 |
| `search` | ripgrep 优先，无 rg 时纯 Python 回退；正则 + glob；支持 `workspace` | 只读 |
| `list_files` | 目录树，深度受限；支持 `workspace` | 只读 |
| `bash` | 统一命令执行入口（git/测试/构建等），超时杀进程组 | 由 policy 分级 |
| `edit_file` | replace/create/delete，单文件 stale 检测，拒绝非 UTF-8 | 必经 approval |
| `review_diff` | 对当前 git diff 跑确定性 review 规则 | 只读 |

项目信息（测试命令、protected paths、`.aicode/rules.md`、语言）通过 system prompt 注入，不再需要独立的 detect 工具。

## 8. Policy Engine

统一闸门 `PolicyEngine.gate(tool, args, mode, language)` 返回 `GateDecision(verdict, risk_level, reason)`，verdict ∈ `allow` / `ask` / `deny`：

- 只读工具 → `allow`（review 模式也允许）。
- review 模式下写工具 → `deny`。
- `edit_file` → 恒 `ask`（走 edit approval）。
- `bash` → 命令分级（见下）。
- 未知工具 → `deny`。

`bash` 命令分级采用**引号感知拆分 + 逐子命令取最严**（deny > ask > allow），防止 `ls; rm -rf /`、`ls & rm`、`FOO=1 rm`、`/bin/rm` 之类通过分隔符/前缀绕过：

- **allow**：`ls`/`pwd`/`rg`/`cat`/`git status|diff|show|log`/`pytest`/`go test`/`npm test`/`python -m pytest` 等。
- **ask**：`sed -i`、`git push`/`git commit`、未知命令、含控制符/重定向/命令替换的命令。
- **deny**：`rm`/`sudo`、`git reset|clean|rebase`、`git push --force|-f|--delete`、`git branch -D`、`git stash drop`、`git checkout --`。

deny 不终止循环，而是把拒绝理由作为 tool result 返回给模型，模型自行改道。gate 的 `reason` 按会话语言本地化。

## 9. Edit Approval

所有写入走 `edit_file` → build proposal → approval → apply：

```text
模型调用 edit_file
  |
  v
build_edit_proposal  ← protected 路径 / 非 UTF-8 / old_text 不唯一 → 直接报错，不写
  |
  v
session.auto_accept_edits 且非 protected？
  +-- 是：发 edit.auto_approved，直接应用
  +-- 否：发 approval.requested（携带 unified diff），等待用户
  |         CLI 交互：y=应用一次 / a=本会话后续自动放行 / 其它=拒绝
  v
apply_edit  ← 应用前校验文件内容 hash 未被外部修改（stale 检测），否则报错让模型重读
  |
  v
edit.applied（写入 history 供后续验证）
```

## 10. Context 管理（history）

`runtime/app/agent/history.py` 三层防线，单位为估算 token：

1. **源头截断**：工具结果入 history 前限长（`bash`/`run_tests` 头尾保留，`read_file` 已按行限），标注截断可续读。
2. **滚动压缩**：超预算时把最老的 tool 消息替换为一行摘要，保留最近 N 条完整。
3. **溢出兜底**：仍超硬上限时调 `summarizer` 把前半段压成一条历史摘要。

压缩发生时发 `context.budget` 事件。

## 11. Model Provider 与 Model Router

### 11.1 Provider 抽象

统一接口 `stream_complete(request) -> AsyncIterator[StreamEvent]`，`StreamEvent` 为 `text_delta` / `tool_call` / `done`（含 usage 与 model）。两个实现把各自协议的流式增量归一为该序列：

- `OpenAICompatibleProvider`：原生 `tools` / `tool_calls` 增量。
- `AnthropicProvider`：原生 `tool_use` content block 增量。

HTTP 用 `httpx.AsyncClient`，超时 + 指数退避重试（429/5xx 与传输错误重试，4xx 不重试）；daemon 关闭时经 lifespan 释放连接池。`provider.type` 选择使用哪个。

### 11.2 Model Router

三角色路由：

```text
main        主循环，代码理解与修改
reviewer    review 模式的审查
summarizer  history 压缩，用便宜模型
```

配置示例（用户级）：

```toml
[models]
main = "gpt-5"
reviewer = "gpt-5"
summarizer = "gpt-5-mini"

[provider]
type = "openai_compatible"   # 或 "anthropic"
```

未配置对应 provider 的 API key 时，Runtime 直接报错并提示配置方式，不回退 stub。

## 12. Token 与成本统计

本地统计，存审计日志。记录 `session_id` / `provider` / `model` / `purpose` / `input_tokens` / `output_tokens` / `estimated_cost` / `created_at`。成本用本地价格表估算（USD / 1M tokens），无价格时 `estimated_cost = 0`。

CLI：`aicode usage` / `--today` / `--session <id>` / `--json`。

## 13. 配置

### 13.1 用户级 `~/.aicode/config.toml`

```toml
[ui]
language = "zh-CN"

[models]
main = "gpt-5"
reviewer = "gpt-5"
summarizer = "gpt-5-mini"

[provider]
type = "openai_compatible"

[provider.openai_compatible]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[provider.anthropic]
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
```

### 13.2 项目级 `.aicode/config.json`

```json
{
  "commands": { "test": "python3 -m pytest tests/unit" },
  "protectedPaths": [".env", "secrets/**", "infra/prod/**"],
  "review": { "disabledRules": ["large_diff"], "largeDiffThreshold": 1200, "maxFindings": 25 },
  "workspaces": [ { "name": "api", "path": "../api", "mode": "read_only" } ]
}
```

优先级：命令行 > 项目级 > 用户级。

## 14. Session 与持久化

SQLite 存 session / message / event。message 的 role 为 user/assistant/tool，即完整对话 history，因此 `resume` 恢复后可继续多轮。事件默认每 session 保留最近 2000 条，可用 `AICODE_SESSION_EVENT_LIMIT` 调整。

## 15. 审计日志

从第一版记录到本地 JSONL（`~/.aicode/audit.jsonl`，或 `$AICODE_HOME/audit.jsonl`）：session 创建、消息、工具调用、approval、edit（含 diff 大小）、usage、final、error。敏感内容脱敏（`.env`/password/token/secret/api_key/private_key）。

## 16. 多仓库 Workspace

只读分析。`read_file`/`search`/`list_files` 接受 `workspace` 参数，Runtime 把路径限制在该配置仓库内并强制只读；`edit_file`/`bash`/测试命令只作用于主 workspace。路径逃逸（`../`）被拦截。

## 17. Docker Sandbox

当前 CLI 已支持 `aicode --sandbox docker test`：自动探测测试命令，在 Docker 中运行，workspace 只读挂载，默认禁网，仅注入 `AICODE_SANDBOX=1`，并用空文件遮住仓库根目录的 `.env*`。后续补齐 build/lint、CPU/内存限制和审计日志记录。

## 18. 国际化

默认中文，`aicode config set ui.language en-US` 切英文。plan/审批提示/final summary/error/policy 拒绝原因遵循语言配置。内部日志和机器协议字段保持英文。

## 19. 推荐仓库结构

```text
aicode/
  cli/                     # Go CLI（薄客户端）
  runtime/
    app/
      server/main.py       # FastAPI + SSE + lifespan
      agent/               # loop / turn / history / prompts / types
      tools/               # registry / edit / file / review / command / base
      models/              # provider / openai_compatible / anthropic / router
      policy/engine.py     # 三态 gate
      sessions/store.py    # SQLite session
      audit/ usage/ project/ config/ events/
    tests/
  schemas/                 # events / tools / config JSON Schema
  docs/superpowers/        # 设计 spec 与实施计划
  ARCHITECTURE.md ROADMAP.md README.md
```

## 20. 非目标（当前）

- IDE / Web / Desktop UI
- 本地模型
- 自动发布、跨仓库自动写入、自动删除文件、自动执行高风险 shell
- 完整 Docker sandbox（build/lint、资源限制、审计增强）
- 企业远端审计服务
