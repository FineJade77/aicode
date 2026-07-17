# aicode Roadmap

## 1. 路线图原则

`aicode` 的开发顺序遵循以下原则：

1. 先完成真实 coding 闭环，再做高级体验。
2. 安全和权限从第一天开始设计，不后补。
3. Go CLI 保持薄客户端，Python Runtime 保持唯一智能体大脑。
4. 所有文件写入必须经过 inline diff 确认。
5. 低风险测试命令可以自动执行，写入和高风险命令必须确认。
6. 默认中文交互，但所有用户可见文案必须支持英文配置。
7. 第一版多仓库只做只读分析。
8. token/cost 第一版只做本地统计。
9. Docker sandbox 放到 Phase 4。

## 2. 版本规划总览

| 阶段 | 周期 | 目标 |
| --- | --- | --- |
| Phase 0 | 1 周 | 建立 Go CLI + Python Runtime 基础骨架 |
| Phase 1 | 2-3 周 | 完成 coding MVP：读、搜、跑命令、出 diff、确认后写入 |
| Phase 2 | 2 周 | 完成验证闭环：自动测试、失败分析、session resume、usage |
| Agent Loop v2 | 2-3 周 | 模型驱动重构：原生 function calling 主循环、流式输出、双 provider、edit_file 确认链路 |
| Phase 3 | 3 周 | 完成上下文引擎：索引、符号、测试映射、多仓库只读分析 |
| Phase 4 | 2-3 周 | 完成安全增强：Docker sandbox、审计增强、敏感信息脱敏 |
| Phase 5 | 2 周 | 打磨 Codex 风格 CLI 体验和开发者效率工具 |

## 3. Phase 0: 基础骨架

周期：1 周

### 目标

建立最小可运行系统：

```text
aicode CLI -> Python Runtime -> SSE streaming response
```

### 交付物

Go CLI：

- `aicode` 命令入口
- `aicode daemon start`
- `aicode daemon stop`
- `aicode daemon status`
- `aicode config init`
- 基础配置加载
- Runtime 自动启动
- SSE 客户端
- 中文默认输出

Python Runtime：

- FastAPI app
- `/v1/daemon/status`
- `/v1/sessions`
- `/v1/sessions/{id}/messages`
- `/v1/sessions/{id}/events`
- session in-memory store
- 基础事件流
- OpenAI-compatible provider stub

工程基础：

- monorepo 目录结构
- Go lint/test 基础命令
- Python lint/test 基础命令
- `ARCHITECTURE.md`
- `ROADMAP.md`

### 验收标准

运行：

```bash
aicode "解释当前目录"
```

应满足：

- CLI 自动启动 Runtime
- Runtime 创建 session
- CLI 能收到 SSE 事件
- 输出默认为中文
- 不发生文件写入
- daemon status 可查询

### 不做

- 不做真实代码修改
- 不做复杂 planner
- 不做 patch apply
- 不做 Docker sandbox

## 4. Phase 1: Coding MVP

周期：2-3 周

### 目标

完成第一个可用 coding loop：

```text
理解任务 -> 搜索代码 -> 读取文件 -> 生成 patch -> 展示 diff -> 用户确认 -> 应用 patch
```

### 交付物

Runtime Agent：

- Agent Loop v1
- Planner v1
- Tool Router v1
- Policy Engine v1
- Diff Approval Engine v1
- Audit Logger v1
- Model Router v1

内置工具：

- `list_files`
- `find_files`
- `read_file`
- `search_text`
- `git_status`
- `git_diff`
- `git_show`
- `run_shell`
- `detect_project`
- `generate_patch`
- `apply_patch`

CLI：

- 实时 plan 展示
- tool call 展示
- inline diff 展示
- approve/reject patch
- `aicode diff`

模型：

- OpenAI-compatible provider
- planner/coder/reviewer/summarizer 路由接口
- 模型配置读取

安全：

- 写文件必须确认
- 高风险命令阻止
- review 模式只读
- protected paths 基础规则

### 验收标准

运行：

```bash
aicode "修复这个测试失败"
```

应满足：

- Agent 能搜索相关代码
- Agent 能读取相关文件
- Agent 能生成 patch
- CLI 展示 inline diff
- 未确认前不修改文件
- 用户确认后才 apply patch
- 审计日志记录本次变更

运行：

```bash
aicode review
```

应满足：

- 只读取 git diff
- 输出 review finding
- 不写文件
- 不运行会改变文件的命令

### 不做

- 不做 tree-sitter 索引
- 不做 Docker sandbox
- 不做多仓库写入
- 不做 IDE 插件

## 5. Phase 2: 验证闭环

周期：2 周

### 目标

让 Agent 可以完成：

```text
运行低风险测试 -> 分析失败 -> 修改 -> 再运行测试 -> 汇报结果
```

### 交付物

项目识别：

- TypeScript/JavaScript 项目识别
- Python 项目识别
- Go 项目识别
- package manager 识别
- 测试框架识别

测试能力：

