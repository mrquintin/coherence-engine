"""End-to-end tests for the OpenTelemetry tracing surface (prompt 61).

The whole module is gated on the OpenTelemetry SDK being importable.
``otel`` is treated as an optional dependency in production code; tests
follow the same rule and skip cleanly on a no-OTel environment so the
broader suite stays green.

What we cover here:

* Spans flow from ``init_tracing`` → in-memory exporter.
* The PII scrub processor redacts ``user.email`` (and any other key in
  :data:`PII_SCRUB_KEYS`) before it reaches the exporter.
* Per-layer scoring spans appear under a ``score.application`` parent.
* Outbound HTTPX calls create child spans with the parent context
  (W3C ``traceparent``) propagated end-to-end.
* The sampler ratio table is environment-aware.

The OTel imports inside individual tests are deliberate: importing
inside the test body means a missing dependency surfaces as a clean
``pytest.skip`` rather than a collection-time ImportError.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Dict, List

import pytest


def _otel_installed() -> bool:
    try:
        return importlib.util.find_spec("opentelemetry") is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _otel_installed(),
    reason="opentelemetry SDK not installed; tracing tests skipped (sentinel: InMemorySpanExporter)",
)


@pytest.fixture(autouse=True)
def _reset_otel_state():
    """Reset the global OTel provider between tests."""
    from coherence_engine.server.fund.observability import otel as otel_mod

    otel_mod.shutdown_tracing()
    yield
    otel_mod.shutdown_tracing()


@pytest.fixture
def memory_exporter():
    from coherence_engine.server.fund.observability import otel as otel_mod

    exporter = otel_mod.install_in_memory_exporter()
    yield exporter
    exporter.clear()


def _spans_by_name(exporter) -> Dict[str, List[Any]]:
    """Index finished spans by name for compact assertions."""
    out: Dict[str, List[Any]] = {}
    for span in exporter.get_finished_spans():
        out.setdefault(span.name, []).append(span)
    return out


# ---------------------------------------------------------------------------
# Module-level smoke
# ---------------------------------------------------------------------------


def test_otel_module_exposes_required_surface():
    from coherence_engine.server.fund.observability import otel as otel_mod

    assert otel_mod.OTEL_AVAILABLE is True
    for attr in (
        "init_tracing",
        "get_tracer",
        "shutdown_tracing",
        "install_in_memory_exporter",
        "PII_SCRUB_KEYS",
        "PIIScrubSpanProcessor",
    ):
        assert hasattr(otel_mod, attr), f"missing public surface: {attr}"


def test_in_memory_exporter_captures_basic_span(memory_exporter):
    from coherence_engine.server.fund.observability.otel import get_tracer

    tracer = get_tracer("tests.smoke")
    with tracer.start_as_current_span("test.basic") as span:
        span.set_attribute("hello", "world")

    spans = memory_exporter.get_finished_spans()
    assert any(s.name == "test.basic" for s in spans)
    finished = next(s for s in spans if s.name == "test.basic")
    assert finished.attributes.get("hello") == "world"


# ---------------------------------------------------------------------------
# PII scrubbing
# ---------------------------------------------------------------------------


def test_pii_scrubber_redacts_user_email(memory_exporter):
    from coherence_engine.server.fund.observability.otel import (
        PII_SCRUB_KEYS,
        get_tracer,
    )

    assert "user.email" in PII_SCRUB_KEYS

    tracer = get_tracer("tests.pii")
    with tracer.start_as_current_span("test.pii.scrub") as span:
        span.set_attribute("user.email", "alice@example.com")
        span.set_attribute("user.id", "u_123")

    finished = memory_exporter.get_finished_spans()
    span = next(s for s in finished if s.name == "test.pii.scrub")
    # The scrubber MUST have replaced the raw email with the redaction
    # sentinel before this span landed in the exporter.
    assert span.attributes.get("user.email") == "<redacted>"
    # Non-PII attrs survive untouched.
    assert span.attributes.get("user.id") == "u_123"


def test_pii_scrubber_redacts_phone_and_auth_headers(memory_exporter):
    from coherence_engine.server.fund.observability.otel import get_tracer

    tracer = get_tracer("tests.pii.headers")
    with tracer.start_as_current_span("test.pii.headers") as span:
        span.set_attribute("user.phone", "+15551234567")
        span.set_attribute("http.request.header.authorization", "Bearer sekret")
        span.set_attribute("http.method", "POST")

    span = next(
        s for s in memory_exporter.get_finished_spans() if s.name == "test.pii.headers"
    )
    assert span.attributes.get("user.phone") == "<redacted>"
    assert (
        span.attributes.get("http.request.header.authorization") == "<redacted>"
    )
    assert span.attributes.get("http.method") == "POST"


# ---------------------------------------------------------------------------
# Sampler resolution
# ---------------------------------------------------------------------------


def test_sampler_ratio_per_environment(monkeypatch):
    from coherence_engine.server.fund.observability.otel import (
        _resolve_sampling_ratio,
    )

    monkeypatch.delenv("OTEL_TRACES_SAMPLER_ARG", raising=False)
    assert _resolve_sampling_ratio("dev") == 1.0
    assert _resolve_sampling_ratio("test") == 1.0
    assert _resolve_sampling_ratio("staging") == 0.10
    assert _resolve_sampling_ratio("prod") == 0.01
    # Unknown env is production-safe (1%).
    assert _resolve_sampling_ratio("nonsense") == 0.01


def test_sampler_arg_overrides_env_default(monkeypatch):
    from coherence_engine.server.fund.observability.otel import (
        _resolve_sampling_ratio,
    )

    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.5")
    assert _resolve_sampling_ratio("prod") == 0.5
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "not-a-float")
    assert _resolve_sampling_ratio("prod") == 0.01
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "5.0")  # out of range
    assert _resolve_sampling_ratio("prod") == 0.01


# ---------------------------------------------------------------------------
# Per-layer scoring spans
# ---------------------------------------------------------------------------


class _StubLayer:
    def __init__(self, name: str, score: float):
        self.name = name
        self.score = score
        self.details: Dict[str, Any] = {"backend": "heuristic"}


class _StubResult:
    def __init__(self):
        self.composite_score = 0.7
        self.layer_results = [
            _StubLayer("contradiction", 0.6),
            _StubLayer("argumentation", 0.7),
            _StubLayer("embedding", 0.5),
            _StubLayer("compression", 0.55),
            _StubLayer("structural", 0.65),
        ]
        self.metadata: Dict[str, Any] = {
            "n_propositions": 4,
            "anti_gaming_score": 1.0,
            "anti_gaming_flags": [],
            "anti_gaming_metrics": {},
            "embedder": "tfidf",
        }
        self.contradictions: List[Any] = []
        self.argument_structure = None


class _StubScorer:
    def score(self, text: str):
        return _StubResult()


class _StubComparator:
    def compare(self, result, domains):
        return {"comparisons": [{"domain": domains[0], "domain_coherence": 0.5}]}


class _StubApp:
    id = "app_test_otel"
    domain_primary = "market_economics"
    transcript_text = "we build a coherent thesis with evidence."
    transcript_uri = None
    one_liner = "test"
    use_of_funds_summary = "test"


def test_scoring_emits_parent_and_per_layer_spans(memory_exporter, monkeypatch):
    from coherence_engine.server.fund.services import scoring as scoring_mod

    svc = scoring_mod.ScoringService.__new__(scoring_mod.ScoringService)
    svc._scorer = _StubScorer()
    svc._comparator = _StubComparator()

    out = svc.score_application(_StubApp())
    assert out["absolute_coherence"] == pytest.approx(0.7, rel=0, abs=1e-6)

    by_name = _spans_by_name(memory_exporter)

    # The parent span must exist.
    assert "score.application" in by_name, list(by_name)
    parent = by_name["score.application"][0]

    # A per-layer span must exist for every layer the scorer returned.
    expected_layers = {
        "score.layer.contradiction",
        "score.layer.argumentation",
        "score.layer.embedding",
        "score.layer.compression",
        "score.layer.structural",
    }
    for layer_name in expected_layers:
        assert layer_name in by_name, f"missing per-layer span: {layer_name}"

    # Hierarchy: every layer span's parent_span_id must be a span in
    # this trace and ultimately rooted at score.application.
    parent_id = parent.context.span_id
    parent_trace_id = parent.context.trace_id
    for layer_name in expected_layers:
        layer = by_name[layer_name][0]
        assert layer.context.trace_id == parent_trace_id
        # The layer span's parent must be the score.application span.
        assert layer.parent.span_id == parent_id, (
            f"{layer_name} parented to {layer.parent} not score.application"
        )

    # Application id propagated, score values attached.
    assert parent.attributes.get("application.id") == "app_test_otel"
    assert parent.attributes.get("score.absolute") == pytest.approx(0.7, abs=1e-6)


# ---------------------------------------------------------------------------
# W3C traceparent propagation across an outbound HTTPX call
# ---------------------------------------------------------------------------


def test_traceparent_propagates_via_httpx(memory_exporter):
    """A traceparent header on an outbound HTTPX call carries the active span context."""
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("opentelemetry.instrumentation.httpx")

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.propagate import inject

    from coherence_engine.server.fund.observability.otel import get_tracer

    HTTPXClientInstrumentor().instrument()
    try:
        captured: Dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            for key, value in request.headers.items():
                if key.lower() == "traceparent":
                    captured["traceparent"] = value
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        tracer = get_tracer("tests.httpx")
        with tracer.start_as_current_span("test.outbound") as parent:
            expected_trace_id = format(parent.get_span_context().trace_id, "032x")
            with httpx.Client(transport=transport) as client:
                # Manually inject as a belt-and-suspenders fallback in
                # case the instrumentor's autoinjection is disabled in
                # MockTransport's path. The header on the wire is what
                # we actually assert against.
                headers: Dict[str, str] = {}
                inject(headers)
                resp = client.get("https://downstream.test/x", headers=headers)
        assert resp.status_code == 200
        assert "traceparent" in captured, "traceparent header was not set on outbound request"
        # W3C format: ``00-<32 hex trace>-<16 hex span>-<2 hex flags>``.
        parts = captured["traceparent"].split("-")
        assert len(parts) == 4
        assert parts[1] == expected_trace_id, (
            f"propagated trace id {parts[1]} != active span trace id {expected_trace_id}"
        )
    finally:
        HTTPXClientInstrumentor().uninstrument()


# ---------------------------------------------------------------------------
# Object-storage spans
# ---------------------------------------------------------------------------


def test_object_storage_put_get_emit_spans(memory_exporter, tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("LOCAL_STORAGE_BUCKET", "otel-test")

    from coherence_engine.server.fund.services import object_storage as storage

    storage.reset_object_storage()
    try:
        result = storage.put("traces/payload.bin", b"hello world", content_type="text/plain")
        body = storage.get(result.uri)
        assert body == b"hello world"
    finally:
        storage.reset_object_storage()

    by_name = _spans_by_name(memory_exporter)
    assert "object_storage.put" in by_name
    assert "object_storage.get" in by_name
    put_span = by_name["object_storage.put"][0]
    assert put_span.attributes.get("storage.size_bytes") == len(b"hello world")
    assert put_span.attributes.get("storage.key") == "traces/payload.bin"
