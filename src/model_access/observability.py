from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from .contracts.responses import Usage

_SPAN_KINDS = {
    "internal": SpanKind.INTERNAL,
    "client": SpanKind.CLIENT,
    "server": SpanKind.SERVER,
    "producer": SpanKind.PRODUCER,
    "consumer": SpanKind.CONSUMER,
}


class SpanHandle(AbstractContextManager["SpanHandle"]):
    def __init__(self, span: trace.Span):
        self.span = span

    def __enter__(self) -> SpanHandle:
        return self

    def set_attribute(self, name: str, value: Any) -> None:
        if value is not None and isinstance(value, (str, bool, int, float)):
            self.span.set_attribute(name, value)

    def record_exception(self, exc: BaseException, error_type: str | None = None) -> None:
        self.span.record_exception(exc)
        self.span.set_status(Status(StatusCode.ERROR, str(exc)[:256]))
        if error_type:
            self.span.set_attribute("error.type", error_type)

    def __exit__(self, exc_type, exc, traceback) -> bool:  # type: ignore[no-untyped-def]
        if exc is not None:
            self.record_exception(exc)
        self.span.end()
        return False


class OpenTelemetryFacade:
    """Centralized, content-free OpenTelemetry mapping.

    The API works with the no-op global providers too, so telemetry exporter
    failures never become model invocation failures.
    """

    def __init__(self, instrumentation_name: str = "model_access", version: str = "0.2.0"):
        self._tracer = trace.get_tracer(instrumentation_name, version)
        meter = metrics.get_meter(instrumentation_name, version)
        self._invocations = meter.create_counter(
            "model_access.invocation.total", unit="{call}", description="Model invocations"
        )
        self._duration = meter.create_histogram(
            "gen_ai.client.operation.duration", unit="s", description="Provider call duration"
        )
        self._input_tokens = meter.create_histogram("gen_ai.client.token.usage", unit="{token}")

    def start_span(
        self,
        name: str,
        attributes: dict[str, Any],
        *,
        kind: str = "internal",
    ) -> SpanHandle:
        safe_attributes = {
            key: value
            for key, value in attributes.items()
            if value is not None and isinstance(value, (str, bool, int, float))
        }
        span = self._tracer.start_span(name, kind=_SPAN_KINDS.get(kind, SpanKind.INTERNAL))
        for key, value in safe_attributes.items():
            span.set_attribute(key, value)
        return SpanHandle(span)

    def current_trace_id(self) -> str | None:
        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return None
        return trace.format_trace_id(span_context.trace_id)

    def record_invocation(
        self,
        *,
        provider: str,
        model_type: str,
        status: str,
        duration_seconds: float,
        usage: Usage | None,
    ) -> None:
        attributes = {
            "gen_ai.provider.name": provider,
            "model_access.model.type": model_type,
            "status": status,
        }
        self._invocations.add(1, attributes)
        self._duration.record(duration_seconds, attributes)
        if usage and usage.input_tokens is not None:
            self._input_tokens.record(
                usage.input_tokens,
                {**attributes, "gen_ai.token.type": "input"},
            )
        if usage and usage.output_tokens is not None:
            self._input_tokens.record(
                usage.output_tokens,
                {**attributes, "gen_ai.token.type": "output"},
            )
