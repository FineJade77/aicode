# aicode Agent Loop v2 设计（模型驱动重构）

日期：2026-07-17
状态：已确认

## 1. 背景与目标

当前 agent loop 是"规则驱动为主、模型补位"：规则 planner 走固定工具序列，模型 planner 只在规则结束后单步补选，coder 在末尾一次性输出 JSON patch bundle。该架构导致大量关键词/正则启发式（`steps.py`、`commands.py` 约 1500 行）、coder 无法迭代探索、无多轮对话记忆。

本次重构将控制权交还模型：**主循环由模型通过原生 function calling 驱动，loop 本身保持极简，安全由执行点的统一闸门保证**。安全链路（approval/audit/SSE/policy/protected paths）、进程模型（Go CLI + Python daemon + HTTP/SSE）、Session 持久化全部保留。

## 2. 已确认决策

| 决策点 | 结论 |
| --- | --- |
| 无模型运行 | 不支持。provider 未配置直接报错并引导配置，删除规则 planner 与 StubProvider 降级 |
| Provider | OpenAI 兼容（`tools`/`tool_calls`）+ Anthropic（`tool_use`）双协议，均用原生 tool calling |
| 迁移策略 | 直接替换，旧 loop/patch 协议代码及对应测试一并删除，不做并行开关 |
| 写入确认 | 每次 `edit_file` 弹 unified diff 逐次确认；支持会话级 accept-all 降噪（protected paths 除外） |
| 模型路由 | 三角色：`main`（主循环）/ `reviewer`（review 汇总）/ `summarizer`（history 压缩） |
| 直写命令 | `append`/`replace`/`create`/`shell` 消息前缀全部删除；子命令（review/diff/test/explain）保留为预设 |
| 流式输出 | 本次一起做：provider 接口按流式设计，text delta 经 SSE 实时转发 |

## 3. 模块变更总览

```
runtime/app/
  agent/
    loop.py          # 重写：约 100 行主循环
    turn.py          # 新增：Turn / Message / TurnBudget 数据结构
    history.py       # 新增：history 构建 + 压缩（吸收 context_budget 思路，按估算 token）
    prompts.py       # 新增：system prompt 模板（main / review / explain 预设）
  models/
    provider.py      # 重写：流式 + tool calling 统一抽象（StreamEvent）
    openai_compatible.py  # 重写：原生 tools + SSE 流，httpx + 重试
    anthropic.py     # 新增：原生 tool_use + 流式
    router.py        # 简化：三角色路由
  tools/
    registry.py      # 新增：工具 schema 定义 + 分发
    edit.py          # 新增：edit_file（替代 JSON patch 协议）
    file.py / search.py / shell.py  # 保留改造
  policy/engine.py   # 修漏洞，规则表重构为互斥分级
```

**删除清单**：`agent/steps.py`、`agent/commands.py`、`agent/patch_flow.py`、`agent/summary.py`、`agent/context_budget.py`（思路并入 `history.py`）、`models/provider.py` 中的 StubProvider、`tools/patch.py` 大部分（unified diff 生成保留给 `edit.py`）、`tools/git.py`/`tools/project.py`/`tools/command.py` 中被 `bash` 工具取代的部分。预计净删约 2500 行。

**保留不动**：SSE 事件机制（`event_id`/`run_id`/断点续传）、SessionStore 与事件回放、per-session 串行运行队列、approval 创建/等待/决议链路、审计日志（hash+脱敏）、protected paths、配置体系（用户级/项目级/命令行优先级）、多仓库只读 workspace。

## 4. 数据结构与主循环

```python
@dataclass
class TurnBudget:
    max_steps: int = 40           # 单轮 tool call 上限，可配置
    max_output_tokens: int = 8192
    max_cost_usd: float | None = None   # 可选，配置开启

# Message 与 provider 原生格式对齐：
#   role: user | assistant | tool
#   assistant 消息可携带 tool_calls；tool 消息携带 tool_call_id
```

主循环（示意）：

