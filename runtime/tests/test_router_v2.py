import pytest

from app.config.settings import Settings
from app.models.router import ModelRouter
from app.models.provider import ProviderNotConfigured
from tests.fakes import FakeProvider, text_turn, tool_turn


def make_router(turns) -> tuple[ModelRouter, FakeProvider]:
    settings = Settings()
    fake = FakeProvider(turns)
    router = ModelRouter(primary=fake, fallback=fake, settings=settings)
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
