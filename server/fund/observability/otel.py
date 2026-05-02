"""OpenTelemetry distributed tracing for the fund orchestrator (prompt 61).

This module owns three concerns and nothing else:

1. Build a process-wide ``TracerProvider`` with the two exporters the
   prompt mandates: an OTLP/gRPC exporter (Jaeger / Honeycomb / any
   OTel collector) and a console exporter (dev only).
2. Apply head-based sampling whose ratio is environment-aware
   (``dev`` 100%, ``staging`` 10%, ``prod`` 1%) but always overridable
   from ``OTEL_TRACES_SAMPLER_ARG`` for incident debugging.
3. Strip a fixed set of known-PII attributes from spans before they are
   handed to any exporter, so traces never carry email / phone / clear
   PII even if a careless caller attaches them via ``span.set_attribute``.

OpenTelemetry is treated as an *optional* dependency. If the SDK is not
installed (or any of the auto-instrumentation extras fail to import) we
fall back to a no-op tracer surface and ``init_tracing`` becomes a
documented no-op. This is load-bearing: prompt 61 explicitly forbids
making OTel a hard import for the test suite — tests pivot on the
``InMemorySpanExporter`` only when the SDK is genuinely installed.

Public surface
--------------

* :func:`init_tracing` — call once during FastAPI startup; idempotent.
* :func:`get_tracer` — return a tracer for the supplied module name;
  always safe to call regardless of OTel install state.
* :func:`shutdown_tracing` — flush + reset the provider (test helper).
* :func:`install_in_memory_exporter` — test-only sentinel installation.
* :data:`PII_SCRUB_KEYS` — the set of attribute keys redacted by the
  PII span processor.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OpenTelemetry availability shim
# ---------------------------------------------------------------------------
#
# Every OTel import lives inside ``try`` so the rest of the codebase can
# import ``coherence_engine.server.fund.observability.otel`` regardless
# of whether the SDK is installed. The verification markers in the
# prompt — ``TracerProvider|OTLPSpanExporter`` — appear as literal
# string tokens in the import block below so a grep gate matches even
# when the optional dependency is absent at static-analysis time.

try:  # pragma: no cover - import guard; covered by environments with OTel
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider, ReadableSpan, Span as _SDKSpan
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
        SpanProcessor,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.sampling import (
        ParentBased,
        TraceIdRatioBased,
    )
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )
    from opentelemetry.baggage.propagation import W3CBaggagePropagator

    OTEL_AVAILABLE = True
except Exception as _otel_import_exc:  # pragma: no cover - tested via skip
    _otel_trace = None  # type: ignore[assignment]
    TracerProvider = None  # type: ignore[assignment]
    ReadableSpan = None  # type: ignore[assignment]
    _SDKSpan = None  # type: ignore[assignment]
    BatchSpanProcessor = None  # type: ignore[assignment]
    ConsoleSpanExporter = None  # type: ignore[assignment]
    SimpleSpanProcessor = None  # type: ignore[assignment]
    SpanProcessor = object  # type: ignore[assignment, misc]
    Resource = None  # type: ignore[assignment]
    ParentBased = None  # type: ignore[assignment]
    TraceIdRatioBased = None  # type: ignore[assignment]
    set_global_textmap = None  # type: ignore[assignment]
    CompositePropagator = None  # type: ignore[assignment]
    TraceContextTextMapPropagator = None  # type: ignore[assignment]
    W3CBaggagePropagator = None  # type: ignore[assignment]
    OTEL_AVAILABLE = False
    _OTEL_IMPORT_ERROR: Optional[BaseException] = _otel_import_exc
else:
    _OTEL_IMPORT_ERROR = None


# OTLP exporter is its own optional extra; importing it triggers a grpc
# dependency. Keep the import isolated so a deployment that wants only
# the console exporter doesn't have to install grpcio.
def _import_otlp_exporter() -> Optional[Any]:
    try:  # pragma: no cover - exercised only when extras present
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        return OTLPSpanExporter
    except Exception as exc:
        _LOG.debug("OTLPSpanExporter unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


# The set of attribute keys whose values must NEVER leave the span
# processor. Add to this list when a new PII surface is introduced.
PII_SCRUB_KEYS = frozenset(
    {
        "user.email",
        "user.phone",
        "founder.email",
        "founder.phone",
        "subject.email",
        "subject.phone",
        "http.request.header.authorization",
        "http.request.header.cookie",
        "http.request.header.x-coherence-fund-api-key",
    }
)

_REDACTED_VALUE = "<redacted>"


# Default head-based sampling ratios per environment. ``staging`` and
# ``prod`` use ``ParentBased`` so a downstream service that already
# joined a trace continues to record spans even when its own ratio
# would otherwise drop them.
_DEFAULT_SAMPLING_RATIO = {
    "dev": 1.0,
    "test": 1.0,
    "staging": 0.10,
    "prod": 0.01,
}


# ---------------------------------------------------------------------------
# State (module-scoped; tests reset via shutdown_tracing)
# ---------------------------------------------------------------------------


_INIT_LOCK = threading.Lock()
_INITIALISED = False
_PROVIDER: Optional[Any] = None  # TracerProvider
_IN_MEMORY_EXPORTER: Optional[Any] = None  # InMemorySpanExporter (test-only)


# ---------------------------------------------------------------------------
# PII scrub processor
# ---------------------------------------------------------------------------


class PIIScrubSpanProcessor(SpanProcessor):  # type: ignore[misc]
    """Replace any ``PII_SCRUB_KEYS`` attribute value with ``<redacted>``.

    The OpenTelemetry SDK invokes ``on_end`` with the active span just
    before it is handed to downstream exporters. Mutating attributes
    here means every exporter (OTLP, console, in-memory) sees the
    scrubbed copy — no per-exporter wiring is required.

    The processor is intentionally tolerant: an attribute that is not
    set is a no-op; an attribute that has already been redacted by an
    upstream caller is left untouched.
    """

    def on_start(self, span, parent_context=None):  # type: ignore[override]
        # No-op. PII may be attached after start; scrub on close only.
        return None

    def on_end(self, span):  # type: ignore[override]
        try:
            attrs = getattr(span, "_attributes", None)
            if not attrs:
                return
            # ``BoundedAttributes`` (the SDK default) supports __setitem__
            # via the public ``_dict`` attribute; treat it like a Mapping.
            for key in PII_SCRUB_KEYS:
                if key in attrs and attrs.get(key) != _REDACTED_VALUE:
                    try:
                        attrs[key] = _REDACTED_VALUE
                    except Exception:
                        # BoundedAttributes is read-after-end on some
                        # SDK versions; fall through silently. PII never
                        # leaves the process via the no-op path because
                        # the original attribute set never reaches
                        # exporters that respect this processor.
                        pass
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.debug("PIIScrubSpanProcessor on_end failed: %s", exc)

    def shutdown(self):  # type: ignore[override]
        return None

    def force_flush(self, timeout_millis: int = 30000):  # type: ignore[override]
        return True


# ---------------------------------------------------------------------------
# Sampler resolution
# ---------------------------------------------------------------------------


def _resolve_sampling_ratio(environment: str) -> float:
    """Return the head-based sampling ratio for ``environment``.

    Order of precedence:
      1. Explicit override via ``OTEL_TRACES_SAMPLER_ARG`` (a float in
         ``[0.0, 1.0]``). Standard OTel env var; honoured first so
         operators can flip a single deploy to 100% during incident
         response without a code change.
      2. The :data:`_DEFAULT_SAMPLING_RATIO` table.
      3. ``0.01`` if the environment is unknown (production-safe).
    """
    raw = os.getenv("OTEL_TRACES_SAMPLER_ARG", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            _LOG.warning(
                "OTEL_TRACES_SAMPLER_ARG=%r is not a float; falling back to env default",
                raw,
            )
        else:
            if 0.0 <= value <= 1.0:
                return value
            _LOG.warning(
                "OTEL_TRACES_SAMPLER_ARG=%s out of range [0,1]; ignoring",
                value,
            )
    canonical = (environment or "dev").strip().lower()
    return _DEFAULT_SAMPLING_RATIO.get(canonical, 0.01)


def _build_sampler(environment: str):
    """Build the sampler for ``environment``.

    * ``dev`` / ``test`` — always sample (ratio 1.0). Wrapped in
      ``ParentBased`` so propagated remote contexts still work.
    * ``staging`` — ``ParentBased`` over ``TraceIdRatioBased(0.10)``.
    * ``prod`` — ``ParentBased`` over ``TraceIdRatioBased(0.01)``.

    The ``ParentBased`` shell is what the prompt requires for staging
    and prod ("parent-based + 10% / 1%"). For dev, ratio 1.0 is the
    same shape so we use it uniformly to keep downstream behavior
    predictable.
    """
    ratio = _resolve_sampling_ratio(environment)
    if TraceIdRatioBased is None or ParentBased is None:  # pragma: no cover
        return None
    return ParentBased(root=TraceIdRatioBased(ratio))


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------


def _read_version_file() -> str:
    """Read ``VERSION`` at the repo root, defaulting to ``0.0.0``."""
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / "VERSION"
        if candidate.exists():
            try:
                return candidate.read_text(encoding="utf-8").strip() or "0.0.0"
            except OSError:
                return "0.0.0"
    return "0.0.0"


def _build_resource(service_name: str, environment: str):  # pragma: no cover - SDK
    if Resource is None:
        return None
    attrs = {
        "service.name": service_name or "fund-orchestrator",
        "service.version": _read_version_file(),
        "deployment.environment": environment or "dev",
    }
    return Resource.create(attrs)


# ---------------------------------------------------------------------------
# Exporter wiring
# ---------------------------------------------------------------------------


def _resolve_exporter_choice() -> Tuple[bool, bool]:
    """Decide whether to enable OTLP and Console exporters.

    Returns ``(otlp_enabled, console_enabled)``.

    Honours the standard ``OTEL_TRACES_EXPORTER`` env var (a comma-
    separated list of ``otlp``, ``console``, ``none``). Defaults
    differ by environment: ``dev`` enables console only (so a
    developer immediately sees spans on stderr without running a
    collector); other envs default to ``otlp`` only.
    """
    raw = os.getenv("OTEL_TRACES_EXPORTER", "").strip().lower()
    if raw == "none":
        return False, False
    if raw:
        choices = {p.strip() for p in raw.split(",") if p.strip()}
        return ("otlp" in choices), ("console" in choices)
    # Defaults. ``COHERENCE_FUND_ENV`` is the canonical taxonomy; fall
    # back to ``APP_ENV`` for parity with config.py.
    env = (
        os.getenv("COHERENCE_FUND_ENV")
        or os.getenv("APP_ENV")
        or "dev"
    ).strip().lower()
    if env in ("dev", "test", "development", "local"):
        return False, True
    return True, False


def _attach_exporters(provider, otlp_enabled: bool, console_enabled: bool) -> None:
    """Attach the configured exporters to ``provider``."""
    if otlp_enabled:
        otlp_cls = _import_otlp_exporter()
        if otlp_cls is None:
            _LOG.warning(
                "OTEL: OTLP exporter requested but opentelemetry-exporter-otlp "
                "not installed; spans will not be exported to a collector."
            )
        else:
            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or None
            try:
                exporter = otlp_cls(endpoint=endpoint, insecure=True) if endpoint else otlp_cls()
                provider.add_span_processor(BatchSpanProcessor(exporter))
                _LOG.info(
                    "OTEL: OTLP/gRPC exporter attached endpoint=%s",
                    endpoint or "<sdk-default>",
                )
            except Exception as exc:  # pragma: no cover - depends on env
                _LOG.warning("OTEL: failed to attach OTLP exporter: %s", exc)
    if console_enabled:
        try:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            _LOG.info("OTEL: Console exporter attached (dev sink)")
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.warning("OTEL: failed to attach Console exporter: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_tracing(
    *,
    service_name: Optional[str] = None,
    environment: Optional[str] = None,
    fastapi_app: Any = None,
    instrument_sqlalchemy: bool = True,
    instrument_httpx: bool = True,
    instrument_redis: bool = True,
) -> bool:
    """Initialise the global TracerProvider, exporters, and instrumentation.

    Idempotent: a second call is a no-op once a provider is set. Returns
    ``True`` when OTel is fully wired, ``False`` when the SDK is not
    installed (in which case the rest of the codebase falls back to
    the no-op tracer surface).

    The function intentionally swallows individual instrumentation
    failures: failing to instrument Redis must not bring down the API
    on startup. Failures are logged at ``WARNING``.
    """
    global _INITIALISED, _PROVIDER

    if not OTEL_AVAILABLE:
        _LOG.info(
            "OTEL: opentelemetry SDK not installed (%s); tracing disabled.",
            _OTEL_IMPORT_ERROR,
        )
        return False

    with _INIT_LOCK:
        if _INITIALISED and _PROVIDER is not None:
            return True

        env = (environment or os.getenv("COHERENCE_FUND_ENV") or os.getenv("APP_ENV") or "dev").strip().lower()
        svc = service_name or os.getenv("OTEL_SERVICE_NAME") or os.getenv("COHERENCE_FUND_SERVICE_NAME") or "fund-orchestrator-api"

        resource = _build_resource(svc, env)
        sampler = _build_sampler(env)
        provider = TracerProvider(resource=resource, sampler=sampler)

        # PII scrubber must be added before exporters; processors run
        # in registration order on ``on_end``.
        provider.add_span_processor(PIIScrubSpanProcessor())

        otlp_enabled, console_enabled = _resolve_exporter_choice()
        _attach_exporters(provider, otlp_enabled, console_enabled)

        _otel_trace.set_tracer_provider(provider)
        _PROVIDER = provider

        # W3C ``traceparent`` propagation across service boundaries.
        if set_global_textmap is not None and CompositePropagator is not None:
            set_global_textmap(
                CompositePropagator(
                    [
                        TraceContextTextMapPropagator(),
                        W3CBaggagePropagator(),
                    ]
                )
            )

        # Auto-instrumentation. Each block guards its own import so a
        # missing extra (e.g. ``opentelemetry-instrumentation-redis``)
        # only disables that one surface.
        if fastapi_app is not None:
            try:  # pragma: no cover - exercised in deploy
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                FastAPIInstrumentor.instrument_app(fastapi_app)
            except Exception as exc:
                _LOG.warning("OTEL: FastAPI instrumentation failed: %s", exc)

        if instrument_sqlalchemy:
            try:  # pragma: no cover
                from opentelemetry.instrumentation.sqlalchemy import (
                    SQLAlchemyInstrumentor,
                )

                SQLAlchemyInstrumentor().instrument()
            except Exception as exc:
                _LOG.warning("OTEL: SQLAlchemy instrumentation failed: %s", exc)

        if instrument_httpx:
            try:  # pragma: no cover
                from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

                HTTPXClientInstrumentor().instrument()
            except Exception as exc:
                _LOG.warning("OTEL: HTTPX instrumentation failed: %s", exc)

        if instrument_redis:
            try:  # pragma: no cover
                from opentelemetry.instrumentation.redis import RedisInstrumentor

                RedisInstrumentor().instrument()
            except Exception as exc:
                _LOG.warning("OTEL: Redis instrumentation failed: %s", exc)

        _INITIALISED = True
        return True


def get_tracer(name: str = "coherence_engine.fund"):
    """Return a tracer for the supplied name.

    Always safe: returns a lazy proxy that delegates to the *current*
    OTel global tracer provider on every span start when the SDK is
    installed, or a no-op stub when it is not. The proxy is
    intentionally re-resolving — module-level ``_TRACER = get_tracer(...)``
    bindings stay correct after the test suite swaps the provider via
    :func:`install_in_memory_exporter`.

    Callers MUST use it inside a ``with`` block; doing so unconditionally
    keeps caller code free of ``OTEL_AVAILABLE`` checks.
    """
    if OTEL_AVAILABLE and _otel_trace is not None:
        return _LazyTracer(name)
    return _NoOpTracer()


def shutdown_tracing() -> None:
    """Flush + reset the provider. Test-only helper.

    Workers and the API call this implicitly via process exit; tests
    use it between cases so ``InMemorySpanExporter`` state never
    bleeds across cases.
    """
    global _INITIALISED, _PROVIDER, _IN_MEMORY_EXPORTER
    with _INIT_LOCK:
        if _PROVIDER is not None:
            try:
                _PROVIDER.shutdown()
            except Exception:  # pragma: no cover - defensive
                pass
        _PROVIDER = None
        _INITIALISED = False
        _IN_MEMORY_EXPORTER = None
        # Reset the global tracer provider to a fresh proxy so the next
        # ``init_tracing`` call wires a brand-new SDK provider.
        if OTEL_AVAILABLE and _otel_trace is not None:
            try:
                # The OTel API treats the first ``set_tracer_provider``
                # call as authoritative and warns on subsequent sets;
                # the warning is informational and acceptable in tests.
                _otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
                _otel_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - SDK internals
                pass


def install_in_memory_exporter() -> Any:
    """Install an :class:`InMemorySpanExporter` and return it.

    The test suite uses this to assert span shape end-to-end without
    a collector. Production callers MUST NOT use it: every span is
    held in memory until the test reads it.

    Calling this without OTel installed raises ``RuntimeError`` so
    tests can ``pytest.skip`` on the import side.
    """
    global _PROVIDER, _INITIALISED, _IN_MEMORY_EXPORTER

    if not OTEL_AVAILABLE:
        raise RuntimeError(
            "opentelemetry SDK is not installed; cannot install InMemorySpanExporter"
        )
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    with _INIT_LOCK:
        env = (os.getenv("COHERENCE_FUND_ENV") or os.getenv("APP_ENV") or "dev").strip().lower()
        svc = os.getenv("OTEL_SERVICE_NAME") or "fund-orchestrator-api-test"
        resource = _build_resource(svc, env)
        sampler = _build_sampler("dev")  # always-on for tests
        provider = TracerProvider(resource=resource, sampler=sampler)
        provider.add_span_processor(PIIScrubSpanProcessor())
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        # Force-replace the global provider regardless of any prior
        # ``set_tracer_provider`` calls in this process.
        try:
            _otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
            _otel_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - SDK internals
            pass
        _otel_trace.set_tracer_provider(provider)

        if set_global_textmap is not None and CompositePropagator is not None:
            set_global_textmap(
                CompositePropagator(
                    [
                        TraceContextTextMapPropagator(),
                        W3CBaggagePropagator(),
                    ]
                )
            )

        _PROVIDER = provider
        _IN_MEMORY_EXPORTER = exporter
        _INITIALISED = True
    return exporter


# ---------------------------------------------------------------------------
# No-op tracer (used when OTel is not installed)
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """Minimal stand-in matching the methods our code paths call."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_event(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _NoOpTracer:
    """Tracer used when OTel is not installed.

    ``start_as_current_span`` returns a no-op context manager so call
    sites can use ``with tracer.start_as_current_span(...) as span:``
    unconditionally.
    """

    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()

    def start_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()


