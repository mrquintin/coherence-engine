# OpenTelemetry distributed tracing

This document is the operator-facing reference for the OpenTelemetry
tracing surface introduced by prompt 61. It covers the exporters,
sampling table, PII scrubbing contract, and how to verify a trace
landed at a collector during incident response.

The prompt-18 worker-ops telemetry surface (`ops_telemetry.py`,
`record_stage`, the Prometheus textfile sinks) is unaffected — OTel
is the second, complementary observability surface.

## Module layout

* `server/fund/observability/otel.py` — single entry point.
  - `init_tracing(...)` builds the global `TracerProvider`, attaches
    exporters, applies the PII scrub processor, configures the W3C
    `traceparent` propagator, and runs the auto-instrumentors for
    FastAPI / SQLAlchemy / HTTPX / Redis. Idempotent.
  - `get_tracer(name)` returns a *lazy* tracer that re-resolves the
    current global provider on every span start. Module-level
    `_TRACER = get_tracer(...)` bindings are therefore stable across
    test-suite provider swaps.
  - `install_in_memory_exporter()` is the test-only sentinel; never
    use it in production.
  - `PII_SCRUB_KEYS` is the public allow-list of attribute names the
    PII span processor redacts before export.

* `server/fund/app.py` — calls `init_tracing(fastapi_app=app)` before
  any router is mounted so the FastAPI auto-instrumentor wraps the
  ASGI stack.

* `server/fund/workers/arq_worker.py` — calls `init_tracing(...)` in
  `on_startup`. Each task body is wrapped in
  `tracer.start_as_current_span("arq.<task>")` so a job that spans
  the API and the worker has one continuous trace.

* `server/fund/services/scoring.py` — emits one parent
  `score.application` span and per-layer child spans
  (`score.layer.<name>`) for every coherence layer the scorer
  returns. Layer names are normalised to lower-case.

* `server/fund/services/object_storage.py` — module-level wrappers
  `put` / `get` / `open_stream` / `delete` each emit a span with
  `storage.backend`, `storage.bucket`, `storage.key`, and (where
  applicable) `storage.size_bytes`, `storage.sha256`.

## Exporters

`init_tracing` honours the standard OpenTelemetry env vars:

| Var                              | Effect                                    |
| -------------------------------- | ----------------------------------------- |
| `OTEL_EXPORTER_OTLP_ENDPOINT`    | gRPC endpoint of the collector. Empty → SDK default. |
| `OTEL_TRACES_EXPORTER`           | Comma list of `otlp`, `console`, or `none`. Empty → environment-aware default (see below). |
| `OTEL_SERVICE_NAME`              | Service identifier on every span resource. Empty → `COHERENCE_FUND_SERVICE_NAME` falls through. |
| `OTEL_TRACES_SAMPLER_ARG`        | Float in `[0.0, 1.0]` overriding the per-environment ratio. |

Default exporter selection by environment:

* **dev** / **test** → `console` only. A developer running the API
  locally sees spans on stderr without standing up a collector.
* **staging** / **prod** → `otlp` only. Console output is suppressed
  to avoid log-volume amplification.

When `OTEL_TRACES_EXPORTER=otlp` is set without
`opentelemetry-exporter-otlp-proto-grpc` installed, the wiring logs
a `WARNING` and continues — spans are dropped on the floor rather
than raising.

## Sampling

Head-based, parent-respecting. Applied via
`ParentBased(root=TraceIdRatioBased(ratio))`.

| Environment | Default ratio |
| ----------- | ------------- |
| `dev`       | 1.0 (always sample) |
| `test`      | 1.0 |
| `staging`   | 0.10 |
| `prod`      | 0.01 |
| unknown     | 0.01 (production-safe) |

Set `OTEL_TRACES_SAMPLER_ARG=1.0` for the duration of an incident to
bypass the ratio. The override is process-local; restart the API or
worker to take effect.

