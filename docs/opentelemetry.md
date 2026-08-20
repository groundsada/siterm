# OpenTelemetry

SiteRM can export traces, metrics and logs over OTLP to a collector. Everything
here is **optional and off by default**: with the variables unset, or with the
`opentelemetry-*` packages not installed at all, SiteRM behaves exactly as it
does without this feature.

Nothing on the existing Prometheus scrape path changes. `/metrics` renders the
same series with the same names whether or not any of this is turned on.

## Turning it on

The smallest working configuration is two variables in `/etc/environment`:

```
OPENTELEMETRY_ENABLED=true
OTLP_ENDPOINT=https://collector.example.org
```

With no `OTLP_ENDPOINT`, traces go to the console exporter and metrics and logs
are not exported at all. That is a useful way to check instrumentation without
sending anything anywhere.

## Every variable

### Enabling

| | |
|---|---|
| `OPENTELEMETRY_ENABLED` | the main switch. Default `false` |
| `OTEL_ENABLED` | an alias. Either one alone turns tracing on |
| `OTEL_METRICS_ENABLED` | metrics only. Defaults to the main switch |
| `OTEL_LOGS_ENABLED` | logs only. Defaults to the main switch |

Two names for the main switch is historical: `OTEL_ENABLED` gated the FastAPI
instrumentation and `OPENTELEMETRY_ENABLED` gated the tracer provider, and
neither aliased the other — so setting one and not the other produced either a
provider with nothing feeding it or spans created against a no-op provider and
silently never exported. They now alias, and setting either is enough.

The per-signal switches exist because the signals fail differently. Metrics can
be pushed for parity checking before a site is ready to send traces, and logs
are the one signal nothing samples, so a site needs to be able to drop them
without losing everything else.

Accepted true values are `1`, `true`, `yes`, `on`, case-insensitive, with
surrounding quotes stripped — `OTEL_ENABLED="true"` from `/etc/environment`
works.

### Where telemetry goes

| | |
|---|---|
| `OTLP_ENDPOINT` | one base URL or `host:port` for all three signals |
| `OTLP_PROTOCOL` | `grpc` or `http`. Overrides what the scheme implies |
| `OTLP_INSECURE` | plaintext gRPC. Development only |

The signal path is appended, so configure the base and not
`.../v1/traces`. An already-qualified URL is accepted and rewritten per signal,
so pasting the traces URL does not send metrics to `/v1/traces`.

**The scheme selects the protocol.** `https://host` or `http://host` means
OTLP/HTTP; a bare `host:4317` means gRPC. Prefer HTTP unless the collector is
in the same cluster: gRPC needs HTTP/2 negotiated end to end, and an
institutional egress proxy that terminates and re-originates TLS breaks it,
surfacing as an opaque connection error.

**Prefer `http://host:4317` over `OTLP_INSECURE=true`.** Both give a plaintext
channel, but the endpoint states the intent in one place and stays correct if
the flag handling changes. A schemeless endpoint with `OTLP_INSECURE` unset
attempts TLS, which against a plaintext listener fails silently.

### TLS

| | |
|---|---|
| `OTLP_CA_BUNDLE` | CA file used to verify the collector |
| `OTLP_CLIENT_CERT` | client certificate, for mTLS |
| `OTLP_CLIENT_KEY` | its key |

Unset means the system trust store and no client certificate. Set these when the
collector presents a certificate from a site CA or an internal PKI, which is
common once agents export to their own frontend rather than to a public
endpoint.

`OTLP_CLIENT_CERT` and `OTLP_CLIENT_KEY` must both be set; one without the other
is ignored with a warning rather than passed half-configured, because the
transport reports that as a generic handshake failure. An unreadable path is
reported once at startup and ignored. Neither applies to a plaintext channel.

### Authentication

| | |
|---|---|
| `OTLP_AUTH_URL` | the SiteRM frontend that issues the bearer token |
| `OTLP_AUTH_ENABLED` | set to `false` to export unauthenticated |

