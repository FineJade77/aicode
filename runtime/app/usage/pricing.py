from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0


def estimate_cost(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    prices: dict[str, ModelPrice],
) -> float:
    price = price_for(provider=provider, model=model, prices=prices)
    if price is None:
        return 0.0
    cost = (max(input_tokens, 0) / 1_000_000 * price.input_per_1m) + (
        max(output_tokens, 0) / 1_000_000 * price.output_per_1m
    )
    return round(cost, 8)


def price_for(*, provider: str, model: str, prices: dict[str, ModelPrice]) -> ModelPrice | None:
    candidates = [
        price_key(provider, model),
        price_key(provider, normalize_model_name(model)),
        price_key(provider, "*"),
        price_key("*", model),
        price_key("*", normalize_model_name(model)),
        price_key("*", "*"),
    ]
    for candidate in candidates:
        price = prices.get(candidate)
        if price is not None:
            return price
    return None


def price_key(provider: str, model: str) -> str:
    return f"{provider.strip()}/{model.strip()}"


def normalize_model_name(model: str) -> str:
    return model.split(":", 1)[0].strip()


def parse_model_prices(raw: str | None) -> dict[str, ModelPrice]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}

    prices: dict[str, ModelPrice] = {}
    for raw_key, raw_value in payload.items():
        if not isinstance(raw_key, str) or "/" not in raw_key or not isinstance(raw_value, dict):
            continue
        key = normalize_price_key(raw_key)
        if not key:
            continue
        prices[key] = ModelPrice(
            input_per_1m=as_float(raw_value.get("input_per_1m")),
            output_per_1m=as_float(raw_value.get("output_per_1m")),
        )
    return prices


def model_prices_data(prices: dict[str, ModelPrice]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, price in sorted(prices.items()):
        provider, model = key.split("/", 1)
        rows.append(
            {
                "provider": provider,
                "model": model,
                "input_per_1m": price.input_per_1m,
                "output_per_1m": price.output_per_1m,
            }
        )
    return rows


def normalize_price_key(raw_key: str) -> str:
    provider, model = raw_key.split("/", 1)
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        return ""
    return price_key(provider, model)


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
