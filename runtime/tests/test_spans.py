from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.adapters.composition import build_trace_sink
from app.audit import otel
from app.audit.logger import AuditLogger
from app.audit.spans import SpanTraceSink


@dataclass
class RecordedSpan:
    name: str
    parent: RecordedSpan | None
    attributes: dict[str, Any]
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    ended: bool = False
    failed: bool = False

    def path(self) -> str:
        """Slash-joined ancestry, so tests can assert the hierarchy directly."""
        names = [self.name]
        parent = self.parent
        while parent is not None:
            names.append(parent.name)
            parent = parent.parent
        return " / ".join(reversed(names))


class RecordingEmitter:
    """In-memory SpanEmitter.

    Lets the event-to-span derivation be tested without installing an
    OpenTelemetry SDK; the SDK binding is a thin separate adapter.
    """

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []
        self.shutdowns = 0

    def start_span(self, name: str, *, parent: Any = None, attributes: dict[str, Any]) -> RecordedSpan:
        span = RecordedSpan(name=name, parent=parent, attributes=dict(attributes))
        self.spans.append(span)
        return span

    def end_span(self, handle: RecordedSpan, *, attributes: dict[str, Any], failed: bool) -> None:
        handle.attributes.update(attributes)
        handle.ended = True
        handle.failed = failed

    def add_event(self, handle: RecordedSpan, name: str, attributes: dict[str, Any]) -> None:
        handle.events.append((name, dict(attributes)))

    def shutdown(self) -> None:
        self.shutdowns += 1

    def named(self, name: str) -> RecordedSpan:
        matches = [span for span in self.spans if span.name == name]
        assert matches, f"no span named {name!r}; have {[s.name for s in self.spans]}"
        return matches[-1]


@pytest.fixture
def sink(tmp_path: Path) -> tuple[SpanTraceSink, RecordingEmitter]:
    emitter = RecordingEmitter()
    return SpanTraceSink(AuditLogger(tmp_path / "audit.jsonl"), emitter), emitter


def test_spans_form_the_run_tool_execution_hierarchy(sink) -> None:
    trace, emitter = sink
    session = "sess_1"

    trace.record("run.started", session_id=session, workspace="/repo", data={"run_id": "run_1"})
    trace.record("tool.started", session_id=session, data={"tool": "bash", "tool_call_id": "tc_1"})
    trace.record(
        "execution.started",
        session_id=session,
        data={"execution_id": "exec_1", "action": "agent.bash", "run_id": "run_1", "tool_call_id": "tc_1"},
    )
    trace.record("execution.finished", session_id=session, data={"execution_id": "exec_1", "exit_code": 0})
    trace.record("tool.finished", session_id=session, data={"tool": "bash", "tool_call_id": "tc_1", "success": True})
    trace.record("session.final", session_id=session, data={"mode": "default"})

    assert emitter.named("execution agent.bash").path() == "run / tool.call bash / execution agent.bash"
    assert all(span.ended for span in emitter.spans)
    assert not any(span.failed for span in emitter.spans)


def test_point_events_attach_to_the_innermost_open_span(sink) -> None:
    trace, emitter = sink
    session = "sess_1"

    trace.record("run.started", session_id=session, data={"run_id": "run_1"})
    trace.record("usage.recorded", session_id=session, data={"model": "m", "input_tokens": 10})
    trace.record("tool.started", session_id=session, data={"tool": "read_file", "tool_call_id": "tc_1"})
    trace.record("edit.applied", session_id=session, data={"path": "a.py"})
    trace.record("tool.finished", session_id=session, data={"tool_call_id": "tc_1", "success": True})
    trace.record("run.budget.exceeded", session_id=session, data={"reason": "cost"})

    run = emitter.named("run")
    tool = emitter.named("tool.call read_file")
    assert [name for name, _ in run.events] == ["usage.recorded", "run.budget.exceeded"]
    assert [name for name, _ in tool.events] == ["edit.applied"]


