"""OpenTelemetry initialization and utilities.

SDK wiring only. Span-creation helpers live in SiteRMLibs.OtelWrapper, which is
dependency-free so DBBackend can use it too; see the note there.

Everything here is optional. If the opentelemetry packages are not installed,
init_otel() returns quietly and SiteRM runs untraced rather than failing to
import.
"""

import os

from SiteRMLibs import __version__
from SiteRMLibs.MainUtilities import loadEnvFile
from SiteRMLibs.OtelWrapper import OTEL_AVAILABLE, envBool, otelEnabled

try:  # pragma: no cover - import guard, see module docstring
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    OTEL_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    OTEL_SDK_AVAILABLE = False

loadEnvFile()

__all__ = ["init_otel", "otelEnabled", "OTEL_AVAILABLE"]


def _buildExporter(endpoint):
    """OTLP span exporter for `endpoint`.

    TLS is the default. The previous revision hardcoded `insecure=True`, which is
    fine for an in-cluster collector but not for the deployed topology, where the
    frontend is the site's egress point and pushes spans across the internet to a
    central gateway. Spans carry delta ids, topology and the authenticated user
    subject; that is not plaintext material.

    `insecure` is passed only when OTLP_INSECURE is explicitly set, so when it is
    unset the SDK's own OTEL_EXPORTER_OTLP_* environment handling applies and the
    channel defaults to secure.
    """
    kwargs = {"endpoint": endpoint}

    if os.getenv("OTLP_INSECURE") is not None:
        kwargs["insecure"] = envBool("OTLP_INSECURE", False)

    # Tempo/Loki/Mimir are multi-tenant and keyed on X-Scope-OrgID. Carry the
    # tenant as an OTLP header so spans land in the right tenant instead of being
    # rejected as "no tenant ID".
    tenant = os.getenv("OTLP_TENANT")
    if tenant:
        kwargs["headers"] = {"x-scope-orgid": tenant}

    return OTLPSpanExporter(**kwargs)


def init_otel(service_name):
    """Initializes OpenTelemetry tracing with the given service name."""
    if not otelEnabled():
        return

    if not OTEL_SDK_AVAILABLE:
        print("OpenTelemetry is enabled but the SDK is not installed. Running untraced.")
        return

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return

    resource = Resource.create({"service.name": service_name, "service.version": __version__})

    samplerate = 1.0 if envBool("OPENTELEMETRY_DEBUG", False) else float(os.getenv("OTEL_SAMPLE_RATE", "0.1"))
    provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(samplerate)))
    trace.set_tracer_provider(provider)

    endpoint = os.getenv("OTLP_ENDPOINT")
    if endpoint:
        span_processor = BatchSpanProcessor(_buildExporter(endpoint))
    else:
        span_processor = SimpleSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(span_processor)

    # Outbound propagation. SiteRM talks to agents, other frontends and the
    # orchestrator over httpx; without this no traceparent header is sent and
    # every service starts its own disconnected trace.
    HTTPXClientInstrumentor().instrument()
