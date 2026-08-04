import pytest

from app.config import Settings
from app.models.openai_compatible import chat_completions_url
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
