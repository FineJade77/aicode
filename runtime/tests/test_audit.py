import asyncio
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
async def test_audit_logger_retries_a_transient_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single transient error must not cost an audit record.

    The audit trail is evidence: losing a line to one flaky write makes "no
    record of a dangerous command" indistinguishable from "no dangerous command".
    """
    logger = AuditLogger(tmp_path / "audit.jsonl")
    original_open = logger._open_handle
    calls = {"value": 0}

    def flaky_open():
        calls["value"] += 1
        if calls["value"] == 1:
            raise OSError("simulated transient disk error")
        return original_open()

    monkeypatch.setattr(logger, "_open_handle", flaky_open)

    logger.record("one")
    logger.record("two")
    await logger.flush()
    status = logger.status()

    assert status["enqueued"] == 2
    assert status["written"] == 2
    assert status["failed"] == 0
    assert status["healthy"] is True
    await logger.aclose()


@pytest.mark.asyncio
async def test_audit_logger_survives_and_reports_a_persistent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken audit path must be visible, and must not take the daemon down."""
    logger = AuditLogger(tmp_path / "audit.jsonl")
    monkeypatch.setattr(logger, "_open_handle", _always_fail)

    logger.record("one")
    logger.record("two")
    await logger.flush()
    status = logger.status()

    assert status["failed"] == 2
    assert status["healthy"] is False
    assert "simulated permanent disk error" in status["last_error"]
    assert status["writer_running"] is True, "the writer must survive a failing audit path"
    # Reported once on stderr rather than per event, so a broken path is noticed
    # without flooding the daemon output.
    assert capsys.readouterr().err.count("audit log write failed") == 1
    await logger.aclose()


def _always_fail():
    raise OSError("simulated permanent disk error")


@pytest.mark.asyncio
async def test_audit_logger_writes_inline_instead_of_dropping_when_queue_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue overflow must degrade to a blocking write, never to a dropped record."""
    logger = AuditLogger(tmp_path / "audit.jsonl")
    logger.record("prime-the-writer")
    assert logger._write_queue is not None
    monkeypatch.setattr(logger._write_queue, "put_nowait", _raise_queue_full)

    logger.record("overflowed")
    await logger.flush()
    status = logger.status()

    assert status["inline"] == 1
    assert status["failed"] == 0
    events = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    assert "overflowed" in [event["event_type"] for event in events]
    await logger.aclose()


def _raise_queue_full(_event):
    raise asyncio.QueueFull


@pytest.mark.asyncio
async def test_audit_logger_rotates_by_size_and_keeps_backups(tmp_path: Path) -> None:
    """A long-running daemon cannot append to one file forever."""
    logger = AuditLogger(tmp_path / "audit.jsonl", max_bytes=400, backup_count=2)

    for index in range(40):
        logger.record(f"event.{index}", data={"index": index})
    await logger.flush()
    await logger.aclose()

    assert logger.path.is_file()
    assert logger.path.with_name("audit.jsonl.1").is_file()
    assert logger.path.with_name("audit.jsonl.2").is_file()
    # backup_count=2 caps the retained history; no third backup accumulates.
    assert not logger.path.with_name("audit.jsonl.3").exists()
    assert logger.path.stat().st_size <= 400 * 2


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
