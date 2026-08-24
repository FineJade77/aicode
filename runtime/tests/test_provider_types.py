from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from app.config import Settings
from app.models.provider import (
    RETRY_AFTER_CAP_SECONDS,
    RETRY_BASE_DELAY_SECONDS,
    RETRY_MAX_DELAY_SECONDS,
    CompletionRequest,
    StreamEvent,
    ToolCallRequest,
    backoff_delay,
    retry_after_seconds,
)


def test_completion_request_defaults():
    req = CompletionRequest(purpose="main", system="s", messages=[{"role": "user", "content": "hi"}])
    assert req.tools == []
    assert req.max_tokens == 8192


def test_stream_event_tool_call():
    call = ToolCallRequest(id="tc_1", name="read_file", arguments={"path": "a.py"})
    event = StreamEvent(type="tool_call", tool_call=call)
    assert event.tool_call.name == "read_file"


def test_settings_main_role_from_env(monkeypatch):
    monkeypatch.setenv("AICODE_MODEL_MAIN", "model-x")
    monkeypatch.setenv("AICODE_PROVIDER_TYPE", "anthropic")
    monkeypatch.setenv("AICODE_ANTHROPIC_API_KEY_ENV", "MY_KEY")
    settings = Settings.from_env()
    assert settings.models.main == "model-x"
    assert settings.provider.type == "anthropic"
    assert settings.anthropic.api_key_env == "MY_KEY"


def test_settings_main_falls_back_to_coder(monkeypatch):
    monkeypatch.delenv("AICODE_MODEL_MAIN", raising=False)
    monkeypatch.setenv("AICODE_MODEL_CODER", "legacy-coder")
    settings = Settings.from_env()
    assert settings.models.main == "legacy-coder"


def test_backoff_delay_is_jittered_within_an_exponential_ceiling() -> None:
    """Lockstep retries recreate the burst that caused the rate limit.

    Jitter spreads concurrent clients out; the lower half of the window is kept
    so a retry still waits a sensible minimum instead of hammering immediately.
    """
    for attempt in range(4):
        ceiling = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2**attempt))
        samples = [backoff_delay(attempt) for _ in range(200)]
        assert all(ceiling / 2 <= value <= ceiling for value in samples)
        assert len(set(samples)) > 1, "delays must not be identical across retries"


def test_backoff_delay_is_capped() -> None:
    assert backoff_delay(20) <= RETRY_MAX_DELAY_SECONDS


def test_retry_after_seconds_accepts_delay_seconds() -> None:
    assert retry_after_seconds({"retry-after": "12"}) == 12.0


def test_retry_after_seconds_accepts_http_date() -> None:
    future = datetime.now(UTC) + timedelta(seconds=30)
    value = retry_after_seconds({"retry-after": format_datetime(future, usegmt=True)})
    assert value is not None
    assert 25 <= value <= 31


def test_retry_after_seconds_is_capped_and_ignores_useless_values() -> None:
    # A multi-minute wait should surface as an error rather than a hung request.
    assert retry_after_seconds({"retry-after": "9999"}) == RETRY_AFTER_CAP_SECONDS
    # Past dates, zero, and unparsable values fall back to jittered backoff.
    past = datetime.now(UTC) - timedelta(seconds=30)
    assert retry_after_seconds({"retry-after": format_datetime(past, usegmt=True)}) is None
    assert retry_after_seconds({"retry-after": "0"}) is None
    assert retry_after_seconds({"retry-after": "soon"}) is None
    assert retry_after_seconds({}) is None
    assert retry_after_seconds(None) is None
