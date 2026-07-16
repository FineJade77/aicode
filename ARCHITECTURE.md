# aicode Architecture

## 1. 产品定位

`aicode` 是一个本地优先、CLI-first、默认中文交互的 Coding Agent。

它专注软件开发场景，核心能力是：

- 理解代码仓库
- 搜索和阅读代码
- 规划开发任务
- 生成代码修改方案
- 展示 inline diff 并等待用户确认
- 应用 patch
- 自动运行低风险验证命令
- 审查代码变更
- 记录 token、成本和审计日志

`aicode` 不做通用聊天助手，不做无监督自动上线，不默认访问互联网，也不允许绕过权限策略修改敏感文件。

## 2. 已确认架构决策

| 主题 | 决策 |
| --- | --- |
| 产品形态 | 第一版只做 CLI |
| CLI 名称 | `aicode` |
| CLI 实现 | Go |
| Runtime 实现 | Python 3.11+ |
| 通信协议 | HTTP + Server-Sent Events |
| 默认语言 | 中文 |
| 语言切换 | 通过配置切换英文 |
| 模型接入 | OpenAI-compatible provider first |
| 模型扩展 | 通过 provider 抽象扩展其他模型 |
| 模型路由 | 支持 planner/coder/reviewer/summarizer 分模型 |
| 本地模型 | 第一版暂不支持 |
| token/cost | 本地统计 |
| 文件写入 | 默认必须 inline diff 确认 |
| 低风险测试命令 | 允许自动执行 |
| review 模式 | 严格只读 |
| 多仓库 workspace | 第一版只读分析 |
| Docker sandbox | Phase 4 支持 |
| 审计日志 | 第一版开始记录，后续增强到企业级 |

## 3. 总体架构

```text
aicode CLI (Go)
  |
  | HTTP + SSE over localhost
  v
Python 3.11+ Runtime Daemon
  |
  +-- Agent Loop
  +-- Planner
  +-- Context Engine
  +-- Tool Router
  +-- Policy Engine
  +-- Diff Approval Engine
  +-- Audit Logger
  +-- Session Store
  +-- Model Router
  |
  v
Workspace / Git / Shell / SQLite / Docker Sandbox (Phase 4)
```

Go CLI 是用户交互层。Python Runtime 是唯一智能体大脑。

CLI 不直接调用模型，不直接做复杂规划，也不直接修改文件。所有核心决策、工具调度、权限判断和会话状态都由 Runtime 管理。

## 4. 进程模型

第一版采用本地 daemon + session 模式。

```text
User
  |
  v
aicode "修复这个测试失败"
  |
  v
Go CLI 检查 Runtime 状态
  |
  +-- Runtime 未启动：启动 Python daemon
  +-- Runtime 已启动：复用当前 daemon
  |
  v
创建或恢复 session
  |
  v
通过 SSE 流式接收事件
  |
  v
CLI 展示 plan、tool call、diff、final summary
```

同时保留一次性运行模式：

```bash
aicode --no-daemon "解释当前目录"
```

该模式用于 CI、临时调试和未来 sandbox 场景。

## 5. Go CLI 设计

### 5.1 职责

Go CLI 负责：

- 命令解析
- 自动启动/停止 Runtime daemon
- workspace 检测
- 配置读取和初始化
- SSE 事件消费
- Codex 风格实时输出
- plan 展示
- tool call 展示
- inline diff 展示
- 用户确认/拒绝交互
- session resume
- 本地 usage/cost 查询

Go CLI 不负责：

- 模型调用
- agent planning
- 上下文检索策略
- 文件 patch 生成
- 工具风险判断
- 审计日志生成

### 5.2 CLI 命令

```bash
aicode "修复这个测试失败"
aicode chat
aicode review
aicode explain src/foo.ts
aicode diff
aicode sessions
aicode resume --last
aicode resume <session_id>
aicode usage
aicode usage --today
aicode usage --session <session_id>
aicode config init
aicode config show
aicode config set ui.language en-US
aicode daemon start
aicode daemon stop
aicode daemon status
```

