#!/usr/bin/env python3
"""Builds OTLP exporters: right protocol, right endpoint, authenticated.

Protocol comes from OTEL_EXPORTER_OTLP_PROTOCOL or the endpoint scheme; HTTP is
the default off-NRP because gRPC's end-to-end HTTP/2 does not survive a proxy
that re-originates TLS. Signal paths are appended so a site configures one URL.
Both transports refresh the bearer token without rebuilding the exporter.

    OTLP_CA_BUNDLE    CA file used to verify the collector
    OTLP_CLIENT_CERT  client certificate, for mTLS
    OTLP_CLIENT_KEY   its key

Unset means the system trust store and no client certificate, so a site that
does not need any of this configures none of it.
"""

# Optional packages are imported where they are used, not at module scope: that
# deferral is what keeps them optional.
# pylint: disable=import-outside-toplevel

import os

from SiteRMLibs.OtelAuth import getTokenSource
from SiteRMLibs.OtelHealth import CountingExporter, noteHttpStatus

_SIGNAL_PATHS = {"traces": "v1/traces", "metrics": "v1/metrics", "logs": "v1/logs"}


def envBool(name, default=False):
    """Boolean from environment, same semantics as OtelWrapper.envBool."""
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip('"').strip("'").lower() in {"1", "true", "yes", "on"}


def resolveProtocol(endpoint):
    """"http" or "grpc" for `endpoint`. Explicit env wins over the scheme."""
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

    Accepts a base or an already-qualified signal URL, so a site that pasted the
    traces URL does not end up shipping metrics to /v1/traces.
    """
    base = endpoint.rstrip("/")
    for path in _SIGNAL_PATHS.values():
        if base.endswith("/" + path):
            base = base[: -len(path) - 1].rstrip("/")
            break
    return f"{base}/{_SIGNAL_PATHS[signal]}"


def _insecure(endpoint):
    """Whether the gRPC channel should be plaintext.

    None means "defer to the SDK", which defaults to secure. Plaintext must be
    asked for explicitly, not acquired by omission.
    """
    if os.getenv("OTLP_INSECURE") is not None:
        return envBool("OTLP_INSECURE", False)
    if endpoint.startswith("http://"):
        return True
    return None


def _readable(path, name):
    """`path` if it can be read, otherwise None and a warning.

    Checked once at startup rather than left to the transport, which reports a
    missing trust file as an opaque handshake failure on every export.
    """
    if not path:
        return None
    if os.access(path, os.R_OK):
        return path
    print(f"OpenTelemetry: {name}={path} is not readable. Ignoring it.")
    return None


def tlsMaterial():
    """(caBundle, clientCert, clientKey) paths, each None when unset."""
    ca = _readable(os.getenv("OTLP_CA_BUNDLE"), "OTLP_CA_BUNDLE")
    cert = _readable(os.getenv("OTLP_CLIENT_CERT"), "OTLP_CLIENT_CERT")
    key = _readable(os.getenv("OTLP_CLIENT_KEY"), "OTLP_CLIENT_KEY")
    if bool(cert) != bool(key):
        print("OpenTelemetry: OTLP_CLIENT_CERT and OTLP_CLIENT_KEY must both be set for mTLS. Ignoring both.")
        cert = key = None
    return ca, cert, key


class _AuthSession:  # pylint: disable=too-few-public-methods
    """requests.Session that stamps a fresh bearer token on every request.

    A static `headers` dict would pin the startup token and fail an hour later.
    """

    def __new__(cls, tokenSource, signal, tls=(None, None, None)):
        import requests  # local: keep module import cheap when otel is absent

        session = requests.Session()
        ca, cert, key = tls
        if ca:
            session.verify = ca
        if cert and key:
            session.cert = (cert, key)
        original = session.request

        def request(method, url, **kwargs):
            """Inject the current token, delegate, and note the status."""
            headers = kwargs.pop("headers", None) or {}
            token = tokenSource.token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            response = original(method, url, headers=headers, **kwargs)
            noteHttpStatus(signal, getattr(response, "status_code", None))
            return response

        session.request = request
        return session


def _pem(path):
    """PEM bytes from `path`, or None. grpc wants bytes where requests wants a path."""
    if not path:
        return None
    try:
        with open(path, "rb") as fd:
            return fd.read()
    except OSError as ex:  # pragma: no cover
        print(f"OpenTelemetry: cannot read {path}. Error: {ex}")
        return None


def _grpcCredentials(tokenSource, insecure):
    """Channel credentials for the gRPC exporter, or None to leave it to the SDK.

    Carries the trust material and, when auth is configured, a per-RPC bearer
    token: grpc resolves the token on every RPC, so it refreshes without
    rebuilding the channel. It refuses to attach one to a plaintext channel,
    hence None there.
    """
    if insecure:
        return None
    try:
        import grpc
    except ImportError:  # pragma: no cover
        return None

    ca, cert, key = tlsMaterial()
    if not tokenSource.configured() and not any((ca, cert, key)):
        return None

    channel = grpc.ssl_channel_credentials(
        root_certificates=_pem(ca),
        private_key=_pem(key),
        certificate_chain=_pem(cert),
    )
    if not tokenSource.configured():
        return channel

    class _Plugin(grpc.AuthMetadataPlugin):  # pylint: disable=too-few-public-methods
        """Supplies the Authorization header for each call."""

        def __call__(self, context, callback):
            token = tokenSource.token()
            metadata = (("authorization", f"Bearer {token}"),) if token else ()
            callback(metadata, None)

    return grpc.composite_channel_credentials(channel, grpc.metadata_call_credentials(_Plugin()))


def buildExporter(signal, endpoint, tokenSource=None):  # pylint: disable=too-many-branches
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
        tls = tlsMaterial()
        if tokenSource.configured() or any(tls):
            kwargs["session"] = _AuthSession(tokenSource, signal, tls)
        return CountingExporter(Exporter(**kwargs), signal)

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
    credentials = _grpcCredentials(tokenSource, insecure)
    if credentials is not None:
        kwargs["credentials"] = credentials
        kwargs.pop("insecure", None)
    elif tokenSource.configured():
        # Loud, because the alternative is 401 on every batch surfacing only
        # as a generic export failure.
        print(
            f"OpenTelemetry: OTLP auth is configured but endpoint {endpoint} is plaintext gRPC. "
            "gRPC cannot carry a bearer token without TLS. Use the OTLP/HTTP endpoint "
            "(port 4318) or a TLS gRPC endpoint. Exporting unauthenticated."
        )
    return CountingExporter(Exporter(**kwargs), signal)
