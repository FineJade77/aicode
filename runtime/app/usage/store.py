from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class UsageRecord:
    timestamp: datetime
    session_id: str
    workspace: str
    provider: str
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    # False when the call was cut off before the provider reported usage, so
    # its token and cost fields are zero for lack of data rather than because
    # nothing was spent.
    complete: bool = True


class JsonlUsageRuntime:
    """Read usage aggregates from the append-only audit JSONL stream."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def summarize(self, *, session_id: str | None = None, day: date | None = None) -> dict[str, Any]:
        return summarize_usage(self.path, session_id=session_id, day=day)


def summarize_usage(audit_path: Path, *, session_id: str | None = None, day: date | None = None) -> dict[str, Any]:
    records = list(read_usage_records(audit_path))
    if session_id is not None:
        records = [record for record in records if record.session_id == session_id]
    if day is not None:
        records = [record for record in records if record.timestamp.date() == day]

    by_model: dict[str, dict[str, Any]] = defaultdict(empty_summary)
    by_provider: dict[str, dict[str, Any]] = defaultdict(empty_summary)
    by_purpose: dict[str, dict[str, Any]] = defaultdict(empty_summary)

    summary = empty_summary()
    for record in records:
        add_record(summary, record)
        add_record(by_model[record.model], record)
        add_record(by_provider[record.provider], record)
        add_record(by_purpose[record.purpose], record)

    return {
        "storage": "local",
        "record_count": summary["record_count"],
        "total_input_tokens": summary["input_tokens"],
        "total_output_tokens": summary["output_tokens"],
        "total_tokens": summary["total_tokens"],
        "estimated_cost": round(summary["estimated_cost"], 8),
        # A non-zero count means every total above is a lower bound: these calls
        # consumed tokens the provider never got to report. Reported rather than
        # estimated — a made-up number that looks like a measurement is the
        # thing this project treats as worse than a gap.
        "incomplete_calls": summary["incomplete_calls"],
        "by_provider": normalize_groups(by_provider),
        "by_model": normalize_groups(by_model),
        "by_purpose": normalize_groups(by_purpose),
        "filters": {
            "session_id": session_id,
            "date": day.isoformat() if day else None,
        },
        "audit_path": str(audit_path),
    }


def read_usage_records(audit_path: Path):
    if not audit_path.exists():
        return

    with audit_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event_type") != "usage.recorded":
                continue

            data = event.get("data") or {}
            timestamp = parse_timestamp(str(event.get("timestamp", "")))
            if timestamp is None:
                continue

            yield UsageRecord(
                timestamp=timestamp,
                session_id=str(event.get("session_id") or ""),
                workspace=str(event.get("workspace") or ""),
                provider=str(data.get("provider") or "unknown"),
                complete=bool(data.get("complete", True)),
                model=str(data.get("model") or "unknown"),
                purpose=str(data.get("purpose") or "unknown"),
                input_tokens=as_int(data.get("input_tokens")),
                output_tokens=as_int(data.get("output_tokens")),
                estimated_cost=as_float(data.get("estimated_cost")),
            )


def empty_summary() -> dict[str, Any]:
    return {
        "record_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost": 0.0,
        "incomplete_calls": 0,
    }


def add_record(summary: dict[str, Any], record: UsageRecord) -> None:
    summary["record_count"] += 1
    if not record.complete:
        summary["incomplete_calls"] += 1
    summary["input_tokens"] += record.input_tokens
    summary["output_tokens"] += record.output_tokens
    summary["total_tokens"] += record.input_tokens + record.output_tokens
    summary["estimated_cost"] += record.estimated_cost


def normalize_groups(groups: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "record_count": value["record_count"],
            "input_tokens": value["input_tokens"],
            "output_tokens": value["output_tokens"],
            "total_tokens": value["total_tokens"],
            "estimated_cost": round(value["estimated_cost"], 8),
            "incomplete_calls": value["incomplete_calls"],
        }
        for key, value in sorted(groups.items())
    }


def parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
