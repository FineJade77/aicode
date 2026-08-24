import json
from pathlib import Path

import pytest

from app.usage.store import summarize_usage


def test_summarize_usage_totals_and_groups(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    write_event(
        audit_path,
        session_id="sess_1",
        timestamp="2026-07-16T08:00:00+00:00",
        model="stub",
        purpose="summarizer",
        input_tokens=10,
        output_tokens=5,
        estimated_cost=0.01,
    )
    write_event(
        audit_path,
        session_id="sess_2",
        timestamp="2026-07-16T09:00:00+00:00",
        model="coder",
        purpose="coder",
        input_tokens=20,
        output_tokens=10,
        estimated_cost=0.02,
    )

    summary = summarize_usage(audit_path)

    assert summary["record_count"] == 2
    assert summary["total_input_tokens"] == 30
    assert summary["total_output_tokens"] == 15
    assert summary["total_tokens"] == 45
    assert summary["estimated_cost"] == 0.03
    assert summary["by_provider"]["stub"]["record_count"] == 2
    assert summary["by_model"]["stub"]["total_tokens"] == 15
    assert summary["by_purpose"]["coder"]["record_count"] == 1


def test_summarize_usage_filters_by_session(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    write_event(audit_path, session_id="sess_1", input_tokens=10)
    write_event(audit_path, session_id="sess_2", input_tokens=20)

    summary = summarize_usage(audit_path, session_id="sess_2")

    assert summary["record_count"] == 1
    assert summary["total_input_tokens"] == 20
    assert summary["filters"]["session_id"] == "sess_2"


def test_summarize_usage_ignores_invalid_lines(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("not-json\n", encoding="utf-8")

    assert summarize_usage(audit_path)["record_count"] == 0


def write_event(
    audit_path: Path,
    *,
    session_id: str,
    timestamp: str = "2026-07-16T08:00:00+00:00",
    model: str = "stub",
    provider: str = "stub",
    purpose: str = "summarizer",
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost: float = 0.0,
) -> None:
    event = {
        "timestamp": timestamp,
        "event_type": "usage.recorded",
        "session_id": session_id,
        "workspace": "/repo",
        "data": {
            "provider": provider,
            "model": model,
            "purpose": purpose,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost": estimated_cost,
        },
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


# --- calls the provider never finished reporting ------------------------------
#
# Usage arrives in the final frame of a stream. A run cancelled before that frame
# used tokens the provider billed and we never saw, so the totals silently
# understate. These pin that the gap is declared instead.

import asyncio  # noqa: E402

from app.application.services import RunCoordinator  # noqa: E402
from app.audit.logger import AuditLogger  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models.router import ModelRouter  # noqa: E402
from app.sessions.store import Session, SessionStore  # noqa: E402
from tests.fakes import FakeProvider  # noqa: E402


class _Request:
    def __init__(self, workspace: str) -> None:
        self.message = "go"
        self.mode = "default"
        self.workspace = workspace


def _coordinator(tmp_path, loop_impl):
    return RunCoordinator(
        ModelRouter(primary=FakeProvider([]), settings=Settings()),
        AuditLogger(path=tmp_path / "audit.jsonl"),
        loop_impl,
    )


@pytest.mark.asyncio
async def test_cancelling_mid_stream_records_the_call_as_incomplete(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    session = store.create(workspace=str(tmp_path))
    session.enqueue_agent_run(_Request(str(tmp_path)))
    streaming = asyncio.Event()

    class StreamingLoop:
        async def run(self, target_session: Session, request) -> None:
            target_session.begin_model_call(purpose="main", model="test-model")
            target_session.count_streamed_text("partial answer")
            streaming.set()
            # Stands in for waiting on the provider's remaining frames.
            await asyncio.sleep(60)

    coordinator = _coordinator(tmp_path, StreamingLoop())
    coordinator.ensure_runner(session)
    await asyncio.wait_for(streaming.wait(), timeout=1)

    await coordinator.cancel(session, resume_queued=False)

    usage = [event for event in session.events.events_after(0) if event["type"] == "usage.recorded"]
    assert len(usage) == 1, session.events.events_after(0)
    assert usage[0]["complete"] is False
    assert usage[0]["reason"] == "run_cancelled"
    assert usage[0]["purpose"] == "main"
    assert usage[0]["model"] == "test-model"
    # Evidence the call was not free. Characters, not tokens: the token count is
    # exactly the thing that never arrived.
    assert usage[0]["streamed_chars"] == len("partial answer")
    assert usage[0]["input_tokens"] == 0
    assert usage[0]["estimated_cost"] == 0.0


@pytest.mark.asyncio
async def test_cancelling_between_calls_records_nothing(tmp_path) -> None:
    """No call in flight, nothing unaccounted for — silence is correct here."""
    store = SessionStore(tmp_path / "sessions.sqlite")
    session = store.create(workspace=str(tmp_path))
    session.enqueue_agent_run(_Request(str(tmp_path)))
    idle = asyncio.Event()

    class IdleLoop:
        async def run(self, target_session: Session, request) -> None:
            idle.set()
            await asyncio.sleep(60)

    coordinator = _coordinator(tmp_path, IdleLoop())
    coordinator.ensure_runner(session)
    await asyncio.wait_for(idle.wait(), timeout=1)

    await coordinator.cancel(session, resume_queued=False)

    usage = [event for event in session.events.events_after(0) if event["type"] == "usage.recorded"]
    assert usage == []


@pytest.mark.asyncio
async def test_a_completed_call_leaves_nothing_in_flight(tmp_path) -> None:
    """Otherwise every cancellation after a normal call invents a phantom one."""
    store = SessionStore(tmp_path / "sessions.sqlite")
    session = store.create(workspace=str(tmp_path))

    session.begin_model_call(purpose="main", model="test-model")
    session.count_streamed_text("done")
    record = session.end_model_call()

    assert record is not None
    assert session.in_flight_model_call is None
    assert session.end_model_call() is None


def test_an_incomplete_record_marks_the_totals_as_a_lower_bound(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_type": "usage.recorded",
                        "timestamp": "2026-08-04T10:00:00+00:00",
                        "session_id": "sess_1",
                        "data": {
                            "provider": "anthropic",
                            "model": "claude-sonnet-5",
                            "purpose": "main",
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "estimated_cost": 0.001,
                        },
                    }
                ),
                json.dumps(
                    {
                        "event_type": "usage.recorded",
                        "timestamp": "2026-08-04T10:01:00+00:00",
                        "session_id": "sess_1",
                        "data": {
                            "provider": "anthropic",
                            "model": "claude-sonnet-5",
                            "purpose": "main",
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "estimated_cost": 0.0,
                            "complete": False,
                            "reason": "run_cancelled",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = summarize_usage(audit)

    assert summary["record_count"] == 2
    assert summary["incomplete_calls"] == 1
    # The incomplete call must not invent tokens or cost.
    assert summary["total_input_tokens"] == 100
    assert summary["estimated_cost"] == 0.001
    assert summary["by_purpose"]["main"]["incomplete_calls"] == 1


def test_a_clean_ledger_declares_no_gap(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        json.dumps(
            {
                "event_type": "usage.recorded",
                "timestamp": "2026-08-04T10:00:00+00:00",
                "session_id": "sess_1",
                "data": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "purpose": "main",
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "estimated_cost": 0.001,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert summarize_usage(audit)["incomplete_calls"] == 0
