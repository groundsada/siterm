#!/usr/bin/env python3
"""Tests for the optional OpenTelemetry dependency.

The property under test is the one the whole otel branch rests on:

    Delete every OpenTelemetry package from both requirements files and SiteRM
    behaves exactly as it does at master.

That was previously held up by reading the code, and it broke once already --
`instrumentation-httpx` shared a `try:` with the core SDK, so one missing
optional package set OTEL_SDK_AVAILABLE = False and disabled tracing entirely.

Absent packages are simulated with an import hook rather than a second venv, so
this runs in CI with the packages installed. Needs no frontend and no network.

    python3 -m unittest test-otel -v
"""

import builtins
import importlib
import logging
import os
import sys
import unittest


class _Blocker:
    """Import hook that makes chosen top-level packages unimportable."""

    def __init__(self, *prefixes):
        self.prefixes = prefixes

    def find_module(self, fullname, path=None):
        """Legacy hook, kept because Python still consults meta_path entries."""
        return self.find_spec(fullname, path)

    def find_spec(self, fullname, path=None, target=None):
        """Raise for anything under a blocked prefix."""
        for prefix in self.prefixes:
            if fullname == prefix or fullname.startswith(prefix + "."):
                raise ImportError(f"blocked for test: {fullname}")
        return None


class _Hidden:
    """Context manager: `prefixes` are unimportable and `mods` are reloaded."""

    def __init__(self, prefixes, mods):
        self.blocker = _Blocker(*prefixes)
        self.prefixes = prefixes
        self.mods = mods
        self.saved = {}

    def __enter__(self):
        self.saved = dict(sys.modules)
        for name in list(sys.modules):
            for prefix in self.prefixes:
                if name == prefix or name.startswith(prefix + "."):
                    del sys.modules[name]
        for name in self.mods:
            sys.modules.pop(name, None)
        sys.meta_path.insert(0, self.blocker)
        return self

    def __exit__(self, *args):
        sys.meta_path.remove(self.blocker)
        sys.modules.clear()
        sys.modules.update(self.saved)
        return False


def _env(**kwargs):
    """Set or clear environment variables. None clears."""
    for key, val in kwargs.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


class NoOpPathTestCase(unittest.TestCase):
    """With opentelemetry absent, the wrapper still works."""

    def testWrapperImportsAndTraces(self):
        """A missing package must cost tracing, not the ability to import."""
        with _Hidden(["opentelemetry"], ["SiteRMLibs.OtelWrapper"]):
            wrapper = importlib.import_module("SiteRMLibs.OtelWrapper")
            self.assertFalse(wrapper.OTEL_AVAILABLE)
            self.assertFalse(wrapper.otelEnabled())

            tracer = wrapper.getTracer("test")
            with tracer.start_as_current_span("work") as span:
                span.set_attribute("k", "v")
                span.add_event("e")
                span.record_exception(ValueError("x"))
                span.set_status(None)
            self.assertEqual(wrapper.traceparent(), "")
            self.assertIsNone(wrapper.statusOk())
            self.assertIsNone(wrapper.statusError("boom"))
            self.assertIsNone(wrapper.linksFromTraceparent("00-" + "a" * 32 + "-" + "b" * 16 + "-01"))
            # setSpanStatus must tolerate the None the no-op path produces.
            wrapper.setSpanStatus(wrapper.getCurrentSpan(), wrapper.statusOk())

    def testEnabledStaysFalseEvenWhenAskedFor(self):
        """Enabling tracing without the packages must not raise."""
        with _Hidden(["opentelemetry"], ["SiteRMLibs.OtelWrapper"]):
            wrapper = importlib.import_module("SiteRMLibs.OtelWrapper")
            _env(OPENTELEMETRY_ENABLED="true", OTEL_ENABLED="true")
            try:
                self.assertFalse(wrapper.otelEnabled())
                wrapper.instrumentLogging()
            finally:
                _env(OPENTELEMETRY_ENABLED=None, OTEL_ENABLED=None)


class PartialInstallTestCase(unittest.TestCase):
    """One optional instrumentation package missing must not disable tracing."""

    def testHttpxAbsentKeepsTheSdk(self):
        """The d9a53f0 regression: httpx shared a try: with the core SDK."""
        hidden = ["opentelemetry.instrumentation.httpx"]
        with _Hidden(hidden, ["SiteRMLibs.OpenTelemetry", "SiteRMLibs.OtelWrapper"]):
            module = importlib.import_module("SiteRMLibs.OpenTelemetry")
            self.assertTrue(
                module.OTEL_SDK_AVAILABLE,
                "a missing instrumentation package disabled the SDK",
            )

    def testPrometheusReaderAbsentKeepsMetrics(self):
        """Same shape, metrics side: the OTLP reader must survive without the pull one."""
        with _Hidden(["opentelemetry.exporter.prometheus"], ["SiteRMLibs.OtelMetrics"]):
            module = importlib.import_module("SiteRMLibs.OtelMetrics")
            self.assertFalse(module.PROM_READER_AVAILABLE)
            self.assertTrue(module.OTEL_METRICS_AVAILABLE)


