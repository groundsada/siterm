#!/usr/bin/env python3
"""The meter provider, and the instrument cache that makes counters possible.

SiteRM's existing metrics are `prometheus_client` gauges declared against a
fresh `CollectorRegistry` built once per collection cycle and thrown away. That
has one consequence that shapes everything: a counter cannot exist. Anything
declared against a registry that is discarded each cycle restarts at zero each
cycle, so rate() and increase() over it are meaningless.

One meter provider, two readers:

    MeterProvider ──┬── PrometheusReader ────────► /metrics   (the 29 scrape jobs)
                    └── PeriodicExportingReader ─► OTLP ──► gateway ──► Mimir

Instruments are created once and live for the process, so counters accumulate.
Both readers observe the same instruments, so the scraped series and the pushed
series are the same numbers by construction rather than by careful duplication.

NAMING RULES, and they are not stylistic. The two readers do not agree about
suffixes, so an instrument declared carelessly arrives under two different names
and the parity check the dual path exists for becomes impossible:

  declared                        /metrics (pull)            Mimir (push)
  Counter, unit="s"               name_seconds_total         name
  Counter, unit=""                name_total                 name
  Counter named name_total        name_total                 name_total    OK
  Gauge, unit="s"                 name_seconds               name
  Gauge, unit=""                  name                       name          OK

The pull reader appends `_total` to every counter and a suffix for every unit
other than "1"/""; the gateway is set to `UnderscoreEscapingWithoutSuffixes` and
appends nothing. So:

  * units stay "" -- put the unit in the NAME (`..._seconds`) if it matters
  * counters carry `_total` in the name explicitly; getCounter enforces it
  * gauges and up-down counters use the exact final name

The eleven existing SiteRM series are all gauges, so migrating them under these
rules produces byte-identical output on both paths. 67 alert rules and every
existing panel bind to those names.

Like the rest of the otel path this degrades to no-ops when the SDK is absent.
"""

import os
import threading

from SiteRMLibs.OtelWrapper import envBool, otelEnabled

try:  # pragma: no cover - import guard is the point
    from opentelemetry import metrics as _metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    OTEL_METRICS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _metrics = None
    MeterProvider = None
    PeriodicExportingMetricReader = None
    OTEL_METRICS_AVAILABLE = False

try:  # pragma: no cover
    from opentelemetry.exporter.prometheus import PrometheusMetricReader

    PROM_READER_AVAILABLE = True
except ImportError:  # pragma: no cover
    PrometheusMetricReader = None
    PROM_READER_AVAILABLE = False


_LOCK = threading.Lock()
_PROVIDER = None
_PROM_READER = None
_INSTRUMENTS = {}


# =========================================================
# No-op instruments, used when the SDK is absent or disabled
# =========================================================
class _NoOpInstrument:
    """Accepts every instrument call and does nothing."""

    # pylint: disable=too-few-public-methods,unused-argument

    def add(self, *args, **kwargs):
        """No-op."""

    def record(self, *args, **kwargs):
        """No-op."""

    def set(self, *args, **kwargs):
        """No-op."""


_NOOP = _NoOpInstrument()


def metricsEnabled():
    """Whether the OTel metrics path is on.

    Separate from otelEnabled() so metrics can be turned on without traces and
    the reverse. During the dual-path period a site may well want to push
    metrics for parity checking before it is ready to push traces.
    """
    if not OTEL_METRICS_AVAILABLE:
        return False
    if os.getenv("OTEL_METRICS_ENABLED") is not None:
        return envBool("OTEL_METRICS_ENABLED", False)
    return otelEnabled()


