from __future__ import annotations

import json
from typing import Any


def compact_tool_data(data: dict[str, Any]) -> dict[str, Any]:
    if not data:
        return {}
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) <= 12_000:
        return data
    return {"truncated": True, "preview": encoded[:12_000]}


def truncate_for_model(text: str, limit: int = 12_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[TRUNCATED]"
