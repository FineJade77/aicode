# Agent Loop v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 aicode 的 agent 核心从"规则驱动 + 一次性 JSON patch"重构为"模型通过原生 function calling 驱动的工具循环"，含流式输出、双 provider、edit_file 逐次确认链路。

**Architecture:** 采用"新旧并存、最后切换删除"策略：Task 1-11 在旧代码旁边搭建 v2（新类型加在旧模块内、新模块用独立文件名如 `loop_v2.py`），每个 task 独立测试通过；Task 12 把 server 切到 v2；Task 13 删除全部旧实现并重命名；Task 14-16 完成 CLI、端到端与文档。每次 commit 全量测试保持绿色。

**Tech Stack:** Python 3.11+ / FastAPI / httpx（新增依赖）/ pytest；Go CLI（标准库）；SQLite；SSE。

**Spec:** `docs/superpowers/specs/2026-07-17-agent-loop-redesign-design.md`

## Global Constraints

- Python 3.11+；新增运行时依赖仅 `httpx>=0.27`，不引入其它新库。
- 所有文件写入必须经 approval；review 模式硬只读（工具集过滤 + policy 双保险）。
- 用户可见文案走 `app.agent.utils.localized(language, zh, en)`，中文默认。
- SSE 事件保持既有机制（`event_id`/`run_id`/断点续传）；保留事件类型：`session.created`、`run.queued`、`run.started`、`tool.started`、`tool.output`、`tool.denied`、`tool.error`、`tool.rejected`、`approval.requested`、`usage.recorded`、`context.budget`、`error`、`final`；新增：`assistant.delta`、`edit.applied`、`edit.rejected`、`edit.auto_approved`。
- 测试命令：`cd runtime && python3 -m pytest`（Python）、`go test ./cli/...`（Go）。每个 task 结束两者都必须通过。
- commit message 末尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 执行建议在隔离 worktree 中进行（superpowers:using-git-worktrees）。

## File Structure（最终形态）

```
runtime/app/
  agent/
    loop.py          # v2 主循环（由 loop_v2.py 在 Task 13 重命名而来）
    turn.py          # 新增：TurnBudget + canonical message 构造
    history.py       # 新增：history 加载/持久化/三层压缩
    prompts.py       # 新增：system prompt 构建 + 项目信息注入
    types.py         # 修改：AgentRuntime 增加 policy 字段
    utils.py         # 保留：localized 等
  models/
    provider.py      # 重写：CompletionRequest/StreamEvent/CompletionResult/协议
    openai_compatible.py  # 重写：原生 tools + SSE 流式 + 重试
    anthropic.py     # 新增：原生 tool_use + 流式
    router.py        # 重写：三角色 + stream_complete 聚合
  tools/
    registry.py      # 新增：v2 工具 schema + 分发 + read_file/search/bash 实现
    edit.py          # 新增：edit_file 提议/应用 + stale 检测
    base.py          # 保留：ToolContext/ToolResult/路径与保护校验
    file.py          # 保留 ListFilesTool；删除 FindFilesTool/ReadFileTool（Task 13）
    review.py        # 保留：ReviewDiffTool + review_rules_data
  policy/engine.py   # 重构：gate() 三态分级（allow/ask/deny）
  server/main.py     # 修改：接线 v2、approve 支持 accept_all
  sessions/store.py  # 修改：Session 增加 auto_accept_edits
删除（Task 13）：agent/steps.py、agent/commands.py、agent/patch_flow.py、agent/summary.py、
  agent/context_budget.py、tools/patch.py、tools/project.py、tools/git.py、tools/shell.py、
  tools/command.py、tools/search.py、tools/test_analysis.py、tools/router.py、
  models StubProvider/ModelRequest/ModelResponse 及对应旧测试
cli/internal/
  client/client.go   # 修改：Approve 带 accept_all
  renderer/renderer.go  # 修改：assistant.delta / edit.* 渲染，删除死事件分支
  config/config.go   # 修改：models.main / provider.type / provider.anthropic.* 配置键
cli/main.go          # 修改：edit 确认交互 y/a/其它
```

Canonical 数据约定（全计划通用，实现见 Task 1）：

- message：`{"role":"user","content":str}` / `{"role":"assistant","content":str,"tool_calls":[{"id","name","arguments":dict}]}` / `{"role":"tool","tool_call_id":str,"content":str}`
- tool schema：`{"name":str,"description":str,"input_schema":{JSON Schema}}`
- StreamEvent 简化为三种：`text_delta` / `tool_call`（provider 内部聚合参数增量后整体发出）/ `done`。spec 中的 `tool_call_start/delta` 由 provider 内部消化，上层不需要部分参数。

---

### Task 1: Provider 核心类型与 Settings 三角色

**Files:**
- Modify: `runtime/app/models/provider.py`（追加新类型，保留旧类型到 Task 13）
- Modify: `runtime/app/config/settings.py`
- Test: `runtime/tests/test_provider_types.py`

**Interfaces:**
- Produces: `ToolCallRequest(id, name, arguments)`、`Usage(input_tokens, output_tokens)`、`CompletionRequest(purpose, system, messages, tools, model, temperature, max_tokens)`、`StreamEvent(type, text, tool_call, usage, model)`、`CompletionResult(text, tool_calls, model, provider, input_tokens, output_tokens, estimated_cost)`、`ProviderError`、`ProviderNotConfigured`、`RETRYABLE_STATUS`；`settings.models.main`、`settings.provider.type`、`settings.anthropic.*`

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_provider_types.py
from app.config.settings import Settings
from app.models.provider import CompletionRequest, CompletionResult, StreamEvent, ToolCallRequest


def test_completion_request_defaults():
    req = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}])
    assert req.tools == []
    assert req.max_tokens == 8192


def test_stream_event_tool_call():
    call = ToolCallRequest(id="tc_1", name="read_file", arguments={"path": "a.py"})
    event = StreamEvent(type="tool_call", tool_call=call)
    assert event.tool_call.name == "read_file"


def test_settings_main_role_from_env(monkeypatch):
    monkeypatch.setenv("AICODE_MODEL_MAIN", "model-x")
    monkeypatch.setenv("AICODE_PROVIDER_TYPE", "anthropic")
    monkeypatch.setenv("AICODE_ANTHROPIC_API_KEY_ENV", "MY_KEY")
    settings = Settings.from_env()
    assert settings.models.main == "model-x"
    assert settings.provider.type == "anthropic"
    assert settings.anthropic.api_key_env == "MY_KEY"


def test_settings_main_falls_back_to_coder(monkeypatch):
    monkeypatch.delenv("AICODE_MODEL_MAIN", raising=False)
    monkeypatch.setenv("AICODE_MODEL_CODER", "legacy-coder")
    settings = Settings.from_env()
    assert settings.models.main == "legacy-coder"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_provider_types.py -v`
Expected: FAIL（ImportError: CompletionRequest 等不存在）

- [ ] **Step 3: 实现**

在 `runtime/app/models/provider.py` 顶部 imports 后**追加**（保留文件中已有的 `ModelRequest`/`ModelResponse`/`ModelProvider`/`StubProvider`/`ModelProviderUnavailable` 不动）：

```python
from collections.abc import AsyncIterator
from dataclasses import field
from typing import Any, Protocol

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass(slots=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class CompletionRequest:
    purpose: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 8192


@dataclass(slots=True)
class StreamEvent:
    type: str  # "text_delta" | "tool_call" | "done"
    text: str = ""
    tool_call: ToolCallRequest | None = None
    usage: Usage | None = None
    model: str = ""


@dataclass(slots=True)
class CompletionResult:
    text: str
    tool_calls: list[ToolCallRequest]
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0


class ProviderError(Exception):
    pass


class ProviderNotConfigured(ProviderError):
    pass


class StreamingModelProvider(Protocol):
    provider_name: str

    def is_configured(self) -> bool: ...

    def stream_complete(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]: ...
```

修改 `runtime/app/config/settings.py`：`ModelSettings` 增加 `main` 字段，新增 `ProviderSettings`、`AnthropicSettings`：

```python
class ModelSettings(BaseModel):
    default: str = "gpt-5"
    planner: str = "gpt-5-high"   # legacy，Task 13 删除
    coder: str = "gpt-5"          # legacy，Task 13 删除
    main: str = "gpt-5"
    reviewer: str = "gpt-5"
    summarizer: str = "gpt-5-mini"


class ProviderSettings(BaseModel):
    type: str = "openai_compatible"  # openai_compatible | anthropic


class AnthropicSettings(BaseModel):
    base_url: str = "https://api.anthropic.com"
    api_key_env: str = "ANTHROPIC_API_KEY"
    timeout_seconds: float = 120.0
```

`Settings` 增加字段 `provider: ProviderSettings = ProviderSettings()`、`anthropic: AnthropicSettings = AnthropicSettings()`；`from_env` 中对应增加：

```python
            models=ModelSettings(
                default=os.getenv("AICODE_MODEL_DEFAULT", "gpt-5"),
                planner=os.getenv("AICODE_MODEL_PLANNER", "gpt-5-high"),
                coder=os.getenv("AICODE_MODEL_CODER", "gpt-5"),
                main=os.getenv("AICODE_MODEL_MAIN", os.getenv("AICODE_MODEL_CODER", "gpt-5")),
                reviewer=os.getenv("AICODE_MODEL_REVIEWER", "gpt-5"),
                summarizer=os.getenv("AICODE_MODEL_SUMMARIZER", "gpt-5-mini"),
            ),
            provider=ProviderSettings(type=os.getenv("AICODE_PROVIDER_TYPE", "openai_compatible")),
            anthropic=AnthropicSettings(
                base_url=os.getenv("AICODE_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                api_key_env=os.getenv("AICODE_ANTHROPIC_API_KEY_ENV", "ANTHROPIC_API_KEY"),
                timeout_seconds=float(os.getenv("AICODE_ANTHROPIC_TIMEOUT_SECONDS", "120")),
            ),
```

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_provider_types.py -v && python3 -m pytest -q`
Expected: 新测试 PASS，全量 PASS（旧类型未动）

- [ ] **Step 5: Commit**

```bash
git add runtime/app/models/provider.py runtime/app/config/settings.py runtime/tests/test_provider_types.py
git commit -m "Add v2 provider types and three-role model settings"
```

---

### Task 2: OpenAI 兼容 provider（流式 + 原生 tools + 重试）

**Files:**
- Modify: `runtime/pyproject.toml`（dependencies 增加 `"httpx>=0.27"`）
- Modify: `runtime/app/models/openai_compatible.py`（追加 v2 代码，旧 `complete` 保留到 Task 13）
- Test: `runtime/tests/test_openai_stream.py`

**Interfaces:**
- Consumes: Task 1 的 `CompletionRequest/StreamEvent/ToolCallRequest/Usage/ProviderError/RETRYABLE_STATUS`
- Produces: `OpenAICompatibleProvider.stream_complete(request) -> AsyncIterator[StreamEvent]`；模块函数 `to_openai_messages(system, messages)`、`to_openai_tools(tools)`（供测试与复查）

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_openai_stream.py
import httpx
import pytest

from app.config.settings import OpenAICompatibleSettings
from app.models.openai_compatible import OpenAICompatibleProvider, to_openai_messages, to_openai_tools
from app.models.provider import CompletionRequest


def sse_bytes(*chunks: str) -> bytes:
    return "".join(f"data: {c}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"


STREAM_BODY = sse_bytes(
    '{"choices":[{"delta":{"content":"你好"}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc_1","function":{"name":"read_file","arguments":"{\\"pa"}}]}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"th\\": \\"a.py\\"}"}}]}}]}',
    '{"choices":[{"delta":{}}],"usage":{"prompt_tokens":12,"completion_tokens":5},"model":"m1"}',
)


def make_provider(handler) -> OpenAICompatibleProvider:
    settings = OpenAICompatibleSettings(base_url="https://fake.local/v1", api_key_env="FAKE_KEY")
    return OpenAICompatibleProvider(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_stream_parses_text_tool_calls_and_usage(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    provider = make_provider(lambda request: httpx.Response(200, content=STREAM_BODY))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="m1")
    events = [event async for event in provider.stream_complete(request)]
    assert [e.type for e in events] == ["text_delta", "tool_call", "done"]
    assert events[0].text == "你好"
    assert events[1].tool_call.name == "read_file"
    assert events[1].tool_call.arguments == {"path": "a.py"}
    assert events[2].usage.input_tokens == 12
    assert events[2].model == "m1"


@pytest.mark.asyncio
async def test_retries_on_retryable_status(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, content=STREAM_BODY)

    provider = make_provider(handler)
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="m1")
    events = [event async for event in provider.stream_complete(request)]
    assert calls["n"] == 3
    assert events[-1].type == "done"


def test_message_and_tool_mapping():
    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "tool_calls": [{"id": "tc_1", "name": "search", "arguments": {"query": "x"}}]},
        {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
    ]
    mapped = to_openai_messages("sys", messages)
    assert mapped[0] == {"role": "system", "content": "sys"}
    assert mapped[2]["tool_calls"][0]["function"]["name"] == "search"
    assert mapped[3] == {"role": "tool", "tool_call_id": "tc_1", "content": "result"}
    tools = to_openai_tools([{"name": "search", "description": "d", "input_schema": {"type": "object"}}])
    assert tools[0]["function"]["parameters"] == {"type": "object"}
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -c 'import httpx' ; python3 -m pytest tests/test_openai_stream.py -v`
Expected: httpx 缺失先安装（`pip install httpx`，并在 pyproject dependencies 加 `"httpx>=0.27"`）；测试 FAIL（stream_complete 不存在）

- [ ] **Step 3: 实现**

在 `runtime/app/models/openai_compatible.py` 追加（顶部补 `import asyncio`、`import httpx`，从 provider 导入新类型）：

```python
from app.models.provider import (
    RETRYABLE_STATUS,
    CompletionRequest,
    ProviderError,
    StreamEvent,
    ToolCallRequest,
    Usage,
)


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.get("content") or None}
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)},
                    }
                    for call in tool_calls
                ]
            mapped.append(entry)
        elif role == "tool":
            mapped.append({"role": "tool", "tool_call_id": message["tool_call_id"], "content": str(message.get("content") or "")})
        else:
            mapped.append({"role": "user", "content": str(message.get("content") or "")})
    return mapped


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}}
        for tool in tools
    ]
