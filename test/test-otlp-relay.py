#!/usr/bin/env python3
"""Tests for the frontend's OTLP relay gating.

The relay is the site's only telemetry egress, so the interesting behaviour is
all in what it REFUSES: an agent must get one clear answer here rather than a
confusing error from a collector it cannot see. The forwarding itself is a
single httpx.post and is not worth mocking; the decisions in front of it are.

The logic lives in SiteRMLibs.OtelRelay rather than SiteFE.REST.Otlp precisely
so it can be imported without a deployed frontend -- SiteFE.REST.dependencies
builds the git config at module scope and needs a real site configuration.

    python3 -m unittest test-otlp-relay -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "python"))

from SiteRMLibs.OtelRelay import (  # noqa: E402  pylint: disable=wrong-import-position
    MAX_BODY_BYTES,
    SIGNALS,
    relayTarget,
)


def config(relay=True, endpoint="https://collector.example.org"):
    """The config `general` section, shaped enough for relayTarget."""
    general = {}
    if relay:
        general["otlp_relay"] = True
    if endpoint is not None:
        general["otlp_endpoint"] = endpoint
    return general


def target(general, signal):
    """The URL, asserting no problem was reported."""
    url, problem = relayTarget(general, signal)
    assert problem is None, f"unexpected problem: {problem}"
    return url


def problemFor(general, signal):
    """The (status, detail) tuple, asserting no URL was produced."""
    url, problem = relayTarget(general, signal)
    assert url is None, f"unexpected url: {url}"
    return problem


class GatingTestCase(unittest.TestCase):
    """Off by default, and it says why when it is off."""

    def setUp(self):
        os.environ.pop("OTLP_ENDPOINT", None)

    def testDisabledByDefault(self):
        """A frontend that never asked for this must not become an open relay."""
        self.assertEqual(problemFor({}, "traces")[0], 404)

    def testDisabledMentionsTheFlag(self):
        """404 alone would look like a version mismatch to an agent."""
        self.assertIn("otlp_relay", problemFor({}, "traces")[1])

    def testMissingGeneralSectionIsNotACrash(self):
        """gitConf can hand back None for an absent section."""
        self.assertEqual(problemFor(None, "traces")[0], 404)

    def testEnabledWithNoEndpointIs503(self):
        """Enabled but unusable is a different problem from not enabled."""
        self.assertEqual(problemFor(config(endpoint=None), "traces")[0], 503)

    def testUnknownSignalIsRejected(self):
        """A typo must not become a POST to an arbitrary upstream path."""
        self.assertEqual(problemFor(config(), "profiles")[0], 404)

    def testUnknownSignalIsCheckedBeforeTheFlag(self):
        """A bad signal is a client bug either way; say so even when relaying is off."""
        self.assertEqual(problemFor({}, "profiles")[0], 404)
        self.assertIn("profiles", problemFor({}, "profiles")[1])


class TargetTestCase(unittest.TestCase):
    """The signal path is appended, not guessed."""

    def setUp(self):
        os.environ.pop("OTLP_ENDPOINT", None)

    def testEachSignalGetsItsOwnPath(self):
        """Shipping metrics to /v1/traces is the failure this prevents."""
        for signal in sorted(SIGNALS):
            with self.subTest(signal=signal):
                self.assertEqual(target(config(), signal), f"https://collector.example.org/v1/{signal}")

    def testEnvironmentUsedWhenConfigIsSilent(self):
        """A site that already pointed its own exporter upstream configures once."""
        os.environ["OTLP_ENDPOINT"] = "https://from-env.example.org"
        self.assertEqual(target(config(endpoint=None), "logs"), "https://from-env.example.org/v1/logs")

    def testConfigWinsOverEnvironment(self):
        """The explicit setting is the one an operator edited on purpose."""
        os.environ["OTLP_ENDPOINT"] = "https://from-env.example.org"
        self.assertEqual(target(config(), "traces"), "https://collector.example.org/v1/traces")

    def testTrailingSlashDoesNotDoubleUp(self):
        """signalEndpoint strips it; a //v1/traces would 404 upstream."""
        self.assertEqual(target(config(endpoint="https://c.example.org/"), "traces"), "https://c.example.org/v1/traces")

    def testAlreadyQualifiedEndpointIsNotDoubled(self):
        """A site that pasted the traces URL must not ship to /v1/traces/v1/traces."""
        self.assertEqual(target(config(endpoint="https://c.example.org/v1/traces"), "traces"), "https://c.example.org/v1/traces")

    def testQualifiedEndpointIsRebasedPerSignal(self):
        """Pasting the traces URL must still send metrics to /v1/metrics."""
        self.assertEqual(target(config(endpoint="https://c.example.org/v1/traces"), "metrics"), "https://c.example.org/v1/metrics")


class GrpcTestCase(unittest.TestCase):
    """A gRPC upstream cannot take an HTTP POST body."""

    def setUp(self):
        os.environ.pop("OTLP_ENDPOINT", None)

    def testBareHostPortIsRefused(self):
        """OTLP_ENDPOINT is legitimately a bare host:port for gRPC exporters."""
        self.assertEqual(problemFor(config(endpoint="collector.example.org:4317"), "traces")[0], 503)

    def testRefusalExplainsWhatToChange(self):
        """Forwarding anyway would fail once per export with a worse message."""
        self.assertIn("gRPC", problemFor(config(endpoint="collector.example.org:4317"), "traces")[1])

    def testHttpSchemeIsAccepted(self):
        """Plaintext upstream is a deployment choice, not an error."""
        self.assertEqual(target(config(endpoint="http://c.example.org"), "traces"), "http://c.example.org/v1/traces")


class ShapeTestCase(unittest.TestCase):
    """Constants the route depends on."""

    def testOnlyTheThreeOtlpSignals(self):
        """An unknown signal is rejected rather than forwarded to any path."""
        self.assertEqual(SIGNALS, {"traces", "metrics", "logs"})

    def testBodyLimitIsBounded(self):
        """The body is read into memory before forwarding, so it must be capped."""
        self.assertTrue(0 < MAX_BODY_BYTES <= 64 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
