import json

import pytest

from app.config.settings import ModelSettings, OpenAICompatibleSettings, PricingSettings, Settings
from app.models.openai_compatible import chat_completions_url, parse_chat_completion_response
from app.models.provider import ModelProvider, ModelRequest, ModelResponse
from app.models.router import ModelRouter
from app.usage.pricing import ModelPrice


def test_chat_completions_url() -> None:
    assert chat_completions_url("https://api.example.com/v1") == "https://api.example.com/v1/chat/completions"
    assert chat_completions_url("https://api.example.com/v1/chat/completions") == "https://api.example.com/v1/chat/completions"


def test_parse_chat_completion_response() -> None:
    response = parse_chat_completion_response(
        {
            "model": "demo-model",
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
        fallback_model="fallback",
        provider="openai_compatible",
    )

    assert response.text == "hello"
    assert response.model == "demo-model"
    assert response.provider == "openai_compatible"
    assert response.input_tokens == 3
    assert response.output_tokens == 2


@pytest.mark.asyncio
async def test_model_router_falls_back_to_stub_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AICODE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AICODE_TEST_MISSING_KEY", raising=False)
    settings = Settings(
        models=ModelSettings(summarizer="summary-model"),
        openai_compatible=OpenAICompatibleSettings(api_key_env="AICODE_TEST_MISSING_KEY"),
    )

    response = await ModelRouter.from_settings(settings).complete(
        ModelRequest(purpose="summarizer", messages=[{"role": "user", "content": "hello"}])
    )

    assert response.provider == "stub"
    assert response.model == "stub"


def test_model_router_selects_model_by_purpose() -> None:
    settings = Settings(models=ModelSettings(planner="plan-model", coder="code-model"))
    router = ModelRouter.from_settings(settings)

    assert router.model_for_purpose("planner") == "plan-model"
    assert router.model_for_purpose("coder") == "code-model"


def test_model_router_route_status_does_not_expose_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICODE_TEST_SECRET_KEY", "secret-value")
    settings = Settings(
        models=ModelSettings(reviewer="review-model", summarizer="summary-model"),
        openai_compatible=OpenAICompatibleSettings(
            base_url="https://api.example.com/v1",
            api_key_env="AICODE_TEST_SECRET_KEY",
            timeout_seconds=12.5,
        ),
        pricing=PricingSettings(model_prices={"openai_compatible/review-model": ModelPrice(input_per_1m=1.25, output_per_1m=10)}),
    )

    status = ModelRouter.from_settings(settings).route_status()

    assert status["provider"]["primary"] == "openai_compatible"
    assert status["provider"]["primary_configured"] is True
    assert status["provider"]["fallback"] == "stub"
    assert status["routes"]["reviewer"] == "review-model"
    assert status["routes"]["summarizer"] == "summary-model"
    assert status["openai_compatible"]["base_url"] == "https://api.example.com/v1"
    assert status["openai_compatible"]["api_key_env"] == "AICODE_TEST_SECRET_KEY"
    assert status["openai_compatible"]["timeout_seconds"] == 12.5
    assert status["pricing"]["currency"] == "USD"
    assert status["pricing"]["unit"] == "per_1m_tokens"
    assert status["pricing"]["models"][0]["model"] == "review-model"
    assert "secret-value" not in json.dumps(status)


@pytest.mark.asyncio
async def test_model_router_estimates_cost_from_price_table() -> None:
    settings = Settings(
        pricing=PricingSettings(
            model_prices={
                "openai_compatible/demo-model": ModelPrice(input_per_1m=1.25, output_per_1m=10),
            }
        )
    )
    router = ModelRouter(
        primary=FixedProvider(),
        fallback=FixedProvider(),
        settings=settings,
    )

    response = await router.complete(ModelRequest(purpose="summarizer", messages=[{"role": "user", "content": "hello"}]))

    assert response.provider == "openai_compatible"
    assert response.model == "demo-model"
    assert response.input_tokens == 1_000
    assert response.output_tokens == 2_000
    assert response.estimated_cost == 0.02125


class FixedProvider(ModelProvider):
    provider_name = "openai_compatible"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="ok",
            provider="openai_compatible",
            model="demo-model",
            input_tokens=1_000,
            output_tokens=2_000,
        )
