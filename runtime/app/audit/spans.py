from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.audit.redaction import redact

# Trace events that open a span, mapped to the span name and the data key that
# identifies the span. `run.started` carries run_id; tool and execution events
# carry tool_call_id / execution_id.
SPAN_OPENERS = {
    "run.started": ("run", "run_id"),
    "tool.started": ("tool.call", "tool_call_id"),
    "execution.started": ("execution", "execution_id"),
}

# Events that close an open span. `session.final` and `session.error` carry no
# run_id, so a run is closed by session instead of by key.
SPAN_CLOSERS = {
    "tool.finished": ("tool.call", "tool_call_id"),
    "execution.finished": ("execution", "execution_id"),
    "run.cancelled": ("run", "run_id"),
    "session.final": ("run", None),
    "session.error": ("run", None),
}

FAILURE_EVENTS = {"session.error", "tool.argument_parse_error", "tool.argument_validation_error"}


class SpanHandle(Protocol):
    """Opaque handle returned by an emitter; only the emitter interprets it."""


class SpanEmitter(Protocol):
    """Backend that turns derived spans into whatever a tracing system wants.

    Kept separate from the derivation logic so the event-to-span mapping is
    testable without installing an OpenTelemetry SDK, and so a second backend can
    be added without touching the derivation.
    """

    def start_span(
        self,
        name: str,
        *,
        parent: Any = None,
        attributes: dict[str, Any],
    ) -> Any: ...

    def end_span(self, handle: Any, *, attributes: dict[str, Any], failed: bool) -> None: ...

    def add_event(self, handle: Any, name: str, attributes: dict[str, Any]) -> None: ...

    def shutdown(self) -> None: ...


@dataclass(slots=True)
class _OpenSpan:
    kind: str
    key: str
    handle: Any
    parent: Any


@dataclass(slots=True)
class _SessionSpans:
    """Spans currently open for one session, innermost last."""

    stack: list[_OpenSpan] = field(default_factory=list)

    def innermost(self) -> _OpenSpan | None:
        return self.stack[-1] if self.stack else None

    def find(self, kind: str, key: str | None) -> _OpenSpan | None:
        for span in reversed(self.stack):
            if span.kind != kind:
                continue
            if key is None or span.key == key:
                return span
        return None

    def remove(self, span: _OpenSpan) -> None:
        # Anything opened inside the closing span is abandoned with it; a tool
        # span cannot outlive the run that issued it.
        index = self.stack.index(span)
        del self.stack[index:]


class SpanTraceSink:
    """TraceSink decorator that also derives spans from recorded events.

    Wraps the existing JSONL sink rather than replacing it: the local audit trail
    stays the source of truth and tracing is additive, so losing a tracing
    backend never costs an audit record.

    Spans are derived from the start/finish pairs the Runtime already records
    (`run.started`/`session.final`, `tool.started`/`tool.finished`,
    `execution.started`/`execution.finished`), producing the hierarchy
    `run -> tool.call -> execution`. Every other event becomes a point event on
    the innermost open span for that session.
    """

    def __init__(self, inner: Any, emitter: SpanEmitter) -> None:
        self.inner = inner
        self.emitter = emitter
        self._sessions: dict[str, _SessionSpans] = {}

    @property
    def path(self) -> Path:
        return self.inner.path

    def record(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.inner.record(event_type, session_id=session_id, workspace=workspace, data=data)
        try:
            self._derive(event_type, session_id, workspace, redact(data or {}))
        except Exception:
            # Tracing must never break the audit path or an agent turn. A broken
            # exporter is a degraded observability signal, not a failed run.
            pass

    def status(self) -> dict[str, Any]:
        return {**self.inner.status(), "spans": {"open_sessions": len(self._sessions)}}

    async def flush(self) -> None:
        await self.inner.flush()

    async def aclose(self) -> None:
        # Close spans still open at shutdown so a killed daemon does not leave
        # dangling traces, then release the exporter before the inner sink.
        for session_id in list(self._sessions):
            spans = self._sessions.pop(session_id)
            for span in reversed(spans.stack):
                self.emitter.end_span(span.handle, attributes={"aicode.shutdown": True}, failed=False)
        self.emitter.shutdown()
        await self.inner.aclose()

    def _derive(
        self,
        event_type: str,
        session_id: str | None,
        workspace: str | None,
        data: dict[str, Any],
    ) -> None:
        if not session_id:
            # Session-less events (project trust changes, retention passes) have
            # no span to attach to; the JSONL trail already records them.
            return
        spans = self._sessions.setdefault(session_id, _SessionSpans())

        closer = SPAN_CLOSERS.get(event_type)
        if closer is not None:
            kind, key_field = closer
            key = str(data.get(key_field) or "") if key_field else None
            target = spans.find(kind, key)
            if target is not None:
                self.emitter.end_span(
                    target.handle,
                    attributes=span_attributes(event_type, data),
                    failed=event_type in FAILURE_EVENTS or data.get("success") is False,
                )
                spans.remove(target)
                if not spans.stack:
                    self._sessions.pop(session_id, None)
                return
            # No matching open span (daemon restarted mid-run, or the opener was
            # never recorded): fall through and keep it as a point event.

        opener = SPAN_OPENERS.get(event_type)
        if opener is not None:
            kind, key_field = opener
            parent = spans.innermost()
            handle = self.emitter.start_span(
                span_name(kind, data),
                parent=parent.handle if parent is not None else None,
                attributes={
                    "aicode.session_id": session_id,
                    **({"aicode.workspace": workspace} if workspace else {}),
                    **span_attributes(event_type, data),
                },
            )
            spans.stack.append(
                _OpenSpan(kind=kind, key=str(data.get(key_field) or ""), handle=handle, parent=parent)
            )
            return

        current = spans.innermost()
        if current is None:
            self._sessions.pop(session_id, None)
            return
        self.emitter.add_event(current.handle, event_type, span_attributes(event_type, data))


def span_name(kind: str, data: dict[str, Any]) -> str:
    if kind == "tool.call":
        tool = str(data.get("tool") or "").strip()
        return f"tool.call {tool}" if tool else "tool.call"
    if kind == "execution":
        action = str(data.get("action") or "").strip()
        return f"execution {action}" if action else "execution"
    return kind


def span_attributes(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Flatten already-redacted event data into span attributes.

    Values are limited to the scalar types tracing backends accept; anything
    else is stringified so a nested payload cannot be dropped silently.
    """
    attributes: dict[str, Any] = {"aicode.event": event_type}
    for key, value in data.items():
        if value is None:
            continue
        name = f"aicode.{key}"
        attributes[name] = value if isinstance(value, (str, bool, int, float)) else str(value)
    return attributes