- 自动发现测试命令
- 自动执行低风险测试命令
- 测试失败日志解析
- 根据失败 replan
- 自动生成最小相关测试

Session：

- SQLite session store
- `aicode sessions`
- `aicode resume --last`
- `aicode resume <session_id>`

Usage：

- token 本地统计
- cost 本地估算
- `aicode usage`
- `aicode usage --today`
- `aicode usage --session <session_id>`

### 验收标准

TypeScript 项目：

```bash
aicode "给这个函数补测试"
```

应满足：

- 识别测试框架
- 生成测试 patch
- 展示 diff 等待确认
- 确认后运行相关测试
- 输出测试结果

Python 项目：

```bash
aicode "修复 pytest 失败"
```

应满足：

- 自动运行低风险 pytest
- 分析失败堆栈
- 生成修复 patch
- 确认后修改
- 再次运行相关测试

Go 项目：

```bash
aicode "运行测试并修复失败"
```

应满足：

- 自动识别 `go test ./...`
- 根据失败定位源码
- 完成确认式修复

### 不做

- 不做 Docker sandbox
- 不做大型索引
- 不做跨仓库 patch

## 6. Agent Loop v2: 模型驱动重构（当前进行中）

周期：2-3 周

设计文档：`docs/superpowers/specs/2026-07-17-agent-loop-redesign-design.md`

### 目标

把控制权从规则交还给模型：主循环由模型通过原生 function calling 驱动，loop 保持极简，安全由执行点的统一策略闸门保证。

### 交付物

- 模型驱动主循环：history 为唯一状态并跨消息持久化，多轮修正开箱即用；TurnBudget 步数/成本上限强制收尾
- 工具集收敛：`read_file` / `search` / `list_files` / `bash` / `edit_file` / `review_diff`
- `edit_file` 逐次 inline diff 确认 + 会话级 accept-all（protected paths 除外）+ 单文件 stale 检测
- Provider 重写：OpenAI 兼容（`tools`）+ Anthropic（`tool_use`）双协议，流式输出，httpx 超时重试
- 模型路由收敛为 `main` / `reviewer` / `summarizer` 三角色
- history 三层上下文压缩：源头截断、滚动压缩、summarizer 兜底
- Policy Engine 分级漏洞修复（`sed -i` 免确认写入、`git push` 免确认外发等）
- 删除：规则 planner、关键词意图检测、JSON patch proposal 协议、`append`/`replace`/`create`/`shell` 直写命令、无模型 stub 降级

### 验收标准

运行 `aicode "修复 pytest 失败"` 应满足：模型自主搜索、读取、运行测试、提出 edit；每次写入展示 diff 并确认；应用后在同一循环内验证并继续修复。

多轮会话："不对，改成 X" 能基于上一轮上下文继续修改。

review 模式仍为硬只读；未配置 provider 时明确报错并提示配置方式；全部写入走 approval，audit/usage/SSE 事件完整。

### 不做

- API 本地 token 认证、事件落盘异步化（独立修补项，不混入本次重构）
- `related_files` 工具、tree-sitter 索引（Phase 3 重新评估）
- Docker sandbox、IDE/Web UI

## 7. Phase 3: 上下文引擎

周期：3 周

### 目标

提升中型仓库可用性，减少用户手动提供上下文的需求。

定位调整（Agent Loop v2 之后）：上下文探索主要由模型驱动 loop 承担，索引与 tree-sitter 符号解析降级为可选加速层，优先级重新评估；source/test mapping 和 import/dependency graph 不再内置于 loop 步骤，如实测有需要，以 `related_files` 只读工具形态提供给模型选择性调用。

### 交付物

索引：

- SQLite workspace index
- file table
- symbol table
- chunk table
- import table
- test mapping table

代码理解：

- tree-sitter 集成
- TypeScript symbol extraction
- Python symbol extraction
- Go symbol extraction
- source/test mapping
- import/dependency graph

多仓库：

- `.aicode/config.json` workspace 配置
- 多仓库扫描
- 多仓库只读分析
- 跨仓库接口关系说明
- 禁止跨仓库自动写入

记忆：

- `.aicode/rules.md`
- project memory
- 常用命令记忆
- 语言偏好读取

### 验收标准

在中型项目中运行：

```bash
aicode "解释登录流程，并指出前端和后端接口在哪里对应"
```

应满足：

- 自动搜索相关文件
- 读取前后端配置
- 输出跨模块关系
- 不修改任何文件

运行：

```bash
aicode "修复认证模块的边界条件并补测试"
```

应满足：

- 自动找到源码和测试文件
- 生成最小 patch
- 用户确认后写入
- 运行相关测试

### 不做

- 不做 embeddings 强依赖
- 不做跨仓库 patch
- 不做企业远端服务

## 8. Phase 4: 安全增强

周期：2-3 周

### 目标

把安全能力做成核心产品特性。

### 交付物

Docker Sandbox：

