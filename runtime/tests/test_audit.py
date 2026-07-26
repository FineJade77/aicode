import json
from pathlib import Path

import pytest

from app.audit.logger import AuditLogger, stable_hash
from app.audit.redaction import redact


def test_audit_logger_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)

    logger.record("session.created", session_id="sess_1", workspace="/repo", data={"token": "secret", "ok": True})

    line = path.read_text(encoding="utf-8").strip()
    event = json.loads(line)

    assert event["event_type"] == "session.created"
    assert event["session_id"] == "sess_1"
    assert event["data"]["token"] == "[REDACTED]"
    assert event["data"]["ok"] is True


@pytest.mark.asyncio
async def test_audit_logger_flushes_async_writes(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)

    logger.record("usage.recorded", session_id="sess_1", workspace="/repo", data={"input_tokens": 10})
    assert logger.status()["enqueued"] == 1

    await logger.flush()
    status = logger.status()
    event = json.loads(path.read_text(encoding="utf-8").strip())

    assert event["event_type"] == "usage.recorded"
    assert event["data"]["input_tokens"] == 10
    assert status["written"] == 1
    assert status["failed"] == 0
    await logger.aclose()


@pytest.mark.asyncio
async def test_audit_logger_counts_individual_write_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")
    original_write = logger._write_event_sync
    call_count = {"value": 0}

    def flaky_write(event: dict) -> None:
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError("simulated disk error")
        original_write(event)

    monkeypatch.setattr(logger, "_write_event_sync", flaky_write)

    logger.record("one")
    logger.record("two")
    await logger.flush()
    status = logger.status()

    assert status["enqueued"] == 2
    assert status["written"] == 1
    assert status["failed"] == 1
    await logger.aclose()


@pytest.mark.asyncio
async def test_audit_logger_aclose_flushes_and_stops_writer(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")

    logger.record("session.final")
    assert logger.status()["writer_running"] is True

    await logger.aclose()
    status = logger.status()

    assert status["writer_running"] is False
    assert status["queue_size"] == 0
    assert (tmp_path / "audit.jsonl").exists()


def test_redact_truncates_long_strings() -> None:
    value = redact({"message": "x" * 1200})

    assert value["message"].endswith("...[TRUNCATED]")


def test_redact_keeps_token_usage_counts() -> None:
    value = redact({"input_tokens": 10, "output_tokens": 5, "api_token": "secret"})

    assert value["input_tokens"] == 10
    assert value["output_tokens"] == 5
    assert value["api_token"] == "[REDACTED]"


def test_redact_removes_known_secret_from_neutral_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-value")

    value = redact({"message_preview": "please use provider-secret-value"})

    assert value["message_preview"] == "please use [REDACTED]"


def test_stable_hash_is_deterministic() -> None:
    assert stable_hash("abc") == stable_hash("abc")
