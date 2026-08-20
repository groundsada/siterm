"""OpenTelemetry initialization and utilities.

SDK wiring only. Span-creation helpers live in SiteRMLibs.OtelWrapper, which is
dependency-free so DBBackend can use it too; see the note there. Exporter
construction lives in SiteRMLibs.OtelExporters.

Everything here is optional. If the opentelemetry packages are not installed,
init_otel() returns quietly and SiteRM runs untraced rather than failing to
import.
"""

import os

from SiteRMLibs import __version__
from SiteRMLibs.MainUtilities import loadEnvFile
from SiteRMLibs.OtelExporters import buildExporter
from SiteRMLibs.OtelWrapper import OTEL_AVAILABLE, envBool, otelEnabled

try:  # pragma: no cover - import guard, see module docstring
    from opentelemetry import trace
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

__all__ = ["init_otel", "otelEnabled", "OTEL_AVAILABLE", "buildResource"]


def buildResource(service_name):
    """Resource for this process.

    `sitename` is deliberately absent: the gateway stamps it from the verified
    credential and overwrites anything sent.

    `siterm.component` is one of Loki's three index labels and nothing was
    setting it. It defaults to the daemon name, which is bounded; SITERM_COMPONENT
    overrides it for a deployment that wants to group daemons by role instead.
    """
    return Resource.create(
        {
            "service.name": service_name,
            "service.version": __version__,
            "siterm.component": os.getenv("SITERM_COMPONENT") or service_name,
        }
    )


def _sampler():
    """Head sampler, defaulting to 1.0 so the gateway's tail sampler decides.

    Dropping here first would multiply: the gateway can only choose among traces
    it was given, and would never see the error traces discarded at the site.
    """
    rate = float(os.getenv("OTEL_SAMPLE_RATE", "1.0"))
    return ParentBased(TraceIdRatioBased(rate))


def init_otel(service_name):
    """Initializes OpenTelemetry tracing with the given service name."""
    if not otelEnabled():
        return

    if not OTEL_SDK_AVAILABLE:
        print("OpenTelemetry is enabled but the SDK is not installed. Running untraced.")
        return

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return

    provider = TracerProvider(resource=buildResource(service_name), sampler=_sampler())
    trace.set_tracer_provider(provider)

    endpoint = os.getenv("OTLP_ENDPOINT")
    exporter = buildExporter("traces", endpoint) if endpoint else None
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    _instrumentHttpx()


def _instrumentHttpx():
    """Outbound traceparent propagation over httpx.

    Guarded separately from the SDK import: grouping them let one optional
    package disable tracing entirely. Losing this should cost the header, not
    the traces.
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as ex:  # pragma: no cover
        print(f"OpenTelemetry httpx instrumentation skipped. Error: {ex}")
