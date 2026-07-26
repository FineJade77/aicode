import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.config.settings import Settings
from app.events.sse import encode_sse
from app.events.types import EVENT_TYPES
from app.execution.models import ExecutionStatus
from app.models.router import ModelRouter
from app.server.main import (
    CancelExecutionResponse,
    CancelRunResponse,
    CreateSessionResponse,
    ExecutionResponse,
    SendMessageResponse,
    TrustListResponse,
    TrustStatusResponse,
)
from app.sessions.store import SessionEvents
from app.tools.registry import TOOL_SCHEMAS


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = REPOSITORY_ROOT / "schemas"
FIXTURE_ROOT = SCHEMA_ROOT / "fixtures"
CONTRACT_VERSION = "2.0"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


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


def test_install_manifest_schema_and_runtime_version_share_source_version() -> None:
    schema = load_schema("install-manifest.schema.json")
    required = set(schema["required"])
    source_version = (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert required == {"schema_version", "version", "runtime_dir", "python"}
    assert schema["properties"]["schema_version"]["const"] == 1
    assert source_version == Settings().version


def test_execution_schema_matches_runtime_terminal_states() -> None:
    schema = load_schema("execution.schema.json")

    assert schema["x-aicode-contract-version"] == "1.0"
    assert set(schema["$defs"]["status"]["enum"]) == {status.value for status in ExecutionStatus}
    assert schema["$defs"]["request"]["oneOf"]


def test_project_trust_schema_is_external_and_versioned() -> None:
    schema = load_schema("project-trust.schema.json")
    project = next(iter(schema["properties"]["projects"]["patternProperties"].values()))

    assert schema["x-aicode-contract-version"] == "1.0"
    assert schema["properties"]["schema_version"]["const"] == 1
    assert set(project["required"]) == {"workspace", "level", "git_remote", "updated_at"}
    assert project["properties"]["level"]["const"] == "trusted"


def test_provider_profile_schema_validates_runtime_status() -> None:
    schema = load_schema("provider-profile.schema.json")
    profile = ModelRouter.from_settings(Settings()).profile_status()

    assert schema["x-aicode-contract-version"] == "1.0"
    Draft202012Validator(schema).validate(profile)
    assert profile["schema_version"] == 1
    assert set(schema["properties"]["auth_mode"]["enum"]) == {"required", "optional", "none"}


def test_http_response_fixture_matches_runtime_models() -> None:
    fixture = load_fixture("http-responses.v2.json")
    responses = fixture["responses"]

    assert fixture["contract_version"] == CONTRACT_VERSION
    assert CreateSessionResponse.model_validate(responses["create_session"]).session_id == "sess_fixture"
    assert SendMessageResponse.model_validate(responses["send_message"]).run_id == "run_fixture"

    cancelled = CancelRunResponse.model_validate(responses["cancel_run_cancelled"])
    idle = CancelRunResponse.model_validate(responses["cancel_run_idle"])
    assert cancelled.run_id == "run_fixture"
    assert idle.status == "idle"
    assert idle.run_id is None
    execution = ExecutionResponse.model_validate(responses["execute_sandbox"])
    assert execution.execution_id == "exec_fixture"
    assert execution.status == "succeeded"
    assert CancelExecutionResponse.model_validate(responses["cancel_execution"]).status == "cancelled"
    assert TrustStatusResponse.model_validate(responses["trust_status"]).level == "untrusted"
    assert TrustStatusResponse.model_validate(responses["trust_project"]).level == "trusted"
    assert TrustListResponse.model_validate(responses["trust_list"]).projects[0].level == "trusted"
    assert TrustStatusResponse.model_validate(responses["trust_removed"]).removed is True


def test_sse_fixture_covers_v2_events_and_round_trips() -> None:
    fixture = load_fixture("sse-events.v2.json")
    events = fixture["events"]
    event_types = [event["type"] for event in events]
    event_ids = [event["event_id"] for event in events]

    assert fixture["contract_version"] == CONTRACT_VERSION
    assert set(event_types) == EVENT_TYPES
    assert len(event_types) == len(set(event_types))
    assert event_ids == sorted(set(event_ids))
    assert event_types[-1] == "final"

    for event in events:
        encoded = encode_sse(event)
        lines = encoded.splitlines()
        assert lines[0] == f"id: {event['event_id']}"
        assert lines[1] == f"event: {event['type']}"
        assert json.loads(lines[2].removeprefix("data: ")) == event

    assert fixture["forward_compat_event"]["type"] not in EVENT_TYPES


@pytest.mark.asyncio
async def test_session_events_reject_missing_or_unknown_types() -> None:
    events = SessionEvents()

    with pytest.raises(ValueError, match="must include"):
        await events.put({})
    with pytest.raises(ValueError, match="unknown runtime event type"):
        await events.put({"type": "plan.created"})