The fixed 1% prod ratio is not configurable to a number above 1.0.
Operators tempted to "trace everything in prod" should instead run
the override at 1.0 against a single canary instance.

## PII scrubbing

Spans never carry the unredacted body of any attribute key in
`PII_SCRUB_KEYS`. The `PIIScrubSpanProcessor` runs immediately
before the exporter chain on `on_end`; every exporter (OTLP,
Console, in-memory) sees the redacted copy.

Currently scrubbed:

* `user.email`, `user.phone`
* `founder.email`, `founder.phone`
* `subject.email`, `subject.phone`
* `http.request.header.authorization`
* `http.request.header.cookie`
* `http.request.header.x-coherence-fund-api-key`

Add to `PII_SCRUB_KEYS` whenever a new PII surface is introduced.
The scrubber leaves non-PII attributes untouched and is a no-op for
attributes that have already been redacted upstream.

## W3C traceparent propagation

`init_tracing` installs a `CompositePropagator` of
`TraceContextTextMapPropagator` + `W3CBaggagePropagator`. Outbound
HTTPX requests carry a `traceparent` header automatically when the
HTTPX instrumentor is installed; downstream services that participate
in the same propagator chain join the trace as child spans.

The trace-context format is `00-<trace_id_32hex>-<span_id_16hex>-<flags_2hex>`.

## Operator runbook

### Verify a trace lands

```bash
OTEL_TRACES_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
OTEL_SERVICE_NAME=fund-orchestrator-api \
COHERENCE_FUND_ENV=dev \
uvicorn coherence_engine.server.fund.app:create_app --factory
```

Hit a single application endpoint:

```bash
curl -H "traceparent: 00-$(openssl rand -hex 16)-$(openssl rand -hex 8)-01" \
  http://localhost:8000/api/v1/health
```

In the collector UI you should see one span per HTTP request, child
spans for every DB query (SQLAlchemy auto-instrumentor), Redis
commands when the worker backend is `arq`, and outbound HTTPX calls.

### Force-sample one canary instance in prod

`OTEL_TRACES_SAMPLER_ARG=1.0` on the canary deployment. Leave the
rest of the fleet at the default 1%.

### Disable tracing entirely

`OTEL_TRACES_EXPORTER=none`. The provider is still installed but no
exporter receives spans. To drop tracing without restarting, set
the env var and roll the pod.

### When OTel is not installed

`init_tracing` returns `False` and logs an info line. `get_tracer`
returns a no-op proxy whose `start_as_current_span` is a context
manager that yields a stub span — every call site continues to work
without `OTEL_AVAILABLE` checks.

## Test contract

`tests/test_otel_tracing.py` is the load-bearing regression suite.
The whole module is `pytest.skipif`'d on a no-OTel environment so
the broader suite stays green when the optional dependency is
absent. The tests assert:

* The PII scrubber redacts `user.email` (and `user.phone`,
  authorization headers).
* `score.application` is the parent of every `score.layer.*`
  span and they share a trace id.
* W3C `traceparent` is injected into outbound HTTPX requests.
* `object_storage.put` and `object_storage.get` produce spans with
  the documented attribute set.
* The sampler ratio table matches the documented per-environment
  defaults and respects `OTEL_TRACES_SAMPLER_ARG` overrides.

Run with:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  coherence_engine/tests/test_otel_tracing.py -v
```

## Prohibitions

* Do **not** export spans with raw PII attributes. Add new keys to
  `PII_SCRUB_KEYS` rather than relying on callers to scrub.
* Do **not** raise the prod sampling ratio above the documented
  default fleet-wide. Use the canary path described above instead.
* Do **not** make OpenTelemetry a hard import. The optional-import
  pattern is what keeps the test suite green on environments that
  do not install the SDK.
* Do **not** install `InMemorySpanExporter` from production code.
  It is a test-only sentinel; spans accumulate until the test reads
  them, which would leak in long-lived processes.
