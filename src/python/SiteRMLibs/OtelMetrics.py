#!/usr/bin/env python3
"""The meter provider, and the instrument cache that makes counters possible.

SiteRM's metrics are declared against a CollectorRegistry rebuilt every cycle,
so a counter cannot exist there. Instruments here live for the process instead,
and both readers observe the same ones:

    MeterProvider ──┬── PrometheusReader ────────► /metrics   (29 scrape jobs)
                    └── PeriodicExportingReader ─► OTLP ──► gateway ──► Mimir

The readers disagree about suffixes, so a careless declaration arrives under two
different names and parity cannot be checked:

  declared                        /metrics (pull)            Mimir (push)
  Counter, unit="s"               name_seconds_total         name
  Counter, unit=""                name_total                 name
  Counter named name_total        name_total                 name_total    OK
  Gauge, unit="s"                 name_seconds               name
  Gauge, unit=""                  name                       name          OK

Hence: units stay "" and go in the name, counters carry `_total` explicitly
(getCounter enforces it), gauges use the exact final name.

Degrades to no-ops when the SDK is absent.
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

    Separate from otelEnabled() so a site can push metrics for parity checking
    before it is ready to push traces.
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

        # Local import: buildResource pulls in MainUtilities, and this module is
        # imported from places that must stay cycle-free.
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

    Keyed on kind and name: the SDK will hand out two different instruments with
    the same name and different types.
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
    """Monotonic counter, surviving across cycles.

    `_total` is appended when absent: the pull reader adds it and the push path
    does not, so a name without it diverges between the two.
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
    """Synchronous gauge, for last-known-value signals.

    Keeps reporting every attribute set it has ever been given, for the life of
    the process. Fine for a bounded label set; use getObservableGauge when the
    labels come and go.
    """
    return _instrument("gauge", name, description, "", meterName)


def getObservableGauge(name, callback, description="", meterName="siterm"):
    """Async gauge reporting exactly what `callback` returns at each collection.

    The difference that matters: an attribute set the callback stops returning
    stops being reported. A synchronous gauge would keep emitting it forever,
    so a label like a delta id would accumulate without bound no matter how few
    are written per cycle.

    `callback` takes the SDK's CallbackOptions and yields Observation.
    """
    if not metricsEnabled():
        return None
    meter = getMeter(meterName)
    if meter is None:
        return None
    key = ("observablegauge", name)
    with _LOCK:
        if key in _INSTRUMENTS:
            return _INSTRUMENTS[key]
        inst = meter.create_observable_gauge(name, callbacks=[callback], unit="", description=description)
        _INSTRUMENTS[key] = inst
        return inst


def observation(value, attributes):
    """An Observation, or None when the SDK is absent."""
    if not OTEL_METRICS_AVAILABLE:
        return None
    from opentelemetry.metrics import Observation

    return Observation(value, attributes)


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
