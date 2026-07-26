import json

import pytest

from app.config.settings import ContextSettings, ModelSettings, OpenAICompatibleSettings, PricingSettings, Settings
from app.models.router import ModelRouter
from app.models.provider import ProviderCapabilityError, ProviderNotConfigured
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


def test_capability_is_model_and_provider_aware():
    settings = Settings(
        models=ModelSettings(main="local-8k", reviewer="remote-large", summarizer="summary-model"),
        context=ContextSettings(
            default_context_window=32_768,
            model_context_windows={"fake:local-8k": 8_192, "remote-large": 200_000},
            model_max_output_tokens={"fake:local-8k": 2_048},
        ),
    )
    router = ModelRouter(primary=FakeProvider([]), settings=settings)

    local = router.capability_for_purpose("main")
    remote = router.capability_for_purpose("reviewer")

    assert (local.context_window, local.max_output_tokens, local.source) == (8_192, 2_048, "configured")
    assert (remote.context_window, remote.max_output_tokens, remote.source) == (200_000, 8_192, "configured")
    assert router.route_status()["capabilities"]["main"]["context_window"] == 8_192


def test_context_capabilities_load_from_environment(monkeypatch):
    monkeypatch.setenv("AICODE_MODEL_CONTEXT_WINDOWS_JSON", '{"openai_compatible:local":8192,"remote":200000}')
    monkeypatch.setenv("AICODE_MODEL_MAX_OUTPUT_TOKENS_JSON", '{"openai_compatible:local":2048}')
    monkeypatch.setenv("AICODE_CONTEXT_COMPACT_THRESHOLD", "0.75")

    settings = Settings.from_env()

    assert settings.context.model_context_windows["openai_compatible:local"] == 8_192
    assert settings.context.model_context_windows["remote"] == 200_000
    assert settings.context.model_max_output_tokens["openai_compatible:local"] == 2_048
    assert settings.context.compact_threshold == 0.75


def test_local_provider_profile_loads_from_environment(monkeypatch):
    monkeypatch.setenv("AICODE_OPENAI_PROFILE", "ollama")
    monkeypatch.setenv("AICODE_OPENAI_AUTH_MODE", "none")
    monkeypatch.setenv("AICODE_OPENAI_CONTEXT_WINDOW", "16384")
    monkeypatch.setenv("AICODE_OPENAI_MAX_OUTPUT_TOKENS", "2048")
    monkeypatch.setenv("AICODE_OPENAI_TOOL_CALLING", "false")
    monkeypatch.setenv("AICODE_OPENAI_STREAMING", "true")
    monkeypatch.setenv("AICODE_OPENAI_CHARS_PER_TOKEN", "4")

    profile = Settings.from_env().openai_compatible

    assert profile.profile == "ollama"
    assert profile.auth_mode == "none"
    assert profile.context_window == 16_384
    assert profile.max_output_tokens == 2_048
    assert profile.tool_calling is False
    assert profile.streaming is True
    assert profile.tokenizer == "chars"
    assert profile.chars_per_token == 4


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AICODE_OPENAI_AUTH_MODE", "maybe"),
        ("AICODE_OPENAI_PROFILE_SCHEMA_VERSION", "2"),
        ("AICODE_OPENAI_TOOL_CALLING", "sometimes"),
        ("AICODE_OPENAI_CONTEXT_WINDOW", "1"),
    ],
)
def test_invalid_local_provider_profile_environment_fails_fast(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises((ValueError, TypeError)):
        Settings.from_env()


@pytest.mark.asyncio
async def test_declared_missing_tool_capability_fails_before_provider_call():
    settings = Settings(openai_compatible=OpenAICompatibleSettings(tool_calling=False))
    fake = FakeProvider([text_turn("must not be called")])
    fake.provider_name = "openai_compatible"
    router = ModelRouter(primary=fake, settings=settings)

    with pytest.raises(ProviderCapabilityError, match="native tools"):
        await router.stream_complete(
            purpose="main",
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}],
        )

    assert fake.calls == []


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
            profile="local-test",
            base_url="https://api.example.com/v1",
            api_key_env="AICODE_TEST_SECRET_KEY",
            auth_mode="optional",
            timeout_seconds=12.5,
            context_window=16_384,
            max_output_tokens=2_048,
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
    assert status["openai_compatible"]["auth_mode"] == "optional"
    assert status["openai_compatible"]["timeout_seconds"] == 12.5
    assert status["profile"]["name"] == "local-test"
    assert status["profile"]["context_window"] == 16_384
    assert status["profile"]["max_output_tokens"] == 2_048
    assert status["pricing"]["currency"] == "USD"
    assert status["pricing"]["unit"] == "per_1m_tokens"
    assert status["pricing"]["models"][0]["model"] == "review-model"
    assert "secret-value" not in json.dumps(status)
