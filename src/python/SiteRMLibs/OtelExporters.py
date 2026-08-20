#!/usr/bin/env python3
"""Builds OTLP exporters: right protocol, right endpoint, authenticated.

Three things this decides, none of which the callers should have to know:

PROTOCOL. `OTEL_EXPORTER_OTLP_PROTOCOL` wins if set; otherwise the endpoint
scheme picks. A URL with http:// or https:// means OTLP/HTTP; a bare host:port
means gRPC. 15 of the 29 frontends are off NRP and reach the gateway only
through the public HTTPS ingress, where gRPC's end-to-end HTTP/2 does not
survive an institutional proxy that re-originates TLS.

ENDPOINT. OTLP/HTTP needs a per-signal path. A site pastes one URL and gets all
three signals routed correctly, whether the URL is a base or already ends in
/v1/traces.

AUTH. The gateway derives site identity from the credential, so every export
carries a bearer token. Both transports refresh it without rebuilding the
exporter: gRPC through call credentials, HTTP through a session hook.
"""

import os

from SiteRMLibs.OtelAuth import getTokenSource

_SIGNAL_PATHS = {"traces": "v1/traces", "metrics": "v1/metrics", "logs": "v1/logs"}


def envBool(name, default=False):
    """Boolean from environment, same semantics as OtelWrapper.envBool."""
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip('"').strip("'").lower() in {"1", "true", "yes", "on"}


def resolveProtocol(endpoint):
    """"http" or "grpc" for `endpoint`.

    OTEL_EXPORTER_OTLP_PROTOCOL is the OTel-standard name and takes precedence,
    so a site that needs gRPC to an https endpoint (or the reverse) has a
    documented way to say so rather than fighting the inference.
    """
    explicit = (os.getenv("OTLP_PROTOCOL") or os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL") or "").strip().lower()
    if explicit in {"grpc"}:
        return "grpc"
    if explicit in {"http", "http/protobuf", "httpprotobuf", "http/json"}:
        return "http"
    if endpoint.startswith(("http://", "https://")):
        return "http"
    return "grpc"


def signalEndpoint(endpoint, signal):
    """Full OTLP/HTTP URL for `signal`.

    Accepts a base (https://gw.example) or an already-qualified signal URL
    (https://gw.example/v1/traces) and returns the correct URL for the signal
    asked for. Without this a site that pasted the traces URL -- which is what
    the docs show -- would ship metrics to /v1/traces and have them rejected.
    """
    base = endpoint.rstrip("/")
    for path in _SIGNAL_PATHS.values():
        if base.endswith("/" + path):
            base = base[: -len(path) - 1].rstrip("/")
            break
    return f"{base}/{_SIGNAL_PATHS[signal]}"


def _insecure(endpoint):
    """Whether the gRPC channel should be plaintext.

    Explicit OTLP_INSECURE wins. Otherwise None, meaning "say nothing and let
    the SDK's own env handling decide", which defaults to secure. Spans carry
    delta ids, topology and the authenticated user subject, so plaintext must be
    something a deployment asks for rather than something it gets by omission.
    """
    if os.getenv("OTLP_INSECURE") is not None:
        return envBool("OTLP_INSECURE", False)
    if endpoint.startswith("http://"):
        return True
    return None


class _AuthSession:
    """requests.Session that stamps a fresh bearer token on every request.

    The OTLP/HTTP exporters accept a `session`, which is the supported hook for
    this. Setting a static `headers` dict instead would pin the token taken at
    startup and start failing an hour later, once, silently.
    """

    def __new__(cls, tokenSource):
        import requests  # local: keep module import cheap when otel is absent

        session = requests.Session()
        original = session.request

        def request(method, url, **kwargs):
            """Inject the current token, then delegate."""
            headers = kwargs.pop("headers", None) or {}
            token = tokenSource.token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return original(method, url, headers=headers, **kwargs)

        session.request = request
        return session


def _grpcCredentials(tokenSource, insecure):
    """Channel credentials carrying a per-RPC bearer token, or None.

    grpc resolves call credentials on every RPC, so the token refreshes without
    rebuilding the channel. Call credentials require a secure channel -- grpc
    refuses to attach them to a plaintext one -- so an insecure endpoint gets
    no auth here and relies on the endpoint being a local collector.
    """
    if insecure:
        return None
    try:
        import grpc
    except ImportError:  # pragma: no cover
        return None

    class _Plugin(grpc.AuthMetadataPlugin):
        """Supplies the Authorization header for each call."""

        def __call__(self, context, callback):
            token = tokenSource.token()
            metadata = (("authorization", f"Bearer {token}"),) if token else ()
            callback(metadata, None)

    return grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(),
        grpc.metadata_call_credentials(_Plugin()),
    )


def buildExporter(signal, endpoint, tokenSource=None):
    """OTLP exporter for `signal`, or None if the SDK pieces are missing.

    `signal` is one of traces, metrics, logs.
    """
    if signal not in _SIGNAL_PATHS:
        raise ValueError(f"unknown signal {signal}")
    tokenSource = tokenSource if tokenSource is not None else getTokenSource()
    protocol = resolveProtocol(endpoint)

    if protocol == "http":
        try:
            if signal == "traces":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as Exporter
            elif signal == "metrics":
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as Exporter
            else:
                from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter as Exporter
        except ImportError as ex:  # pragma: no cover
            print(f"OpenTelemetry OTLP/HTTP exporter unavailable for {signal}. Error: {ex}")
            return None
        kwargs = {"endpoint": signalEndpoint(endpoint, signal)}
        if tokenSource.configured():
            kwargs["session"] = _AuthSession(tokenSource)
        return Exporter(**kwargs)

    try:
        if signal == "traces":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as Exporter
        elif signal == "metrics":
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as Exporter
        else:
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter as Exporter
    except ImportError as ex:  # pragma: no cover
        print(f"OpenTelemetry OTLP/gRPC exporter unavailable for {signal}. Error: {ex}")
        return None

    kwargs = {"endpoint": endpoint}
    insecure = _insecure(endpoint)
    if insecure is not None:
        kwargs["insecure"] = insecure
    if tokenSource.configured():
        credentials = _grpcCredentials(tokenSource, insecure)
        if credentials is not None:
            kwargs["credentials"] = credentials
            kwargs.pop("insecure", None)
        else:
            # grpc refuses to attach call credentials to a plaintext channel, so
            # there is no way to authenticate this and no way to refresh a token
            # on it. Say so loudly: the alternative is exporting unauthenticated
            # to a gateway that will 401 every batch, which surfaces only as a
            # generic export failure.
            print(
                f"OpenTelemetry: OTLP auth is configured but endpoint {endpoint} is plaintext gRPC. "
                "gRPC cannot carry a bearer token without TLS. Use the OTLP/HTTP endpoint "
                "(port 4318) or a TLS gRPC endpoint. Exporting unauthenticated."
            )
    return Exporter(**kwargs)