class GateSemanticsTestCase(unittest.TestCase):
    """OTEL_ENABLED and OPENTELEMETRY_ENABLED alias each other."""

    def setUp(self):
        self.wrapper = importlib.import_module("SiteRMLibs.OtelWrapper")
        if not self.wrapper.OTEL_AVAILABLE:
            self.skipTest("opentelemetry not installed")

    def tearDown(self):
        _env(OPENTELEMETRY_ENABLED=None, OTEL_ENABLED=None)

    def testNeither(self):
        """Unset means off."""
        _env(OPENTELEMETRY_ENABLED=None, OTEL_ENABLED=None)
        self.assertFalse(self.wrapper.otelEnabled())

    def testEitherAloneIsEnough(self):
        """Setting one alone used to leave tracing half configured."""
        _env(OPENTELEMETRY_ENABLED="true", OTEL_ENABLED=None)
        self.assertTrue(self.wrapper.otelEnabled())
        _env(OPENTELEMETRY_ENABLED=None, OTEL_ENABLED="true")
        self.assertTrue(self.wrapper.otelEnabled())

    def testBoth(self):
        """Both set is the deployed configuration."""
        _env(OPENTELEMETRY_ENABLED="true", OTEL_ENABLED="true")
        self.assertTrue(self.wrapper.otelEnabled())

    def testFalseIsFalse(self):
        """A quoted false from /etc/environment must not read as true."""
        for value in ("false", '"false"', "0", "off", "no"):
            _env(OPENTELEMETRY_ENABLED=value, OTEL_ENABLED=None)
            self.assertFalse(self.wrapper.otelEnabled(), value)


class TraceparentTestCase(unittest.TestCase):
    """Round trip, and every malformed case returning None rather than Link(None)."""

    def setUp(self):
        self.wrapper = importlib.import_module("SiteRMLibs.OtelWrapper")
        if not self.wrapper.OTEL_AVAILABLE:
            self.skipTest("opentelemetry not installed")

    def testRoundTrip(self):
        """Ids must survive traceparent -> SpanContext unchanged."""
        traceid, spanid = "a" * 31 + "1", "b" * 15 + "2"
        header = f"00-{traceid}-{spanid}-01"
        ctx = self.wrapper.spanContextFromTraceparent(header)
        self.assertIsNotNone(ctx)
        self.assertEqual(f"{ctx.trace_id:032x}", traceid)
        self.assertEqual(f"{ctx.span_id:016x}", spanid)
        self.assertTrue(ctx.is_remote)

    def testLinksForAValidHeader(self):
        """A parseable traceparent yields exactly one link."""
        header = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"
        links = self.wrapper.linksFromTraceparent(header)
        self.assertIsNotNone(links)
        self.assertEqual(len(links), 1)

    def testMalformedYieldsNone(self):
        """None, not [Link(None)]: a corrupt delta file used to produce the latter."""
        bad = [
            "",
            None,
            "garbage",
            "00-tooshort-" + "d" * 16 + "-01",
            "00-" + "c" * 32 + "-short-01",
            "00-" + "c" * 32 + "-" + "d" * 16,
            "0-" + "c" * 32 + "-" + "d" * 16 + "-01",
            "00-" + "z" * 32 + "-" + "d" * 16 + "-01",
            "00-" + "c" * 32 + "-" + "d" * 16 + "-zz",
            "-".join(["00", "c" * 32, "d" * 16, "01", "extra"]),
        ]
        for header in bad:
            self.assertIsNone(self.wrapper.linksFromTraceparent(header), repr(header))


