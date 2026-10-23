"""Metrics (Prometheus), traces (OpenTelemetry) and redacted structured logs."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from qed.guard import redact

REGISTRY = CollectorRegistry()
PLANS = Counter("qed_compiles_total", "Compilations by outcome", ["outcome"], registry=REGISTRY)
BOOKINGS = Counter("qed_bookings_total", "Bookings by outcome", ["outcome"], registry=REGISTRY)
STAGE = Histogram(
    "qed_stage_seconds",
    "Latency per compiler stage",
    ["stage"],
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5),
)
COMPILE = Histogram(
    "qed_compile_seconds",
    "End-to-end compile latency",
    registry=REGISTRY,
    buckets=(0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1, 2, 5),
)
PARTNER = Counter("qed_partner_calls_total", "Partner calls", ["partner", "freshness"], registry=REGISTRY)
BREAKER = Gauge("qed_breaker_open", "1 when a partner circuit is open", ["partner"], registry=REGISTRY)
GUARD = Counter("qed_guard_events_total", "Guardrail events", ["kind"], registry=REGISTRY)
DISRUPTIONS = Counter("qed_disruptions_total", "Detected trip disruptions", ["kind"], registry=REGISTRY)

_tracer: trace.Tracer | None = None


def setup(*, json_logs: bool, otel_console: bool) -> None:
    global _tracer
    provider = TracerProvider(resource=Resource.create({"service.name": "qed", "service.version": "0.1.0"}))
    if otel_console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("qed")

    def _redact(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
        for k, v in list(event.items()):
            if isinstance(v, str):
                event[k] = redact(v)
        return event

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact,
            structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )
    for noisy in (
        "mcp",
        "httpx",
        "httpx2",
        "httpcore",
        "uvicorn.access",
        "mcp.server.streamable_http",
        "mcp.server.streamable_http_manager",
        "mcp.client.streamable_http",
        "strands",
        "botocore",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    tracer = _tracer or trace.get_tracer("qed")
    with tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if isinstance(v, (str, int, float, bool)):
                s.set_attribute(k, v)
        yield s


def metrics_text() -> bytes:
    return generate_latest(REGISTRY)


log = structlog.get_logger("qed")
