from app.models.openai_compatible import chat_completions_url


def test_chat_completions_url() -> None:
    assert chat_completions_url("https://api.example.com/v1") == "https://api.example.com/v1/chat/completions"
    assert chat_completions_url("https://api.example.com/v1/chat/completions") == "https://api.example.com/v1/chat/completions"