class ExporterConstructionTestCase(unittest.TestCase):
    """What reaches the SDK exporter, which is the behaviour behind #11 and #15."""

    VARS = (
        "OTLP_INSECURE",
        "OTLP_CA_BUNDLE",
        "OTLP_CLIENT_CERT",
        "OTLP_CLIENT_KEY",
        "OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
    )

    def setUp(self):
        try:
            self.exporters = importlib.import_module("SiteRMLibs.OtelExporters")
        except ImportError:
            self.skipTest("SiteRMLibs.OtelExporters unavailable")
        self.saved = {key: os.environ.get(key) for key in self.VARS}
        for key in self.VARS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self.saved.items():
            _env(**{key: val})

    def testInsecureUnsetPassesNoKwarg(self):
        """Unset must defer to the SDK, which defaults to secure."""
        self.assertIsNone(self.exporters._insecure("collector:4317"))

    def testInsecureSetIsPassedThrough(self):
        """Plaintext has to be asked for, never acquired by omission."""
        _env(OTLP_INSECURE="true")
        self.assertTrue(self.exporters._insecure("collector:4317"))
        _env(OTLP_INSECURE="false")
        self.assertFalse(self.exporters._insecure("collector:4317"))

    def testHttpSchemeImpliesInsecure(self):
        """http:// states the intent in the endpoint, which #11 preferred."""
        self.assertTrue(self.exporters._insecure("http://tempo:4317"))
        self.assertIsNone(self.exporters._insecure("https://collector:4317"))

    def testProtocolFromSchemeAndOverride(self):
        """Scheme selects the protocol; explicit env wins over it."""
        self.assertEqual(self.exporters.resolveProtocol("https://c:4318"), "http")
        self.assertEqual(self.exporters.resolveProtocol("c:4317"), "grpc")
        _env(OTLP_PROTOCOL="grpc")
        self.assertEqual(self.exporters.resolveProtocol("https://c:4318"), "grpc")

    def testSignalPathsAreNotDoubled(self):
        """A site that pasted the traces URL must not ship metrics to /v1/traces."""
        self.assertEqual(self.exporters.signalEndpoint("https://c", "metrics"), "https://c/v1/metrics")
        self.assertEqual(self.exporters.signalEndpoint("https://c/v1/traces", "metrics"), "https://c/v1/metrics")

    def testTlsMaterialUnset(self):
        """Unset means the system trust store and no client certificate."""
        self.assertEqual(self.exporters.tlsMaterial(), (None, None, None))

    def testTlsMaterialNeedsBothHalves(self):
        """A half-configured mTLS pair is dropped, not passed on."""
        _env(OTLP_CLIENT_CERT=__file__)
        self.assertEqual(self.exporters.tlsMaterial(), (None, None, None))
        _env(OTLP_CLIENT_KEY=__file__)
        self.assertEqual(self.exporters.tlsMaterial(), (None, __file__, __file__))

    def testUnreadableTlsPathIsIgnored(self):
        """An unreadable file must be reported once, not on every export."""
        _env(OTLP_CA_BUNDLE="/nonexistent/ca.pem")
        self.assertEqual(self.exporters.tlsMaterial(), (None, None, None))


class LogExportTestCase(unittest.TestCase):
    """Level gating, and that the file handler is never displaced."""

    VARS = ("OTEL_LOGS_ENABLED", "OTEL_LOG_LEVEL", "OPENTELEMETRY_ENABLED", "OTEL_ENABLED")

    def setUp(self):
        self.otellogs = importlib.import_module("SiteRMLibs.OtelLogs")
        if not self.otellogs.OTEL_LOGS_AVAILABLE:
            self.skipTest("opentelemetry sdk logs unavailable")
        self.saved = {key: os.environ.get(key) for key in self.VARS}
        for key in self.VARS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self.saved.items():
            _env(**{key: val})

    def testDefaultThresholdIsWarning(self):
        """Logs are the one signal with no sampler in front of them."""
        self.assertEqual(self.otellogs.exportLevel(), logging.WARNING)

    def testThresholdIsConfigurable(self):
        """A site that wants everything lowers it."""
        _env(OTEL_LOG_LEVEL="debug")
        self.assertEqual(self.otellogs.exportLevel(), logging.DEBUG)

    def testNonsenseThresholdFallsBack(self):
        """A typo must not ship every line from 29 sites."""
        _env(OTEL_LOG_LEVEL="VERBOSE")
        self.assertEqual(self.otellogs.exportLevel(), logging.WARNING)

    def testLogsCanBeTurnedOffAlone(self):
        """Dropping logs must not cost traces."""
        _env(OPENTELEMETRY_ENABLED="true", OTEL_LOGS_ENABLED="false")
        self.assertFalse(self.otellogs.logsEnabled())
        _env(OTEL_LOGS_ENABLED=None)
        self.assertTrue(self.otellogs.logsEnabled())

    def testAttachIsSafeWhenOff(self):
        """attachHandler must never raise: it runs from getLoggingObject."""
        _env(OPENTELEMETRY_ENABLED=None, OTEL_ENABLED=None)
        logger = logging.getLogger("test-otel-off")
        logger.addHandler(logging.NullHandler())
        before = list(logger.handlers)
        self.otellogs.attachHandler(logger, "test")
        self.assertEqual(logger.handlers, before)
        self.otellogs.attachHandler(None, "test")


class ResourceTestCase(unittest.TestCase):
    """The resource is what Loki and Mimir key on."""

    def setUp(self):
        module = importlib.import_module("SiteRMLibs.OpenTelemetry")
        if not module.OTEL_SDK_AVAILABLE:
            self.skipTest("opentelemetry sdk not installed")
        self.buildResource = module.buildResource
        self.saved = os.environ.get("SITERM_COMPONENT")
        os.environ.pop("SITERM_COMPONENT", None)

    def tearDown(self):
        _env(SITERM_COMPONENT=self.saved)

    def testComponentDefaultsToServiceName(self):
        """Loki indexes siterm.component; it must never be empty."""
        res = self.buildResource("LookUpService")
        self.assertEqual(res.attributes["siterm.component"], "LookUpService")

    def testComponentIsOverridable(self):
        """A deployment grouping daemons by role sets it explicitly."""
        _env(SITERM_COMPONENT="frontend")
        res = self.buildResource("LookUpService")
        self.assertEqual(res.attributes["siterm.component"], "frontend")
        self.assertEqual(res.attributes["service.name"], "LookUpService")

    def testSitenameIsNotSent(self):
        """The gateway stamps it from the credential and overwrites anything sent."""
        res = self.buildResource("LookUpService")
        self.assertNotIn("sitename", res.attributes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