### 5.3 CLI 目录建议

```text
cli/
  cmd/
    root.go
    chat.go
    review.go
    explain.go
    diff.go
    resume.go
    usage.go
    daemon.go
    config.go
  internal/
    client/
    daemon/
    renderer/
    approval/
    config/
    workspace/
```

## 6. Python Runtime 设计

### 6.1 职责

Python Runtime 负责：

- session 生命周期
- agent loop
- planner/replanner
- repository context gathering
- tool calling
- policy/risk evaluation
- patch generation and application
- audit logging
- model routing
- token/cost accounting
- project memory
- multi-repo read-only analysis

### 6.2 Runtime 目录建议

```text
runtime/
  app/
    server/
      main.py
      routes.py
      sse.py
    agent/
      loop.py
      state.py
      prompts.py
    planner/
      planner.py
      plan.py
    context/
      detector.py
      search.py
      index.py
      symbols.py
      workspace.py
    tools/
      base.py
      file.py
      search.py
      shell.py
      git.py
      patch.py
      test.py
      docker.py
    policy/
      engine.py
      risk.py
      rules.py
    approval/
      diff.py
      pending.py
    audit/
      logger.py
      redaction.py
    memory/
      store.py
      project.py
      user.py
    models/
      provider.py
      openai_compatible.py
      router.py
      usage.py
    workflows/
      fix_bug.py
      review.py
      explain.py
      write_tests.py
  tests/
  pyproject.toml
```

## 7. 通信协议

第一版使用 HTTP + SSE。

选择理由：

- Go 和 Python 实现简单
- 支持流式输出
- 易调试
- 后续 IDE/Desktop/Web UI 可复用
- 比 gRPC 更适合第一版快速迭代

### 7.1 API 草案

```http
GET  /v1/daemon/status

POST /v1/sessions
GET  /v1/sessions
GET  /v1/sessions/{session_id}
POST /v1/sessions/{session_id}/messages
GET  /v1/sessions/{session_id}/events
POST /v1/sessions/{session_id}/approve
POST /v1/sessions/{session_id}/reject

GET  /v1/usage
GET  /v1/usage/sessions/{session_id}
```

### 7.2 SSE 事件

事件必须结构化，CLI 只负责渲染。

```json
{"type":"session.created","session_id":"sess_123"}
{"type":"plan.created","items":[{"id":"1","text":"扫描项目","status":"pending"}]}
{"type":"plan.updated","item_id":"1","status":"completed"}
{"type":"tool.started","tool":"search_text","args":{"query":"login"}}
{"type":"tool.output","tool":"search_text","text":"found 4 matches"}
{"type":"approval.requested","approval_id":"appr_123","kind":"patch","risk":"medium"}
{"type":"patch.preview","approval_id":"appr_123","files":["src/auth/login.py"]}
{"type":"patch.applied","approval_id":"appr_123"}
{"type":"usage.recorded","model":"gpt-5","input_tokens":1200,"output_tokens":500}
{"type":"final","summary":"已修复登录失败，并通过相关测试。"}
```

## 8. Agent Loop

```text
User Task
  |
  v
Intent Classification
  |
  v
Gather Context
  |
  v
Create Plan
  |
  v
Execute Step
  |
  v
Call Tool
  |
  v
Observe Result
  |
  v
Replan If Needed
  |
  v
Generate Patch If Needed
  |
  v
Inline Diff Approval
  |
  v
Apply Patch
  |
  v
Verify
  |
  v
Final Summary
```

所有重要阶段都必须发出事件，便于 CLI 展示、session 恢复和审计。

## 9. Tool 系统

### 9.1 工具原则

模型不能直接自由操作系统。所有工具必须是结构化工具，由 Tool Router 调用，由 Policy Engine 判断风险。

