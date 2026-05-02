"""Observability surface for the fund orchestrator.

This package owns OpenTelemetry initialisation, exporter configuration,
and PII scrubbing for spans. The single public entry point for app
startup is :func:`coherence_engine.server.fund.observability.otel.init_tracing`;
every code site that needs to emit a span should call
:func:`coherence_engine.server.fund.observability.otel.get_tracer` and
``tracer.start_as_current_span(...)``.
"""
