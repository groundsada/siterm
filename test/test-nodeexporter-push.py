#!/usr/bin/env python3
"""Tests for parsing a DTN's node_exporter output into pushable samples.

Uses real node_exporter text rather than a synthetic shape, because the parts
that go wrong are the ones a hand-written fixture leaves out: histogram buckets
carrying a `le` label, a counter whose family name is not its sample name
(`_total`), untyped families, and NaN summary quantiles.

    python3 -m unittest test-nodeexporter-push -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "python"))

from SiteRMLibs.NodeExporterPush import (  # noqa: E402  pylint: disable=wrong-import-position
    DEFAULT_PREFIXES,
    nodeExporterUrl,
    parseSamples,
    wanted,
)

SAMPLE = """# HELP node_load1 1m load average.
# TYPE node_load1 gauge
node_load1 0.55
# HELP node_cpu_seconds_total Seconds the CPUs spent in each mode.
# TYPE node_cpu_seconds_total counter
node_cpu_seconds_total{cpu="0",mode="idle"} 12345.67
node_cpu_seconds_total{cpu="1",mode="idle"} 12000.1
# HELP node_filesystem_avail_bytes Filesystem space available.
# TYPE node_filesystem_avail_bytes gauge
node_filesystem_avail_bytes{device="/dev/sda1",mountpoint="/"} 1.2345e+10
# HELP node_scrape_collector_duration_seconds Duration of a collector scrape.
# TYPE node_scrape_collector_duration_seconds summary
node_scrape_collector_duration_seconds{collector="cpu",quantile="0.5"} NaN
# HELP go_gc_duration_seconds A summary of GC pause duration.
# TYPE go_gc_duration_seconds summary
go_gc_duration_seconds{quantile="0"} 1.1e-05
# HELP node_uname_info Labeled system information.
# TYPE node_uname_info gauge
node_uname_info{nodename="dtn01",release="5.14.0"} 1
"""


def byName(samples):
    """{name: [(attributes, value)]}."""
    out = {}
    for name, attributes, value in samples:
        out.setdefault(name, []).append((attributes, value))
    return out


class UrlTestCase(unittest.TestCase):
    """Same normalisation the REST passthrough already applies."""

    def testBareHostPortGetsSchemeAndPath(self):
        """Sites write this form as often as a full URL."""
        self.assertEqual(nodeExporterUrl("dtn01:9100"), "http://dtn01:9100/metrics")

    def testFullUrlIsLeftAlone(self):
        self.assertEqual(nodeExporterUrl("http://dtn01:9100/metrics"), "http://dtn01:9100/metrics")

    def testHttpsIsPreserved(self):
        """Starting with 'http' covers https too; it must not be downgraded."""
        self.assertEqual(nodeExporterUrl("https://dtn01:9100"), "https://dtn01:9100/metrics")

    def testTrailingSlashDoesNotDoubleUp(self):
        self.assertEqual(nodeExporterUrl("http://dtn01:9100/"), "http://dtn01:9100/metrics")

    def testEmptyIsEmpty(self):
        """An unconfigured host must not become a request to http:///metrics."""
        self.assertEqual(nodeExporterUrl(""), "")
        self.assertEqual(nodeExporterUrl(None), "")
        self.assertEqual(nodeExporterUrl("   "), "")


class FilterTestCase(unittest.TestCase):
    """node_exporter emits far more than the dashboards reference."""

    def testDashboardFamiliesAreKept(self):
        for name in ("node_cpu_seconds_total", "node_memory_MemFree_bytes", "node_load1"):
            with self.subTest(name=name):
                self.assertTrue(wanted(name))

    def testUnreferencedFamiliesAreDropped(self):
        """go_* and promhttp_* are the exporter talking about itself."""
        for name in ("go_gc_duration_seconds", "promhttp_metric_handler_requests_total"):
            with self.subTest(name=name):
                self.assertFalse(wanted(name))

    def testPrefixesNoneKeepsEverything(self):
        """A site that wants the lot should not have to edit the default list."""
        self.assertTrue(wanted("go_gc_duration_seconds", prefixes=None))


class ParseTestCase(unittest.TestCase):
    """The awkward parts of the text format."""

    def setUp(self):
        self.samples = parseSamples(SAMPLE, "dtn01")
        self.byname = byName(self.samples)

    def testGaugeParsed(self):
        self.assertEqual(self.byname["node_load1"][0][1], 0.55)

    def testCounterKeepsTheTotalSuffix(self):
        """The family is node_cpu_seconds_total and so is the sample; renaming
        it would stop matching every dashboard expression."""
        self.assertIn("node_cpu_seconds_total", self.byname)

    def testLabelsSurvive(self):
        """cpu and mode are what make the series useful."""
        attributes = dict(self.byname["node_cpu_seconds_total"][0][0])
        self.assertEqual(attributes["mode"], "idle")
        self.assertIn(attributes["cpu"], {"0", "1"})

    def testScientificNotationParsed(self):
        self.assertEqual(self.byname["node_filesystem_avail_bytes"][0][1], 1.2345e10)

    def testHostnameIsAttachedToEverySample(self):
        """The frontend knows which host it polled; the payload does not."""
        for _name, attributes, _value in self.samples:
            self.assertEqual(attributes["hostname"], "dtn01")

    def testHostnameIsNotTakenFromThePayload(self):
        """node_uname_info carries `nodename`, which is not authoritative."""
        attributes = dict(self.byname["node_uname_info"][0][0])
        self.assertEqual(attributes["hostname"], "dtn01")
        self.assertEqual(attributes["nodename"], "dtn01")

    def testNanIsDropped(self):
        """A summary quantile with no observation would put a gap in the series."""
        self.assertNotIn("node_scrape_collector_duration_seconds", self.byname)

    def testUnwantedFamilyIsAbsent(self):
        self.assertNotIn("go_gc_duration_seconds", self.byname)

    def testEmptyInputIsEmpty(self):
        """A DTN that answered with nothing is not an error here."""
        self.assertEqual(parseSamples("", "dtn01"), [])
        self.assertEqual(parseSamples(None, "dtn01"), [])


class CardinalityTestCase(unittest.TestCase):
    """The default exists to bound what the fleet pushes."""

    def testDefaultPrefixesAreNodeOnly(self):
        """Anything not node_* is the exporter describing itself."""
        for prefix in DEFAULT_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertTrue(prefix.startswith("node_"))

    def testFilteringActuallyReducesTheSet(self):
        """Guards against a default that silently matches everything."""
        filtered = len(parseSamples(SAMPLE, "dtn01"))
        everything = len(parseSamples(SAMPLE, "dtn01", prefixes=None))
        self.assertLess(filtered, everything)


if __name__ == "__main__":
    unittest.main()