工具调用至少包含：

```python
class ToolCall:
    name: str
    args: dict
    session_id: str
    workspace_id: str
    risk_level: str
    requires_approval: bool
```

### 9.2 内置工具

第一版工具：

- `list_files`
- `read_file`
- `search_text`
- `git_status`
- `git_diff`
- `git_show`
- `run_shell`
- `detect_project`
- `generate_patch`
- `apply_patch`
- `run_tests`

Phase 4 工具：

- `docker_run`
- `sandbox_test`
- `sandbox_build`

## 10. Policy Engine

### 10.1 默认策略

默认允许：

- 读取 workspace 文件
- 搜索代码
- 查看 git status/diff/show
- 运行低风险只读命令
- 自动运行低风险测试命令

默认需要确认：

- 写文件
- apply patch
- 安装依赖
- 联网请求
- 大规模重构
- 运行中风险命令

默认拒绝：

- `rm`
- `rm -rf`
- `git reset --hard`
- `git checkout --`
- `sudo`
- 修改 `.env`
- 删除文件
- review 模式下任何写入

### 10.2 命令风险分级

```text
low:
  pwd
  ls
  rg
  git status
  git diff
  git show
  npm test
  pnpm test
  yarn test
  pytest
  go test ./...

medium:
  npm install
  pnpm install
  pip install
  docker build
  chmod
  mv

high:
  rm
  rm -rf
  sudo
  curl | sh
  wget | sh
  git reset
  git checkout --
```

风险判断不能只依赖字符串前缀，必须做命令解析和参数检查。

## 11. Inline Diff Approval

所有写入必须走 patch flow。

```text
Agent 生成 patch
  |
  v
Policy Engine 判断风险
  |
  v
Runtime 创建 pending approval
  |
  v
CLI 展示 inline diff
  |
  v
用户选择 accept / reject
  |
  v
Runtime apply patch 或丢弃 patch
```

CLI 需要支持：

- accept all
- reject all
- accept file
- reject file
- add instruction and regenerate

第一版可以先支持 accept all / reject all。

## 12. Context Engine

### 12.1 第一版能力

- 识别当前 workspace
- 识别 Git repo
- 识别 package manager
- 识别 TypeScript/Python/Go 项目
- 使用 `rg` 搜索文本
- 根据任务读取相关文件
- 根据 package config 推断测试命令

### 12.2 Phase 3 能力

- SQLite workspace index
- tree-sitter 符号解析
- 文件 chunk
- import/dependency graph
- source/test mapping
- 多仓库只读分析
- project memory

### 12.3 多仓库 Workspace

第一版多仓库只读分析。

允许：

- 扫描多个仓库
- 读取多个仓库的代码和配置
- 分析跨仓库接口关系
- 生成跨仓库修改建议

禁止：

- 自动写入其他仓库
- 跨仓库自动 patch
- 跨仓库自动提交
- 跨仓库自动发布

项目配置示例：

```json
{
  "workspaces": [
    {
      "name": "frontend",
      "path": "../frontend",
      "mode": "read_only"
    },
    {
      "name": "backend",
      "path": "../backend",
      "mode": "read_only"
    }
  ]
}
```

## 13. Model Provider 与 Model Router

### 13.1 Provider 抽象

第一版实现 OpenAI-compatible provider。

```python
class ModelProvider:
    async def complete(self, request):
        raise NotImplementedError
```

后续可扩展：

- Anthropic provider
- Google provider
- Azure OpenAI provider
- enterprise gateway provider
- local model provider

### 13.2 Model Router

根据任务用途路由模型：

```text
planner     复杂规划，使用强模型
coder       代码修改，使用强模型
reviewer    代码审查，使用强模型
summarizer  摘要和压缩，使用便宜模型
```

配置示例：

