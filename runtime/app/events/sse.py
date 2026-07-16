from __future__ import annotations

import json
from typing import Any


def encode_sse(event: dict[str, Any]) -> str:
    event_type = event.get("type", "message")
    payload = json.dumps(event, ensure_ascii=False)
    event_id = event.get("event_id")
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event_type}\ndata: {payload}\n\n"
