#!/usr/bin/env python3
"""Builds OTLP exporters: right protocol, right endpoint, authenticated.

Protocol comes from OTEL_EXPORTER_OTLP_PROTOCOL or the endpoint scheme; HTTP is
the default off-NRP because gRPC's end-to-end HTTP/2 does not survive a proxy
that re-originates TLS. Signal paths are appended so a site configures one URL.
Both transports refresh the bearer token without rebuilding the exporter.
"""

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


class _AuthSession:
    """requests.Session that stamps a fresh bearer token on every request.

    A static `headers` dict would pin the startup token and fail an hour later.
    """

    def __new__(cls, tokenSource, signal):
        import requests  # local: keep module import cheap when otel is absent

        session = requests.Session()
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


def _grpcCredentials(tokenSource, insecure):
    """Channel credentials carrying a per-RPC bearer token, or None.

    grpc resolves these on every RPC, so the token refreshes without rebuilding
    the channel. It refuses to attach them to a plaintext channel, hence None.
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
            kwargs["session"] = _AuthSession(tokenSource, signal)
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
    if tokenSource.configured():
        credentials = _grpcCredentials(tokenSource, insecure)
        if credentials is not None:
            kwargs["credentials"] = credentials
            kwargs.pop("insecure", None)
        else:
            # Loud, because the alternative is 401 on every batch surfacing only
            # as a generic export failure.
            print(
                f"OpenTelemetry: OTLP auth is configured but endpoint {endpoint} is plaintext gRPC. "
                "gRPC cannot carry a bearer token without TLS. Use the OTLP/HTTP endpoint "
                "(port 4318) or a TLS gRPC endpoint. Exporting unauthenticated."
            )
    return CountingExporter(Exporter(**kwargs), signal)
