#!/usr/bin/env python3
"""Turning a DTN's node_exporter output into OTel metrics the frontend can push.

Roughly 220 dashboard expressions reference `node_*`, and 140 of the 308 autogole
scrape jobs are node_exporter. None of it has a push path today, so retiring the
pull path would take the DTN health panels with it.

The frontend already knows every agent's node_exporter URL and already proxies it
(`SiteFE/REST/Monitoring.py`), so this is the same reach into the private network
the passthrough already performs -- on a timer, pushed outward, rather than
served on demand. That matters beyond convenience: if the site pushes these
metrics out through the frontend, port 9100 never needs to be reachable from
outside the site at all, which is the posture SENSE's own docs recommend and
that 63 sites currently do not have.

This module is deliberately pure and free of DB and HTTP. It takes text and
gives back samples. The fetch loop belongs to whatever daemon calls it, and
keeping the parsing separable is what makes the awkward parts -- histograms,
untyped families, NaN -- testable without a DTN.
"""

import math

try:  # pragma: no cover - optional, same posture as the otel packages
    from prometheus_client.parser import text_string_to_metric_families

    PARSER_AVAILABLE = True
except ImportError:  # pragma: no cover
    text_string_to_metric_families = None
    PARSER_AVAILABLE = False

# node_exporter emits well over a thousand series per host. Pushing all of them
# for every DTN would multiply the fleet's cardinality by more than the pull
# path ever carried, so the default is the families the dashboards actually
# reference. A site wanting everything passes prefixes=None.
DEFAULT_PREFIXES = (
    "node_cpu",
    "node_memory",
    "node_filesystem",
    "node_disk",
    "node_network",
    "node_load",
    "node_boot_time",
    "node_time",
    "node_uname",
    "node_exporter_build",
)


def nodeExporterUrl(value):
    """Full metrics URL for a configured `node_exporter` value, or "".

    Sites write this as a bare `host:9100` as often as a URL, and
    SiteFE/REST/Monitoring.py normalises it inline. Same normalisation here so
    the pushed path and the proxied path cannot disagree about what a given
    config value means.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith("http"):
        value = f"http://{value}"
    if not value.endswith("/metrics"):
        value = f"{value.rstrip('/')}/metrics"
    return value


def wanted(name, prefixes=DEFAULT_PREFIXES):
    """Whether `name` is a family worth pushing."""
    if prefixes is None:
        return True
    return name.startswith(tuple(prefixes))


def parseSamples(text, hostname, prefixes=DEFAULT_PREFIXES):
    """[(name, attributes, value)] from one node_exporter response.

    `hostname` is attached here rather than trusted from the payload. A DTN
    reports whatever its own node_exporter was told to say, and the frontend is
    the thing that actually knows which host it just polled -- the same reason
    the gateway stamps `sitename` instead of believing the resource.

    NaN is dropped. node_exporter emits it for a summary quantile it has no
    observation for, and it is not a value any dashboard wants; passing it on
    would put gaps into series that are otherwise continuous.
    """
    if not PARSER_AVAILABLE or not text:
        return []
    samples = []
    for family in text_string_to_metric_families(text):
        if not wanted(family.name, prefixes):
            continue
        for sample in family.samples:
            if sample.value is None or math.isnan(sample.value):
                continue
            attributes = dict(sample.labels)
            attributes["hostname"] = hostname
            samples.append((sample.name, attributes, float(sample.value)))
    return samples