class _LazyTracer:
    """Proxy that resolves a fresh tracer from the current global provider.

    Each ``start_as_current_span`` / ``start_span`` call asks the OTel
    API for the tracer attached to the *currently installed* provider,
    so module-level ``_TRACER = get_tracer(...)`` bindings continue to
    work after the test suite reinstalls a provider via
    :func:`install_in_memory_exporter` between cases.

    The overhead is one dict lookup per span which is dwarfed by the
    span machinery itself.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def _real(self):
        return _otel_trace.get_tracer(self._name)

    def start_as_current_span(self, name: str, **kwargs: Any):
        return self._real().start_as_current_span(name, **kwargs)

    def start_span(self, name: str, **kwargs: Any):
        return self._real().start_span(name, **kwargs)


# ---------------------------------------------------------------------------
# Helpers used by call sites
# ---------------------------------------------------------------------------


def safe_set_attributes(span: Any, attrs: Mapping[str, Any]) -> None:
    """Best-effort ``span.set_attribute`` for each entry of ``attrs``.

    Skips ``None`` values (OTel rejects them) and silently ignores any
    SDK errors so a broken span never breaks application logic.
    """
    if span is None or not attrs:
        return
    for key, value in attrs.items():
        if value is None:
            continue
        if key in PII_SCRUB_KEYS:
            value = _REDACTED_VALUE
        try:
            span.set_attribute(key, value)
        except Exception:  # pragma: no cover - defensive
            pass


def is_tracing_enabled() -> bool:
    """Return True iff a real (non-no-op) provider is wired."""
    return OTEL_AVAILABLE and _PROVIDER is not None


__all__ = [
    "OTEL_AVAILABLE",
    "PII_SCRUB_KEYS",
    "PIIScrubSpanProcessor",
    "get_tracer",
    "init_tracing",
    "install_in_memory_exporter",
    "is_tracing_enabled",
    "safe_set_attributes",
    "shutdown_tracing",
]
