import pytest

from app.config import Settings
from app.models.openai_compatible import chat_completions_url
from app.models.provider import (
    ContextOverflowError,
    ProviderCapabilityError,
    ProviderError,
    StreamEvent,
    Usage,
)
from app.models.router import ModelRouter


def test_chat_completions_url() -> None:
    assert chat_completions_url("https://api.example.com/v1") == "https://api.example.com/v1/chat/completions"
    assert chat_completions_url("https://api.example.com/v1/chat/completions") == "https://api.example.com/v1/chat/completions"


# --- per-route health check ---------------------------------------------------
#
# A single-model probe answers "is the provider reachable". A three-route setup
# asks something else: the summarizer is usually a different, cheaper model, and
# nothing exercises it until a compaction fires mid-run.


class RecordingProbeProvider:
    provider_name = "openai_compatible"

    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self.statuses = statuses or {}
        self.calls: list[tuple[str, bool]] = []

    def is_configured(self) -> bool:
        return True

    async def probe(self, model: str, *, tools: bool = True) -> dict:
        self.calls.append((model, tools))
        status = self.statuses.get(model, "ok")
        checks = [] if status == "ok" else [{"name": "chat", "status": "fail", "summary": f"{model} is unknown"}]
        return {"schema_version": 1, "status": status, "model": model, "checks": checks, "latency_ms": 5}


def route_settings(**models) -> Settings:
    settings = Settings()
    for purpose, name in models.items():
        setattr(settings.models, purpose, name)
    return settings


@pytest.mark.asyncio
async def test_probe_routes_covers_every_route() -> None:
    provider = RecordingProbeProvider()
    router = ModelRouter(
        primary=provider,
        settings=route_settings(main="main-model", reviewer="review-model", summarizer="summary-model"),
    )

    result = await router.probe_routes()

    assert set(result["routes"]) == {"main", "reviewer", "summarizer"}
    assert result["routes"]["summarizer"]["model"] == "summary-model"
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_one_broken_route_fails_the_whole_check() -> None:
    """A setup where compaction cannot run is not healthy on two routes out of three."""
    provider = RecordingProbeProvider(statuses={"summary-model": "error"})
    router = ModelRouter(
        primary=provider,
        settings=route_settings(main="main-model", reviewer="main-model", summarizer="summary-model"),
    )

    result = await router.probe_routes()

    assert result["status"] == "error"
    assert result["routes"]["main"]["status"] == "ok"
    assert result["routes"]["summarizer"]["status"] == "error"
    assert result["routes"]["summarizer"]["checks"][0]["summary"] == "summary-model is unknown"


@pytest.mark.asyncio
async def test_routes_sharing_a_model_are_probed_once() -> None:
    """The point is finding broken configuration, not paying three times for it."""
    provider = RecordingProbeProvider()
    router = ModelRouter(
        primary=provider,
        settings=route_settings(main="same-model", reviewer="same-model", summarizer="same-model"),
    )

    result = await router.probe_routes()

    assert len(provider.calls) == 1
    assert result["probes"] == 1
    # The shared result still has to be reported under every route.
    assert {route["status"] for route in result["routes"].values()} == {"ok"}


@pytest.mark.asyncio
async def test_each_route_is_probed_with_its_own_tool_capability() -> None:
    """Probing a tool-less profile with tools reports the probe's fault as the config's."""
    settings = route_settings(main="main-model", reviewer="review-model", summarizer="summary-model")
    settings.provider.type = "openai_compatible"
    settings.openai_compatible.tool_calling = False
    provider = RecordingProbeProvider()
    router = ModelRouter(primary=provider, settings=settings)

    await router.probe_routes()

    assert {tools for _, tools in provider.calls} == {False}


# --- provider fallback --------------------------------------------------------
#
# Answering from a different provider changes the price, the declared
# capabilities and what a rerun produces. It is therefore off unless configured,
# limited to the one failure it can honestly repair, and never silent.


class FailingProvider:
    provider_name = "anthropic"

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    async def stream_complete(self, request):
        self.calls += 1
        raise self.error
        yield  # pragma: no cover - makes this an async generator


