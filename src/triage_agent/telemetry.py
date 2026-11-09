"""OpenTelemetry tracing setup.

Spans are always created (they are cheap). They are exported over OTLP/HTTP
only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, so local runs and CI need no
collector. Tests can pass an in-memory exporter to assert on spans.
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

_configured = False


def setup_tracing(exporter: SpanExporter | None = None) -> TracerProvider | None:
    """Install a global tracer provider once. Returns it, or None if already set."""
    global _configured
    if _configured:
        return None
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": os.environ.get("OTEL_SERVICE_NAME", "aiops-triage-agent")}
        )
    )
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True
    return provider
