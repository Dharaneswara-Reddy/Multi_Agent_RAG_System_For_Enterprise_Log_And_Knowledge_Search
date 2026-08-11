"""OpenTelemetry setup and GenAI span helpers.

Attribute names follow the OpenTelemetry **GenAI semantic conventions**
(`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.response.finish_reasons`).
Those conventions are still in *Development* status as of mid-2026 — not GA — so
the OTel dependency is pinned and the attribute keys live in one place here.
When the spec stabilises, this module is the only thing that changes.

Exporting is optional. With no `AIOPS_OTLP_ENDPOINT` set, spans are collected
in-process by `SpanCollector` so the Streamlit dashboard has something to draw
without requiring anyone to run a collector just to see a trace.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanProcessor
from opentelemetry.trace import Status, StatusCode

from aiops.config import settings

# --- GenAI semantic convention keys (spec status: Development) --------------
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"

# --- Project-specific attributes -------------------------------------------
AIOPS_COST_USD = "aiops.cost_usd"
AIOPS_ROUTE = "aiops.route"
AIOPS_CONFIDENCE = "aiops.confidence"
AIOPS_VERDICT = "aiops.verdict"
AIOPS_RETRIEVED_K = "aiops.retrieval.k"
AIOPS_RETRIEVAL_TOP_SCORE = "aiops.retrieval.top_score"
AIOPS_GUARDRAIL = "aiops.guardrail"


@dataclass
class SpanRecord:
    name: str
    start_ms: float
    duration_ms: float
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "OK"
    trace_id: str = ""
    span_id: str = ""
    parent_id: str | None = None


class SpanCollector(SpanProcessor):
    """Keeps recent spans in memory so the dashboard works with no collector."""

    def __init__(self, maxlen: int = 4000) -> None:
        self._spans: deque[SpanRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def on_start(self, span, parent_context=None) -> None:
        return

    def on_end(self, span: ReadableSpan) -> None:
        ctx = span.get_span_context()
        rec = SpanRecord(
            name=span.name,
            start_ms=(span.start_time or 0) / 1e6,
            duration_ms=((span.end_time or 0) - (span.start_time or 0)) / 1e6,
            attributes=dict(span.attributes or {}),
            status=span.status.status_code.name if span.status else "UNSET",
            trace_id=format(ctx.trace_id, "032x"),
            span_id=format(ctx.span_id, "016x"),
            parent_id=format(span.parent.span_id, "016x") if span.parent else None,
        )
        with self._lock:
            self._spans.append(rec)

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def recent(self, limit: int = 500) -> list[SpanRecord]:
        with self._lock:
            return list(self._spans)[-limit:]

    def by_trace(self, trace_id: str) -> list[SpanRecord]:
        with self._lock:
            return [s for s in self._spans if s.trace_id == trace_id]

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


_COLLECTOR = SpanCollector()
_CONFIGURED = False
_CONFIG_LOCK = threading.Lock()


def configure_tracing(force: bool = False) -> SpanCollector:
    """Idempotent tracer setup. Safe to call from every entry point."""
    global _CONFIGURED
    with _CONFIG_LOCK:
        if _CONFIGURED and not force:
            return _COLLECTOR
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.service_name,
                    "service.version": "0.1.0",
                }
            )
        )
        provider.add_span_processor(_COLLECTOR)
        if settings.otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
                )
            except Exception:  # a missing collector must not break the app
                pass
        trace.set_tracer_provider(provider)
        _CONFIGURED = True
        return _COLLECTOR


def get_collector() -> SpanCollector:
    configure_tracing()
    return _COLLECTOR


def tracer(name: str = "aiops"):
    configure_tracing()
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Start a span, record wall time, and mark it ERROR on exception."""
    with tracer().start_as_current_span(name) as sp:
        for k, v in attributes.items():
            if v is not None:
                sp.set_attribute(k, v)
        started = time.perf_counter()
        try:
            yield sp
        except Exception as exc:
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            sp.record_exception(exc)
            raise
        finally:
            sp.set_attribute("duration_ms", round((time.perf_counter() - started) * 1000, 2))


def current_trace_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx and ctx.trace_id else ""
