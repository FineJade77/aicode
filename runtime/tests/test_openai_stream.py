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
