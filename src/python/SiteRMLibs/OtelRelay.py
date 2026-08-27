#!/usr/bin/env python3
"""Where an OTLP payload relayed by the frontend should be sent, and whether.

Separate from SiteFE.REST.Otlp on purpose. That module cannot be imported
without a real site config -- SiteFE.REST.dependencies builds the git config at
module scope -- so anything living there is untestable without a deployed
frontend. The decisions here are pure, and they are the part with the edge
cases: the forwarding itself is one httpx call.

Returns an error tuple rather than raising an HTTP exception, so this stays
free of any web framework and the REST layer owns the translation.
"""

import os

from SiteRMLibs.OtelExporters import resolveProtocol, signalEndpoint

# The three OTLP signal paths. Anything else is rejected rather than forwarded,
# so a typo cannot become a request to an arbitrary upstream path.
SIGNALS = {"traces", "metrics", "logs"}

# An OTLP batch is small; a request this large is a bug, or an attempt to use
# the frontend as a general-purpose proxy. The body is read before forwarding,
# so this bounds frontend memory rather than just the upstream write.
MAX_BODY_BYTES = 8 * 1024 * 1024


def relayTarget(general, signal):
    """(url, None) when `signal` can be relayed, else (None, (status, detail)).

    `general` is the config's general section. The status codes distinguish
    three situations an operator confuses easily:

      404 the relay was never turned on -- nothing is wrong, it is just off
      503 it is on but unusable, which is a misconfiguration to go and fix
      200 fine

    The gRPC case is refused rather than attempted. OTLP_ENDPOINT is
    legitimately a bare `host:port` for a gRPC exporter, and a gRPC endpoint
    cannot be fed an HTTP POST body -- forwarding anyway would fail once per
    export with a message that points at the collector instead of at the
    setting that is wrong.
    """
    general = general or {}
    if signal not in SIGNALS:
        return None, (404, f"Unknown OTLP signal '{signal}'. Expected one of: {', '.join(sorted(SIGNALS))}.")
    if not general.get("otlp_relay", False):
        return None, (404, "OTLP relay is not enabled on this frontend. Set general.otlp_relay to enable it.")

    # Config first, environment second. A site that already set OTLP_ENDPOINT
    # for the frontend's own exporter gets the relay aimed at the same place
    # without configuring it twice.
    endpoint = (general.get("otlp_endpoint") or os.getenv("OTLP_ENDPOINT") or "").strip()
    if not endpoint:
        return None, (503, "OTLP relay is enabled but no upstream endpoint is configured (general.otlp_endpoint or OTLP_ENDPOINT).")
    if resolveProtocol(endpoint) != "http":
        return None, (
            503,
            f"OTLP relay needs an OTLP/HTTP upstream. {endpoint} resolves to gRPC; give it an http:// or https:// endpoint.",
        )
    return signalEndpoint(endpoint, signal), None
