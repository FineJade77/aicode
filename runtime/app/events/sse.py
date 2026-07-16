from __future__ import annotations

import json
from typing import Any


def encode_sse(event: dict[str, Any]) -> str:
    event_type = event.get("type", "message")
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"
