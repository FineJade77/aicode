import pytest

from app.config import ModelSettings, PricingSettings, Settings
from app.models.provider import StreamEvent, Usage
from app.models.router import ModelRouter
from app.usage.pricing import ModelPrice, estimate_cost, parse_model_prices


def test_parse_model_prices_ignores_invalid_entries() -> None:
    prices = parse_model_prices(
        """
        {
          "openai_compatible/gpt-5": {"input_per_1m": 1.25, "output_per_1m": 10},
          "bad": {"input_per_1m": 999},
          "openai_compatible/": {"input_per_1m": 999},
          "openai_compatible/gpt-bad": "not-an-object"
        }
        """
    )

    assert list(prices) == ["openai_compatible/gpt-5"]
    assert prices["openai_compatible/gpt-5"].input_per_1m == 1.25
    assert prices["openai_compatible/gpt-5"].output_per_1m == 10


def test_estimate_cost_supports_exact_normalized_and_wildcard_prices() -> None:
    prices = {
        "openai_compatible/gpt-5": ModelPrice(input_per_1m=1.25, output_per_1m=10),
        "openai_compatible/*": ModelPrice(input_per_1m=0.5, output_per_1m=1),
    }

    exact = estimate_cost(provider="openai_compatible", model="gpt-5", input_tokens=1_000, output_tokens=2_000, prices=prices)
    normalized = estimate_cost(
        provider="openai_compatible", model="gpt-5:2026-07-16", input_tokens=1_000, output_tokens=2_000, prices=prices
    )
    wildcard = estimate_cost(provider="openai_compatible", model="unknown", input_tokens=1_000, output_tokens=2_000, prices=prices)

    assert exact == 0.02125
    assert normalized == 0.02125
    assert wildcard == 0.0025


def test_estimate_cost_returns_zero_without_price() -> None:
    assert estimate_cost(provider="openai_compatible", model="gpt-5", input_tokens=1_000, output_tokens=2_000, prices={}) == 0.0


@pytest.mark.asyncio
async def test_a_provider_alias_is_still_priced() -> None:
    """DeepSeek serves `deepseek-chat` under the name `deepseek-v4-flash`.

    Pricing only the name in the response silently misses and reports $0.00 for
    a call that was billed — worse than an approximate number, because a zero
    reads as a fact.
    """

    class AliasingProvider:
        provider_name = "openai_compatible"

        def is_configured(self) -> bool:
            return True

        async def stream_complete(self, request):
            yield StreamEvent(
                type="done",
                usage=Usage(input_tokens=1_000_000, output_tokens=1_000_000),
                model="deepseek-v4-flash",
            )

    settings = Settings(
        models=ModelSettings(main="deepseek-chat", reviewer="deepseek-chat", summarizer="deepseek-chat"),
        pricing=PricingSettings(
            model_prices={
                "openai_compatible/deepseek-chat": ModelPrice(input_per_1m=0.28, output_per_1m=0.42)
            }
        ),
    )
    router = ModelRouter(primary=AliasingProvider(), settings=settings)

    result = await router.stream_complete(purpose="main", system="s", messages=[])

    assert result.model == "deepseek-v4-flash"
    assert result.estimated_cost == pytest.approx(0.70)


@pytest.mark.asyncio
async def test_an_entirely_unknown_model_still_reports_zero() -> None:
    """The fallback must not invent a price for a model nobody configured."""

    class UnknownProvider:
        provider_name = "openai_compatible"

        def is_configured(self) -> bool:
            return True

        async def stream_complete(self, request):
            yield StreamEvent(type="done", usage=Usage(input_tokens=1000, output_tokens=10), model="mystery")

    settings = Settings(models=ModelSettings(main="also-mystery", reviewer="x", summarizer="x"))
    router = ModelRouter(primary=UnknownProvider(), settings=settings)

    assert (await router.stream_complete(purpose="main", system="s", messages=[])).estimated_cost == 0.0
