#!/usr/bin/env python3
"""The single place in SiteRM that knows OpenTelemetry exists.

SiteRM points at OpenTelemetry; it does not embed it. Every other module imports
tracing helpers from here and never imports `opentelemetry` directly, which buys
two things:

1. OpenTelemetry is an OPTIONAL dependency. A site that installs SiteRM without
   the otel packages gets no-op tracers and runs unchanged. Previously
   `Daemonizer`, `REST/Delta`, `Backends/Ansible` and `PolicyService` imported
   `opentelemetry` at module scope, so a missing package stopped SiteRM from
   starting at all.

2. This module imports NOTHING from SiteRM. `SiteRMLibs.MainUtilities` imports
   `dbinterface` from `SiteRMLibs.DBBackend`, so anything importing
   MainUtilities cannot be imported by DBBackend. Staying dependency-free lets
   DBBackend use this too, and removes the duplicated `otelEnabled` the previous
   revision needed to work around exactly that cycle.

Configuration is by environment variable, so the deployment decides where
telemetry goes and SiteRM carries no backend knowledge.
"""

import os

try:  # pragma: no cover - import guard is the point of this module
    from opentelemetry import trace as _trace
    from opentelemetry.trace import Link as _Link
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator as _Propagator,
    )

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _trace = None
    _Link = None
    _Propagator = None
    OTEL_AVAILABLE = False


def envBool(name, default=False):
    """Boolean from environment. Same semantics as MainUtilities.envBool, which
    cannot be imported here without creating the DBBackend cycle described above.
    """
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip('"').strip("'").lower() in {"1", "true", "yes", "on"}


def otelEnabled():
    """Whether tracing is enabled.

    Both names are accepted. OPENTELEMETRY_ENABLED gated init_otel while
    OTEL_ENABLED gated the FastAPI app, and neither aliased the other, so setting
    only one left tracing half-configured: either a provider with nothing
    instrumenting it, or spans created against a no-op provider and silently
    never exported.
    """
    if not OTEL_AVAILABLE:
        return False
    return envBool("OPENTELEMETRY_ENABLED", False) or envBool("OTEL_ENABLED", False)


# =========================================================
# No-op fallbacks, used when opentelemetry is not installed
# =========================================================
class _NoOpSpan:
    """Accepts every call the real Span API takes and does nothing."""

    def set_attribute(self, *args, **kwargs):
        """No-op."""

    def set_status(self, *args, **kwargs):
        """No-op."""

    def record_exception(self, *args, **kwargs):
        """No-op."""

    def add_event(self, *args, **kwargs):
        """No-op."""

    def get_span_context(self):
        """No-op."""
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _NoOpTracer:
    """Mirrors the slice of the Tracer API SiteRM uses.

    One public method is the whole point, and the arguments are accepted and
    discarded so callers need no branching.
    """

    # pylint: disable=too-few-public-methods,unused-argument

    def start_as_current_span(self, *args, **kwargs):
        """Return a context manager yielding a no-op span."""
        return _NoOpSpan()


_NOOP_SPAN = _NoOpSpan()
_propagator = _Propagator() if OTEL_AVAILABLE else None


def getTracer(name):
    """Tracer for `name`, or a no-op tracer when opentelemetry is absent.

    When opentelemetry IS installed but tracing is disabled, the API's own
    global no-op provider already makes this cheap, so there is no second gate.
    """
    if not OTEL_AVAILABLE:
        return _NoOpTracer()
    return _trace.get_tracer(name)


def getCurrentSpan():
    """Current active span, or a no-op span."""
    if not OTEL_AVAILABLE:
        return _NOOP_SPAN
    return _trace.get_current_span()


def traceparent():
    """W3C traceparent for the active span, or "" when there is none.

    Persisted alongside a delta so a consumer in another process can link back
    to the trace that submitted it.
    """
    if not OTEL_AVAILABLE:
        return ""
    carrier = {}
    _propagator.inject(carrier)
    return carrier.get("traceparent", "")


def spanContextFromTraceparent(tphdr):
    """Remote parent SpanContext from a W3C traceparent, or None if unusable."""
    if not OTEL_AVAILABLE or not tphdr:
        return None
    try:
        version, traceid, spanid, flags = tphdr.split("-", 3)
    except (ValueError, AttributeError):
        return None
    if len(version) != 2 or len(traceid) != 32 or len(spanid) != 16:
        return None
    try:
        return _trace.SpanContext(
            trace_id=int(traceid, 16),
            span_id=int(spanid, 16),
            is_remote=True,
            trace_flags=_trace.TraceFlags(int(flags, 16)),
            trace_state=_trace.TraceState(),
        )
    except ValueError:
        return None


def linksFromTraceparent(tphdr):
    """Links list for `start_as_current_span`, or None.

    Keyed on whether the traceparent PARSED, not on whether the string was
    non-empty. The previous revision tested the string, so a corrupt or
    truncated traceparent in a delta file produced `Link(None)`.
    """
    ctx = spanContextFromTraceparent(tphdr)
    if ctx is None:
        return None
    return [_Link(ctx)]


def statusOk():
    """OK status, or None when opentelemetry is absent."""
    if not OTEL_AVAILABLE:
        return None
    return _trace.Status(_trace.StatusCode.OK)


def statusError(description=None):
    """ERROR status, or None when opentelemetry is absent."""
    if not OTEL_AVAILABLE:
        return None
    return _trace.Status(_trace.StatusCode.ERROR, description)


def setSpanStatus(span, status):
    """Set a status built by statusOk/statusError, tolerating the no-op case."""
    if status is not None:
        span.set_status(status)


def instrumentLogging():
    """Add trace ids to log records, when tracing is on.

    Gated on otelEnabled(). Previously this ran unconditionally, so a deployment
    with the packages installed but tracing off still had its logging format
    rewritten to carry trace_id=0 on every line -- correlating nothing while
    making every log line wider.
    """
    if not otelEnabled():
        return
    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(set_logging_format=True)
    except Exception as ex:  # pragma: no cover
        print(f"OpenTelemetry logging instrumentation skipped. Error: {ex}")
