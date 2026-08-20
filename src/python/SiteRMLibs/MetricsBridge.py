#!/usr/bin/env python3
"""Writes one value to both metric paths at once.

29 scrape jobs, 67 alert rules and every existing panel read `snmpinfo.txt`, so
its output must not change while the push path is being proven. Each class here
keeps the `prometheus_client` object byte-identical and additionally records the
same value into an OTel instrument, set from the same variable on the same line.

A migration scaffold: once parity is confirmed the prometheus_client half comes
out. Call sites keep the shape they already have:

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

        OTel has no Info type, so the prometheus shape is rebuilt by hand: a
        gauge fixed at 1 with the payload merged into the labels.
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

        One series per state with a label named after the metric, 1.0 on the
        active one. Panels select on that label, e.g. service_state{service_state="OK"}.
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
