import json

import pytest

from app.config.settings import ModelSettings, OpenAICompatibleSettings, Settings
from app.models.openai_compatible import chat_completions_url, parse_chat_completion_response
from app.models.provider import ModelRequest
from app.models.router import ModelRouter


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
    assert "secret-value" not in json.dumps(status)