def test_failed_tool_marks_the_span_as_error(sink) -> None:
    trace, emitter = sink
    session = "sess_1"

    trace.record("run.started", session_id=session, data={"run_id": "run_1"})
    trace.record("tool.started", session_id=session, data={"tool": "bash", "tool_call_id": "tc_1"})
    trace.record("tool.finished", session_id=session, data={"tool_call_id": "tc_1", "success": False})

    assert emitter.named("tool.call bash").failed is True


def test_session_error_fails_and_closes_the_run_span(sink) -> None:
    trace, emitter = sink
    session = "sess_1"

    trace.record("run.started", session_id=session, data={"run_id": "run_1"})
    trace.record("session.error", session_id=session, data={"error_type": "ProviderError"})

    run = emitter.named("run")
    assert run.ended is True
    assert run.failed is True


def test_closing_a_run_abandons_spans_opened_inside_it(sink) -> None:
    """A tool span cannot outlive the run that issued it.

    Without this a cancelled run would leave a tool span open forever, and the
    next run's events would be attached under it.
    """
    trace, emitter = sink
    session = "sess_1"

    trace.record("run.started", session_id=session, data={"run_id": "run_1"})
    trace.record("tool.started", session_id=session, data={"tool": "bash", "tool_call_id": "tc_1"})
    trace.record("run.cancelled", session_id=session, data={"run_id": "run_1"})

    trace.record("run.started", session_id=session, data={"run_id": "run_2"})
    trace.record("usage.recorded", session_id=session, data={"model": "m"})

    runs = [span for span in emitter.spans if span.name == "run"]
    assert len(runs) == 2
    assert runs[1].parent is None, "a new run must not be nested under the cancelled one"
    assert [name for name, _ in runs[1].events] == ["usage.recorded"]


def test_concurrent_sessions_do_not_share_spans(sink) -> None:
    trace, emitter = sink

    trace.record("run.started", session_id="sess_a", data={"run_id": "run_a"})
    trace.record("run.started", session_id="sess_b", data={"run_id": "run_b"})
    trace.record("tool.started", session_id="sess_b", data={"tool": "search", "tool_call_id": "tc_b"})
    trace.record("usage.recorded", session_id="sess_a", data={"model": "m"})

    tool = emitter.named("tool.call search")
    assert tool.parent is not None
    assert tool.parent.attributes["aicode.run_id"] == "run_b"
    run_a = next(s for s in emitter.spans if s.attributes.get("aicode.run_id") == "run_a")
    assert [name for name, _ in run_a.events] == ["usage.recorded"]


def test_secrets_are_redacted_before_reaching_span_attributes(sink) -> None:
    """Spans go to an external system, so they reuse the audit redaction."""
    trace, emitter = sink

    trace.record("run.started", session_id="sess_1", data={"run_id": "run_1"})
    trace.record("tool.started", session_id="sess_1", data={"tool": "bash", "tool_call_id": "tc_1", "api_key": "sk-secret"})

    attributes = emitter.named("tool.call bash").attributes
    assert "sk-secret" not in str(attributes)


def test_unmatched_close_event_does_not_lose_the_run_span(sink) -> None:
    """A finish without its opener (daemon restarted mid-run) must degrade to a
    point event rather than closing an unrelated span."""
    trace, emitter = sink
    session = "sess_1"

    trace.record("run.started", session_id=session, data={"run_id": "run_1"})
    trace.record("tool.finished", session_id=session, data={"tool_call_id": "tc_unknown", "success": True})

    run = emitter.named("run")
    assert run.ended is False
    assert [name for name, _ in run.events] == ["tool.finished"]


def test_session_less_events_are_audited_but_not_traced(sink) -> None:
    trace, emitter = sink

    trace.record("project.trust.changed", workspace="/repo", data={"level": "trusted"})
    trace.record("session.pruned", data={"deleted_sessions": 2})

    assert emitter.spans == []