**There is no client secret.** The token comes from SiteRM's existing X509
challenge-response: the host certificate is presented, a server challenge is
signed with the host key, and a short-lived JWT comes back. Cert and key are
found the same way every other SiteRM component finds them — `X509_HOST_CERT`
and `X509_HOST_KEY`, then `X509_USER_PROXY`, then `X509_USER_CERT`/`_KEY`, then
the proxy in the temp directory, then `~/.globus`, then
`/etc/grid-security/hostcert.pem`. Nothing new needs distributing.

With `OTLP_AUTH_URL` unset the exporters send no `Authorization` header, which a
collector requiring authentication will reject with 401.

Tokens refresh on their own. Both transports resolve the current token per
request, so nothing is pinned at startup and nothing needs restarting after an
expiry.

### Sampling and volume

| | |
|---|---|
| `OTEL_SAMPLE_RATE` | head sampling ratio. Default `1.0` |
| `OTEL_METRIC_EXPORT_INTERVAL` | milliseconds between metric exports. Default `60000` |
| `OTEL_LOG_LEVEL` | minimum level exported. Default `WARNING` |

**Leave `OTEL_SAMPLE_RATE` at 1.0 if the collector tail-samples.** Dropping here
multiplies: the collector can only choose among traces it was given, so a site
that head-samples at 10% hides 90% of its error traces from a tail sampler whose
whole job is to keep them.

**`OTEL_LOG_LEVEL` is not the logger's level.** File logging keeps whatever
`logLevel` the SiteRM config sets; this only decides what is additionally shipped
over OTLP. It defaults to `WARNING` because logs are not sampled anywhere and
mirroring whole log files is a different order of magnitude from shipping spans.
Lower it deliberately.

The file handler is never replaced. If OTLP were the only sink, the logs needed
to work out why the collector is unreachable would be the ones that vanished
with it.

### Identity

| | |
|---|---|
| `SITERM_COMPONENT` | groups daemons. Defaults to the daemon name |

Set this to something like `frontend` or `agent` if you want to query all
daemons of a role together. It is one of the resource attributes a Loki
deployment may promote to an index label, so keep it low-cardinality — never a
hostname or a delta id.

**`sitename` is deliberately not sent** and there is no variable for it. The
collector stamps site identity from the verified credential and overwrites
anything the payload claims. A site that could label its own telemetry could
label it as another site.

## What you should see when it works

Four counters appear on the ordinary `/metrics` scrape path, deliberately not on
the push path — a metric reporting "telemetry is not reaching the collector"
must not travel over the broken path:

```
siterm_otel_exported_total{signal="traces"}
siterm_otel_dropped_total{signal="traces"}
siterm_otel_export_failures_total{signal="traces",reason="auth"}
siterm_otel_last_export_success_timestamp_seconds{signal="traces"}
```

Working looks like `exported` climbing and `last_export_success_timestamp`
staying close to now.

## Telling "off" from "broken"

This is the distinction the counters exist to make.

| what you see | what it means |
|---|---|
| no `siterm_otel_*` series at all | off, or the packages are not installed |
| `exported` climbing, recent `last_export_success` | working |
| `failures{reason="auth"}` climbing | the collector rejected the token — check `OTLP_AUTH_URL` and that the host certificate is enrolled |
| `failures{reason="unreachable"}` | DNS, firewall or wrong port |
| `failures{reason="timeout"}` | the collector is up but not keeping up |
| `failures{reason="throttled"}` | rate limited; the exporter retries |
| `exported` climbing but nothing in the backend | the collector accepted and dropped it. Look at the collector, not at SiteRM |

`reason` is coarse on purpose — it is a metric label, and a label with
unbounded values is how a metrics store gets damaged.

The `dropped` counter is the one to alert on. It counts telemetry items lost
because an export failed, so a non-zero rate is data that no longer exists
anywhere.

## Running without the packages

Uninstalling every `opentelemetry-*` package is a supported configuration, not a
broken one. Imports of the wrapper still succeed, tracers become no-ops, and
`traceparent()` returns an empty string. Nothing raises and nothing needs a code
change.

Partial installs are also safe: one missing instrumentation package costs that
instrumentation, not tracing as a whole. This is covered by
`test/test-otel.py`:

```sh
cd test && python3 -m unittest test-otel -v
```
