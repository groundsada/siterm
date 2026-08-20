#!/usr/bin/env python3
"""OTLP log export, as a second handler beside the existing file logging.

Level-gated, WARNING and above by default. SiteRM logs at DEBUG, traces are
thinned by the gateway's tail sampler and logs are thinned by nothing, so
mirroring whole log files from 29 sites is what would take Loki's ingester
down. The default ships what someone would be paged for; a site that wants more
lowers OTEL_LOG_LEVEL.

    OTEL_LOGS_ENABLED  optional, defaults to whatever tracing is set to
    OTEL_LOG_LEVEL     threshold for export, default WARNING

The file handler is never replaced. If OTLP were the only sink, the logs needed
to work out why the gateway is unreachable would be the ones that went with it.

Degrades to a no-op when the SDK is absent, like the rest of the otel modules.
"""

import logging
import os
import threading

from SiteRMLibs.OtelWrapper import envBool, otelEnabled

try:  # pragma: no cover - import guard is the point
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    OTEL_LOGS_AVAILABLE = True
except ImportError:  # pragma: no cover
    LoggerProvider = None
    LoggingHandler = None
    BatchLogRecordProcessor = None
    set_logger_provider = None
    OTEL_LOGS_AVAILABLE = False


_LOCK = threading.Lock()
_PROVIDER = None
_HANDLER = None


def logsEnabled():
    """Whether the OTLP log path is on.

    Separate from otelEnabled() because this is the signal with no sampler in
    front of it, so a site needs to be able to turn it off on its own.
    """
    if not OTEL_LOGS_AVAILABLE:
        return False
    if os.getenv("OTEL_LOGS_ENABLED") is not None:
        return envBool("OTEL_LOGS_ENABLED", False)
    return otelEnabled()


def exportLevel():
    """Threshold for what is shipped."""
    name = (os.getenv("OTEL_LOG_LEVEL") or "WARNING").strip().upper()
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        print(f"OpenTelemetry: OTEL_LOG_LEVEL={name} is not a level name. Using WARNING.")
        return logging.WARNING
    return level


def initLogs(service_name, resource=None):
    """Build the logger provider. Idempotent, safe from every daemon.

    Returns the handler to attach, or None when logs are unavailable or off.
    """
    global _PROVIDER, _HANDLER  # pylint: disable=global-statement
    if not logsEnabled():
        return None
    endpoint = os.getenv("OTLP_ENDPOINT")
    if not endpoint:
        return None
    with _LOCK:
        if _HANDLER is not None:
            return _HANDLER

        # Local import: buildResource pulls in MainUtilities, which imports this
        # module back. Deferring it is what keeps that from being a cycle.
        if resource is None:
            from SiteRMLibs.OpenTelemetry import buildResource

            resource = buildResource(service_name)

        from SiteRMLibs.OtelExporters import buildExporter

        exporter = buildExporter("logs", endpoint)
        if exporter is None:
            return None

        _PROVIDER = LoggerProvider(resource=resource)
        _PROVIDER.add_log_record_processor(BatchLogRecordProcessor(exporter))
        set_logger_provider(_PROVIDER)
        _HANDLER = LoggingHandler(level=exportLevel(), logger_provider=_PROVIDER)
        return _HANDLER


def attachHandler(logger, service_name):
    """Add the OTLP handler to `logger`, once.

    Never raises: losing log export must not stop a daemon from getting a
    logger, which is the thing it needs in order to report the failure.
    """
    if logger is None:
        return
    try:
        handler = initLogs(service_name)
        if handler is None:
            return
        if any(h is handler for h in logger.handlers):
            return
        logger.addHandler(handler)
    except Exception as ex:  # pragma: no cover
        print(f"OpenTelemetry log export not attached for {service_name}. Error: {ex}")


def shutdownLogs():
    """Flush and tear down. Tests, and orderly daemon shutdown."""
    global _PROVIDER, _HANDLER  # pylint: disable=global-statement
    with _LOCK:
        if _PROVIDER is not None:
            try:
                _PROVIDER.shutdown()
            except Exception:  # pragma: no cover
                pass
        _PROVIDER = None
        _HANDLER = None
