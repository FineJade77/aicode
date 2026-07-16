from app.events.sse import encode_sse


def test_encode_sse_contains_event_and_data() -> None:
    payload = encode_sse({"type": "final", "summary": "ok"})

    assert "event: final" in payload
    assert 'data: {"type": "final", "summary": "ok"}' in payload