```

在 `OpenAICompatibleProvider` 类中：`__init__` 增加可选 `client: httpx.AsyncClient | None = None` 参数存为 `self._client`（None 时懒创建 `httpx.AsyncClient(timeout=self.settings.timeout_seconds)`），并追加：

```python
    async def stream_complete(self, request: CompletionRequest):
        api_key = self.api_key()
        if not api_key:
            raise ProviderError(f"missing API key env: {self.settings.api_key_env}")
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": to_openai_messages(request.system, request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = to_openai_tools(request.tools)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        url = chat_completions_url(self.settings.base_url)

        for attempt in range(3):
            yielded = False
            try:
                async with self.client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        if response.status_code in RETRYABLE_STATUS and attempt < 2:
                            raise _Retry(body)
                        raise ProviderError(f"openai-compatible HTTP {response.status_code}: {body}")
                    async for event in self._parse_stream(response, request.model):
                        yielded = True
                        yield event
                return
            except _Retry:
                await asyncio.sleep(0.5 * 2**attempt)
            except httpx.TransportError as exc:
                if yielded or attempt >= 2:
                    raise ProviderError(f"openai-compatible request failed: {exc}") from exc
                await asyncio.sleep(0.5 * 2**attempt)

    async def _parse_stream(self, response, fallback_model: str):
        pending: dict[int, dict[str, Any]] = {}
        usage = Usage()
        model = fallback_model
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            model = str(chunk.get("model") or model)
            raw_usage = chunk.get("usage")
            if isinstance(raw_usage, dict):
                usage = Usage(input_tokens=as_int(raw_usage.get("prompt_tokens")), output_tokens=as_int(raw_usage.get("completion_tokens")))
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            content = delta.get("content")
            if content:
                yield StreamEvent(type="text_delta", text=str(content))
            for raw_call in delta.get("tool_calls") or []:
                index = as_int(raw_call.get("index"))
                slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if raw_call.get("id"):
                    slot["id"] = str(raw_call["id"])
                function = raw_call.get("function") or {}
                if function.get("name"):
                    slot["name"] = str(function["name"])
                slot["arguments"] += str(function.get("arguments") or "")
        for index in sorted(pending):
            slot = pending[index]
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            yield StreamEvent(type="tool_call", tool_call=ToolCallRequest(id=slot["id"] or f"tc_{index}", name=slot["name"], arguments=arguments))
        yield StreamEvent(type="done", usage=usage, model=model)


class _Retry(Exception):
    pass
```

`self.client` 属性：

```python
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        return self._client
```

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_openai_stream.py -v && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/pyproject.toml runtime/app/models/openai_compatible.py runtime/tests/test_openai_stream.py
git commit -m "Add streaming tool-calling support to OpenAI-compatible provider"
```

---

### Task 3: Anthropic provider

**Files:**
- Create: `runtime/app/models/anthropic.py`
- Test: `runtime/tests/test_anthropic_stream.py`

**Interfaces:**
- Consumes: Task 1 类型；`settings.anthropic`（`AnthropicSettings`）
- Produces: `AnthropicProvider(settings, client=None)`，`provider_name = "anthropic"`，`is_configured()`，`stream_complete(request) -> AsyncIterator[StreamEvent]`；模块函数 `to_anthropic_messages(messages)`

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_anthropic_stream.py
import httpx
import pytest

from app.config.settings import AnthropicSettings
from app.models.anthropic import AnthropicProvider, to_anthropic_messages
from app.models.provider import CompletionRequest


def sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


STREAM_BODY = (
    sse("message_start", '{"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":9}}}')
    + sse("content_block_start", '{"type":"content_block_start","index":0,"content_block":{"type":"text"}}')
    + sse("content_block_delta", '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"好的"}}')
    + sse("content_block_stop", '{"type":"content_block_stop","index":0}')
    + sse("content_block_start", '{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu_1","name":"bash"}}')
    + sse("content_block_delta", '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\":"}}')
    + sse("content_block_delta", '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":" \\"ls\\"}"}}')
    + sse("content_block_stop", '{"type":"content_block_stop","index":1}')
    + sse("message_delta", '{"type":"message_delta","usage":{"output_tokens":7}}')
    + sse("message_stop", '{"type":"message_stop"}')
).encode()


@pytest.mark.asyncio
async def test_stream_parses_anthropic_events(monkeypatch):
    monkeypatch.setenv("FAKE_ANTHROPIC_KEY", "sk-ant")
    settings = AnthropicSettings(base_url="https://fake.local", api_key_env="FAKE_ANTHROPIC_KEY")
    provider = AnthropicProvider(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=STREAM_BODY))))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="claude-x")
    events = [event async for event in provider.stream_complete(request)]
    assert [e.type for e in events] == ["text_delta", "tool_call", "done"]
    assert events[0].text == "好的"
    assert events[1].tool_call.id == "tu_1"
    assert events[1].tool_call.arguments == {"command": "ls"}
    assert events[2].usage.input_tokens == 9
    assert events[2].usage.output_tokens == 7
    assert events[2].model == "claude-x"


def test_message_mapping_tool_roundtrip():
    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "tool_calls": [{"id": "tu_1", "name": "bash", "arguments": {"command": "ls"}}]},
        {"role": "tool", "tool_call_id": "tu_1", "content": "out"},
        {"role": "user", "content": "next"},
    ]
    mapped = to_anthropic_messages(messages)
    assert mapped[1]["content"][0] == {"type": "text", "text": "a"}
    assert mapped[1]["content"][1]["type"] == "tool_use"
    assert mapped[2]["role"] == "user"
    assert mapped[2]["content"][0]["type"] == "tool_result"
    assert mapped[2]["content"][1] == {"type": "text", "text": "next"}  # 连续 user 合并
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_anthropic_stream.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `runtime/app/models/anthropic.py`**

```python
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from app.config.settings import AnthropicSettings
from app.models.provider import (
    RETRYABLE_STATUS,
    CompletionRequest,
    ProviderError,
    StreamEvent,
    ToolCallRequest,
    Usage,
)

ANTHROPIC_VERSION = "2023-06-01"


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": str(message["content"])})
            for call in message.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]})
            mapped.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": str(message.get("content") or "")}
            if mapped and mapped[-1]["role"] == "user":
                mapped[-1]["content"].append(block)
            else:
                mapped.append({"role": "user", "content": [block]})
        else:
            block = {"type": "text", "text": str(message.get("content") or "")}
            if mapped and mapped[-1]["role"] == "user":
                mapped[-1]["content"].append(block)
            else:
                mapped.append({"role": "user", "content": [block]})
    return mapped


class AnthropicProvider:
    provider_name = "anthropic"

    def __init__(self, settings: AnthropicSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        return self._client

    def api_key(self) -> str | None:
        return os.getenv(self.settings.api_key_env)

    def is_configured(self) -> bool:
        return bool(self.api_key())

    async def stream_complete(self, request: CompletionRequest):
        api_key = self.api_key()
        if not api_key:
            raise ProviderError(f"missing API key env: {self.settings.api_key_env}")
        payload: dict[str, Any] = {
            "model": request.model,
            "system": request.system,
            "messages": to_anthropic_messages(request.messages),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
        }
        if request.tools:
            payload["tools"] = request.tools  # canonical schema 与 Anthropic 格式一致
        headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}
        url = self.settings.base_url.rstrip("/") + "/v1/messages"

        for attempt in range(3):
            yielded = False
            try:
                async with self.client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        if response.status_code in RETRYABLE_STATUS and attempt < 2:
                            raise _Retry(body)
                        raise ProviderError(f"anthropic HTTP {response.status_code}: {body}")
                    async for event in self._parse_stream(response, request.model):
                        yielded = True
                        yield event
                return
            except _Retry:
                await asyncio.sleep(0.5 * 2**attempt)
            except httpx.TransportError as exc:
                if yielded or attempt >= 2:
                    raise ProviderError(f"anthropic request failed: {exc}") from exc
                await asyncio.sleep(0.5 * 2**attempt)

    async def _parse_stream(self, response, fallback_model: str):
        usage = Usage()
        model = fallback_model
        current_tool: dict[str, str] | None = None
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            chunk = json.loads(line[len("data: "):])
            kind = chunk.get("type")
            if kind == "message_start":
                message = chunk.get("message") or {}
                model = str(message.get("model") or model)
                usage.input_tokens = int((message.get("usage") or {}).get("input_tokens") or 0)
            elif kind == "content_block_start":
                block = chunk.get("content_block") or {}
                if block.get("type") == "tool_use":
                    current_tool = {"id": str(block.get("id") or ""), "name": str(block.get("name") or ""), "json": ""}
            elif kind == "content_block_delta":
                delta = chunk.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield StreamEvent(type="text_delta", text=str(delta["text"]))
                elif delta.get("type") == "input_json_delta" and current_tool is not None:
                    current_tool["json"] += str(delta.get("partial_json") or "")
            elif kind == "content_block_stop" and current_tool is not None:
                try:
                    arguments = json.loads(current_tool["json"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                yield StreamEvent(type="tool_call", tool_call=ToolCallRequest(id=current_tool["id"], name=current_tool["name"], arguments=arguments))
                current_tool = None
            elif kind == "message_delta":
                usage.output_tokens = int((chunk.get("usage") or {}).get("output_tokens") or usage.output_tokens)
        yield StreamEvent(type="done", usage=usage, model=model)


class _Retry(Exception):
    pass
```

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_anthropic_stream.py -v && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/app/models/anthropic.py runtime/tests/test_anthropic_stream.py
git commit -m "Add Anthropic streaming provider"
```

---

### Task 4: ModelRouter v2（三角色 + 流式聚合）+ FakeProvider 测试基建

**Files:**
- Modify: `runtime/app/models/router.py`（追加 v2 方法，保留旧 complete 到 Task 13）
- Create: `runtime/tests/fakes.py`
- Test: `runtime/tests/test_router_v2.py`

**Interfaces:**
- Consumes: Task 1-3 的 provider 类型与实现；`estimate_cost`（现有 `app.usage.pricing`）
- Produces: `ModelRouter.stream_complete(*, purpose, system, messages, tools=(), on_text_delta=None, temperature=0.2, max_tokens=8192) -> CompletionResult`；`ModelRouter.from_settings` 按 `settings.provider.type` 选择 primary；`tests/fakes.py` 的 `FakeProvider(turns)`（turns 为 `list[list[StreamEvent]]`，每次调用弹出一组）

- [ ] **Step 1: 写测试基建与失败测试**

```python
# runtime/tests/fakes.py
from __future__ import annotations

from app.models.provider import CompletionRequest, StreamEvent, ToolCallRequest, Usage


class FakeProvider:
    provider_name = "fake"

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self.turns = list(turns)
        self.calls: list[CompletionRequest] = []

    def is_configured(self) -> bool:
        return True

    async def stream_complete(self, request: CompletionRequest):
        self.calls.append(request)
        for event in self.turns.pop(0):
            yield event


def text_turn(text: str, input_tokens: int = 10, output_tokens: int = 5) -> list[StreamEvent]:
    return [
        StreamEvent(type="text_delta", text=text),
        StreamEvent(type="done", usage=Usage(input_tokens, output_tokens), model="fake-model"),
    ]


def tool_turn(name: str, arguments: dict, call_id: str = "tc_1", text: str = "") -> list[StreamEvent]:
    events = []
    if text:
        events.append(StreamEvent(type="text_delta", text=text))
    events.append(StreamEvent(type="tool_call", tool_call=ToolCallRequest(id=call_id, name=name, arguments=arguments)))
    events.append(StreamEvent(type="done", usage=Usage(10, 5), model="fake-model"))
    return events
```

```python
# runtime/tests/test_router_v2.py
import pytest

from app.config.settings import Settings
from app.models.router import ModelRouter
from app.models.provider import ProviderNotConfigured
from tests.fakes import FakeProvider, text_turn, tool_turn


def make_router(turns) -> tuple[ModelRouter, FakeProvider]:
    settings = Settings()
    fake = FakeProvider(turns)
    router = ModelRouter(primary=fake, fallback=fake, settings=settings)
    return router, fake


@pytest.mark.asyncio
async def test_stream_complete_aggregates_and_calls_delta():
    router, fake = make_router([tool_turn("bash", {"command": "ls"}, text="先看目录")])
    deltas = []

    async def on_delta(text):
        deltas.append(text)

    result = await router.stream_complete(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], on_text_delta=on_delta)
    assert result.text == "先看目录"
    assert deltas == ["先看目录"]
    assert result.tool_calls[0].name == "bash"
    assert result.input_tokens == 10
    assert fake.calls[0].model == Settings().models.main


@pytest.mark.asyncio
async def test_purpose_routes_model():
    router, fake = make_router([text_turn("ok")])
    await router.stream_complete(purpose="summarizer", system="s", messages=[{"role": "user", "content": "hi"}])
    assert fake.calls[0].model == Settings().models.summarizer


@pytest.mark.asyncio
async def test_not_configured_raises():
    router, fake = make_router([text_turn("ok")])
    fake.is_configured = lambda: False
    with pytest.raises(ProviderNotConfigured):
        await router.stream_complete(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}])


