import json
from pathlib import Path

import pytest

from app.events.types import EVENT_TYPES
from app.sessions.store import SessionEvents
from app.tools.registry import TOOL_SCHEMAS


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = REPOSITORY_ROOT / "schemas"
CONTRACT_VERSION = "2.0"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def test_runtime_tools_match_canonical_schema() -> None:
    schema = load_schema("tools.schema.json")
    schema_names = schema["properties"]["name"]["enum"]
    runtime_names = [tool["name"] for tool in TOOL_SCHEMAS]

    assert schema["x-aicode-contract-version"] == CONTRACT_VERSION
    assert len(schema_names) == len(set(schema_names))
    assert set(schema_names) == set(runtime_names)


def test_runtime_events_match_canonical_schema() -> None:
    schema = load_schema("events.schema.json")
    schema_names = schema["properties"]["type"]["enum"]

    assert schema["x-aicode-contract-version"] == CONTRACT_VERSION
    assert len(schema_names) == len(set(schema_names))
    assert set(schema_names) == EVENT_TYPES


@pytest.mark.asyncio
async def test_session_events_reject_missing_or_unknown_types() -> None:
    events = SessionEvents()

    with pytest.raises(ValueError, match="must include"):
        await events.put({})
    with pytest.raises(ValueError, match="unknown runtime event type"):
        await events.put({"type": "plan.created"})
