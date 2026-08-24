from __future__ import annotations

import os
from typing import Any

OTEL_ENABLED_ENV = "AICODE_OTEL_ENABLED"
OTEL_ENDPOINT_ENV = "AICODE_OTEL_ENDPOINT"
OTEL_SERVICE_NAME_ENV = "AICODE_OTEL_SERVICE_NAME"
DEFAULT_SERVICE_NAME = "aicode-runtime"
_TRUTHY = {"1", "true", "yes", "on"}

INSTALL_HINT = (
    "OpenTelemetry export is enabled but the SDK is not installed. "
    "Install it with `pip install 'aicode-runtime[otel]'`, or unset "
    f"{OTEL_ENABLED_ENV} to keep JSONL-only tracing."
)


def otel_enabled() -> bool:
    return os.getenv(OTEL_ENABLED_ENV, "").strip().casefold() in _TRUTHY


def otel_endpoint() -> str:
    # Empty means "let the SDK read the standard OTEL_EXPORTER_OTLP_* variables".
    return os.getenv(OTEL_ENDPOINT_ENV, "").strip()


def otel_service_name() -> str:
    return os.getenv(OTEL_SERVICE_NAME_ENV, "").strip() or DEFAULT_SERVICE_NAME


class OtelSpanEmitter:
    """SpanEmitter backed by the OpenTelemetry SDK.

    The SDK is an optional dependency and is imported lazily, so a Runtime with
    tracing disabled neither needs nor loads it. Enabling tracing without the SDK
    installed fails loudly rather than silently dropping traces — a tracing
    backend the operator believes is running but is not is worse than none.
    """

    def __init__(self, *, endpoint: str = "", service_name: str = DEFAULT_SERVICE_NAME) -> None:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.trace import Status, StatusCode, set_span_in_context
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(INSTALL_HINT) from exc

        self._status = Status
        self._status_code = StatusCode
        self._set_span_in_context = set_span_in_context
        exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        self._provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("aicode.runtime")

    def start_span(self, name: str, *, parent: Any = None, attributes: dict[str, Any]) -> Any:
        context = self._set_span_in_context(parent) if parent is not None else None
        # Spans are started without a context manager because their lifetime is
        # driven by recorded events, not by a lexical block.
        return self._tracer.start_span(name, context=context, attributes=attributes)

    def end_span(self, handle: Any, *, attributes: dict[str, Any], failed: bool) -> None:
        handle.set_attributes(attributes)
        handle.set_status(self._status(self._status_code.ERROR if failed else self._status_code.OK))
        handle.end()

    def add_event(self, handle: Any, name: str, attributes: dict[str, Any]) -> None:
        handle.add_event(name, attributes=attributes)

    def shutdown(self) -> None:
        self._provider.shutdown()
