import httpx
import pytest

from app.config import OpenAICompatibleSettings
from app.models.openai_compatible import OpenAICompatibleProvider, to_openai_messages, to_openai_tools
from app.models.provider import TOOL_ARGUMENT_PARSE_ERROR_KEY, CompletionRequest, ContextOverflowError, ProviderError


def sse_bytes(*chunks: str) -> bytes:
    return "".join(f"data: {c}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"


STREAM_BODY = sse_bytes(
    '{"choices":[{"delta":{"content":"hello"}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc_1","function":{"name":"read_file","arguments":"{\\"pa"}}]}}]}',
    '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"th\\": \\"a.py\\"}"}}]}}]}',
    '{"choices":[{"delta":{}}],"usage":{"prompt_tokens":12,"completion_tokens":5},"model":"m1"}',
)


def make_provider(handler) -> OpenAICompatibleProvider:
    settings = OpenAICompatibleSettings(base_url="https://fake.local/v1", api_key_env="FAKE_KEY")
    return OpenAICompatibleProvider(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_no_auth_profile_sends_no_authorization_header(monkeypatch):
    monkeypatch.setenv("AICODE_OPENAI_API_KEY", "must-not-be-sent")
    monkeypatch.setenv("UNUSED_LOCAL_KEY", "must-also-not-be-sent")
    seen = {}

    def handler(request):
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, content=STREAM_BODY)

    provider = OpenAICompatibleProvider(
        OpenAICompatibleSettings(
            profile="local",
            base_url="http://local.invalid/v1",
            api_key_env="UNUSED_LOCAL_KEY",
            auth_mode="none",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        events = [
            event
            async for event in provider.stream_complete(
                CompletionRequest(
                    purpose="main",
                    system="s",
                    messages=[{"role": "user", "content": "hi"}],
                    model="m1",
                )
            )
        ]
    finally:
        await provider.aclose()

    assert provider.is_configured() is True
    assert seen["authorization"] is None
    assert events[-1].type == "done"


def test_optional_auth_is_configured_without_key(monkeypatch):
    monkeypatch.delenv("AICODE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPTIONAL_LOCAL_KEY", raising=False)
    provider = OpenAICompatibleProvider(
        OpenAICompatibleSettings(api_key_env="OPTIONAL_LOCAL_KEY", auth_mode="optional")
    )

    assert provider.is_configured() is True
    assert provider.request_headers() == {"Content-Type": "application/json"}


@pytest.mark.asyncio
async def test_stream_parses_text_tool_calls_and_usage(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    provider = make_provider(lambda request: httpx.Response(200, content=STREAM_BODY))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="m1")
    events = [event async for event in provider.stream_complete(request)]
    assert [e.type for e in events] == ["text_delta", "tool_call", "done"]
    assert events[0].text == "hello"
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


@pytest.mark.asyncio
async def test_retry_exhaustion_on_500(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, content=b"server error")

    async def _fast_sleep(*a, **k):
        pass

    monkeypatch.setattr("app.models.openai_compatible.asyncio.sleep", _fast_sleep)
    provider = make_provider(handler)
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="m1")
    with pytest.raises(ProviderError, match="HTTP 500"):
        async for _ in provider.stream_complete(request):
            pass
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_no_retry_on_400(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, content=b"bad request")

    provider = make_provider(handler)
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="m1")
    with pytest.raises(ProviderError, match="HTTP 400"):
        async for _ in provider.stream_complete(request):
            pass
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_context_overflow_is_classified_without_transport_retry(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded", "message": "maximum context length"}})

    provider = make_provider(handler)
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="m1")
    with pytest.raises(ContextOverflowError):
        async for _ in provider.stream_complete(request):
            pass
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_multi_index_tool_calls(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    multi_tool_body = sse_bytes(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc_0","function":{"name":"tool_a","arguments":"{\\"x"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\":1}"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":1,"id":"tc_1","function":{"name":"tool_b","arguments":"{\\"y"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"\\":2}"}}]}}]}',
        '{"choices":[{"delta":{}}],"usage":{"prompt_tokens":10,"completion_tokens":3},"model":"m1"}',
    )
    provider = make_provider(lambda request: httpx.Response(200, content=multi_tool_body))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="m1")
    events = [event async for event in provider.stream_complete(request)]
    assert [e.type for e in events] == ["tool_call", "tool_call", "done"]
    assert events[0].tool_call.name == "tool_a"
    assert events[0].tool_call.arguments == {"x": 1}
    assert events[0].tool_call.id == "tc_0"
    assert events[1].tool_call.name == "tool_b"
    assert events[1].tool_call.arguments == {"y": 2}
    assert events[1].tool_call.id == "tc_1"
    assert events[2].usage.input_tokens == 10


@pytest.mark.asyncio
async def test_malformed_tool_call_arguments(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    malformed_body = sse_bytes(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc_1","function":{"name":"broken","arguments":"not valid json {{{{"}}]}}]}',
        '{"choices":[{"delta":{}}],"usage":{"prompt_tokens":5,"completion_tokens":2},"model":"m1"}',
    )
    provider = make_provider(lambda request: httpx.Response(200, content=malformed_body))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="m1")
    events = [event async for event in provider.stream_complete(request)]
    assert [e.type for e in events] == ["tool_call", "done"]
    assert events[0].tool_call.name == "broken"
    parse_error = events[0].tool_call.arguments[TOOL_ARGUMENT_PARSE_ERROR_KEY]
    assert "Expecting value" in parse_error["error"]
    assert parse_error["raw_arguments"] == "not valid json {{{{"
    assert events[1].type == "done"


@pytest.mark.asyncio
async def test_non_object_tool_call_arguments_reports_parse_error(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    malformed_body = sse_bytes(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tc_1","function":{"name":"broken","arguments":"[]"}}]}}]}',
        '{"choices":[{"delta":{}}],"usage":{"prompt_tokens":5,"completion_tokens":2},"model":"m1"}',
    )
    provider = make_provider(lambda request: httpx.Response(200, content=malformed_body))
    request = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}], model="m1")
    events = [event async for event in provider.stream_complete(request)]
    parse_error = events[0].tool_call.arguments[TOOL_ARGUMENT_PARSE_ERROR_KEY]
    assert parse_error["error"] == "tool arguments JSON must be an object"
    assert parse_error["raw_arguments"] == "[]"


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


@pytest.mark.asyncio
async def test_aclose_closes_client(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=STREAM_BODY)))
    provider = make_provider(lambda request: httpx.Response(200, content=STREAM_BODY))
    provider._client = client
    assert not client.is_closed
    await provider.aclose()
    assert client.is_closed
    assert provider._client is None
    # A second aclose call is idempotent.
    await provider.aclose()
