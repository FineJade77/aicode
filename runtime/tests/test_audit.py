import json
from pathlib import Path

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


def test_redact_truncates_long_strings() -> None:
    value = redact({"message": "x" * 1200})

    assert value["message"].endswith("...[TRUNCATED]")


def test_redact_keeps_token_usage_counts() -> None:
    value = redact({"input_tokens": 10, "output_tokens": 5, "api_token": "secret"})

    assert value["input_tokens"] == 10
    assert value["output_tokens"] == 5
    assert value["api_token"] == "[REDACTED]"


def test_stable_hash_is_deterministic() -> None:
    assert stable_hash("abc") == stable_hash("abc")
