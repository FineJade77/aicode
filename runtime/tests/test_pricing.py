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