```toml
[models]
planner = "gpt-5-high"
coder = "gpt-5"
reviewer = "gpt-5"
summarizer = "gpt-5-mini"
```

## 14. Token 与成本统计

第一版仅本地统计，存 SQLite。

记录字段：

- `session_id`
- `workspace_path`
- `provider`
- `model`
- `purpose`
- `input_tokens`
- `output_tokens`
- `estimated_cost`
- `created_at`

CLI 查询：

```bash
aicode usage
aicode usage --today
aicode usage --session <session_id>
```

## 15. 配置设计

### 15.1 用户级配置

路径：

```text
~/.aicode/config.toml
```

示例：

```toml
[ui]
language = "zh-CN"
style = "codex"

[models]
planner = "gpt-5-high"
coder = "gpt-5"
reviewer = "gpt-5"
summarizer = "gpt-5-mini"

[provider.openai_compatible]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[permissions]
file_write = "ask"
shell = "allow_low_risk"
network = "ask"
delete_file = "deny"
git_destructive = "deny"

[usage]
track_tokens = true
track_cost = true
storage = "local"
```

### 15.2 项目级配置

路径：

```text
.aicode/config.json
.aicode/rules.md
```

示例：

```json
{
  "projectName": "my-project",
  "defaultLanguage": "zh-CN",
  "commands": {
    "test": "auto",
    "lint": "auto",
    "build": "auto"
  },
  "protectedPaths": [
    ".env",
    "secrets/**",
    "infra/prod/**"
  ],
  "workspaces": [
    {
      "name": "frontend",
      "path": "../frontend",
      "mode": "read_only"
    }
  ]
}
```

用户级配置优先级低于项目级配置。命令行参数优先级最高。

## 16. Session 与持久化

Session 必须可恢复。

建议 SQLite 表：

- `sessions`
- `messages`
- `plans`
- `tool_calls`
- `approvals`
- `patches`
- `token_usage`
- `audit_events`
- `workspace_snapshots`

恢复 session 时需要恢复：

- 用户原始任务
- 当前 plan
- 已执行工具
- 已确认/拒绝 patch
- 模型用量
- workspace 状态摘要

## 17. 审计日志

第一版开始记录审计日志，后续增强到企业级。

必须记录：

- session 创建
- 用户输入摘要
- 模型调用元数据
- tool call
- shell command
- file read
- patch hash
- approval accept/reject
- token/cost
- final result
- error

敏感内容需要脱敏：

- `.env`
- `password`
- `token`
- `secret`
- `api_key`
- `private_key`

第一版本地存储：

```text
~/.aicode/audit.sqlite
```

项目级审计可选：

```text
.aicode/audit.sqlite
```

## 18. Docker Sandbox

Docker Sandbox 放到 Phase 4。

目标能力：

- sandbox 内运行 test/build/lint
- workspace 只读挂载
- 可选 workspace 写入挂载
- 禁用网络
- 限制 CPU/内存
- 隔离依赖安装
- 不向容器注入敏感环境变量

命令示例：

```bash
aicode --sandbox docker "运行测试"
```

## 19. 国际化

默认交互语言为中文。

用户可通过配置切换为英文：

```bash
aicode config set ui.language en-US
```

Runtime 生成内容时必须遵守当前语言配置，包括：

- plan
- tool explanation
- approval prompt
- final summary
- error message

内部日志和机器协议字段保持英文，方便调试和兼容。

## 20. 推荐仓库结构

```text
aicode/
  cli/
  runtime/
  schemas/
    events.schema.json
    tools.schema.json
    config.schema.json
  docs/
  ARCHITECTURE.md
  ROADMAP.md
  README.md
```

## 21. 第一版非目标

第一版不做：

- IDE 插件
- Web UI
- Desktop UI
- 本地模型
- 自动发布
- 自动跨仓库 patch
- 自动删除文件
- 自动执行高风险 shell
- 完整 Docker sandbox
- 企业远端审计服务