def test_tracing_failures_never_break_the_audit_path(tmp_path: Path) -> None:
    """A broken exporter is a degraded observability signal, not a failed run."""

    class BrokenEmitter(RecordingEmitter):
        def start_span(self, name, *, parent=None, attributes):
            raise RuntimeError("exporter is down")

    audit = AuditLogger(tmp_path / "audit.jsonl")
    trace = SpanTraceSink(audit, BrokenEmitter())

    trace.record("run.started", session_id="sess_1", data={"run_id": "run_1"})

    assert audit.status()["failed"] == 0
    assert audit.path.read_text(encoding="utf-8").count("run.started") == 1


@pytest.mark.asyncio
async def test_aclose_ends_dangling_spans_and_releases_the_exporter(sink) -> None:
    trace, emitter = sink
    trace.record("run.started", session_id="sess_1", data={"run_id": "run_1"})
    trace.record("tool.started", session_id="sess_1", data={"tool": "bash", "tool_call_id": "tc_1"})

    await trace.aclose()

    assert all(span.ended for span in emitter.spans), "a killed daemon must not leave dangling traces"
    assert emitter.shutdowns == 1


def test_status_reports_both_the_audit_writer_and_open_spans(sink) -> None:
    trace, emitter = sink
    trace.record("run.started", session_id="sess_1", data={"run_id": "run_1"})

    status = trace.status()

    assert "written" in status, "the audit writer's own status must still be reported"
    assert status["spans"]["open_sessions"] == 1


def test_tracing_is_off_by_default_and_yields_the_plain_audit_sink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(otel.OTEL_ENABLED_ENV, raising=False)
    monkeypatch.setenv("AICODE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    assert isinstance(build_trace_sink(), AuditLogger)


def test_enabling_tracing_without_the_sdk_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A tracing backend the operator believes is running but is not is worse
    than none, so a missing SDK must not degrade to silence."""
    monkeypatch.setenv(otel.OTEL_ENABLED_ENV, "1")
    monkeypatch.setenv("AICODE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    def missing_sdk(*_args, **_kwargs):
        raise RuntimeError(otel.INSTALL_HINT)

    monkeypatch.setattr("app.adapters.composition.OtelSpanEmitter", missing_sdk)

    with pytest.raises(RuntimeError) as excinfo:
        build_trace_sink()

    assert "aicode-runtime[otel]" in str(excinfo.value), "the error must say how to install it"
    assert otel.OTEL_ENABLED_ENV in str(excinfo.value), "and how to turn it off"


def test_enabled_tracing_wraps_the_audit_sink_rather_than_replacing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(otel.OTEL_ENABLED_ENV, "1")
    monkeypatch.setenv("AICODE_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr("app.adapters.composition.OtelSpanEmitter", lambda **_kwargs: RecordingEmitter())

    sink = build_trace_sink()

    assert isinstance(sink, SpanTraceSink)
    assert isinstance(sink.inner, AuditLogger), "the JSONL trail stays the source of truth"
    assert sink.path == sink.inner.path


def test_otel_settings_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(otel.OTEL_SERVICE_NAME_ENV, raising=False)
    monkeypatch.delenv(otel.OTEL_ENDPOINT_ENV, raising=False)
    assert otel.otel_service_name() == otel.DEFAULT_SERVICE_NAME
    # Empty means "defer to the standard OTEL_EXPORTER_OTLP_* variables".
    assert otel.otel_endpoint() == ""

    monkeypatch.setenv(otel.OTEL_SERVICE_NAME_ENV, "aicode-dev")
    monkeypatch.setenv(otel.OTEL_ENDPOINT_ENV, "http://localhost:4318/v1/traces")
    assert otel.otel_service_name() == "aicode-dev"
    assert otel.otel_endpoint() == "http://localhost:4318/v1/traces"

    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv(otel.OTEL_ENABLED_ENV, value)
        assert otel.otel_enabled() is True
    monkeypatch.setenv(otel.OTEL_ENABLED_ENV, "0")
    assert otel.otel_enabled() is False