```python
async def run_turn(session, request, runtime):
    history = load_history(session)              # 含历史 turn 的完整对话
    history.append(user_message(request))
    tools = tool_schemas_for_mode(request.mode)  # review 模式只注入只读工具
    for step in range(budget.max_steps):
        response = await runtime.router.stream_complete(
            purpose="main", system=system_prompt(request),
            messages=history, tools=tools,
        )   # text delta 实时转发为 assistant.delta 事件
        history.append(response.as_assistant_message())
        if not response.tool_calls:
            break                                # 模型自然结束
        for call in response.tool_calls:
            result = await execute_gated(call, session, request, runtime)
            history.append(tool_message(call.id, result))
        history = await compact_if_needed(history, runtime)
    persist_history(session, history)
    emit_final(session, response.text)
```

要点：

- **history 是唯一状态**，持久化到现有 messages 表（role 扩展为 assistant/tool），多轮修正（"不对，改成 X"）开箱即用。
- **验证闭环在循环内自然发生**：edit 应用成功后向 history 注入一条系统提示（"编辑已应用，请运行相关测试验证；连续 3 次修复失败请停止并汇报"），不再有独立的 repair/rebuild 机制。
- **预算强制收尾**：步数/成本超限时注入"请立即停止并总结当前进度"，保证 turn 总能以 final 事件结束。
- system prompt 注入项目信息：检测到的语言/测试命令（来自 `.aicode/config.json` 或启动时检测）、protected paths、`.aicode/rules.md`、语言配置（zh-CN/en-US）。

## 5. 工具集

| 工具 | 参数要点 | 风险处理 |
| --- | --- | --- |
| `read_file` | path, offset, limit；带行号返回，单次上限约 500 行；支持 `workspace` 参数读只读仓库 | 只读 |
| `search` | rg 封装：query(regex), glob, limit；支持 `workspace` | 只读 |
| `list_files` | path, depth；支持 `workspace` | 只读 |
| `bash` | command, timeout；取代 run_shell/run_tests/git_*/detect_project | policy 三态分级 |
| `edit_file` | path, old_text, new_text；old_text 为空且文件不存在 = create；new_text 省略 = delete | 必走 approval |
| `review_diff` | 现有确定性 review 规则，保留为只读工具（仅 review 模式注入） | 只读 |

原则：工具少而正交，description 写清"什么时候用我"。`detect_project` 不再作为工具，项目信息进 system prompt。启发式测试映射/依赖提取代码删除；若后续实测有需要，可将其收编为只读工具 `related_files` 供模型选择性调用（本次不做）。

## 6. Provider 层与流式

```python
class ModelProvider(Protocol):
    def is_configured(self) -> bool: ...
    async def stream_complete(self, req: CompletionRequest) -> AsyncIterator[StreamEvent]: ...

# StreamEvent: text_delta | tool_call_start | tool_call_delta | done(usage)
```

- 两个实现（OpenAI 兼容、Anthropic）把各自协议的流式增量归一为统一 `StreamEvent`；上层 loop 不感知协议差异。
- HTTP 用 `httpx.AsyncClient`：超时 + 指数退避，网络错误与 429/5xx 重试 2 次，4xx 不重试。
- `text_delta` 逐段转发为 SSE `assistant.delta` 事件，CLI 实时渲染；`done` 携带 usage，照现有方式记账并发 `usage.recorded`。
- 配置：`provider.type = "openai_compatible" | "anthropic"`；三角色（main/reviewer/summarizer）可分别指定模型名。旧配置中的 planner/coder 字段在加载时映射到 main 并提示迁移。
- provider 未配置：CLI 侧启动检查即报错，输出配置引导命令；不再有 Stub 降级。

## 7. 安全层

