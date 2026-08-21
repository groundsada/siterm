#!/usr/bin/env python3
"""Tests for the OIDC issuer string the frontend publishes.

The issuer is a protocol identifier, not a display value: a relying party
compares it to the token's `iss` claim byte for byte, and go-oidc compares its
configured URL to the one in the discovery document the same way. SiteRM
derives it from `general.webdomain` when OIDC_ISSUER is unset, so whatever a
site wrote there for humans becomes the thing every federating party must
match. These tests pin the two ends of that: what gets cleaned up, and what is
deliberately left alone.

    python3 -m unittest test-auth -v
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "python"))

from SiteRMLibs.Auth import normalizeIssuer  # noqa: E402  pylint: disable=wrong-import-position


def normalized(value, derived=False):
    """(result, printed warnings) for `value`."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = normalizeIssuer(value, derived=derived)
    return result, buf.getvalue()


class TrailingSlashTestCase(unittest.TestCase):
    """A trailing slash is not cosmetic here."""

    def testStripped(self):
        """getOpenIDConfiguration concatenates, so a slash yields `//.well-known/`."""
        self.assertEqual(normalized("https://fe.example.org/")[0], "https://fe.example.org")

    def testRepeatedStripped(self):
        """More than one is the same bug."""
        self.assertEqual(normalized("https://fe.example.org///")[0], "https://fe.example.org")

    def testDerivedEndpointsHaveOneSlash(self):
        """The property the stripping exists for."""
        issuer = normalized("https://fe.example.org/")[0]
        self.assertEqual(f"{issuer}/.well-known/jwks.json", "https://fe.example.org/.well-known/jwks.json")

    def testSurroundingWhitespaceStripped(self):
        """A stray newline from a config file must not reach the `iss` claim."""
        self.assertEqual(normalized("  https://fe.example.org  ")[0], "https://fe.example.org")


class DefaultPortTestCase(unittest.TestCase):
    """The default port is reported and kept, never silently removed."""

    def testHttpsDefaultPortPreserved(self):
        """Removing it would invalidate every already-enrolled relying party."""
        self.assertEqual(normalized("https://fe.example.org:443")[0], "https://fe.example.org:443")

    def testHttpsDefaultPortWarns(self):
        """Silence would leave the instability undiscovered until a gateway broke."""
        self.assertIn("default port", normalized("https://fe.example.org:443")[1])

    def testHttpDefaultPortWarns(self):
        """Same rule for the other scheme."""
        self.assertIn("default port", normalized("http://fe.example.org:80")[1])

    def testNonDefaultPortIsSilent(self):
        """:8443 is load-bearing -- it is not noise and must not be reported as such."""
        result, warning = normalized("https://fe.example.org:8443")
        self.assertEqual(result, "https://fe.example.org:8443")
        self.assertEqual(warning, "")

    def testPlainHostIsSilent(self):
        """The form we would like everyone to publish draws no comment."""
        result, warning = normalized("https://fe.example.org")
        self.assertEqual(result, "https://fe.example.org")
        self.assertEqual(warning, "")

    def testWarningNamesTheSource(self):
        """An operator has to know which knob to turn."""
        self.assertIn("general.webdomain", normalized("https://fe.example.org:443", derived=True)[1])
        self.assertIn("OIDC_ISSUER", normalized("https://fe.example.org:443", derived=False)[1])

    def testPortInPathIsNotTreatedAsAPort(self):
        """Only the authority is inspected."""
        result, warning = normalized("https://fe.example.org/tenant:443")
        self.assertEqual(result, "https://fe.example.org/tenant:443")
        self.assertEqual(warning, "")


class MalformedTestCase(unittest.TestCase):
    """Bad input is reported, never raised -- this runs during AuthHandler init."""

    def testMissingSchemeWarns(self):
        """`iss` has to be an absolute URL; a bare host:port is not one."""
        self.assertIn("no scheme", normalized("fe.example.org:8443")[1])

    def testMissingSchemeIsReturnedUnchanged(self):
        """Guessing https:// would be a different kind of silent rewrite."""
        self.assertEqual(normalized("fe.example.org:8443")[0], "fe.example.org:8443")

    def testEmpty(self):
        """An unset webdomain must not crash the frontend at startup."""
        self.assertEqual(normalized("")[0], "")

    def testNone(self):
        """gitConf.get can return None."""
        self.assertEqual(normalized(None)[0], "")

    def testEmptyDoesNotWarn(self):
        """Nothing useful to say, and it would fire on every unconfigured install."""
        self.assertEqual(normalized("")[1], "")


class FleetTestCase(unittest.TestCase):
    """Strings actually published by deployed frontends, read from their
    discovery documents on 2026-08-20. Both forms are in the fleet at once,
    which is the whole reason this cannot be normalised silently."""

    WITH_PORT = [
        "https://sense-ladowntown.nrp-nautilus.io:443",
        "https://sense-gpn.nrp-nautilus.io:443",
        "https://sense.af.uchicago.edu:443",
    ]
    WITHOUT_PORT = [
        "https://sense-nrp-internet2.nrp-nautilus.io",
        "https://sense-fe.nrp-nautilus.io",
    ]
    NON_DEFAULT_PORT = [
        "https://red-sense-rm.unl.edu:10443",
        "https://g5intel2.it.northwestern.edu:8443",
    ]

    def testEveryPublishedIssuerSurvivesUnchanged(self):
        """Normalisation must never move a site off the string it publishes."""
        for issuer in self.WITH_PORT + self.WITHOUT_PORT + self.NON_DEFAULT_PORT:
            with self.subTest(issuer=issuer):
                self.assertEqual(normalized(issuer)[0], issuer)


if __name__ == "__main__":
    unittest.main()
