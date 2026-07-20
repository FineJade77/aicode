import json

import pytest

from app.config.settings import ModelSettings, OpenAICompatibleSettings, PricingSettings, Settings
from app.models.router import ModelRouter
from app.models.provider import ProviderNotConfigured
from app.usage.pricing import ModelPrice
from tests.fakes import FakeProvider, text_turn, tool_turn


def make_router(turns) -> tuple[ModelRouter, FakeProvider]:
    settings = Settings()
    fake = FakeProvider(turns)
    router = ModelRouter(primary=fake, settings=settings)
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


def test_model_for_purpose_maps_roles():
    settings = Settings(models=ModelSettings(main="main-model", reviewer="review-model", summarizer="summary-model"))
    router = ModelRouter(primary=FakeProvider([]), settings=settings)

    assert router.model_for_purpose("main") == "main-model"
    assert router.model_for_purpose("reviewer") == "review-model"
    assert router.model_for_purpose("summarizer") == "summary-model"
    assert router.model_for_purpose("unknown") == "main-model"


@pytest.mark.asyncio
async def test_stream_complete_estimates_cost_from_price_table():
    settings = Settings(
        pricing=PricingSettings(model_prices={"fake/fake-model": ModelPrice(input_per_1m=1.25, output_per_1m=10)})
    )
    fake = FakeProvider([text_turn("ok", input_tokens=1_000, output_tokens=2_000)])
    router = ModelRouter(primary=fake, settings=settings)

    result = await router.stream_complete(purpose="summarizer", system="s", messages=[{"role": "user", "content": "hi"}])

    assert result.provider == "fake"
    assert result.model == "fake-model"
    assert result.estimated_cost == 0.02125


def test_route_status_reports_roles_without_leaking_api_key(monkeypatch):
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
    assert status["provider"]["type"] == "openai_compatible"
    assert set(status["routes"]) == {"main", "reviewer", "summarizer"}
    assert status["routes"]["reviewer"] == "review-model"
    assert status["routes"]["summarizer"] == "summary-model"
    assert status["openai_compatible"]["base_url"] == "https://api.example.com/v1"
    assert status["openai_compatible"]["api_key_env"] == "AICODE_TEST_SECRET_KEY"
    assert status["openai_compatible"]["timeout_seconds"] == 12.5
    assert status["pricing"]["currency"] == "USD"
    assert status["pricing"]["unit"] == "per_1m_tokens"
    assert status["pricing"]["models"][0]["model"] == "review-model"
    assert "secret-value" not in json.dumps(status)