- **统一闸门 `execute_gated`**：每个 tool call 落地前过 policy，得到 allow / ask / deny。deny 不终止循环，而是把拒绝理由作为 tool result 返回，模型自行改道。
- **PolicyEngine 修复**：规则表重构为互斥分级；`sed`/`cat` 等移出无条件低风险（`sed -i` 不得免确认写文件）；`git` 子命令白名单（status/diff/show/log 为 low；commit/push 需确认；push --force/branch -D/reset/checkout --/clean/rebase 拒绝）；未知命令默认 ask。
- **edit approval**：应用前生成 unified diff → SSE `approval.requested`（携带 diff）→ 用户 accept / reject / accept-all。accept-all 为会话级状态：本会话后续 edit 免确认，但 protected paths 仍强制逐次确认；开启动作本身记入事件流与审计。reject 作为 tool result 回给模型。
- **单文件 stale 检测**：`edit_file` 应用前校验文件内容自模型读取后未被外部修改（内容 hash 比对），变了则返回错误让模型重读，取代旧的 patch bundle 级 stale/rebuild 机制。
- **review 模式双保险**：工具集只注入只读工具，policy 同时拒绝一切写入类调用。

## 8. 错误处理

| 场景 | 行为 |
| --- | --- |
| provider 未配置 | 启动即报错，提示配置命令 |
| provider 请求最终失败（重试耗尽） | 该 turn 以 error + final 事件结束，不静默降级 |
| 工具执行异常 | 捕获为 tool result 错误文本回给模型，模型可重试或绕路 |
| 审批超时（默认 300s） | 视为拒绝，模型收到拒绝结果 |
| edit stale | 返回错误让模型重读后重试 |
| history 压缩后仍超窗口 | summarizer 强制摘要重建 history |
| 步数/成本超限 | 注入收尾指令，强制输出进度总结 |

## 9. 上下文管理（history.py）

三层防线，单位为估算 token（chars/3.5 起步）：

1. **源头截断**：工具结果入 history 前限长（read_file 限行数、bash 输出头尾保留），标注"已截断，可用 offset 继续读"。
2. **滚动压缩**：history 超阈值（模型窗口约 70%）时，最老的工具轮次替换为一行摘要，保留用户消息、assistant 决策与最近 N 轮完整内容。
3. **溢出兜底**：仍超限时调 summarizer 把前半段压成结构化摘要（做过什么/改了什么/待办什么）作为新 history 开头。

压缩行为发 `context.budget` 事件（沿用现有事件类型），并在被压缩内容处留标记告知模型不要臆测省略内容。

## 10. CLI 变更

- 删除 append/replace/create/shell 消息前缀解析。
- 子命令保留并映射为预设：`review`（只读工具集 + reviewer 汇总）、`diff`/`test`/`explain`（预设 prompt）。
- 新增渲染：`assistant.delta` 流式文本、edit approval 的 diff 展示与 accept/reject/accept-all 交互。
- 其余命令（sessions/resume/usage/models/config/daemon）不变。

## 11. 测试策略

**保留**：sessions、SSE、policy、audit、usage、pricing、project_config、CLI（config/renderer/client/daemon）等约一半现有测试。

**新增**：

- loop 单测：FakeProvider 脚本化 tool_calls 序列，覆盖循环推进、自然结束、max_steps 收尾、验证提示注入、accept-all、stale 检测、deny 改道。
- provider 协议测试：两种协议各配录制的流式响应 fixture，验证 StreamEvent 归一与 usage 解析、重试逻辑。
- policy 回归测试：`sed -i`、`git push`、`git push --force` 等漏洞场景。
- 端到端冒烟：FakeProvider 驱动完整"改文件 → 确认 → 跑测试 → final"流程。

**删除**：test_agent_steps、test_agent_commands、test_coder_patch、test_patch（patch 协议部分）、test_context_budget（随模块并入 history 测试）等。

## 12. 非目标（本次不做）

- API 本地 token 认证、事件落盘异步化等稳定性修补（独立于本重构，另行处理）
- `related_files` 工具、tree-sitter 索引
- Docker sandbox、IDE/Web UI
- 分文件 accept/reject（accept-all 已覆盖主要降噪需求）