def test_from_settings_selects_anthropic(monkeypatch):
    monkeypatch.setenv("AICODE_PROVIDER_TYPE", "anthropic")
    router = ModelRouter.from_settings(Settings.from_env())
    assert router.primary.provider_name == "anthropic"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_router_v2.py -v`
Expected: FAIL（stream_complete 不存在）

- [ ] **Step 3: 实现**

`runtime/app/models/router.py` 追加导入与方法：

```python
from collections.abc import Awaitable, Callable

from app.models.anthropic import AnthropicProvider
from app.models.provider import (
    CompletionRequest,
    CompletionResult,
    ProviderNotConfigured,
    ToolCallRequest,
    Usage,
)
```

`from_settings` 修改为按类型选择 primary：

```python
    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRouter":
        if settings.provider.type == "anthropic":
            primary: ModelProvider = AnthropicProvider(settings.anthropic)
        else:
            primary = OpenAICompatibleProvider(settings.openai_compatible)
        return cls(primary=primary, fallback=StubProvider(), settings=settings)
```

追加方法：

```python
    def model_for_purpose_v2(self, purpose: str) -> str:
        if purpose == "reviewer":
            return self.settings.models.reviewer
        if purpose == "summarizer":
            return self.settings.models.summarizer
        return self.settings.models.main

    async def stream_complete(
        self,
        *,
        purpose: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | tuple = (),
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> CompletionResult:
        is_configured = getattr(self.primary, "is_configured", None)
        if callable(is_configured) and not is_configured():
            raise ProviderNotConfigured("模型 provider 未配置，请设置 API key 后重试")
        request = CompletionRequest(
            purpose=purpose,
            system=system,
            messages=messages,
            tools=list(tools),
            model=self.model_for_purpose_v2(purpose),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        usage = Usage()
        model_name = request.model
        async for event in self.primary.stream_complete(request):
            if event.type == "text_delta":
                text_parts.append(event.text)
                if on_text_delta is not None:
                    await on_text_delta(event.text)
            elif event.type == "tool_call" and event.tool_call is not None:
                tool_calls.append(event.tool_call)
            elif event.type == "done":
                usage = event.usage or usage
                model_name = event.model or model_name
        provider_name = getattr(self.primary, "provider_name", self.primary.__class__.__name__)
        return CompletionResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            model=model_name,
            provider=provider_name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost=estimate_cost(
                provider=provider_name,
                model=model_name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                prices=self.settings.pricing.model_prices,
            ),
        )
```

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_router_v2.py -v && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/app/models/router.py runtime/tests/fakes.py runtime/tests/test_router_v2.py
git commit -m "Add three-role streaming completion to model router"
```

---

### Task 5: PolicyEngine v2 gate（三态互斥分级，修复 sed/git 漏洞）

**Files:**
- Modify: `runtime/app/policy/engine.py`（追加 `GateDecision` 与 `gate()`，旧 `evaluate` 保留到 Task 13）
- Test: `runtime/tests/test_policy_gate.py`

**Interfaces:**
- Produces: `GateDecision(verdict, risk_level, reason)`，verdict ∈ `"allow" | "ask" | "deny"`；`PolicyEngine.gate(tool_name, args, mode="default") -> GateDecision`；常量 `READ_ONLY_TOOLS_V2 = {"read_file", "search", "list_files", "review_diff"}`

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_policy_gate.py
import pytest

from app.policy.engine import PolicyEngine


@pytest.fixture
def engine():
    return PolicyEngine()


def gate_bash(engine, command, mode="default"):
    return engine.gate("bash", {"command": command}, mode=mode)


def test_read_only_tools_allowed_in_review(engine):
    assert engine.gate("read_file", {"path": "a.py"}, mode="review").verdict == "allow"
    assert engine.gate("search", {"query": "x"}, mode="review").verdict == "allow"


def test_write_tools_denied_in_review(engine):
    assert engine.gate("edit_file", {"path": "a.py"}, mode="review").verdict == "deny"
    assert gate_bash(engine, "ls", mode="review").verdict == "deny"


def test_edit_file_always_asks(engine):
    assert engine.gate("edit_file", {"path": "a.py"}).verdict == "ask"


def test_low_risk_commands_allowed(engine):
    for command in ["ls", "pwd", "rg pattern", "cat a.py", "git status", "git diff", "git log -5", "pytest", "go test ./...", "python3 -m pytest"]:
        assert gate_bash(engine, command).verdict == "allow", command


def test_sed_requires_confirmation(engine):
    assert gate_bash(engine, "sed -i 's/a/b/' file.py").verdict == "ask"


def test_git_push_requires_confirmation(engine):
    assert gate_bash(engine, "git push origin main").verdict == "ask"
    assert gate_bash(engine, "git commit -m x").verdict == "ask"


def test_destructive_commands_denied(engine):
    for command in [
        "rm -rf /",
        "sudo ls",
        "git reset --hard",
        "git checkout -- .",
        "git clean -fd",
        "git rebase main",
        "git push --force origin main",
        "git push -f origin main",
        "git branch -D feature",
        "git stash drop",
    ]:
        assert gate_bash(engine, command).verdict == "deny", command


def test_control_tokens_ask(engine):
    assert gate_bash(engine, "cat a.py | head").verdict == "ask"
    assert gate_bash(engine, "echo hi > out.txt").verdict == "ask"


def test_unknown_command_asks(engine):
    assert gate_bash(engine, "docker build .").verdict == "ask"
    assert gate_bash(engine, "npm install").verdict == "ask"


def test_unknown_tool_denied(engine):
    assert engine.gate("mystery", {}).verdict == "deny"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_policy_gate.py -v`
Expected: FAIL（gate 不存在）

- [ ] **Step 3: 实现**

在 `runtime/app/policy/engine.py` 追加（文件顶部已 import shlex/dataclass）：

```python
READ_ONLY_TOOLS_V2 = {"read_file", "search", "list_files", "review_diff"}

DENY_EXECUTABLES = {"rm", "sudo", "su", "shutdown", "reboot", "mkfs", "dd"}
ALLOW_EXECUTABLES = {"pwd", "ls", "rg", "grep", "head", "tail", "wc", "cat", "which", "echo"}
ALLOW_GIT_SUBCOMMANDS = {"status", "diff", "show", "log", "blame", "rev-parse"}
DENY_GIT_SUBCOMMANDS = {"reset", "clean", "rebase"}
CONTROL_TOKENS = {"|", "&&", "||", ";", ">", ">>", "<", "$(", "`"}


@dataclass(slots=True)
class GateDecision:
    verdict: str  # allow | ask | deny
    risk_level: str
    reason: str = ""
```

在 `PolicyEngine` 类中追加方法：

```python
    def gate(self, tool_name: str, args: dict[str, Any], mode: str = "default") -> GateDecision:
        if tool_name in READ_ONLY_TOOLS_V2:
            return GateDecision("allow", "low")
        if mode == "review":
            return GateDecision("deny", "high", "review 模式只允许只读工具")
        if tool_name == "edit_file":
            return GateDecision("ask", "medium", "文件写入需要 inline diff 确认")
        if tool_name == "bash":
            return self.gate_bash(str(args.get("command", "")))
        return GateDecision("deny", "high", f"未知工具: {tool_name}")

    def gate_bash(self, command: str) -> GateDecision:
        command = command.strip()
        if not command:
            return GateDecision("deny", "low", "空命令")
        if any(token in command for token in CONTROL_TOKENS):
            return GateDecision("ask", "high", "包含 shell 控制符，需要确认后执行")
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return GateDecision("deny", "high", str(exc))
        if not parts:
            return GateDecision("deny", "low", "空命令")
        executable = parts[0]
        if executable in DENY_EXECUTABLES:
            return GateDecision("deny", "high", f"禁止执行高风险命令: {executable}")
        if executable == "git":
            return self._gate_git(parts)
        if self._is_low_risk_test(parts):
            return GateDecision("allow", "low")
        if executable in ALLOW_EXECUTABLES:
            return GateDecision("allow", "low")
        return GateDecision("ask", "medium", f"命令需要确认后执行: {executable}")

    def _gate_git(self, parts: list[str]) -> GateDecision:
        subcommand = parts[1] if len(parts) > 1 else ""
        if subcommand in DENY_GIT_SUBCOMMANDS:
            return GateDecision("deny", "high", f"禁止执行破坏性 git 命令: git {subcommand}")
        if subcommand == "checkout" and "--" in parts:
            return GateDecision("deny", "high", "禁止 git checkout -- 丢弃改动")
        if subcommand == "push" and any(flag in parts for flag in ("--force", "-f", "--force-with-lease", "--delete")):
            return GateDecision("deny", "high", "禁止强制/删除式 git push")
        if subcommand == "branch" and any(flag in parts for flag in ("-D", "-d", "-M", "-m")):
            return GateDecision("deny", "high", "禁止删除/重命名分支")
        if subcommand == "stash" and "drop" in parts:
            return GateDecision("deny", "high", "禁止 git stash drop")
        if subcommand in ALLOW_GIT_SUBCOMMANDS:
            return GateDecision("allow", "low")
        return GateDecision("ask", "medium", f"git {subcommand} 需要确认后执行")
```

`_is_low_risk_test` 复用现有 `_is_low_risk_test`（已存在于类中，签名一致，无需改动）。

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_policy_gate.py -v && python3 -m pytest -q`
Expected: PASS（旧 evaluate 与测试不受影响）

- [ ] **Step 5: Commit**

```bash
git add runtime/app/policy/engine.py runtime/tests/test_policy_gate.py
git commit -m "Add three-verdict policy gate with git and sed hardening"
```

---

### Task 6: 工具 registry（schema + 分发 + read_file/search/list_files/review_diff）

**Files:**
- Create: `runtime/app/tools/registry.py`
- Test: `runtime/tests/test_registry.py`

**Interfaces:**
- Consumes: `app.tools.base` 的 `ToolContext/ToolResult/ToolError/resolve_tool_workspace/resolve_workspace_path/reject_protected_path/is_protected_path/scoped_display_path/display_path`；`ListFilesTool`（`app.tools.file`）；`ReviewDiffTool`（`app.tools.review`）
- Produces: `TOOL_SCHEMAS: list[dict]`（canonical schema）；`tool_schemas_for_mode(mode) -> list[dict]`（review 模式仅只读工具）；`build_tool_context(workspace, mode, language) -> ToolContext`；`async run_tool(name, arguments, context) -> ToolResult`。`bash`/`edit_file` 的执行在 Task 7/8 补上，本 task 中先注册 schema、执行返回"未实现"错误。

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_registry.py
import pytest

from app.tools.base import ToolContext
from app.tools.registry import TOOL_SCHEMAS, run_tool, tool_schemas_for_mode


def make_context(tmp_path) -> ToolContext:
    return ToolContext(workspace=tmp_path)


def test_schema_names_and_modes():
    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert names == {"read_file", "search", "list_files", "bash", "edit_file", "review_diff"}
    review_names = {schema["name"] for schema in tool_schemas_for_mode("review")}
    assert review_names == {"read_file", "search", "list_files", "review_diff"}
    for schema in TOOL_SCHEMAS:
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_read_file_returns_numbered_lines(tmp_path):
    (tmp_path / "a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    result = await run_tool("read_file", {"path": "a.py", "offset": 2, "limit": 1}, make_context(tmp_path))
    assert result.success
    assert "2\tline2" in result.text
    assert "line1" not in result.text
    assert "共 3 行" in result.text


@pytest.mark.asyncio
async def test_read_file_missing(tmp_path):
    result = await run_tool("read_file", {"path": "nope.py"}, make_context(tmp_path))
    assert not result.success
    assert "不存在" in result.error


@pytest.mark.asyncio
async def test_read_file_protected(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    result = await run_tool("read_file", {"path": ".env"}, make_context(tmp_path))
    assert not result.success


@pytest.mark.asyncio
async def test_search_finds_matches(tmp_path):
    (tmp_path / "a.py").write_text("def login():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("login docs\n", encoding="utf-8")
    result = await run_tool("search", {"query": "login", "glob": "*.py"}, make_context(tmp_path))
    assert result.success
    assert "a.py" in result.text
    assert "b.md" not in result.text


@pytest.mark.asyncio
async def test_search_no_match(tmp_path):
    (tmp_path / "a.py").write_text("nothing here\n", encoding="utf-8")
    result = await run_tool("search", {"query": "zzz_not_found"}, make_context(tmp_path))
    assert result.success
    assert "没有匹配" in result.text


@pytest.mark.asyncio
async def test_unknown_tool(tmp_path):
    result = await run_tool("mystery", {}, make_context(tmp_path))
    assert not result.success
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_registry.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `runtime/app/tools/registry.py`**

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.project.config import load_project_config
from app.tools.base import (
    ToolContext,
    ToolError,
    ToolResult,
    is_protected_path,
    reject_protected_path,
    resolve_tool_workspace,
    resolve_workspace_path,
    scoped_display_path,
)
from app.tools.file import ListFilesTool
from app.tools.review import ReviewDiffTool

MAX_READ_LINES = 500
DEFAULT_READ_LINES = 200
MAX_SEARCH_RESULTS = 40

WORKSPACE_ARG = {"type": "string", "description": "可选：配置的只读 workspace 名称，默认主 workspace"}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "读取文本文件，按行号返回。文件较大时用 offset/limit 分段继续读。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "offset": {"type": "integer", "description": "起始行号，从 1 开始", "default": 1},
                "limit": {"type": "integer", "description": f"读取行数，默认 {DEFAULT_READ_LINES}，最大 {MAX_READ_LINES}"},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": "在代码库中用正则搜索文本（ripgrep）。定位符号、字符串、文件时优先用它。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "正则表达式"},
                "glob": {"type": "string", "description": "可选：文件过滤，如 *.py 或 src/**"},
                "limit": {"type": "integer", "default": MAX_SEARCH_RESULTS},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_files",
        "description": "列出目录结构。只在需要了解目录布局时使用，找具体内容用 search。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "max_depth": {"type": "integer", "default": 2},
                "workspace": WORKSPACE_ARG,
            },
        },
    },
    {
        "name": "bash",
        "description": "在主 workspace 执行 shell 命令（git、测试、构建等）。低风险命令直接执行；中风险需要用户确认；破坏性命令会被拒绝。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "秒，默认 120", "default": 120},
            },
            "required": ["command"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "编辑主 workspace 的文件，用户确认 diff 后生效。"
            "replace：old_text 必须是文件中完整且唯一的原文片段，new_text 为替换内容；"
            "create：文件不存在时 old_text 留空、new_text 为完整内容；"
            "delete：delete 参数设为 true。每次编辑独立确认。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "default": ""},
                "new_text": {"type": "string", "default": ""},
                "delete": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
    },
    {
        "name": "review_diff",
        "description": "对当前 git diff 运行确定性 review 规则，输出结构化 finding。",
        "input_schema": {"type": "object", "properties": {}},
    },
]

