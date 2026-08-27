#!/usr/bin/env python3
"""Bearer tokens for the OTLP exporters, from the SiteRM frontend.

The gateway derives site identity from the token issuer, so every export carries
one. Acquiring it is SiteRM's existing X509 challenge-response: the host cert is
presented, a server challenge is signed with the host key, and a short-lived JWT
comes back. HTTPLibrary.Requests already implements that, including refresh and
rate-limit backoff, so this only adds caching and a never-raise contract.

    OTLP_AUTH_URL     frontend that issues the token, e.g. https://fe.example
    OTLP_AUTH_ENABLED optional, "false" to export unauthenticated

Cert and key come from getKeyCertFromEnv(), the same search order every other
SiteRM component uses. When neither is configured this module is inert.
"""

import os
import threading
import time

from SiteRMLibs.HTTPLibrary import Requests, getKeyCertFromEnv
from SiteRMLibs.OtelWrapper import envBool

# Retry floor, so a frontend that is down is not hammered on every export.
MIN_RETRY_INTERVAL = 30


class OtelTokenSource:
    """Caches a bearer token for the exporters and renews it on demand."""

    def __init__(self):
        self._lock = threading.Lock()
        self._requests = None
        self._nextRetry = 0
        self._url = (os.getenv("OTLP_AUTH_URL") or "").strip()

    def configured(self):
        """Whether a token can be obtained at all."""
        if not self._url or not envBool("OTLP_AUTH_ENABLED", True):
            return False
        cert, key = getKeyCertFromEnv()
        return bool(cert and key)

    def _client(self):
        """Lazily built Requests, which owns the token and its refresh."""
        if self._requests is None:
            self._requests = Requests(url=self._url)
        return self._requests

    def token(self):
        """Current bearer token, or "" if one cannot be obtained.

        Never raises: a failure here must degrade to an unauthenticated export
        the gateway rejects and OtelHealth counts, not take down the caller.
        """
        if not self.configured():
            return ""
        with self._lock:
            now = time.time()
            if now < self._nextRetry:
                return ""
            try:
                # Cheap when the cached token is still valid; renews when not.
                return self._client().ensureBearerToken() or ""
            except Exception as ex:
                self._nextRetry = now + MIN_RETRY_INTERVAL
                print(f"OpenTelemetry: could not obtain a token from {self._url}. Error: {ex}")
                return ""

    def headers(self):
        """Authorization header dict, empty when unauthenticated."""
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}


_SOURCE = None
_SOURCE_LOCK = threading.Lock()


def getTokenSource():
    """Process-wide token source."""
    global _SOURCE  # pylint: disable=global-statement
    with _SOURCE_LOCK:
        if _SOURCE is None:
            _SOURCE = OtelTokenSource()
        return _SOURCE


def resetTokenSource():
    """Drop the cached source. Tests only."""
    global _SOURCE  # pylint: disable=global-statement
    with _SOURCE_LOCK:
        _SOURCE = None
