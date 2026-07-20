import httpx
import pytest

from app.config.settings import AnthropicSettings
from app.models.anthropic import AnthropicProvider, to_anthropic_messages
from app.models.provider import TOOL_ARGUMENT_PARSE_ERROR_KEY, CompletionRequest, ProviderError


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


@pytest.mark.asyncio
async def test_retry_exhaustion_on_500(monkeypatch):
    monkeypatch.setenv("FAKE_ANTHROPIC_KEY", "sk-ant")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, content=b"server error")

    async def _fast_sleep(*a, **k):
        pass

    monkeypatch.setattr("app.models.anthropic.asyncio.sleep", _fast_sleep)
    settings = AnthropicSettings(base_url="https://fake.local", api_key_env="FAKE_ANTHROPIC_KEY")
    provider = AnthropicProvider(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="claude-x")
    with pytest.raises(ProviderError, match="HTTP 500"):
        async for _ in provider.stream_complete(request):
            pass
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_no_retry_on_400(monkeypatch):
    monkeypatch.setenv("FAKE_ANTHROPIC_KEY", "sk-ant")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, content=b"bad request")

    settings = AnthropicSettings(base_url="https://fake.local", api_key_env="FAKE_ANTHROPIC_KEY")
    provider = AnthropicProvider(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="claude-x")
    with pytest.raises(ProviderError, match="HTTP 400"):
        async for _ in provider.stream_complete(request):
            pass
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_malformed_tool_input_json_reports_parse_error(monkeypatch):
    monkeypatch.setenv("FAKE_ANTHROPIC_KEY", "sk-ant")
    # Build an SSE body with malformed input_json_delta
    malformed_body = (
        sse("message_start", '{"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":5}}}')
        + sse("content_block_start", '{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tu_bad","name":"broken"}}')
        + sse("content_block_delta", '{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{bad"}}')
        + sse("content_block_stop", '{"type":"content_block_stop","index":0}')
        + sse("message_delta", '{"type":"message_delta","usage":{"output_tokens":2}}')
        + sse("message_stop", '{"type":"message_stop"}')
    ).encode()
    settings = AnthropicSettings(base_url="https://fake.local", api_key_env="FAKE_ANTHROPIC_KEY")
    provider = AnthropicProvider(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=malformed_body))))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="claude-x")
    events = [event async for event in provider.stream_complete(request)]
    assert [e.type for e in events] == ["tool_call", "done"]
    assert events[0].tool_call.name == "broken"
    parse_error = events[0].tool_call.arguments[TOOL_ARGUMENT_PARSE_ERROR_KEY]
    assert "Expecting property name" in parse_error["error"]
    assert parse_error["raw_arguments"] == "{bad"
    assert events[0].tool_call.id == "tu_bad"


@pytest.mark.asyncio
async def test_non_object_tool_input_json_reports_parse_error(monkeypatch):
    monkeypatch.setenv("FAKE_ANTHROPIC_KEY", "sk-ant")
    malformed_body = (
        sse("message_start", '{"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":5}}}')
        + sse("content_block_start", '{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tu_bad","name":"broken"}}')
        + sse("content_block_delta", '{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"[]"}}')
        + sse("content_block_stop", '{"type":"content_block_stop","index":0}')
        + sse("message_stop", '{"type":"message_stop"}')
    ).encode()
    settings = AnthropicSettings(base_url="https://fake.local", api_key_env="FAKE_ANTHROPIC_KEY")
    provider = AnthropicProvider(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=malformed_body))))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="claude-x")
    events = [event async for event in provider.stream_complete(request)]
    parse_error = events[0].tool_call.arguments[TOOL_ARGUMENT_PARSE_ERROR_KEY]
    assert parse_error["error"] == "tool arguments JSON must be an object"
    assert parse_error["raw_arguments"] == "[]"


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