class AnsweringProvider:
    provider_name = "openai_compatible"

    def __init__(self) -> None:
        self.requests = []

    def is_configured(self) -> bool:
        return True

    async def stream_complete(self, request):
        self.requests.append(request)
        yield StreamEvent(type="text_delta", text="from fallback")
        yield StreamEvent(type="done", usage=Usage(input_tokens=5, output_tokens=2), model=request.model)


def fallback_settings(**overrides) -> Settings:
    settings = Settings()
    settings.provider.type = "anthropic"
    settings.provider.fallback = overrides.get("fallback", "openai_compatible")
    settings.provider.fallback_model = overrides.get("fallback_model", "backup-model")
    return settings


def fallback_router(error: Exception, **overrides):
    primary = FailingProvider(error)
    secondary = AnsweringProvider()
    router = ModelRouter(primary=primary, settings=fallback_settings(**overrides), fallback=secondary)
    return router, primary, secondary


@pytest.mark.asyncio
async def test_an_unreachable_primary_is_answered_by_the_fallback() -> None:
    events: list[dict] = []
    router, _, secondary = fallback_router(ProviderError("anthropic HTTP 503: overloaded"))
    router.on_fallback = lambda event: events.append(event) or _noop()

    result = await router.stream_complete(purpose="main", system="s", messages=[])

    assert result.text == "from fallback"
    assert result.provider == "openai_compatible"
    # The fallback answers with its own model: the primary's name means nothing
    # to a different provider.
    assert secondary.requests[0].model == "backup-model"
    assert result.model == "backup-model"


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_the_switch_is_announced_not_inferred() -> None:
    events: list[dict] = []

    async def record(event):
        events.append(event)

    router, _, _ = fallback_router(ProviderError("anthropic HTTP 503: overloaded"))
    router.on_fallback = record

    await router.stream_complete(purpose="main", system="s", messages=[])

    assert [event["type"] for event in events] == ["provider.fallback"]
    assert events[0]["primary"] == "anthropic"
    assert events[0]["fallback"] == "openai_compatible"
    assert "503" in events[0]["error"]


@pytest.mark.asyncio
async def test_a_capability_error_never_falls_back() -> None:
    """The request is wrong for this provider; a second one with different
    declared capabilities would answer it by silently changing what the model
    can do."""
    router, _, secondary = fallback_router(ProviderCapabilityError("no tools"))

    with pytest.raises(ProviderCapabilityError):
        await router.stream_complete(purpose="main", system="s", messages=[])
    assert secondary.requests == []


@pytest.mark.asyncio
async def test_a_context_overflow_never_falls_back() -> None:
    """Overflow has its own recovery; handing it to another provider would skip it."""
    router, _, secondary = fallback_router(ContextOverflowError("too long"))

    with pytest.raises(ContextOverflowError):
        await router.stream_complete(purpose="main", system="s", messages=[])
    assert secondary.requests == []


@pytest.mark.asyncio
async def test_without_a_fallback_the_failure_reaches_the_caller() -> None:
    router = ModelRouter(primary=FailingProvider(ProviderError("down")), settings=Settings())

    with pytest.raises(ProviderError):
        await router.stream_complete(purpose="main", system="s", messages=[])


def test_a_fallback_is_off_unless_both_provider_and_model_are_named() -> None:
    for overrides in ({"fallback": ""}, {"fallback_model": ""}, {"fallback": "anthropic"}):
        settings = fallback_settings(**overrides)
        assert ModelRouter.from_settings(settings).fallback is None, overrides


def test_route_status_reports_the_configured_fallback() -> None:
    router = ModelRouter(
        primary=AnsweringProvider(), settings=fallback_settings(), fallback=AnsweringProvider()
    )

    provider = router.route_status()["provider"]

    assert provider["fallback"] == "openai_compatible"
    assert provider["fallback_model"] == "backup-model"
    assert ModelRouter(primary=AnsweringProvider(), settings=Settings()).route_status()["provider"]["fallback"] == ""
