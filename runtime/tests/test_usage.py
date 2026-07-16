import json
from pathlib import Path

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