- `aicode --sandbox docker`
- sandbox 内运行 test/build/lint
- workspace 只读挂载
- 可选写入挂载
- 默认禁用网络
- CPU/内存限制
- 敏感环境变量隔离

审计增强：

- audit event schema
- patch hash
- command hash
- sensitive redaction
- protected path enforcement
- audit export

Policy 增强：

- 高风险命令解析
- shell allowlist/denylist
- 大规模重构提前确认
- install dependency 确认
- network 确认
- review mode hard lock

### 验收标准

运行：

```bash
aicode --sandbox docker "运行测试"
```

应满足：

- 测试在容器中运行
- 默认不传入 `.env`
- 默认无网络
- 审计日志记录 sandbox 配置

运行：

```bash
aicode "删除这些废弃文件"
```

应满足：

- 不自动删除
- 明确提示高风险
- 需要强确认
- 审计日志记录确认结果

### 不做

- 不做远端企业控制台
- 不做 SSO
- 不做云端执行

## 9. Phase 5: CLI 体验打磨

周期：2 周

### 目标

让 CLI 体验接近 Codex 风格：过程透明、简洁、可靠、可恢复。

### 交付物

体验：

- 更清晰的实时 plan
- 工具调用折叠展示
- 失败原因摘要
- inline diff 分文件确认
- accept file / reject file
- add instruction and regenerate
- 更好的 session resume

开发者效率：

- `aicode commit-message`
- `aicode pr-description`
- `aicode explain <file>`
- `aicode explain <symbol>`
- `aicode config show`

成本查看：

- 按天统计
- 按 session 统计
- 按模型统计

### 验收标准

运行：

```bash
aicode "重构这个模块，但保持行为一致"
```

应满足：

- 提前识别大规模重构
- 请求用户确认
- 展示 plan
- 分步骤生成 patch
- 每次写入前展示 diff
- 验证后输出摘要

运行：

```bash
aicode commit-message
```

应满足：

- 基于当前 git diff 生成提交信息
- 不修改文件
- 不执行写入命令

## 10. P0 / P1 / P2 功能分级

说明：本分级制定早于 Agent Loop v2。其中 patch generation / apply patch 在 v2 中由 `edit_file` 确认链路实现，model router 收敛为三角色，tree-sitter index 与多仓库增强的优先级以第 6、7 节为准。

### P0

- Go CLI
- Python Runtime
- HTTP + SSE
- OpenAI-compatible provider
- model router
- 文件读取
- 代码搜索
- git diff/status
- low-risk shell
- patch generation
- inline diff approval
- apply patch after approval
- review read-only mode
- audit log v1
- 中文默认交互
- 英文配置切换

### P1

- 自动测试命令识别
- 测试失败分析
- 自动生成测试
- session resume
- token/cost 本地统计
- SQLite session store
- project config
- protected paths
- TypeScript/Python/Go 项目增强识别

### P2

- tree-sitter index
- 多仓库只读分析
- project memory
- Docker sandbox
- 审计日志增强
- sensitive redaction
- diff 分文件确认
- commit message
- PR description

## 11. 风险清单

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| Agent 自动执行危险命令 | 数据丢失 | Policy Engine 第一版上线，默认拒绝高风险命令 |
| Patch 应用错误 | 破坏代码 | 所有写入前展示 diff，使用 patch apply，不整文件覆盖 |
| 上下文不足导致错误修改 | 修错位置 | 先读相关文件和 git diff，必要时要求用户补充 |
| 模型成本失控 | 使用成本不可控 | 第一版记录 token/cost，后续支持预算限制 |
| 多仓库复杂度过高 | MVP 延误 | 第一版多仓库只读分析，不支持跨仓库写入 |
| Docker sandbox 复杂 | 影响进度 | 放到 Phase 4，不阻塞 MVP |
| 中英文输出混乱 | 体验不稳定 | 用户可见文案统一走 language setting |

## 12. Definition of Done

每个功能完成时必须满足：

- 有最小测试覆盖
- 有失败路径处理
- 有审计日志记录
- 用户可见输出支持中文和英文
- 涉及写入时必须走 approval flow
- 涉及 shell 时必须经过 Policy Engine
- 涉及模型调用时必须记录 token usage
- 文档或配置示例同步更新

## 13. 第一批开发任务建议

建议按以下顺序开工：

1. 创建 monorepo 目录结构。
2. 初始化 Go CLI，注册 `aicode` 根命令。
3. 初始化 Python FastAPI Runtime。
4. 实现 daemon start/status。
5. 实现 session create 和 SSE event stream。
6. 实现配置文件加载，默认 `ui.language = "zh-CN"`。
7. 实现 OpenAI-compatible provider stub。
8. 实现 `list_files`、`find_files`、`read_file`、`search_text`。
9. 实现 Policy Engine v1。
10. 实现 patch preview 和 approval flow。
11. 实现 apply patch after approval。
12. 实现 audit log v1。
13. 实现 usage token/cost 本地记录。

完成以上任务后，`aicode` 就具备第一个真实可用的 Coding Agent 闭环。
