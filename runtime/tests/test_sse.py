from app.events.sse import encode_sse


def test_encode_sse_includes_event_id() -> None:
    encoded = encode_sse({"event_id": 7, "type": "final", "summary": "done"})

    assert encoded.startswith("id: 7\n")
    assert "event: final\n" in encoded
    assert '"event_id": 7' in encoded