def initMetrics(service_name, resource=None):
    """Build the meter provider. Idempotent, safe to call from every daemon.

    Returns the provider, or None when metrics are unavailable or disabled.
    """
    global _PROVIDER, _PROM_READER  # pylint: disable=global-statement
    if not metricsEnabled():
        return None
    with _LOCK:
        if _PROVIDER is not None:
            return _PROVIDER

        # Imported here rather than at module scope: buildResource pulls in
        # SiteRMLibs.__version__ and MainUtilities, and this module is imported
        # from places that must stay cycle-free.
        if resource is None:
            from SiteRMLibs.OpenTelemetry import buildResource

            resource = buildResource(service_name)

        readers = []
        if PROM_READER_AVAILABLE:
            _PROM_READER = PrometheusMetricReader()
            readers.append(_PROM_READER)

        endpoint = os.getenv("OTLP_ENDPOINT")
        if endpoint:
            from SiteRMLibs.OtelExporters import buildExporter

            exporter = buildExporter("metrics", endpoint)
            if exporter is not None:
                interval = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "60000"))
                readers.append(PeriodicExportingMetricReader(exporter, export_interval_millis=interval))

        if not readers:
            return None

        _PROVIDER = MeterProvider(resource=resource, metric_readers=readers)
        _metrics.set_meter_provider(_PROVIDER)
        return _PROVIDER


def getPrometheusReader():
    """The Prometheus reader, so the REST layer can render /metrics from it."""
    return _PROM_READER


def getMeter(name="siterm"):
    """Meter for `name`, or None when metrics are off."""
    if not metricsEnabled() or _PROVIDER is None:
        return None
    return _metrics.get_meter(name)


def _instrument(kind, name, description, unit, meterName):
    """Get-or-create an instrument, cached for the life of the process.

    The cache is the whole point. Creating a counter per collection cycle is
    what makes a counter useless, so every call site must get the same object
    back. Keyed on kind and name because the SDK will happily hand out two
    different instruments with the same name and different types.
    """
    if not metricsEnabled():
        return _NOOP
    meter = getMeter(meterName)
    if meter is None:
        return _NOOP
    key = (kind, name)
    with _LOCK:
        if key in _INSTRUMENTS:
            return _INSTRUMENTS[key]
        if kind == "counter":
            inst = meter.create_counter(name, unit=unit, description=description)
        elif kind == "updowncounter":
            inst = meter.create_up_down_counter(name, unit=unit, description=description)
        elif kind == "histogram":
            inst = meter.create_histogram(name, unit=unit, description=description)
        elif kind == "gauge":
            inst = meter.create_gauge(name, unit=unit, description=description)
        else:
            raise ValueError(f"unknown instrument kind {kind}")
        _INSTRUMENTS[key] = inst
        return inst


def getCounter(name, description="", meterName="siterm"):
    """Monotonic counter. Survives across cycles, which is the point.

    `_total` is appended when absent rather than left to the caller: the pull
    reader adds it and the push path does not, so a name without it silently
    diverges between the two. Appending here makes both agree, and the reader
    does not double-suffix a name that already ends in `_total`.
    """
    if not name.endswith("_total"):
        name = f"{name}_total"
    return _instrument("counter", name, description, "", meterName)


def getUpDownCounter(name, description="", meterName="siterm"):
    """Counter that can decrease. Renders as a gauge, so no `_total`."""
    return _instrument("updowncounter", name, description, "", meterName)


def getHistogram(name, description="", meterName="siterm"):
    """Histogram, for durations. Put the unit in the name, e.g. `..._seconds`."""
    return _instrument("histogram", name, description, "", meterName)


def getGauge(name, description="", meterName="siterm"):
    """Synchronous gauge, for last-known-value signals."""
    return _instrument("gauge", name, description, "", meterName)


def shutdownMetrics():
    """Flush and tear down. Tests, and orderly daemon shutdown."""
    global _PROVIDER, _PROM_READER  # pylint: disable=global-statement
    with _LOCK:
        if _PROVIDER is not None:
            try:
                _PROVIDER.shutdown()
            except Exception:  # pragma: no cover
                pass
        _PROVIDER = None
        _PROM_READER = None
        _INSTRUMENTS.clear()
