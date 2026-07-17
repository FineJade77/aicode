from app.config.settings import Settings
from app.models.provider import CompletionRequest, CompletionResult, StreamEvent, ToolCallRequest


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
