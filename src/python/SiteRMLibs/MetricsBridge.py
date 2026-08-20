#!/usr/bin/env python3
"""Writes one value to both metric paths at once.

The migration constraint: 29 production scrape jobs, 67 alert rules and every
existing dashboard panel read `snmpinfo.txt`, which is rendered by
`prometheus_client` from a registry built fresh each cycle. That output must not
change while the push path is being proven.

So each of these keeps the existing `prometheus_client` object exactly as it was
-- same name, same labels, same rendered bytes -- and additionally records the
same value into an OTel instrument that the OTLP reader pushes to Mimir. The two
paths cannot disagree, because they are set from the same variable on the same
line.

This is a migration scaffold, not the end state. Once parity is confirmed in
Grafana by graphing both datasources in one panel, the prometheus_client half
comes out and `snmpinfo.txt` is rendered from the meter provider instead. Until
then, dual-emit is the only version that is provably non-regressive.

Call sites keep the shape they already have:

    memInfo = DualGauge("memory_usage", "Memory Usage for Service",
                        ["servicename", "key", "hostname"], registry)
    memInfo.labels(servicename=..., key=..., hostname=...).set(val)
"""

from prometheus_client import Enum, Gauge, Info

from SiteRMLibs.OtelMetrics import getGauge


class _BoundGauge:
    """One label set of a DualGauge."""

    # pylint: disable=too-few-public-methods

    def __init__(self, prom, otel, labels):
        self._prom = prom
        self._otel = otel
        self._labels = labels

    def set(self, value):
        """Set both paths."""
        self._prom.set(value)
        self._otel.set(value, self._labels)


class DualGauge:
    """A prometheus_client Gauge that also feeds an OTel gauge."""

    # pylint: disable=too-few-public-methods

    def __init__(self, name, documentation, labelnames=(), registry=None):
        self._prom = Gauge(name, documentation, labelnames, registry=registry)
        self._otel = getGauge(name, documentation)

    def labels(self, **labels):
        """Bind a label set."""
        return _BoundGauge(self._prom.labels(**labels), self._otel, labels)


class _BoundInfo:
    """One label set of a DualInfo."""

    # pylint: disable=too-few-public-methods

    def __init__(self, prom, otel, labels):
        self._prom = prom
        self._otel = otel
        self._labels = labels

    def info(self, payload):
        """Set both paths.

        prometheus_client renders an Info as `<name>_info` with the payload
        merged into the labels and a constant value of 1. OTel has no Info type,
        so the same shape is built by hand: a gauge fixed at 1 carrying the same
        labels. The `_info` suffix is written into the instrument name because
        the gateway appends nothing.
        """
        self._prom.info(payload)
        attrs = dict(self._labels)
        attrs.update(payload)
        self._otel.set(1, attrs)


class DualInfo:
    """A prometheus_client Info that also feeds an OTel gauge."""

    # pylint: disable=too-few-public-methods

    def __init__(self, name, documentation, labelnames=(), registry=None):
        self._prom = Info(name, documentation, labelnames=labelnames, registry=registry)
        self._otel = getGauge(f"{name}_info", documentation)

    def labels(self, **labels):
        """Bind a label set."""
        return _BoundInfo(self._prom.labels(**labels), self._otel, labels)


class _BoundEnum:
    """One label set of a DualEnum."""

    # pylint: disable=too-few-public-methods

    def __init__(self, prom, otel, labels, name, states):
        self._prom = prom
        self._otel = otel
        self._labels = labels
        self._name = name
        self._states = states

    def state(self, value):
        """Set both paths.

        prometheus_client renders an Enum as one series per state, carrying an
        extra label named after the metric, with 1.0 on the active state and 0.0
        on the rest. Reproduced exactly rather than emitted as a single series,
        because panels select on that label -- `service_state{service_state="OK"}`
        is a real query in the existing dashboards.
        """
        self._prom.state(value)
        for state in self._states:
            attrs = dict(self._labels)
            attrs[self._name] = state
            self._otel.set(1 if state == value else 0, attrs)


class DualEnum:
    """A prometheus_client Enum that also feeds an OTel gauge."""

    # pylint: disable=too-few-public-methods

    def __init__(self, name, documentation, labelnames=(), states=None, registry=None):
        self._prom = Enum(name, documentation, labelnames, states=states, registry=registry)
        self._otel = getGauge(name, documentation)
        self._name = name
        self._states = list(states)

    def labels(self, **labels):
        """Bind a label set."""
        return _BoundEnum(self._prom.labels(**labels), self._otel, labels, self._name, self._states)