READ_ONLY_TOOL_NAMES = {"read_file", "search", "list_files", "review_diff"}


def tool_schemas_for_mode(mode: str) -> list[dict[str, Any]]:
    if mode == "review":
        return [schema for schema in TOOL_SCHEMAS if schema["name"] in READ_ONLY_TOOL_NAMES]
    return list(TOOL_SCHEMAS)


def build_tool_context(workspace: str, mode: str, language: str) -> ToolContext:
    project_config = load_project_config(Path(workspace))
    return ToolContext(
        workspace=Path(workspace),
        mode=mode,
        language=language,
        protected_paths=project_config.protected_paths,
        workspace_refs=project_config.workspaces,
        review_disabled_rules=project_config.review.disabled_rules,
        review_large_diff_threshold=project_config.review.large_diff_threshold,
        review_max_findings=project_config.review.max_findings,
    )


async def run_tool(name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        if name == "read_file":
            return read_file_lines(context, arguments)
        if name == "search":
            return await run_search(context, arguments)
        if name == "list_files":
            return await ListFilesTool().run(arguments, context)
        if name == "review_diff":
            return await ReviewDiffTool().run(arguments, context)
        if name == "bash":
            return await run_bash(context, arguments)
        if name == "edit_file":
            return ToolResult(success=False, error="edit_file 由 agent loop 单独处理", risk_level="medium")
        return ToolResult(success=False, error=f"未知工具: {name}", risk_level="high")
    except ToolError as exc:
        return ToolResult(success=False, error=str(exc))
    except Exception as exc:  # 工具异常回给模型，不中断循环
        return ToolResult(success=False, error=f"{exc.__class__.__name__}: {exc}", risk_level="high")


def read_file_lines(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    root, workspace_name = resolve_tool_workspace(context, arguments.get("workspace"))
    target = resolve_workspace_path(root, str(arguments.get("path") or ""))
    reject_protected_path(root, target, context.protected_paths)
    if not target.is_file():
        raise ToolError(f"文件不存在: {arguments.get('path')}")
    offset = max(1, int(arguments.get("offset") or 1))
    limit = max(1, min(int(arguments.get("limit") or DEFAULT_READ_LINES), MAX_READ_LINES))
    lines = target.read_text("utf-8", errors="replace").splitlines()
    chunk = lines[offset - 1 : offset - 1 + limit]
    shown = "\n".join(f"{offset + index}\t{line}" for index, line in enumerate(chunk))
    label = scoped_display_path(workspace_name, root, target)
    end = offset + len(chunk) - 1
    header = f"{label} 共 {len(lines)} 行，显示第 {offset}-{end} 行"
    if end < len(lines):
        header += f"（未完，可用 offset={end + 1} 继续读）"
    return ToolResult(success=True, text=f"{header}\n{shown}", data={"path": label, "total_lines": len(lines), "offset": offset, "shown": len(chunk)})


async def run_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    root, workspace_name = resolve_tool_workspace(context, arguments.get("workspace"))
    query = str(arguments.get("query") or "")
    if not query:
        raise ToolError("query 不能为空")
    limit = max(1, min(int(arguments.get("limit") or MAX_SEARCH_RESULTS), MAX_SEARCH_RESULTS))
    command = ["rg", "--line-number", "--no-heading", "--max-count", "5", "-e", query]
    glob = str(arguments.get("glob") or "")
    if glob:
        command += ["--glob", glob]
    process = await asyncio.create_subprocess_exec(
        *command, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode not in (0, 1):
        raise ToolError(f"rg 失败: {stderr.decode(errors='replace')[:500]}")
    matches = []
    for line in stdout.decode(errors="replace").splitlines():
        rel_path = line.split(":", 1)[0]
        if is_protected_path(rel_path, context.protected_paths):
            continue
        matches.append(line)
        if len(matches) >= limit:
            break
    prefix = f"{workspace_name}: " if workspace_name else ""
    if not matches:
        return ToolResult(success=True, text=f"{prefix}没有匹配: {query}", data={"query": query, "matches": 0})
    return ToolResult(success=True, text=f"{prefix}匹配 {len(matches)} 处:\n" + "\n".join(matches), data={"query": query, "matches": len(matches)})


async def run_bash(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return ToolResult(success=False, error="bash 将在 Task 7 实现", risk_level="medium")
```

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_registry.py -v && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/app/tools/registry.py runtime/tests/test_registry.py
git commit -m "Add v2 tool registry with schemas and read/search dispatch"
```

---

### Task 7: bash 工具实现

**Files:**
- Modify: `runtime/app/tools/registry.py`（替换 `run_bash` 占位实现）
- Test: `runtime/tests/test_registry_bash.py`

**Interfaces:**
- Produces: `run_bash(context, arguments) -> ToolResult`：`cwd` 为主 workspace；输出合并 stdout/stderr；超时 kill 并报错；`text` 以 `exit=N` 开头

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_registry_bash.py
import pytest

from app.tools.base import ToolContext
from app.tools.registry import run_tool


@pytest.mark.asyncio
async def test_bash_runs_command(tmp_path):
    (tmp_path / "hello.txt").write_text("x", encoding="utf-8")
    result = await run_tool("bash", {"command": "ls"}, ToolContext(workspace=tmp_path))
    assert result.success
    assert result.text.startswith("exit=0")
    assert "hello.txt" in result.text


@pytest.mark.asyncio
async def test_bash_nonzero_exit(tmp_path):
    result = await run_tool("bash", {"command": "false"}, ToolContext(workspace=tmp_path))
    assert not result.success
    assert "exit=1" in result.text


@pytest.mark.asyncio
async def test_bash_timeout(tmp_path):
    result = await run_tool("bash", {"command": "sleep 5", "timeout": 1}, ToolContext(workspace=tmp_path))
    assert not result.success
    assert "超时" in result.error
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_registry_bash.py -v`
Expected: FAIL（占位实现返回未实现错误）

- [ ] **Step 3: 实现（替换 registry.py 中的 `run_bash`）**

```python
async def run_bash(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    command = str(arguments.get("command") or "").strip()
    if not command:
        raise ToolError("command 不能为空")
    timeout = max(1, min(int(arguments.get("timeout") or 120), 600))
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=context.workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ToolResult(success=False, error=f"命令超时（{timeout}s）: {command}", risk_level="medium", data={"command": command})
    output = stdout.decode(errors="replace")
    text = f"exit={process.returncode}\n{output}".rstrip()
    return ToolResult(
        success=process.returncode == 0,
        text=text,
        error="" if process.returncode == 0 else text,
        data={"command": command, "exit_code": process.returncode},
    )
```

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_registry_bash.py -v && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/app/tools/registry.py runtime/tests/test_registry_bash.py
git commit -m "Implement bash tool with timeout and merged output"
```

---

### Task 8: edit_file（提议/应用 + 单文件 stale 检测）

**Files:**
- Create: `runtime/app/tools/edit.py`
- Test: `runtime/tests/test_edit.py`

**Interfaces:**
- Consumes: `app.tools.base` 路径与保护工具
- Produces:
  - `EditProposal(path, kind, diff, new_content, base_hash)`：`path` 为相对显示路径；`kind` ∈ `"create" | "replace" | "delete"`；`new_content` 删除时为 `None`；`base_hash` 创建时为 `None`
  - `EditError(Exception)`、`EditStaleError(EditError)`
  - `build_edit_proposal(workspace: Path, arguments: dict, protected_paths: list[str]) -> EditProposal`
  - `apply_edit(workspace: Path, proposal: EditProposal) -> None`（应用前重新校验 base_hash，不一致抛 `EditStaleError`）
  - `file_hash(path: Path) -> str`

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_edit.py
import pytest

from app.tools.edit import EditError, EditStaleError, apply_edit, build_edit_proposal


def test_replace_generates_diff_and_applies(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    proposal = build_edit_proposal(tmp_path, {"path": "a.py", "old_text": "return 1", "new_text": "return 2"}, [])
    assert proposal.kind == "replace"
    assert "-    return 1" in proposal.diff
    assert "+    return 2" in proposal.diff
    apply_edit(tmp_path, proposal)
    assert target.read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_replace_requires_unique_match(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(EditError, match="唯一"):
        build_edit_proposal(tmp_path, {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, [])


def test_replace_missing_old_text(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(EditError, match="未找到"):
        build_edit_proposal(tmp_path, {"path": "a.py", "old_text": "not there", "new_text": "y"}, [])


def test_create_new_file(tmp_path):
    proposal = build_edit_proposal(tmp_path, {"path": "new/b.py", "new_text": "print(1)\n"}, [])
    assert proposal.kind == "create"
    apply_edit(tmp_path, proposal)
    assert (tmp_path / "new" / "b.py").read_text(encoding="utf-8") == "print(1)\n"


def test_create_existing_file_rejected(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    with pytest.raises(EditError, match="old_text"):
        build_edit_proposal(tmp_path, {"path": "a.py", "new_text": "y"}, [])


def test_delete_file(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x", encoding="utf-8")
    proposal = build_edit_proposal(tmp_path, {"path": "a.py", "delete": True}, [])
    assert proposal.kind == "delete"
    apply_edit(tmp_path, proposal)
    assert not target.exists()


def test_stale_detection(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    proposal = build_edit_proposal(tmp_path, {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, [])
    target.write_text("x = 999\n", encoding="utf-8")  # 外部修改
    with pytest.raises(EditStaleError):
        apply_edit(tmp_path, proposal)


def test_protected_path_rejected(tmp_path):
    with pytest.raises(EditError):
        build_edit_proposal(tmp_path, {"path": ".env", "new_text": "SECRET=1"}, [".env"])
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_edit.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `runtime/app/tools/edit.py`**

```python
from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.tools.base import ToolError, display_path, is_protected_path, resolve_workspace_path


class EditError(Exception):
    pass


class EditStaleError(EditError):
    pass


@dataclass(slots=True)
class EditProposal:
    path: str
    kind: str  # create | replace | delete
    diff: str
    new_content: str | None
    base_hash: str | None


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_edit_proposal(workspace: Path, arguments: dict, protected_paths: list[str]) -> EditProposal:
    raw_path = str(arguments.get("path") or "").strip()
    if not raw_path:
        raise EditError("path 不能为空")
    try:
        target = resolve_workspace_path(workspace, raw_path)
    except ToolError as exc:
        raise EditError(str(exc)) from exc
    rel = display_path(workspace, target)
    if is_protected_path(rel, protected_paths):
        raise EditError(f"受保护路径不可修改: {rel}")

    old_text = str(arguments.get("old_text") or "")
    new_text = str(arguments.get("new_text") or "")
    delete = bool(arguments.get("delete"))

    if delete:
        if not target.is_file():
            raise EditError(f"文件不存在，无法删除: {rel}")
        original = target.read_text("utf-8", errors="replace")
        diff = unified_diff(original, "", rel)
        return EditProposal(path=rel, kind="delete", diff=diff, new_content=None, base_hash=file_hash(target))

    if not target.exists():
        if old_text:
            raise EditError(f"文件不存在: {rel}")
        if not new_text:
            raise EditError("创建文件时 new_text 不能为空")
        diff = unified_diff("", new_text, rel)
        return EditProposal(path=rel, kind="create", diff=diff, new_content=new_text, base_hash=None)

    if not target.is_file():
        raise EditError(f"不是普通文件: {rel}")
    if not old_text:
        raise EditError("修改已有文件必须提供 old_text（文件中完整且唯一的原文片段）")
    original = target.read_text("utf-8", errors="replace")
    count = original.count(old_text)
    if count == 0:
        raise EditError(f"未找到 old_text，请先 read_file 确认原文: {rel}")
    if count > 1:
        raise EditError(f"old_text 在文件中出现 {count} 次，不唯一，请扩大片段范围: {rel}")
    updated = original.replace(old_text, new_text, 1)
    diff = unified_diff(original, updated, rel)
    return EditProposal(path=rel, kind="replace", diff=diff, new_content=updated, base_hash=file_hash(target))


def apply_edit(workspace: Path, proposal: EditProposal) -> None:
    target = resolve_workspace_path(workspace, proposal.path)
    if proposal.kind == "create":
        if target.exists():
            raise EditStaleError(f"文件已被外部创建: {proposal.path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(proposal.new_content or "", encoding="utf-8")
        return
    if not target.is_file():
        raise EditStaleError(f"文件已被外部删除: {proposal.path}")
    if proposal.base_hash and file_hash(target) != proposal.base_hash:
        raise EditStaleError(f"文件已被外部修改，请重新 read_file 后再试: {proposal.path}")
    if proposal.kind == "delete":
        target.unlink()
        return
    target.write_text(proposal.new_content or "", encoding="utf-8")


def unified_diff(original: str, updated: str, rel_path: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )
```

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_edit.py -v && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/app/tools/edit.py runtime/tests/test_edit.py
git commit -m "Add edit_file proposal and apply with stale detection"
```

---

### Task 9: turn.py + prompts.py（消息构造与 system prompt）

**Files:**
- Create: `runtime/app/agent/turn.py`
- Create: `runtime/app/agent/prompts.py`
- Test: `runtime/tests/test_turn_prompts.py`

**Interfaces:**
- Consumes: `CompletionResult/ToolCallRequest`（Task 1）；`load_project_config`、`detect_test_command`（现有 `app.project`）
- Produces:
  - `TurnBudget(max_steps=40, max_tokens_per_call=8192)`
  - `user_message(text) -> dict`、`assistant_message(result: CompletionResult) -> dict`、`tool_message(tool_call_id, content) -> dict`、`user_note(text) -> dict`（内容加 `[系统提示]` 前缀的 user 消息）
  - `build_system_prompt(request) -> str`（内部调用 `load_project_info`）；`VERIFY_NOTE_ZH/EN`、`BUDGET_NOTE_ZH/EN` 常量

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_turn_prompts.py
import json

from app.agent.prompts import build_system_prompt
from app.agent.turn import TurnBudget, assistant_message, tool_message, user_message, user_note
from app.models.provider import CompletionResult, ToolCallRequest


class FakeRequest:
    def __init__(self, workspace, mode="default", language="zh-CN", message="做点事"):
        self.workspace = str(workspace)
        self.mode = mode
        self.language = language
        self.message = message


def test_message_builders():
    assert user_message("hi") == {"role": "user", "content": "hi"}
    result = CompletionResult(text="t", tool_calls=[ToolCallRequest(id="tc_1", name="bash", arguments={"command": "ls"})], model="m", provider="p")
    message = assistant_message(result)
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["name"] == "bash"
    json.dumps(message)  # 必须可序列化
    assert tool_message("tc_1", "out") == {"role": "tool", "tool_call_id": "tc_1", "content": "out"}
    assert user_note("请验证")["content"].startswith("[系统提示]")


def test_budget_defaults():
    budget = TurnBudget()
    assert budget.max_steps == 40


def test_system_prompt_includes_project_info(tmp_path):
    (tmp_path / ".aicode").mkdir()
    (tmp_path / ".aicode" / "config.json").write_text('{"commands": {"test": "pytest -q"}, "protectedPaths": [".env"]}', encoding="utf-8")
    (tmp_path / ".aicode" / "rules.md").write_text("永远写中文注释", encoding="utf-8")
    prompt = build_system_prompt(FakeRequest(tmp_path))
    assert "pytest -q" in prompt
    assert ".env" in prompt
    assert "永远写中文注释" in prompt
    assert "中文" in prompt  # 语言指令


def test_system_prompt_review_mode(tmp_path):
    prompt = build_system_prompt(FakeRequest(tmp_path, mode="review"))
    assert "只读" in prompt
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_turn_prompts.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`runtime/app/agent/turn.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.provider import CompletionResult


@dataclass(slots=True)
class TurnBudget:
    max_steps: int = 40
    max_tokens_per_call: int = 8192


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def user_note(text: str) -> dict[str, Any]:
    return {"role": "user", "content": f"[系统提示] {text}"}


def assistant_message(result: CompletionResult) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": result.text}
    if result.tool_calls:
        message["tool_calls"] = [call.to_dict() for call in result.tool_calls]
    return message


def tool_message(tool_call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
```

`runtime/app/agent/prompts.py`：

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.project.config import load_project_config
from app.project.detect import detect_test_command

VERIFY_NOTE_ZH = "编辑已应用。请运行相关测试或命令验证改动；如果验证失败请继续修复，连续 3 次修复失败请停止并汇报现状。"
VERIFY_NOTE_EN = "Edits applied. Run relevant tests to verify; keep fixing on failure, stop and report after 3 consecutive failed attempts."
BUDGET_NOTE_ZH = "已达到本轮步数上限。请立即停止调用工具，总结目前完成了什么、剩余什么。"
BUDGET_NOTE_EN = "Step limit reached. Stop calling tools now and summarize what is done and what remains."

MODE_INSTRUCTIONS_ZH = {
    "review": "当前是 review 模式：你只有只读工具，禁止任何修改建议之外的写入动作。审查当前 git diff（可用 review_diff 获取规则结果），输出结构化审查结论。",
    "diff": "查看当前 git diff（bash: git diff）并总结变更要点。",
    "test": "发现并运行本项目的测试命令，报告结果；如有失败，定位原因。",
    "explain": "解释用户指定的文件或符号，先定位再阅读，不要修改任何文件。",
}


def build_system_prompt(request: Any) -> str:
    language_line = (
        "所有面向用户的输出使用英文。" if str(request.language).startswith("en") else "所有面向用户的输出使用中文。"
    )
    workspace = Path(request.workspace)
    config = load_project_config(workspace)
    test_command = ""
    if config.commands.test and config.commands.test != "auto":
        test_command = config.commands.test
    else:
        detected = detect_test_command(workspace)
        test_command = detected or ""
    rules_path = workspace / ".aicode" / "rules.md"
    rules_text = rules_path.read_text("utf-8", errors="replace")[:4000] if rules_path.is_file() else ""
    workspaces = ", ".join(f"{ref.name}（只读）" for ref in config.workspaces) or "无"

    sections = [
        "你是 aicode，一个在用户本机工作区工作的 coding agent。通过提供的工具探索代码、执行命令、修改文件。",
        language_line,
        "工作准则：",
        "- 修改文件前必须先用 read_file 读到要改的原文；edit_file 的 old_text 必须与文件原文完全一致且唯一。",
        "- 每次 edit_file 都会展示 diff 等用户确认；被拒绝时调整方案或询问，不要原样重试。",
        "- 命令被策略拒绝时换安全的替代做法，或把需要用户手动执行的命令写进最终答复。",
        "- 完成修改后要运行测试验证；工具输出被截断时可用 offset 继续读。",
        "- 任务完成或无事可做时直接输出结论文本，不要空转调用工具。",
        f"工作区: {request.workspace}",
        f"测试命令: {test_command or '未检测到，可自行探测'}",
        f"受保护路径（禁止读写）: {', '.join(config.protected_paths)}",
        f"额外只读 workspace: {workspaces}",
    ]
    mode_line = MODE_INSTRUCTIONS_ZH.get(request.mode)
    if mode_line:
        sections.append(f"本次任务模式: {mode_line}")
    if rules_text:
        sections.append(f"项目规则（.aicode/rules.md）:\n{rules_text}")
    return "\n".join(sections)
```

注意：`load_project_config` 返回对象的 `commands.test` 字段名以 `runtime/app/project/config.py` 实际实现为准（执行时先读该文件确认属性名，可能是 `config.commands.test` 或 `config.test_command`），`detect_test_command(workspace)` 签名以 `runtime/app/project/detect.py` 为准；如不一致，按实际 API 调整这两行调用，测试断言不变。

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_turn_prompts.py -v && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/app/agent/turn.py runtime/app/agent/prompts.py runtime/tests/test_turn_prompts.py
git commit -m "Add turn message builders and system prompt with project info"
```

---

### Task 10: history.py（加载/持久化/三层压缩）

**Files:**
- Create: `runtime/app/agent/history.py`
- Test: `runtime/tests/test_history.py`

**Interfaces:**
- Consumes: `Session`（现有 store）、`store.append_message`；`ModelRouter.stream_complete`（layer 3 summarizer）
- Produces:
  - `load_history(session) -> list[dict]`：把 `session.messages` 转为 canonical messages。规则：dict 含 `role` 键 → 原样采用；dict 含 `message` 键（legacy user 请求）→ `{"role":"user","content":message}`；其它跳过
  - `persist_message(session, message) -> None`（调 `store.append_message(session, message)`）
  - `truncate_tool_output(tool_name, text) -> str`（layer 1：`bash`/`run_tests` 8000 字符头 65% 尾 35% 保留、`read_file` 不再截（已限行）、默认 6000 字符；截断处插入 `[输出已截断: N 字符省略]`）
  - `estimate_tokens(messages) -> int`（`len(json.dumps(messages, ensure_ascii=False)) // 3.5` 取整）
  - `async compact_if_needed(history, runtime, session) -> list[dict]`：预算 `HISTORY_TOKEN_BUDGET = 60_000`。layer 2：超预算时从最老开始把 `role=="tool"` 消息内容替换为一行摘要（保留最近 `KEEP_RECENT_MESSAGES = 8` 条不动），直到达标；layer 3：layer 2 后仍超 `1.5 * budget` 时调 summarizer 把前一半消息压成一条 `[历史摘要] ...` user 消息。压缩发生时发 `context.budget` 事件（字段：`purpose:"history"`、`compacted:true`、`before_tokens`、`after_tokens`）

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_history.py
import pytest

from app.agent.history import (
    HISTORY_TOKEN_BUDGET,
    compact_if_needed,
    estimate_tokens,
    load_history,
    truncate_tool_output,
)
from app.agent.types import AgentRuntime
from app.models.router import ModelRouter
from app.config.settings import Settings
from app.sessions.store import SessionStore
from tests.fakes import FakeProvider, text_turn


@pytest.fixture
def session(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    return store.create(workspace=str(tmp_path), language="zh-CN"), store


def test_load_history_converts_legacy(session, tmp_path):
    sess, store = session
    store.append_message(sess, {"message": "修个 bug", "mode": "default", "workspace": str(tmp_path), "language": "zh-CN"})
    store.append_message(sess, {"role": "assistant", "content": "好的"})
    history = load_history(sess)
    assert history[0] == {"role": "user", "content": "修个 bug"}
    assert history[1]["role"] == "assistant"


def test_truncate_tool_output_layers():
    long_text = "x" * 20_000
    truncated = truncate_tool_output("bash", long_text)
    assert len(truncated) < 9_000
    assert "已截断" in truncated
    assert truncate_tool_output("read_file", long_text) == long_text


@pytest.mark.asyncio
async def test_compact_replaces_old_tool_messages(session):
    sess, _store = session
    runtime = AgentRuntime(model_router=None, tools=None, audit=None)
    big = "y" * 40_000
    history = [{"role": "user", "content": "task"}]
    for index in range(12):
        history.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"tc_{index}", "name": "bash", "arguments": {}}]})
        history.append({"role": "tool", "tool_call_id": f"tc_{index}", "content": big})
    before = estimate_tokens(history)
    assert before > HISTORY_TOKEN_BUDGET
    compacted = await compact_if_needed(history, runtime, sess)
    assert estimate_tokens(compacted) < before
    assert "[工具输出已压缩" in compacted[2]["content"]  # 最老的 tool 消息被压缩
    assert compacted[-1]["content"] == big  # 最近消息保留


@pytest.mark.asyncio
async def test_compact_noop_under_budget(session):
    sess, _store = session
    runtime = AgentRuntime(model_router=None, tools=None, audit=None)
    history = [{"role": "user", "content": "hi"}]
    assert await compact_if_needed(history, runtime, sess) == history
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_history.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `runtime/app/agent/history.py`**

```python
from __future__ import annotations

import json
from typing import Any

from app.sessions.store import Session, store

HISTORY_TOKEN_BUDGET = 60_000
HARD_BUDGET_FACTOR = 1.5
KEEP_RECENT_MESSAGES = 8

TOOL_OUTPUT_LIMITS = {"bash": 8_000, "run_tests": 8_000, "read_file": 0, "default": 6_000}


def load_history(session: Session) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for raw in session.messages:
        if not isinstance(raw, dict):
            continue
        if "role" in raw:
            history.append(dict(raw))
        elif "message" in raw:
            history.append({"role": "user", "content": str(raw["message"])})
    return history


def persist_message(session: Session, message: dict[str, Any]) -> None:
    store.append_message(session, message)


def truncate_tool_output(tool_name: str, text: str) -> str:
    limit = TOOL_OUTPUT_LIMITS.get(tool_name, TOOL_OUTPUT_LIMITS["default"])
    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n[输出已截断: {len(text) - limit} 字符省略，可用 offset/分页参数继续查看]\n"
    head = int(limit * 0.65)
    tail = limit - head
    return text[:head] + marker + text[-tail:]


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return int(len(json.dumps(messages, ensure_ascii=False, default=str)) / 3.5)


async def compact_if_needed(history: list[dict[str, Any]], runtime: Any, session: Session) -> list[dict[str, Any]]:
    before = estimate_tokens(history)
    if before <= HISTORY_TOKEN_BUDGET:
        return history

    compacted = [dict(message) for message in history]
    cutoff = max(0, len(compacted) - KEEP_RECENT_MESSAGES)
    for index in range(cutoff):
        if estimate_tokens(compacted) <= HISTORY_TOKEN_BUDGET:
            break
        message = compacted[index]
        if message.get("role") != "tool" or str(message.get("content") or "").startswith("[工具输出已压缩"):
            continue
        original_chars = len(str(message.get("content") or ""))
        message["content"] = f"[工具输出已压缩: {original_chars} 字符，如需内容请重新调用工具]"

    if estimate_tokens(compacted) > HISTORY_TOKEN_BUDGET * HARD_BUDGET_FACTOR and runtime.model_router is not None:
        compacted = await summarize_history_head(compacted, runtime)

    after = estimate_tokens(compacted)
    await session.events.put(
        {
            "type": "context.budget",
            "purpose": "history",
            "compacted": True,
            "before_tokens": before,
            "after_tokens": after,
        }
    )
    return compacted


async def summarize_history_head(history: list[dict[str, Any]], runtime: Any) -> list[dict[str, Any]]:
    half = len(history) // 2
    head, tail = history[:half], history[half:]
    result = await runtime.model_router.stream_complete(
        purpose="summarizer",
        system="把以下 agent 对话压缩为要点：用户目标、已完成的探索/修改、关键发现、未完成事项。只输出要点列表。",
        messages=[{"role": "user", "content": json.dumps(head, ensure_ascii=False, default=str)[:40_000]}],
        max_tokens=800,
    )
    summary = {"role": "user", "content": f"[历史摘要] {result.text}"}
    return [summary, *tail]
```

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_history.py -v && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/app/agent/history.py runtime/tests/test_history.py
git commit -m "Add history loading, persistence and three-layer compaction"
```

---

### Task 11: 主循环 loop_v2.py（execute_gated + approval + 验证注入 + 预算收尾）

**Files:**
- Create: `runtime/app/agent/loop_v2.py`
- Modify: `runtime/app/agent/types.py`（`AgentRuntime` 增加 `policy: Any = None` 字段）
- Test: `runtime/tests/test_loop_v2.py`

**Interfaces:**
- Consumes: Task 4 `ModelRouter.stream_complete` 与 `tests/fakes.py`；Task 5 `PolicyEngine.gate`；Task 6-8 `registry`/`edit`；Task 9 `turn`/`prompts`；Task 10 `history`；现有 `Session.create_approval/wait_for_approval`、`localized`
- Produces:
  - `async run_turn(session, request, runtime) -> None`
  - `async run_turn_safely(session, request, runtime) -> None`（异常 → `error` + `final` 事件 + audit `session.error`，复用旧 loop 的 `emit_agent_failure` 模式）
  - 事件契约：每次模型文本增量发 `assistant.delta {text}`；每个 tool call 发 `tool.started {tool,args,risk_level}`；结果发 `tool.output`/`tool.denied`/`tool.error`/`tool.rejected`；edit 流程发 `approval.requested {approval_id, kind:"edit", path, diff, message}`、`edit.applied {path}`、`edit.rejected {path}`、`edit.auto_approved {path}`；每次模型调用后发 `usage.recorded`（沿用现有字段）；结束发 `final {summary}`
  - 行为契约：模型返回无 tool_calls 且本轮有已应用编辑且未提示过验证 → 注入 `user_note(VERIFY_NOTE_ZH)` 继续；到达 `max_steps` → 注入 `user_note(BUDGET_NOTE_ZH)` 后做最后一次 `tools=[]` 调用强制总结；`session.auto_accept_edits` 为 True 且路径非 protected → 免确认应用并发 `edit.auto_approved`

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_loop_v2.py
import asyncio

import pytest

from app.agent.loop_v2 import run_turn
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config.settings import Settings
from app.models.router import ModelRouter
from app.policy.engine import PolicyEngine
from app.sessions.store import SessionStore
from tests.fakes import FakeProvider, text_turn, tool_turn


class Request:
    def __init__(self, workspace, message="修复 bug", mode="default", language="zh-CN"):
        self.workspace = str(workspace)
        self.message = message
        self.mode = mode
        self.language = language


def make_runtime(turns, tmp_path):
    fake = FakeProvider(turns)
    router = ModelRouter(primary=fake, fallback=fake, settings=Settings())
    audit = AuditLogger(path=tmp_path / "audit.jsonl")
    return AgentRuntime(model_router=router, tools=None, audit=audit, policy=PolicyEngine()), fake


def make_session(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path), language="zh-CN")
    return session


def events_of(session, event_type):
    return [e for e in session.events.events_after(0) if e.get("type") == event_type]


@pytest.mark.asyncio
async def test_plain_text_turn_emits_final(tmp_path):
    runtime, _ = make_runtime([text_turn("没什么要改的")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    finals = events_of(session, "final")
    assert finals and "没什么要改的" in finals[0]["summary"]
    assert events_of(session, "assistant.delta")


@pytest.mark.asyncio
async def test_tool_loop_executes_and_feeds_back(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, fake = make_runtime(
        [tool_turn("read_file", {"path": "a.py"}), text_turn("读完了")], tmp_path
    )
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert events_of(session, "tool.output")
    # 第二次模型调用的 messages 里包含 tool 结果
    second_call = fake.calls[1]
    assert any(m.get("role") == "tool" and "x = 1" in str(m.get("content")) for m in second_call.messages)


@pytest.mark.asyncio
async def test_denied_bash_feeds_reason_to_model(tmp_path):
    runtime, fake = make_runtime(
        [tool_turn("bash", {"command": "rm -rf /"}), text_turn("那我不删了")], tmp_path
    )
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert events_of(session, "tool.denied")
    second_call = fake.calls[1]
    assert any("被策略拒绝" in str(m.get("content")) for m in second_call.messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_edit_approval_flow_applies_after_accept(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, _ = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("已修改"),  # 验证注入后的回应
            text_turn("完成"),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)

    async def approve_soon():
        for _ in range(100):
            await asyncio.sleep(0.01)
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=True)
                return

    _task = asyncio.create_task(approve_soon())
    await run_turn(session, Request(tmp_path), runtime)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    assert events_of(session, "approval.requested")
    assert events_of(session, "edit.applied")


@pytest.mark.asyncio
async def test_edit_rejected_reported_to_model(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, fake = make_runtime(
        [tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}), text_turn("好吧")],
        tmp_path,
    )
    session = make_session(tmp_path)

    async def reject_soon():
        for _ in range(100):
            await asyncio.sleep(0.01)
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=False)
                return

    _task = asyncio.create_task(reject_soon())
    await run_turn(session, Request(tmp_path), runtime)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert any("拒绝" in str(m.get("content")) for m in fake.calls[1].messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_accept_all_skips_approval(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, _ = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("已修改"),
            text_turn("完成"),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)
    session.auto_accept_edits = True
    await run_turn(session, Request(tmp_path), runtime)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    assert events_of(session, "edit.auto_approved")
    assert not events_of(session, "approval.requested")


@pytest.mark.asyncio
async def test_verification_note_injected_after_edit(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, fake = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("改完了"),  # 尝试结束 → 应被注入验证提示
            text_turn("验证过了"),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)
    session.auto_accept_edits = True
    await run_turn(session, Request(tmp_path), runtime)
    assert len(fake.calls) == 3
    last_call = fake.calls[2]
    assert any("[系统提示]" in str(m.get("content")) for m in last_call.messages if m.get("role") == "user")


@pytest.mark.asyncio
async def test_review_mode_has_no_write_tools(tmp_path):
    runtime, fake = make_runtime([text_turn("审查完成")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path, mode="review"), runtime)
    tool_names = {t["name"] for t in fake.calls[0].tools}
    assert "edit_file" not in tool_names
    assert "bash" not in tool_names


@pytest.mark.asyncio
async def test_max_steps_forces_summary(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    turns = [tool_turn("read_file", {"path": "a.py"}, call_id=f"tc_{i}") for i in range(40)]
    turns.append(text_turn("被迫总结"))
    runtime, fake = make_runtime(turns, tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert fake.calls[-1].tools == []  # 最后一次调用不带工具
    finals = events_of(session, "final")
    assert finals and "被迫总结" in finals[0]["summary"]
```

注意：`AuditLogger(path=...)` 构造方式以 `runtime/app/audit/logger.py` 实际为准（现有测试里应有可参考的构造写法，照抄即可）；`AgentRuntime` 新增字段后旧调用点（`server/main.py` 的 `AgentRuntime(model_router=..., tools=..., audit=...)`）因有默认值不受影响。

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_loop_v2.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`runtime/app/agent/types.py` 的 `AgentRuntime` 增加字段：

```python
@dataclass(slots=True)
class AgentRuntime:
    model_router: Any
    tools: Any
    audit: Any
    policy: Any = None
```

创建 `runtime/app/agent/loop_v2.py`：

```python
from __future__ import annotations

from typing import Any

from app.agent.history import compact_if_needed, load_history, persist_message, truncate_tool_output
from app.agent.prompts import BUDGET_NOTE_EN, BUDGET_NOTE_ZH, VERIFY_NOTE_EN, VERIFY_NOTE_ZH, build_system_prompt
from app.agent.turn import TurnBudget, assistant_message, tool_message, user_note
from app.agent.utils import localized
from app.models.provider import CompletionResult, ProviderNotConfigured, ToolCallRequest
from app.policy.engine import PolicyEngine
from app.sessions.store import Session
from app.tools.base import is_protected_path
from app.tools.edit import EditError, EditStaleError, apply_edit, build_edit_proposal
from app.tools.registry import build_tool_context, run_tool, tool_schemas_for_mode


async def run_turn_safely(session: Session, request: Any, runtime: Any) -> None:
    try:
        await run_turn(session, request, runtime)
    except Exception as exc:
        runtime.audit.record(
            "session.error",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"mode": request.mode, "error_type": exc.__class__.__name__, "error": str(exc)},
        )
        message = localized(request.language, f"Agent 执行失败: {exc}", f"Agent execution failed: {exc}")
        await session.events.put({"type": "error", "error": message, "error_type": exc.__class__.__name__})
        await session.events.put({"type": "final", "summary": message})


async def run_turn(session: Session, request: Any, runtime: Any) -> None:
    policy: PolicyEngine = runtime.policy or PolicyEngine()
    system = build_system_prompt(request)
    history = load_history(session)
    tools = tool_schemas_for_mode(request.mode)
    context = build_tool_context(request.workspace, request.mode, request.language)
    budget = TurnBudget()
    applied_edits = 0
    verify_note_sent = False
    result: CompletionResult | None = None

    async def on_delta(text: str) -> None:
        await session.events.put({"type": "assistant.delta", "text": text})

    for _step in range(budget.max_steps):
        result = await runtime.model_router.stream_complete(
            purpose="main", system=system, messages=history, tools=tools,
            on_text_delta=on_delta, max_tokens=budget.max_tokens_per_call,
        )
        await record_usage(session, result, "main", runtime)
        message = assistant_message(result)
        history.append(message)
        persist_message(session, message)

        if not result.tool_calls:
            if applied_edits > 0 and not verify_note_sent:
                verify_note_sent = True
                note = user_note(localized(request.language, VERIFY_NOTE_ZH, VERIFY_NOTE_EN))
                history.append(note)
                persist_message(session, note)
                continue
            break

        for call in result.tool_calls:
            output, applied = await execute_gated(session, request, call, runtime, policy, context)
            applied_edits += applied
            reply = tool_message(call.id, output)
            history.append(reply)
            persist_message(session, reply)

        history = await compact_if_needed(history, runtime, session)
    else:
        note = user_note(localized(request.language, BUDGET_NOTE_ZH, BUDGET_NOTE_EN))
        history.append(note)
        persist_message(session, note)
        result = await runtime.model_router.stream_complete(
            purpose="main", system=system, messages=history, tools=[], on_text_delta=on_delta,
        )
        await record_usage(session, result, "main", runtime)
        message = assistant_message(result)
        history.append(message)
        persist_message(session, message)

    summary = result.text if result is not None else ""
    runtime.audit.record("session.final", session_id=session.session_id, workspace=session.workspace, data={"mode": request.mode})
    await session.events.put({"type": "final", "summary": summary})


async def execute_gated(
    session: Session, request: Any, call: ToolCallRequest, runtime: Any, policy: PolicyEngine, context: Any
) -> tuple[str, int]:
    gate = policy.gate(call.name, call.arguments, mode=request.mode)
    runtime.audit.record(
        "tool.started",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"tool": call.name, "args": call.arguments, "verdict": gate.verdict, "risk_level": gate.risk_level},
    )
    await session.events.put({"type": "tool.started", "tool": call.name, "args": call.arguments, "risk_level": gate.risk_level})

    if gate.verdict == "deny":
        await session.events.put({"type": "tool.denied", "tool": call.name, "error": gate.reason, "risk_level": gate.risk_level})
        return f"[被策略拒绝] {gate.reason}", 0

    if call.name == "edit_file":
        return await execute_edit(session, request, call, runtime, context)

    if gate.verdict == "ask":
        accepted = await request_approval(session, request, "tool", {"tool": call.name, "args": call.arguments, "reason": gate.reason})
        if accepted is not True:
            reason = localized(request.language, "用户拒绝执行该命令", "user rejected the command")
            await session.events.put({"type": "tool.rejected", "tool": call.name, "error": reason, "risk_level": gate.risk_level})
            return f"[{reason}]", 0

    result = await run_tool(call.name, call.arguments, context)
    if result.success:
        output = truncate_tool_output(call.name, result.text)
        await session.events.put({"type": "tool.output", "tool": call.name, "text": result.text[:2000], "data": result.data})
        return output, 0
    await session.events.put({"type": "tool.error", "tool": call.name, "error": result.error, "data": result.data})
    return f"[错误] {truncate_tool_output(call.name, result.error)}", 0


async def execute_edit(session: Session, request: Any, call: ToolCallRequest, runtime: Any, context: Any) -> tuple[str, int]:
    from pathlib import Path

    workspace = Path(request.workspace)
    try:
        proposal = build_edit_proposal(workspace, call.arguments, context.protected_paths)
    except EditError as exc:
        await session.events.put({"type": "tool.error", "tool": "edit_file", "error": str(exc)})
        return f"[编辑失败] {exc}", 0

    auto = session.auto_accept_edits and not is_protected_path(proposal.path, context.protected_paths)
    if auto:
        await session.events.put({"type": "edit.auto_approved", "path": proposal.path})
        accepted = True
    else:
        accepted = await request_approval(
            session,
            request,
            "edit",
            {"path": proposal.path, "kind": proposal.kind, "diff": proposal.diff},
        )

    if accepted is not True:
        reason = localized(request.language, "用户拒绝了此编辑", "user rejected this edit")
        await session.events.put({"type": "edit.rejected", "path": proposal.path})
        return f"[{reason}] {proposal.path}", 0

    try:
        apply_edit(workspace, proposal)
    except EditStaleError as exc:
        await session.events.put({"type": "tool.error", "tool": "edit_file", "error": str(exc)})
        return f"[编辑失败·stale] {exc}", 0
    runtime.audit.record(
        "edit.applied",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"path": proposal.path, "kind": proposal.kind, "diff_bytes": len(proposal.diff)},
    )
    await session.events.put({"type": "edit.applied", "path": proposal.path, "kind": proposal.kind})
    return f"已应用编辑 {proposal.path}:\n{proposal.diff}", 1


async def request_approval(session: Session, request: Any, kind: str, payload: dict[str, Any]) -> bool | None:
    approval = session.create_approval(kind, payload)
    message = localized(request.language, "等待用户确认", "waiting for user approval")
    await session.events.put({"type": "approval.requested", "approval_id": approval.approval_id, "kind": kind, "message": message, **payload})
    return await session.wait_for_approval(approval.approval_id)


async def record_usage(session: Session, result: CompletionResult, purpose: str, runtime: Any) -> None:
    payload = {
        "model": result.model,
        "provider": result.provider,
        "purpose": purpose,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "estimated_cost": result.estimated_cost,
    }
    runtime.audit.record("usage.recorded", session_id=session.session_id, workspace=session.workspace, data=payload)
    await session.events.put({"type": "usage.recorded", **payload})
```

同时在 `runtime/app/sessions/store.py` 的 `Session` dataclass 增加字段（放在 `agent_runner_task` 之后）：

```python
    auto_accept_edits: bool = False
```

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest tests/test_loop_v2.py -v && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime/app/agent/loop_v2.py runtime/app/agent/types.py runtime/app/sessions/store.py runtime/tests/test_loop_v2.py
git commit -m "Add model-driven agent loop with gated tools and edit approvals"
```

---

### Task 12: server 接线 v2（切换 loop、approve 支持 accept_all、未配置 provider 报错）

**Files:**
- Modify: `runtime/app/server/main.py`
- Test: `runtime/tests/test_server_v2.py`

**Interfaces:**
- Consumes: Task 11 `run_turn_safely`；Task 4 router
- Produces: `POST /v1/sessions/{id}/approve` body 增加 `accept_all: bool = False`，为 True 时先置 `session.auto_accept_edits = True` 再决议；`run_agent` 改为调用 `run_turn_safely`；`AgentRuntime` 构造传入 `policy=PolicyEngine()`；消息入口在 provider 未配置时直接返回 HTTP 400（detail 含配置引导），不进入 loop

- [ ] **Step 1: 写失败测试**

```python
# runtime/tests/test_server_v2.py
import pytest

from app.server import main as server


@pytest.mark.asyncio
async def test_approve_accept_all_sets_session_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("AICODE_SESSION_DB_PATH", str(tmp_path / "s.sqlite"))
    from app.sessions.store import SessionStore

    store = SessionStore(path=tmp_path / "s.sqlite")
    monkeypatch.setattr(server, "store", store)
    session = store.create(workspace=str(tmp_path), language="zh-CN")
    approval = session.create_approval("edit", {"path": "a.py"})

    response = await server.approve(session.session_id, server.ApprovalRequest(approval_id=approval.approval_id, accept_all=True))
    assert response["status"] == "accepted"
    assert session.auto_accept_edits is True
    assert approval.accepted is True


def test_agent_runtime_has_policy():
    assert server.agent_runtime.policy is not None


def test_run_agent_uses_v2_loop():
    import inspect

    source = inspect.getsource(server.run_agent)
    assert "run_turn_safely" in source
```

- [ ] **Step 2: 运行确认失败**

Run: `cd runtime && python3 -m pytest tests/test_server_v2.py -v`
Expected: FAIL（accept_all 字段不存在等）

- [ ] **Step 3: 实现（`runtime/app/server/main.py` 修改点）**

1. imports：删除 `from app.agent.loop import run_agent_safely as agent_run_agent_safely`、`from app.agent.patch_flow import ...`、`from app.agent.commands import ...`、`from app.agent.summary import ...`、`from app.agent.tool_flow import ...`；新增：

```python
from app.agent.loop_v2 import run_turn_safely
from app.policy.engine import PolicyEngine
```

2. runtime 构造改为：

```python
agent_runtime = AgentRuntime(model_router=model_router, tools=tools, audit=audit, policy=PolicyEngine())
```

3. `ApprovalRequest` 增加字段：

```python
class ApprovalRequest(BaseModel):
    approval_id: str
    accept_all: bool = False
```

4. `approve` endpoint 在 `resolve_approval` 之前：

```python
    if request.accept_all:
        session.auto_accept_edits = True
        audit.record(
            "approval.accept_all_enabled",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"approval_id": request.approval_id},
        )
```

5. `run_agent` 改为：

```python
async def run_agent(session: Session, request: MessageRequest) -> None:
    await run_turn_safely(session, request, agent_runtime)
```

6. `send_message` 入口，在 enqueue 之前加 provider 检查：

```python
    is_configured = getattr(agent_runtime.model_router.primary, "is_configured", None)
    if callable(is_configured) and not is_configured():
        raise HTTPException(status_code=400, detail="模型 provider 未配置，请设置 API key（如 OPENAI_API_KEY 或 ANTHROPIC_API_KEY）后重试")
```

7. 删除文件底部的旧 helper `execute_tool`、`run_post_patch_verification`（它们只被旧测试引用）。

8. 运行全量测试，凡因旧 loop 行为断言失败的旧测试文件（`test_agent_runtime.py`、`test_server_helpers.py` 中依赖 `execute_tool`/patch flow 的用例）此时整体删除——它们覆盖的行为已由 `test_loop_v2.py`/`test_server_v2.py` 接管；`test_server_helpers.py` 中与 `event_cursor`/`same_workspace`/queue 相关且仍通过的用例保留。

- [ ] **Step 4: 运行测试通过 + 全量绿**

Run: `cd runtime && python3 -m pytest -q`
Expected: PASS（删除的旧测试不再运行）

- [ ] **Step 5: Commit**

```bash
git add -A runtime/app/server/main.py runtime/tests/
git commit -m "Wire server to v2 loop with accept-all approvals"
```

---

### Task 13: 删除旧实现并重命名 loop_v2 → loop

**Files:**
- Delete: `runtime/app/agent/steps.py`、`runtime/app/agent/commands.py`、`runtime/app/agent/patch_flow.py`、`runtime/app/agent/summary.py`、`runtime/app/agent/context_budget.py`、`runtime/app/agent/tool_flow.py`、`runtime/app/tools/patch.py`、`runtime/app/tools/project.py`、`runtime/app/tools/git.py`、`runtime/app/tools/shell.py`、`runtime/app/tools/search.py`、`runtime/app/tools/command.py`（若存在且无其它引用）、`runtime/app/tools/test_analysis.py`、`runtime/app/tools/router.py`
- Delete tests: `test_agent_steps.py`、`test_agent_commands.py`、`test_coder_patch.py`、`test_patch.py`、`test_context_budget.py`、`test_test_analysis.py`、`test_agent_runtime.py`（若 Task 12 未删完）、`test_tools.py`/`test_command_runner.py` 中依赖已删模块的用例
- Rename: `runtime/app/agent/loop_v2.py` → `runtime/app/agent/loop.py`（先删旧 loop.py）
- Modify: `runtime/app/models/provider.py`（删 `ModelRequest`/`ModelResponse`/`StubProvider`/`ModelProviderUnavailable`/旧 `ModelProvider` 基类，保留 v2 部分）、`runtime/app/models/openai_compatible.py`（删旧 `complete`/`_post_chat_completion`/`parse_chat_completion_response`，保留 `chat_completions_url`/`as_int`/v2 部分）、`runtime/app/models/router.py`（删 `fallback` 字段、旧 `complete`、`model_for_purpose`，把 `model_for_purpose_v2` 改名 `model_for_purpose`；`route_status` 的 routes 改为 `{"main","reviewer","summarizer"}` 并输出 `provider.type`）、`runtime/app/config/settings.py`（`ModelSettings` 删 `default/planner/coder`，`from_env` 同步删）、`runtime/app/agent/types.py`（`AgentRuntime` 删 `tools` 字段）、`runtime/app/server/main.py`（删 `ToolRouter` 构造与 import）、`runtime/app/tools/file.py`（删 `FindFilesTool`/`ReadFileTool`，保留 `ListFilesTool`）

**步骤：**

- [ ] **Step 1: 执行删除与重命名**

```bash
cd runtime
git rm app/agent/steps.py app/agent/commands.py app/agent/patch_flow.py app/agent/summary.py \
      app/agent/context_budget.py app/agent/tool_flow.py app/agent/loop.py \
      app/tools/patch.py app/tools/project.py app/tools/git.py app/tools/shell.py \
      app/tools/search.py app/tools/test_analysis.py app/tools/router.py
git mv app/agent/loop_v2.py app/agent/loop.py
git rm tests/test_agent_steps.py tests/test_agent_commands.py tests/test_coder_patch.py \
      tests/test_patch.py tests/test_context_budget.py tests/test_test_analysis.py
```

（`app/tools/command.py` 先 `rg "from app.tools.command" app/` 确认无引用再删；有引用则保留并记录原因。）

- [ ] **Step 2: 逐个修复 import 与残留引用**

```bash
cd runtime && rg "patch_flow|agent.steps|agent.commands|agent.summary|context_budget|tool_flow|tools.patch|tools.project|tools.git|tools.shell|tools.search|tools.router|StubProvider|ModelRequest|ModelProviderUnavailable|loop_v2" app/ tests/
```

对每处命中按上面 Files 清单的说明修复：v2 模块内 `from app.agent.loop_v2 import` 改为 `from app.agent.loop import`（含 `test_loop_v2.py`）；`server/main.py` 删除 `tools = ToolRouter()` 与对应参数；`prompts.py` 若引用了 `detect_test_command` 保持不变（`app/project/detect.py` 保留）。`review.py` 若 import 了被删模块（执行时用 `rg` 确认），把所需函数就地内联或改用 `subprocess` 直接跑 `git diff`。

- [ ] **Step 3: 清理 settings/router/provider 旧字段**

按 Files 清单执行；`test_models.py`、`test_pricing.py`、`test_usage.py` 中引用 `planner/coder`、`StubProvider`、旧 `complete` 的用例改为 main/reviewer/summarizer 与 `stream_complete`（参考 `test_router_v2.py` 的写法），或删除重复覆盖的用例。

- [ ] **Step 4: 全量验证**

Run: `cd runtime && python3 -m compileall app && python3 -m pytest -q && cd .. && go test ./cli/...`
Expected: 全部 PASS；`rg "loop_v2" runtime/` 无命中

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Remove rule planner, patch protocol and legacy provider path"
```

---

### Task 14: CLI 适配（流式渲染、edit 确认 y/a/其它、配置键）

**Files:**
- Modify: `cli/internal/client/client.go`（Approve 带 accept_all）
- Modify: `cli/internal/renderer/renderer.go`（新事件渲染，删除死分支）
- Modify: `cli/main.go`（edit 确认交互）
- Modify: `cli/internal/config/config.go`（models.main、provider.type、provider.anthropic.*）
- Test: `cli/internal/client/client_test.go`、`cli/internal/renderer/renderer_test.go`、`cli/internal/config/config_test.go`

**Interfaces:**
- Consumes: Task 12 的 approve API（`accept_all` 字段）与 Task 11 的事件契约
- Produces: `client.Approve(ctx, sessionID, approvalID string, acceptAll bool) error`；renderer 处理 `assistant.delta`（原样打印 text，不换行）、`edit.applied`、`edit.rejected`、`edit.auto_approved`；`RuntimeEnv()` 新增 `AICODE_MODEL_MAIN`、`AICODE_PROVIDER_TYPE`、`AICODE_ANTHROPIC_BASE_URL`、`AICODE_ANTHROPIC_API_KEY_ENV`

- [ ] **Step 1: 写失败测试（Go）**

`cli/internal/client/client_test.go` 追加（模仿文件中现有 httptest 用例写法）：

```go
func TestApproveSendsAcceptAll(t *testing.T) {
	var got map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"accepted"}`))
	}))
	defer server.Close()

	c := New(server.URL)
	if err := c.Approve(context.Background(), "sess_1", "appr_1", true); err != nil {
		t.Fatalf("Approve failed: %v", err)
	}
	if got["accept_all"] != true {
		t.Fatalf("expected accept_all=true, got %v", got)
	}
}
```

`cli/internal/renderer/renderer_test.go` 追加（模仿现有捕获 stdout 的测试写法；若现有测试直接调用内部行构造函数，则为 `assistant.delta` 走 RenderEvent 输出断言）：

```go
func TestRenderAssistantDelta(t *testing.T) {
	output := captureOutput(func() {
		RenderEvent(map[string]any{"type": "assistant.delta", "text": "你好"})
	})
	if !strings.Contains(output, "你好") {
		t.Fatalf("expected delta text, got %q", output)
	}
	if strings.HasSuffix(output, "\n\n") {
		t.Fatalf("delta 不应额外换行: %q", output)
	}
}

func TestRenderEditApplied(t *testing.T) {
	output := captureOutput(func() {
		RenderEvent(map[string]any{"type": "edit.applied", "path": "a.py", "kind": "replace"})
	})
	if !strings.Contains(output, "a.py") {
		t.Fatalf("expected path in output, got %q", output)
	}
}
```

（`captureOutput` 若不存在，参照现有 renderer 测试的输出捕获方式实现同名 helper。）

`cli/internal/config/config_test.go` 追加：

```go
func TestModelsMainAndProviderTypeKeys(t *testing.T) {
	cfg := Default()
	if err := Set(&cfg, "models.main", "m-x"); err != nil {
		t.Fatal(err)
	}
	if err := Set(&cfg, "provider.type", "anthropic"); err != nil {
		t.Fatal(err)
	}
	if err := Set(&cfg, "provider.anthropic.api_key_env", "MY_KEY"); err != nil {
		t.Fatal(err)
	}
	env := cfg.RuntimeEnv()
	assertContains(t, env, "AICODE_MODEL_MAIN=m-x")
	assertContains(t, env, "AICODE_PROVIDER_TYPE=anthropic")
	assertContains(t, env, "AICODE_ANTHROPIC_API_KEY_ENV=MY_KEY")
}
```

（`Default`/`Set`/`assertContains` 的实际函数名以 `config.go`/`config_test.go` 现有代码为准，照现有用例的调用方式改写；断言意图不变。）

- [ ] **Step 2: 运行确认失败**

Run: `go test ./cli/...`
Expected: FAIL（编译错误或断言失败）

- [ ] **Step 3: 实现**

`client.go`：

```go
func (c Client) Approve(ctx context.Context, sessionID string, approvalID string, acceptAll bool) error {
	return c.postJSON(ctx, "/v1/sessions/"+sessionID+"/approve", map[string]any{"approval_id": approvalID, "accept_all": acceptAll}, nil)
}
```

（同步更新 `main.go` 中原有 `api.Approve(ctx, sessionID, approvalID)` 调用为 `api.Approve(ctx, sessionID, approvalID, false)`。）

`main.go` 的事件处理（`case "approval.requested"` 分支，位于现约 950 行处）改为区分 kind：

```go
	case "approval.requested":
		approvalID, _ := event["approval_id"].(string)
		if approvalID == "" {
			return fmt.Errorf("approval.requested 缺少 approval_id")
		}
		if kind, _ := event["kind"].(string); kind == "edit" {
			if diff, _ := event["diff"].(string); diff != "" {
				fmt.Println(diff)
			}
			return resolveEditApproval(api, sessionID, approvalID)
		}
		return resolveApprovalWithPrompt(api, sessionID, approvalID, "允许执行这个工具操作吗？输入 y 确认，其它任意输入拒绝 [y/N]: ")
```

新增函数（放在 `resolveApprovalWithPrompt` 旁）：

```go
func resolveEditApproval(api client.Client, sessionID string, approvalID string) error {
	fmt.Print("应用这个编辑吗？[y=应用 / a=应用并允许本会话后续编辑 / 其它=拒绝]: ")
	reader := bufio.NewReader(os.Stdin)
	line, _ := reader.ReadString('\n')
	answer := strings.ToLower(strings.TrimSpace(line))
	ctx, cancel := context.WithTimeout(context.Background(), defaultTimeout)
	defer cancel()
	switch answer {
	case "y", "yes":
		return api.Approve(ctx, sessionID, approvalID, false)
	case "a", "all":
		return api.Approve(ctx, sessionID, approvalID, true)
	default:
		return api.Reject(ctx, sessionID, approvalID)
	}
}
```

删除 `case "patch.preview"` 分支（事件已不存在）。`resolveApprovalWithPrompt` 内部读输入方式保持文件现有实现。

`renderer.go` 的 `RenderEvent`：新增分支：

```go
	case "assistant.delta":
		text, _ := event["text"].(string)
		fmt.Print(text)
	case "edit.applied":
		fmt.Printf("\n已应用编辑: %v (%v)\n", event["path"], event["kind"])
	case "edit.rejected":
		fmt.Printf("\n已拒绝编辑: %v\n", event["path"])
	case "edit.auto_approved":
		fmt.Printf("\n[本会话已允许] 自动应用编辑: %v\n", event["path"])
```

删除已不存在事件的分支及其专属 helper：`plan.created`、`plan.updated`、`agent.step`、`agent.loop.max_steps`、`patch.preview`、`patch.applied`、`patch.rejected`、`patch.stale`、`patch.rebuild.started`、`verification.*`（对应 `contextStepLine`/`patchPreviewLine`/`patchStatusLine`/`patchStaleDetail`/`verificationStatusLine`/`verificationAnalysisLine` 等 helper 一并删除，先 `rg` 确认无其它引用）；`final` 分支前补一个换行（流式文本后收尾）。删除 renderer_test.go 中对应已删事件的用例。

`config.go`：在 `ModelsConfig` 增加 `Main string`；默认值 `"gpt-5"`；`Set`/`Get`/`List`/docs 表按现有 planner 条目的模式增加 `models.main`、`provider.type`、`provider.anthropic.base_url`、`provider.anthropic.api_key_env` 四个键（新增 `ProviderConfig{Type string}` 与 `AnthropicConfig{BaseURL, APIKeyEnv string}` 结构体，挂到 `Config`）；`RuntimeEnv()` 增加：

```go
		"AICODE_MODEL_MAIN=" + cfg.Models.Main,
		"AICODE_PROVIDER_TYPE=" + cfg.Provider.Type,
		"AICODE_ANTHROPIC_BASE_URL=" + cfg.Anthropic.BaseURL,
		"AICODE_ANTHROPIC_API_KEY_ENV=" + cfg.Anthropic.APIKeyEnv,
```

保留 `models.planner`/`models.coder` 键与对应 env（runtime 已忽略），在 `config docs` 文案中标注"已废弃，由 models.main 取代"。`main.go` 的 `printHelp` 增加 `aicode config set models.main gpt-5` 与 `aicode config set provider.type anthropic` 示例行。

- [ ] **Step 4: 运行测试通过**

Run: `go test ./cli/... && cd runtime && python3 -m pytest -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add cli/
git commit -m "Render streaming output and edit approvals in CLI"
```

---

### Task 15: 端到端冒烟测试

**Files:**
- Test: `runtime/tests/test_e2e_smoke.py`

**Interfaces:**
- Consumes: 全部前序任务；`tests/fakes.py`

- [ ] **Step 1: 写测试**

```python
# runtime/tests/test_e2e_smoke.py
"""FakeProvider 驱动的全链路冒烟：改文件 → 确认 → 验证 → final。"""
import asyncio

import pytest

from app.agent.loop import run_turn_safely
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config.settings import Settings
from app.models.router import ModelRouter
from app.policy.engine import PolicyEngine
from app.sessions.store import SessionStore
from tests.fakes import FakeProvider, text_turn, tool_turn


@pytest.mark.asyncio
async def test_full_fix_flow(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    turns = [
        tool_turn("read_file", {"path": "calc.py"}, call_id="tc_1", text="先读文件"),
        tool_turn("edit_file", {"path": "calc.py", "old_text": "return a - b", "new_text": "return a + b"}, call_id="tc_2"),
        tool_turn("bash", {"command": "cat calc.py"}, call_id="tc_3", text="验证一下"),
        text_turn("已修复 add 函数"),
    ]
    fake = FakeProvider(turns)
    runtime = AgentRuntime(
        model_router=ModelRouter(primary=fake, fallback=fake, settings=Settings()),
        tools=None,
        audit=AuditLogger(path=tmp_path / "audit.jsonl"),
        policy=PolicyEngine(),
    )
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path), language="zh-CN")

    class Request:
        workspace = str(tmp_path)
        message = "修复 add"
        mode = "default"
        language = "zh-CN"

    async def approve_all_pending():
        while True:
            await asyncio.sleep(0.01)
            for approval in list(session.approvals.values()):
                if approval.accepted is None:
                    session.resolve_approval(approval.approval_id, accepted=True)

    approver = asyncio.create_task(approve_all_pending())
    try:
        await run_turn_safely(session, Request(), runtime)
    finally:
        approver.cancel()

    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    events = session.events.events_after(0)
    types = [e["type"] for e in events]
    for expected in ["tool.started", "approval.requested", "edit.applied", "usage.recorded", "final"]:
        assert expected in types, f"missing event {expected}"
    finals = [e for e in events if e["type"] == "final"]
    assert "已修复" in finals[-1]["summary"]
    # 多轮记忆：再来一条消息，history 应包含上一轮内容
    fake.turns.append(text_turn("基于上一轮继续"))
    class Request2(Request):
        message = "继续"
    store.append_message(session, {"message": "继续", "mode": "default", "workspace": str(tmp_path), "language": "zh-CN"})
    await run_turn_safely(session, Request2(), runtime)
    last_call = fake.calls[-1]
    assert any("修复 add" in str(m.get("content")) for m in last_call.messages if m.get("role") == "user")
```

- [ ] **Step 2: 运行**

Run: `cd runtime && python3 -m pytest tests/test_e2e_smoke.py -v && python3 -m pytest -q && cd .. && go test ./cli/...`
Expected: 全部 PASS。若失败，按 superpowers:systematic-debugging 定位修复后再提交。

- [ ] **Step 3: Commit**

```bash
git add runtime/tests/test_e2e_smoke.py
git commit -m "Add end-to-end smoke test for v2 agent flow"
```

---

### Task 16: 文档更新（README/ROADMAP 切到 v2 已完成状态）

**Files:**
- Modify: `README.md`
- Modify: `ROADMAP.md`

**步骤：**

- [ ] **Step 1: 更新 README.md**

1. 顶部状态行改为：`当前仓库已完成 Agent Loop v2 模型驱动重构（设计见 docs/superpowers/specs/2026-07-17-agent-loop-redesign-design.md）：`
2. "已具备"列表替换为 v2 事实：模型驱动工具循环（原生 function calling）、流式输出、`read_file`/`search`/`list_files`/`bash`/`edit_file`/`review_diff` 工具集、edit 逐次 diff 确认 + 会话级 accept-all、单文件 stale 检测、OpenAI 兼容 + Anthropic 双 provider、三角色模型路由（main/reviewer/summarizer）、Policy gate 三态分级、多轮对话记忆、审计日志、SQLite session 持久化、多仓库只读分析。
3. 删除 "Agent Loop v2（进行中）" 章节（内容已成事实）。
4. "本地运行"章节：删除描述规则 planner/patch proposal/启发式映射的段落（原 57-63 行区域）与 `create/append/replace` 直写命令示例及其"v2 中移除"标注，替换为一段 v2 行为描述：模型自主探索 → 每次 edit 展示 diff 确认（y/a/拒绝）→ 循环内验证；未配置 provider 时报错并提示设置 `OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY`。
5. "模型配置"章节：删除 stub 回退描述与"v2 将移除"标注；planner/coder 配置示例改为 `models.main`；新增 `provider.type`、`provider.anthropic.*` 与 `AICODE_MODEL_MAIN`/`AICODE_PROVIDER_TYPE`/`AICODE_ANTHROPIC_*` 环境变量说明。

- [ ] **Step 2: 更新 ROADMAP.md**

第 6 节标题改为 `## 6. Agent Loop v2: 模型驱动重构（已完成）`；版本规划总览表中该行目标列末尾加"（已完成）"。

- [ ] **Step 3: 核对文档与实现一致**

Run: `rg "planner|coder|append README|patch proposal|stub" README.md`
Expected: 无残留的旧行为描述（`models.planner` 仅允许出现在"已废弃"说明中）

- [ ] **Step 4: Commit**

```bash
git add README.md ROADMAP.md
git commit -m "Update docs for completed agent loop v2"
```

---

## Self-Review 记录

- **Spec 覆盖**：主循环/预算收尾（Task 11）、工具集六件套（Task 6-8）、双 provider 流式（Task 2-3）、三角色路由（Task 4）、policy 修复（Task 5）、edit 确认 + accept-all + stale（Task 8/11/12/14）、三层上下文压缩（Task 10）、多轮记忆（Task 10/15）、错误处理矩阵（provider 未配置→Task 12；重试耗尽→Task 2/3 抛 ProviderError 由 run_turn_safely 兜底；审批超时→现有 wait_for_approval 300s 默认；stale→Task 8；预算超限→Task 11）、CLI（Task 14）、测试策略（各 task + Task 15）、文档（Task 16）。spec 第 9 节 layer-3 溢出兜底在 Task 10 `summarize_history_head` 实现。
- **已知偏差（有意为之，与 spec 意图一致）**：① StreamEvent 简化为 `text_delta/tool_call/done`，provider 内部聚合参数增量；② 验证提示在"模型试图结束且本轮有已应用编辑"时注入（而非紧跟 apply 之后），效果等价且实现更简单；③ `edit_file` 用显式 `delete` 参数取代"new_text 省略 = delete"的隐式约定，消除歧义。
- **类型一致性**：`stream_complete` 关键字签名（Task 4 定义，Task 10/11/15 使用）、`GateDecision.verdict`（Task 5 定义，Task 11 使用）、`EditProposal/EditStaleError`（Task 8 定义，Task 11 使用）、`user_note/VERIFY_NOTE_ZH`（Task 9 定义，Task 11 使用）、`Approve(ctx, sid, aid, acceptAll)`（Task 14 内自洽）已核对。
- **执行期需现场确认的接口**（已在对应 task 中标注，不属于占位符，是对既有代码的适配点）：`AuditLogger` 构造方式、`load_project_config` 返回对象的 commands 字段名、`detect_test_command` 签名、Go 测试 helper 的实际函数名。适配方法：先读对应源文件，按实际 API 调整调用行，断言意图不变。

