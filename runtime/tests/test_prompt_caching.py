"""Anthropic prompt caching.

The switch exists so the feature is revertible and A/B-able: with it off the
payload must be byte-identical to the uncached one, because the only way to
attribute a cost change to caching is for the alternative to be the same request.
"""

from __future__ import annotations

import copy

import pytest

from app.config import AnthropicSettings, ModelSettings, PricingSettings, Settings
from app.models.anthropic import AnthropicProvider, cached_system, cached_tools
from app.models.provider import CompletionRequest, StreamEvent, Usage
from app.models.router import ModelRouter
from app.usage.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    ModelPrice,
    estimate_cost,
)

TOOLS = [
    {"name": "read_file", "input_schema": {"type": "object", "properties": {}}},
    {"name": "bash", "input_schema": {"type": "object", "properties": {}}},
]


def payload_for(*, caching: bool, monkeypatch) -> dict:
    """Capture the request body the provider would send."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider = AnthropicProvider(AnthropicSettings(prompt_caching=caching))
    captured: dict = {}

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aiter_lines(self):
            yield 'data: {"type":"message_start","message":{"model":"m","usage":{"input_tokens":1}}}'

    class FakeClient:
        def stream(self, method, url, json=None, headers=None):
            captured.update(json or {})
            return FakeStream()

    provider._client = FakeClient()
    return provider, captured


async def drain(provider, request):
    return [event async for event in provider.stream_complete(request)]


def request_with_tools(tools):
    return CompletionRequest(
        purpose="main", system="SYSTEM", messages=[{"role": "user", "content": "hi"}], tools=tools, model="m"
    )


# --- payload shape ----------------------------------------------------------


@pytest.mark.asyncio
async def test_caching_off_leaves_the_payload_untouched(monkeypatch):
    """The A/B baseline: off must be the same bytes as before the feature."""
    provider, captured = payload_for(caching=False, monkeypatch=monkeypatch)

    await drain(provider, request_with_tools(copy.deepcopy(TOOLS)))

    assert captured["system"] == "SYSTEM"
    assert all("cache_control" not in tool for tool in captured["tools"])


@pytest.mark.asyncio
async def test_caching_on_makes_system_a_block_array_with_a_breakpoint(monkeypatch):
    provider, captured = payload_for(caching=True, monkeypatch=monkeypatch)

    await drain(provider, request_with_tools(copy.deepcopy(TOOLS)))

    assert captured["system"] == [
        {"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}
    ]


@pytest.mark.asyncio
async def test_the_breakpoint_goes_on_the_last_tool_only(monkeypatch):
    """One breakpoint covers everything before it — marking each tool would
    spend the 4-breakpoint budget for nothing."""
    provider, captured = payload_for(caching=True, monkeypatch=monkeypatch)

    await drain(provider, request_with_tools(copy.deepcopy(TOOLS)))

    assert "cache_control" not in captured["tools"][0]
    assert captured["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_annotating_tools_does_not_mutate_the_shared_schema():
    """`TOOL_SCHEMAS` is module-level and shared with the OpenAI-compatible path.

    Annotating in place would attach an Anthropic-only key to every subsequent
    OpenAI request — a corruption that surfaces far from here and only when both
    providers run in one process.
    """
    shared = copy.deepcopy(TOOLS)

    annotated = cached_tools(shared)

    assert annotated[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in tool for tool in shared)


def test_the_copy_is_deep_not_shallow():
    """A shallow copy still shares the dicts the annotation writes into."""
    shared = copy.deepcopy(TOOLS)
    annotated = cached_tools(shared)
    annotated[-1]["input_schema"]["properties"]["injected"] = True

    assert "injected" not in shared[-1]["input_schema"]["properties"]


def test_an_empty_tool_list_stays_empty():
    assert cached_tools([]) == []


def test_the_system_block_carries_the_prompt_verbatim():
    assert cached_system("abc")[0]["text"] == "abc"


# --- usage parsing ----------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_tokens_are_parsed_from_the_stream(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider = AnthropicProvider(AnthropicSettings(prompt_caching=True))

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aiter_lines(self):
            yield (
                'data: {"type":"message_start","message":{"model":"m","usage":'
                '{"input_tokens":12,"cache_creation_input_tokens":900,"cache_read_input_tokens":8000}}}'
            )

    class FakeClient:
        def stream(self, *a, **k):
            return FakeStream()

    provider._client = FakeClient()

    events = await drain(provider, request_with_tools(None))
    done = [event for event in events if event.type == "done"][-1]

    assert done.usage.input_tokens == 12
    assert done.usage.cache_creation_input_tokens == 900
    assert done.usage.cache_read_input_tokens == 8000


@pytest.mark.asyncio
async def test_missing_cache_fields_read_as_zero(monkeypatch):
    """A model or provider that does not cache simply omits them."""
    provider, _ = payload_for(caching=True, monkeypatch=monkeypatch)

    events = await drain(provider, request_with_tools(None))
    done = [event for event in events if event.type == "done"][-1]

    assert done.usage.cache_creation_input_tokens == 0
    assert done.usage.cache_read_input_tokens == 0


# --- pricing ----------------------------------------------------------------

PRICES = {"anthropic/m": ModelPrice(input_per_1m=3.0, output_per_1m=15.0)}


def cost(**kwargs) -> float:
    return estimate_cost(provider="anthropic", model="m", prices=PRICES, **kwargs)


def test_cache_tokens_are_priced_off_the_input_rate():
    write = cost(input_tokens=0, output_tokens=0, cache_creation_input_tokens=1_000_000)
    read = cost(input_tokens=0, output_tokens=0, cache_read_input_tokens=1_000_000)

    assert write == pytest.approx(3.0 * CACHE_WRITE_MULTIPLIER)
    assert read == pytest.approx(3.0 * CACHE_READ_MULTIPLIER)


def test_a_cache_read_is_far_cheaper_than_the_same_tokens_uncached():
    """The whole point: 1M tokens served from cache must not cost 1M tokens."""
    assert cost(input_tokens=0, output_tokens=0, cache_read_input_tokens=1_000_000) < cost(
        input_tokens=1_000_000, output_tokens=0
    )


def test_omitting_cache_tokens_reproduces_the_previous_result():
    """Tiering is additive — a caller that knows nothing about caching is unaffected."""
    assert cost(input_tokens=1000, output_tokens=100) == pytest.approx(
        (1000 * 3.0 + 100 * 15.0) / 1_000_000
    )


def test_an_unpriced_model_still_returns_zero():
    assert estimate_cost(
        provider="anthropic",
        model="unpriced",
        input_tokens=10,
        output_tokens=10,
        prices=PRICES,
        cache_read_input_tokens=1000,
    ) == 0.0


@pytest.mark.asyncio
async def test_the_router_prices_cache_tokens_end_to_end():
    class CachingProvider:
        provider_name = "anthropic"

        def is_configured(self):
            return True

        async def stream_complete(self, request):
            yield StreamEvent(
                type="done",
                usage=Usage(
                    input_tokens=100,
                    output_tokens=10,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=1_000_000,
                ),
                model="m",
            )

    settings = Settings(
        models=ModelSettings(main="m", reviewer="m", summarizer="m"),
        pricing=PricingSettings(model_prices=PRICES),
    )
    result = await ModelRouter(primary=CachingProvider(), settings=settings).stream_complete(
        purpose="main", system="s", messages=[]
    )

    # Without the cache term this would be a rounding error rather than $0.30.
    assert result.estimated_cost == pytest.approx(
        (100 * 3.0 + 10 * 15.0) / 1_000_000 + 3.0 * CACHE_READ_MULTIPLIER
    )
