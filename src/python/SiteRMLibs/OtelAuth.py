#!/usr/bin/env python3
"""OAuth2 client credentials for the OTLP exporters.

The central gateway authenticates every push and derives the site's identity
from the token, so a site never declares who it is. That means SiteRM has to
present a credential on every export, and has to keep presenting a valid one for
the life of the process.

Configuration, from /etc/environment like the rest:

    OTLP_AUTH_ISSUER          e.g. https://<keycloak>/realms/sense-telemetry
    OTLP_AUTH_CLIENT_ID       e.g. siterm-T2_US_SDSC
    OTLP_AUTH_CLIENT_SECRET
    OTLP_AUTH_TOKEN_URL       optional, overrides the derived token endpoint
    OTLP_AUTH_VERIFY          optional, "false" to skip TLS verification

When OTLP_AUTH_ISSUER is unset this module is inert and the exporters get no
auth header, so local development against a plaintext collector is unchanged.

Dependency-free apart from `requests`, for the same reason OtelWrapper is: it
must be importable from anywhere without dragging SiteRMLibs.MainUtilities and
the DBBackend cycle along with it.
"""

import os
import threading
import time

try:  # pragma: no cover - requests is a hard dependency, guard is belt and braces
    import requests
except ImportError:  # pragma: no cover
    requests = None

# Refresh this long before the token actually expires. A token that expires
# mid-batch produces a 401 the exporter reports as a generic export failure,
# which is the hardest possible way to discover a clock or config problem.
REFRESH_MARGIN = 300

# Never hammer the IdP. If it is down we retry on this floor rather than on
# every single export call.
MIN_RETRY_INTERVAL = 30


class OtelTokenSource:
    """Caches a bearer token and refreshes it before expiry.

    Thread-safe: BatchSpanProcessor, the metric reader and the log processor all
    export from their own threads, and all three ask for the same token.
    """

    def __init__(self, issuer=None, clientId=None, clientSecret=None, tokenUrl=None, verify=None):
        self.issuer = issuer if issuer is not None else os.getenv("OTLP_AUTH_ISSUER", "")
        self.clientId = clientId if clientId is not None else os.getenv("OTLP_AUTH_CLIENT_ID", "")
        self.clientSecret = clientSecret if clientSecret is not None else os.getenv("OTLP_AUTH_CLIENT_SECRET", "")
        self.tokenUrl = tokenUrl or os.getenv("OTLP_AUTH_TOKEN_URL", "")
        if verify is None:
            verify = os.getenv("OTLP_AUTH_VERIFY", "true").strip('"').strip("'").lower() not in {"0", "false", "no", "off"}
        self.verify = verify
        if not self.tokenUrl and self.issuer:
            self.tokenUrl = f"{self.issuer.rstrip('/')}/protocol/openid-connect/token"
        self._lock = threading.Lock()
        self._token = ""
        self._expiresAt = 0.0
        self._nextAttempt = 0.0
        self.lastError = ""

    def configured(self):
        """Whether enough is set to attempt a token fetch."""
        return bool(self.tokenUrl and self.clientId and self.clientSecret and requests is not None)

    def _fetch(self):
        """One token request. Returns True on success."""
        try:
            resp = requests.post(
                self.tokenUrl,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.clientId,
                    "client_secret": self.clientSecret,
                },
                timeout=10,
                verify=self.verify,
            )
            if resp.status_code != 200:
                self.lastError = f"token endpoint returned {resp.status_code}"
                return False
            payload = resp.json()
            token = payload.get("access_token", "")
            if not token:
                self.lastError = "token endpoint returned no access_token"
                return False
            # Trust expires_in, but floor it so a bogus or tiny value cannot turn
            # this into a request-per-export loop against the IdP.
            lifetime = float(payload.get("expires_in", 3600) or 3600)
            self._token = token
            self._expiresAt = time.time() + max(lifetime, 60.0)
            self.lastError = ""
            return True
        except Exception as ex:  # pragma: no cover - network paths
            self.lastError = str(ex)
            return False

    def token(self):
        """Current token, fetching or refreshing as needed. "" when unavailable.

        Returning "" rather than raising is deliberate. Telemetry is not a hard
        dependency: if the IdP is unreachable at startup SiteRM must still boot
        and serve. The export then fails and is counted, which is #17's job.
        """
        if not self.configured():
            return ""
        with self._lock:
            now = time.time()
            if self._token and now < self._expiresAt - REFRESH_MARGIN:
                return self._token
            if now < self._nextAttempt:
                # Still inside the backoff window; hand back whatever we have,
                # which may be a token that is valid but inside the margin.
                return self._token if now < self._expiresAt else ""
            self._nextAttempt = now + MIN_RETRY_INTERVAL
            if self._fetch():
                return self._token
            return self._token if now < self._expiresAt else ""

    def headers(self):
        """Auth headers for an OTLP exporter, or {} when unconfigured."""
        tok = self.token()
        if not tok:
            return {}
        return {"authorization": f"Bearer {tok}"}


_SOURCE = None
_SOURCE_LOCK = threading.Lock()


def getTokenSource():
    """Process-wide token source, so all three signals share one token."""
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
