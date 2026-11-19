"""OpenTelemetry to the hub's Collector (H-006 Observability).

On only when OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is set. Spans carry a hashed Slack user ID,
the channel, thread and A2A IDs and the outcome: never tokens, never message text.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
from typing import Iterator

_tracer = None


def setup(service: str = "slack-bridge") -> bool:
    global _tracer
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if not endpoint:
        return False
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    provider = TracerProvider(resource=Resource.create({
        "service.name": service, "openinference.project.name": "research-agent"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("slack_bridge")
    return True


def user_hash(team: str, user: str) -> str:
    return hashlib.sha256(f"{team}/{user}".encode()).hexdigest()[:16]


@contextlib.contextmanager
def span(name: str, **attrs: object) -> Iterator[object]:
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v)  # type: ignore[arg-type]
        yield s


def set_attr(s: object, key: str, value: object) -> None:
    if s is not None and value is not None:
        s.set_attribute(key, value)  # type: ignore[attr-defined]


def trace_headers() -> dict[str, str]:
    """traceparent for the A2A call, so research-agent's trace links under the bridge's."""
    if _tracer is None:
        return {}
    from opentelemetry import propagate
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier
