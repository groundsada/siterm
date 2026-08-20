#!/usr/bin/env python3
"""Makes OTLP export failure visible.

When export fails, nothing in SiteRM says so today. `BatchSpanProcessor` logs
through the SDK's own logger and drops spans on a full queue; SiteRM neither
surfaces that nor exposes it. A telemetry system whose own failure is invisible
is worse than no telemetry, because the dashboards keep rendering and simply
stop being true.

These counters go out over the PROMETHEUS PULL PATH, deliberately. A metric that
reports "telemetry is not reaching the gateway" must not itself travel over the
path that is broken. They are accumulated here and rendered into the per-cycle
registry that becomes snmpinfo.txt, so they ride the 29 existing scrape jobs.

That also avoids a recursion: recording export failures into the meter provider
would mean the metrics exporter's own failures mutate an instrument that the
metrics exporter is in the middle of collecting.
"""

import threading
import time

from prometheus_client import Counter, Gauge, disable_created_metrics

# Counters otherwise emit a companion `<name>_created` series. Nothing queries
# them, and snmpinfo.txt is scraped by 29 production jobs -- doubling the series
# count of every counter added here is not worth a timestamp nobody reads. Safe
# to do globally: every pre-existing SiteRM metric is a Gauge, Info or Enum,
# none of which emit `_created`.
disable_created_metrics()

_LOCK = threading.Lock()
_EXPORTED = {}
_DROPPED = {}
_FAILURES = {}
_LAST_SUCCESS = {}
_LAST_STATUS = {}


def recordSuccess(signal, count=0):
    """Note a successful export of `count` items."""
    with _LOCK:
        _EXPORTED[signal] = _EXPORTED.get(signal, 0) + count
        _LAST_SUCCESS[signal] = time.time()


def recordFailure(signal, reason, count=0):
    """Note a failed export. `reason` is coarse on purpose -- it becomes a label."""
    with _LOCK:
        _FAILURES[(signal, reason)] = _FAILURES.get((signal, reason), 0) + 1
        _DROPPED[signal] = _DROPPED.get(signal, 0) + count


def noteHttpStatus(signal, status):
    """Remember the last HTTP status seen for `signal`.

    The OTLP/HTTP exporters return SpanExportResult.FAILURE rather than raising,
    so the status code never reaches the wrapper and every failure would be
    labelled "rejected" -- including the one that matters most, an expired or
    wrong credential. The session hook sees the response, so it records the code
    here and the wrapper uses it to pick an honest reason.
    """
    with _LOCK:
        _LAST_STATUS[signal] = status


def _reasonFromStatus(signal):
    """Reason label from the last HTTP status for `signal`."""
    with _LOCK:
        status = _LAST_STATUS.get(signal)
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "throttled"
    if status is not None and 500 <= status < 600:
        return "backend"
    return "rejected"


def snapshot():
    """Copy of the accumulators. Tests, and the renderer."""
    with _LOCK:
        return {
            "exported": dict(_EXPORTED),
            "dropped": dict(_DROPPED),
            "failures": dict(_FAILURES),
            "lastSuccess": dict(_LAST_SUCCESS),
        }


def reset():
    """Clear the accumulators. Tests only."""
    with _LOCK:
        _EXPORTED.clear()
        _DROPPED.clear()
        _FAILURES.clear()
        _LAST_SUCCESS.clear()
        _LAST_STATUS.clear()


def renderInto(registry):
    """Emit the health counters into a per-cycle prometheus registry.

    The registry is rebuilt each cycle, so these are declared fresh each time and
    incremented by the cumulative total. The rendered value is therefore the real
    cumulative count and the series is a genuine counter, despite the registry
    being thrown away -- which is the same trick that makes the accumulators here
    necessary in the first place.
    """
    data = snapshot()

    exported = Counter(
        "siterm_otel_exported",
        "Telemetry items successfully exported over OTLP",
        ["signal"],
        registry=registry,
    )
    for signal, value in data["exported"].items():
        if value:
            exported.labels(signal=signal).inc(value)

    dropped = Counter(
        "siterm_otel_dropped",
        "Telemetry items lost because an export failed",
        ["signal"],
        registry=registry,
    )
    for signal, value in data["dropped"].items():
        if value:
            dropped.labels(signal=signal).inc(value)

    failures = Counter(
        "siterm_otel_export_failures",
        "OTLP export attempts that failed",
        ["signal", "reason"],
        registry=registry,
    )
    for (signal, reason), value in data["failures"].items():
        if value:
            failures.labels(signal=signal, reason=reason).inc(value)

    # Absolute timestamp rather than an age, so `time() - metric` is computed at
    # query time against the scraping clock rather than baked in at write time.
    lastok = Gauge(
        "siterm_otel_last_export_success_timestamp_seconds",
        "When an OTLP export last succeeded",
        ["signal"],
        registry=registry,
    )
    for signal, value in data["lastSuccess"].items():
        lastok.labels(signal=signal).set(value)


def _classify(exc):
    """Coarse reason label for an exception or a failed result.

    Deliberately low cardinality: this is a label, and the useful question is
    "auth, network, or the backend refused it", not the exact message.
    """
    text = str(exc).lower()
    if "401" in text or "unauthenticated" in text or "authentication" in text or "403" in text:
        return "auth"
    if "timeout" in text or "timed out" in text or "deadline" in text:
        return "timeout"
    if "connect" in text or "unavailable" in text or "refused" in text or "dns" in text:
        return "unreachable"
    if "429" in text or "resource_exhausted" in text:
        return "throttled"
    return "other"


class CountingExporter:
    """Delegating exporter that records the outcome of every export.

    Wraps rather than subclasses so one class covers spans, metrics and logs.
    `__getattr__` forwards everything else, which matters for metrics:
    PeriodicExportingMetricReader reads `_preferred_temporality` and
    `_preferred_aggregation` off the exporter it is given.
    """

    def __init__(self, inner, signal):
        self._inner = inner
        self._signal = signal

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @staticmethod
    def _size(batch):
        """Item count of a batch, best effort."""
        try:
            return len(batch)
        except TypeError:
            return 0

    def export(self, batch, *args, **kwargs):
        """Delegate, then record the outcome."""
        count = self._size(batch)
        try:
            result = self._inner.export(batch, *args, **kwargs)
        except Exception as ex:
            recordFailure(self._signal, _classify(ex), count)
            raise
        # SpanExportResult/LogExportResult expose .name; MetricExportResult too.
        name = getattr(result, "name", "")
        if name and name.upper() != "SUCCESS":
            recordFailure(self._signal, _reasonFromStatus(self._signal), count)
        else:
            recordSuccess(self._signal, count)
        return result
