"""Centralized worker ops telemetry: log (default) plus optional file and Prometheus textfile sinks.

The worker-ops surface (``emit_worker_ops_snapshot``) emits periodic
scoring-worker and outbox-dispatcher snapshots. Prompt 18 of 20 extends
this module with a per-stage pipeline telemetry surface
(``record_stage``) covering the nine workflow-orchestrator stages
introduced in prompt 15: ``intake``, ``transcript_quality``,
``compile``, ``ontology``, ``domain_mix``, ``score``, ``decide``,
``artifact``, ``notify``.

Design goals for the new surface:

* Zero required configuration — default is a structured log line per
  stage transition, no file or network side effects.
* Two optional sinks, each activated by a separate env var so the
  worker-ops sinks stay untouched:
    * JSONL append file (one line per stage event), gated by
      ``COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH``.
    * Prometheus textfile snapshot (cumulative counters + last
      observed duration), gated by
      ``COHERENCE_FUND_PIPELINE_TELEMETRY_PROMETHEUS_TEXTFILE_PATH``.
* No OpenTelemetry / APM SDK (prompt 18 prohibition).
* No change to worker-ops semantics — prompt 18 additions live
  entirely in this module's new ``record_stage`` path plus a fresh
  in-process counter dict.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from typing import Any, Dict, List, Mapping, Optional, Tuple

from coherence_engine.server.fund.services.alert_routing import (
    route_worker_ops_alert,
    verify_alert_router_config,
)

WORKER_OPS_SNAPSHOT_MARKER = "COHERENCE_FUND_WORKER_OPS_SNAPSHOT"
OPS_ALERT_ROUTE_VERIFICATION_MARKER = "COHERENCE_FUND_OPS_ALERT_ROUTE_VERIFICATION"

# Prompt 18 — pipeline stage telemetry marker. Always emitted on
# ``record_stage``; operators can grep on this to collect per-stage
# latency / outcome history without parsing Prometheus text.
PIPELINE_STAGE_EVENT_MARKER = "COHERENCE_FUND_PIPELINE_STAGE_EVENT"

_ENV_FILE_PATH = "COHERENCE_FUND_OPS_TELEMETRY_FILE_PATH"
_ENV_PROM_PATH = "COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH"

# New env vars for the pipeline stage sinks (prompt 18). Kept disjoint
# from the worker-ops env vars so a deployment can opt into either
# surface independently.
_ENV_PIPELINE_FILE_PATH = "COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH"
_ENV_PIPELINE_PROM_PATH = (
    "COHERENCE_FUND_PIPELINE_TELEMETRY_PROMETHEUS_TEXTFILE_PATH"
)

# Warn threshold env var — when set to a positive value, any stage
# whose duration exceeds this many seconds gets ``warn=duration_budget``
# added to the emitted event. Safe default is ``0`` (disabled) to match
# the worker-ops warn-env pattern.
_ENV_STAGE_DURATION_WARN_SECONDS = (
    "COHERENCE_FUND_PIPELINE_STAGE_DURATION_WARN_SECONDS"
)

_VALID_STAGE_STATUSES = ("success", "failure", "skipped")


# ---------------------------------------------------------------------------
# Pipeline stage telemetry — prompt 18
# ---------------------------------------------------------------------------


# In-process counter + last-observation state. Threaded workflows may
# record concurrently; the lock keeps the counter + textfile writes
# consistent.
_STAGE_COUNTERS_LOCK = threading.Lock()
_STAGE_COUNTERS: Dict[Tuple[str, str], int] = {}
_STAGE_LAST_DURATION_S: Dict[str, float] = {}
_STAGE_TOTAL_DURATION_S: Dict[str, float] = {}
_STAGE_WARN_COUNTERS: Dict[str, int] = {}

_STAGE_TELEMETRY_LOGGER = logging.getLogger(
    "coherence_engine.fund.pipeline_telemetry"
)


def _float_metric(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _append_jsonl_line(path: str, line: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        if not line.endswith("\n"):
            fh.write("\n")


def _atomic_write_text(path: str, body: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".prom_", dir=parent or ".", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            if not body.endswith("\n"):
                fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prometheus_text_from_payload(payload: dict) -> str:
    """Render Prometheus text exposition for one ops snapshot (for tests and textfile sink)."""
    component = str(payload.get("component") or "")
    tick = payload.get("tick") or {}
    chunks: List[str] = []

    def add_gauge(name: str, help_text: str, value: float) -> None:
        chunks.append(f"# HELP {name} {help_text}")
        chunks.append(f"# TYPE {name} gauge")
        chunks.append(f"{name} {value}")

    if component == "scoring":
        add_gauge(
            "coherence_fund_scoring_eligible_queue_depth",
            "Scoring jobs currently eligible for worker claim.",
            _float_metric(payload.get("eligible_queue_depth")),
        )
        add_gauge(
            "coherence_fund_scoring_oldest_eligible_age_seconds",
            "Age in seconds of oldest eligible scoring job (0 if none).",
            _float_metric(payload.get("oldest_eligible_age_seconds")),
        )
        add_gauge(
            "coherence_fund_scoring_failed_dlq",
            "Scoring jobs in terminal failed state.",
            _float_metric(payload.get("failed_dlq")),
        )
        add_gauge(
            "coherence_fund_scoring_processing_in_flight",
            "Scoring jobs leased and in processing.",
            _float_metric(payload.get("processing_in_flight")),
        )
        add_gauge(
            "coherence_fund_scoring_tick_processed",
            "Jobs processed in last worker tick.",
            _float_metric(tick.get("processed")),
        )
        add_gauge(
            "coherence_fund_scoring_tick_failed",
            "Jobs failed or retried in last worker tick.",
            _float_metric(tick.get("failed")),
        )
        add_gauge(
            "coherence_fund_scoring_tick_idle",
            "Idle iterations recorded in last tick.",
            _float_metric(tick.get("idle")),
        )
    elif component == "outbox":
        add_gauge(
            "coherence_fund_outbox_pending_dispatchable",
            "Outbox rows ready to dispatch.",
            _float_metric(payload.get("pending_dispatchable")),
        )
        add_gauge(
            "coherence_fund_outbox_oldest_pending_age_seconds",
            "Age in seconds of oldest dispatchable outbox row (0 if none).",
            _float_metric(payload.get("oldest_pending_age_seconds")),
        )
        add_gauge(
            "coherence_fund_outbox_failed_dlq",
            "Outbox rows in terminal failed state.",
            _float_metric(payload.get("failed_dlq")),
        )
        add_gauge(
            "coherence_fund_outbox_tick_published",
            "Events published in last dispatcher tick.",
            _float_metric(tick.get("published")),
        )
        add_gauge(
            "coherence_fund_outbox_tick_failed",
            "Events failed in last dispatcher tick.",
            _float_metric(tick.get("failed")),
        )
        add_gauge(
            "coherence_fund_outbox_tick_scanned",
            "Events scanned in last dispatcher tick.",
            _float_metric(tick.get("scanned")),
        )

    warn = payload.get("warn") or []
    wl = warn if isinstance(warn, list) else []
    if component:
        chunks.append("# HELP coherence_fund_worker_ops_warn_queue_depth Queue depth threshold warning active (1=yes).")
        chunks.append("# TYPE coherence_fund_worker_ops_warn_queue_depth gauge")
        chunks.append(
            f'coherence_fund_worker_ops_warn_queue_depth{{component="{component}"}} '
            f'{1.0 if "queue_depth" in wl else 0.0}'
        )
        chunks.append("# HELP coherence_fund_worker_ops_warn_oldest_latency Oldest-age threshold warning (1=yes).")
        chunks.append("# TYPE coherence_fund_worker_ops_warn_oldest_latency gauge")
        chunks.append(
            f'coherence_fund_worker_ops_warn_oldest_latency{{component="{component}"}} '
            f'{1.0 if "oldest_latency" in wl else 0.0}'
        )
        chunks.append("# HELP coherence_fund_worker_ops_warn_failed_dlq Failed DLQ threshold warning (1=yes).")
        chunks.append("# TYPE coherence_fund_worker_ops_warn_failed_dlq gauge")
        chunks.append(
            f'coherence_fund_worker_ops_warn_failed_dlq{{component="{component}"}} '
            f'{1.0 if "failed_dlq" in wl else 0.0}'
        )

    return "\n".join(chunks) + ("\n" if chunks else "")


def _maybe_file_sink(line: str) -> None:
    path = os.getenv(_ENV_FILE_PATH, "").strip()
    if not path:
        return
    _append_jsonl_line(path, line)


def _maybe_prometheus_sink(payload: dict) -> None:
    path = os.getenv(_ENV_PROM_PATH, "").strip()
    if not path:
        return
    body = prometheus_text_from_payload(payload)
    if body.strip():
        _atomic_write_text(path, body)


def emit_worker_ops_snapshot(logger: logging.Logger, *, warn_tags: List[str], payload: dict) -> None:
    """Emit one ops snapshot: always to logs (marker + JSON line), optionally to file and Prometheus textfile."""
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if warn_tags:
        logger.warning("%s %s", WORKER_OPS_SNAPSHOT_MARKER, line)
    else:
        logger.info("%s %s", WORKER_OPS_SNAPSHOT_MARKER, line)
    _maybe_file_sink(line)
    _maybe_prometheus_sink(payload)
    if warn_tags:
        route_worker_ops_alert(warn_tags, payload)


def verify_worker_ops_alert_routing() -> List[str]:
    """Return static alert-router config issues for cron or health checks (no network I/O)."""
    return verify_alert_router_config()


def log_worker_ops_alert_route_verification(logger: logging.Logger) -> List[str]:
    """Log verification results; returns the same issue list for metrics or exit-code wrappers."""
    issues = verify_worker_ops_alert_routing()
    if issues:
        logger.warning("%s %s", OPS_ALERT_ROUTE_VERIFICATION_MARKER, json.dumps(issues))
    else:
        logger.info("%s ok", OPS_ALERT_ROUTE_VERIFICATION_MARKER)
    return issues


# ---------------------------------------------------------------------------
# Pipeline stage telemetry — prompt 18
#
# ``record_stage`` is the single entry point the workflow orchestrator
# uses to publish per-stage telemetry. It writes:
#
#  * One structured log line (always).
#  * One JSONL append line, if ``COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH``
#    is set.
#  * One Prometheus textfile snapshot overwritten atomically, if
#    ``COHERENCE_FUND_PIPELINE_TELEMETRY_PROMETHEUS_TEXTFILE_PATH`` is
#    set. Counters + last-observed duration gauges follow the
#    ``coherence_fund_pipeline_*`` namespace and match the SLO doc in
#    ``docs/ops/slo_metrics.md``.
#
# In-process state (``_STAGE_COUNTERS`` etc.) is module-scoped so
# short-lived CLI invocations naturally start with empty counters.
# Long-running workers accumulate. Tests should call
# :func:`reset_pipeline_stage_counters` in a fixture teardown to keep
# assertions stable across parameterised cases.
# ---------------------------------------------------------------------------


def _stage_duration_warn_seconds() -> float:
    """Return the configured per-stage duration warn threshold in seconds.

    A value of ``0`` (the default) disables the warn tag. Non-numeric
    values fall back to ``0``; this matches the worker-ops pattern
    (``_OPS_*_WARN_*`` env vars) so operators have one mental model.
    """
    raw = os.getenv(_ENV_STAGE_DURATION_WARN_SECONDS, "").strip()
    if not raw:
        return 0.0
    try:
        val = float(raw)
    except ValueError:
        return 0.0
    return val if val > 0 else 0.0


def _normalise_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s not in _VALID_STAGE_STATUSES:
        return "failure" if s.startswith("fail") else "success" if s.startswith("succ") else s or "failure"
    return s


def _escape_prom_label(value: str) -> str:
    """Escape a Prometheus label value per the exposition format."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _maybe_pipeline_file_sink(line: str) -> None:
    path = os.getenv(_ENV_PIPELINE_FILE_PATH, "").strip()
    if not path:
        return
    _append_jsonl_line(path, line)


def _render_pipeline_prometheus_text() -> str:
    """Render the cumulative in-process counters as Prometheus text."""
    chunks: List[str] = []
    chunks.append(
        "# HELP coherence_fund_pipeline_stage_events_total "
        "Count of workflow orchestrator stage events by stage and status."
    )
    chunks.append("# TYPE coherence_fund_pipeline_stage_events_total counter")
    with _STAGE_COUNTERS_LOCK:
        counters_snapshot = dict(_STAGE_COUNTERS)
        last_duration_snapshot = dict(_STAGE_LAST_DURATION_S)
        total_duration_snapshot = dict(_STAGE_TOTAL_DURATION_S)
        warn_snapshot = dict(_STAGE_WARN_COUNTERS)
    for (stage, status), count in sorted(counters_snapshot.items()):
        chunks.append(
            'coherence_fund_pipeline_stage_events_total'
            f'{{stage="{_escape_prom_label(stage)}",status="{_escape_prom_label(status)}"}} {count}'
        )

    chunks.append(
        "# HELP coherence_fund_pipeline_stage_last_duration_seconds "
        "Duration in seconds of the most recent stage invocation."
    )
    chunks.append(
        "# TYPE coherence_fund_pipeline_stage_last_duration_seconds gauge"
    )
    for stage, duration in sorted(last_duration_snapshot.items()):
        safe = 0.0 if duration is None or math.isnan(duration) else float(duration)
        chunks.append(
            'coherence_fund_pipeline_stage_last_duration_seconds'
            f'{{stage="{_escape_prom_label(stage)}"}} {safe}'
        )

    chunks.append(
        "# HELP coherence_fund_pipeline_stage_duration_seconds_sum "
        "Cumulative wall-clock duration for a stage across all invocations."
    )
    chunks.append(
        "# TYPE coherence_fund_pipeline_stage_duration_seconds_sum counter"
    )
    for stage, total in sorted(total_duration_snapshot.items()):
        safe = 0.0 if total is None or math.isnan(total) else float(total)
        chunks.append(
            'coherence_fund_pipeline_stage_duration_seconds_sum'
            f'{{stage="{_escape_prom_label(stage)}"}} {safe}'
        )

    chunks.append(
        "# HELP coherence_fund_pipeline_stage_warn_total "
        "Count of stage invocations that exceeded the duration warn threshold."
    )
    chunks.append("# TYPE coherence_fund_pipeline_stage_warn_total counter")
    for stage, count in sorted(warn_snapshot.items()):
        chunks.append(
            'coherence_fund_pipeline_stage_warn_total'
            f'{{stage="{_escape_prom_label(stage)}"}} {count}'
        )

    return "\n".join(chunks) + "\n"


def _maybe_pipeline_prometheus_sink() -> None:
    path = os.getenv(_ENV_PIPELINE_PROM_PATH, "").strip()
    if not path:
        return
    _atomic_write_text(path, _render_pipeline_prometheus_text())


def record_stage(
    name: str,
    duration_s: float,
    status: str,
    extra: Optional[Mapping[str, Any]] = None,
    *,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Record one workflow orchestrator stage event (prompt 18).

    Args:
        name: Stage identifier from ``workflow.STEP_NAMES``
            (``intake | transcript_quality | compile | ontology |
            domain_mix | score | decide | artifact | notify``).
            Unknown names are accepted to avoid a hard-coupling on
            the caller but are still tagged and counted.
        duration_s: Wall-clock duration spent in the stage. Negative
            values are clamped to ``0`` for metric sanity; the
            original value is still echoed in the event payload so
            operators can spot clock skew.
        status: One of ``success``, ``failure``, or ``skipped``.
            Other strings are coerced via a best-effort prefix match.
        extra: Optional caller-provided context (e.g. ``application_id``,
            ``error_type``). Must be JSON-serializable; otherwise the
            entry is dropped from the event with a ``serialization_error``
            tag.
        logger: Optional logger override. Defaults to the module-level
            ``coherence_engine.fund.pipeline_telemetry`` logger.

    Returns:
        The JSON-serializable event payload (for tests and callers that
        want to chain additional side effects).

    Side effects (always safe, no network I/O on the default path):
        1. Emits one structured log line tagged with
           ``PIPELINE_STAGE_EVENT_MARKER``.
        2. Updates in-process counters
           (``_STAGE_COUNTERS``, ``_STAGE_LAST_DURATION_S``,
           ``_STAGE_TOTAL_DURATION_S``, ``_STAGE_WARN_COUNTERS``).
        3. Appends a JSONL line to
           ``COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH`` when set.
        4. Overwrites the Prometheus textfile at
           ``COHERENCE_FUND_PIPELINE_TELEMETRY_PROMETHEUS_TEXTFILE_PATH``
           when set.
    """
    stage = str(name or "unknown").strip() or "unknown"
    normalised_status = _normalise_status(status)

    try:
        raw_duration = float(duration_s)
    except (TypeError, ValueError):
        raw_duration = 0.0
    if raw_duration < 0 or math.isnan(raw_duration):
        safe_duration = 0.0
    else:
        safe_duration = raw_duration

    safe_extra: Dict[str, Any] = {}
    if extra:
        try:
            json.dumps(dict(extra), sort_keys=True, default=str)
            safe_extra = {str(k): v for k, v in dict(extra).items()}
        except (TypeError, ValueError):
            safe_extra = {"serialization_error": True}

    warn_tags: List[str] = []
    warn_threshold = _stage_duration_warn_seconds()
    if warn_threshold > 0 and safe_duration > warn_threshold:
        warn_tags.append("duration_budget")

    payload: Dict[str, Any] = {
        "marker": PIPELINE_STAGE_EVENT_MARKER,
        "stage": stage,
        "status": normalised_status,
        "duration_s": round(safe_duration, 6),
        "warn": warn_tags,
    }
    if raw_duration != safe_duration:
        payload["duration_s_raw"] = raw_duration
    if safe_extra:
        payload["extra"] = safe_extra

    with _STAGE_COUNTERS_LOCK:
        key = (stage, normalised_status)
        _STAGE_COUNTERS[key] = _STAGE_COUNTERS.get(key, 0) + 1
        _STAGE_LAST_DURATION_S[stage] = safe_duration
        _STAGE_TOTAL_DURATION_S[stage] = (
            _STAGE_TOTAL_DURATION_S.get(stage, 0.0) + safe_duration
        )
        if warn_tags:
            _STAGE_WARN_COUNTERS[stage] = _STAGE_WARN_COUNTERS.get(stage, 0) + 1

    line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    log = logger or _STAGE_TELEMETRY_LOGGER
    if normalised_status == "failure" or warn_tags:
        log.warning("%s %s", PIPELINE_STAGE_EVENT_MARKER, line)
    else:
        log.info("%s %s", PIPELINE_STAGE_EVENT_MARKER, line)

    _maybe_pipeline_file_sink(line)
    _maybe_pipeline_prometheus_sink()

    return payload


def reset_pipeline_stage_counters() -> None:
    """Clear in-process pipeline stage counters (test helper).

    Not for production use — only the pipeline-telemetry test module
    (``tests/test_pipeline_telemetry.py``) and any local diagnostics
    should call this. Workers are expected to leave counters running
    for the entire process lifetime so Prometheus rate queries work.
    """
    with _STAGE_COUNTERS_LOCK:
        _STAGE_COUNTERS.clear()
        _STAGE_LAST_DURATION_S.clear()
        _STAGE_TOTAL_DURATION_S.clear()
        _STAGE_WARN_COUNTERS.clear()


def get_pipeline_stage_counters_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return a read-only snapshot of the in-process stage counters.

    Used by tests and internal diagnostics. The Prometheus textfile
    path is the preferred surface for operators; this helper exists
    so tests can introspect without parsing the textfile.
    """
    with _STAGE_COUNTERS_LOCK:
        by_stage: Dict[str, Dict[str, Any]] = {}
        for (stage, status), count in _STAGE_COUNTERS.items():
            entry = by_stage.setdefault(
                stage,
                {
                    "events": {},
                    "last_duration_s": 0.0,
                    "total_duration_s": 0.0,
                    "warn_count": 0,
                },
            )
            entry["events"][status] = count
        for stage, duration in _STAGE_LAST_DURATION_S.items():
            entry = by_stage.setdefault(
                stage,
                {
                    "events": {},
                    "last_duration_s": 0.0,
                    "total_duration_s": 0.0,
                    "warn_count": 0,
                },
            )
            entry["last_duration_s"] = duration
        for stage, total in _STAGE_TOTAL_DURATION_S.items():
            entry = by_stage.setdefault(
                stage,
                {
                    "events": {},
                    "last_duration_s": 0.0,
                    "total_duration_s": 0.0,
                    "warn_count": 0,
                },
            )
            entry["total_duration_s"] = total
        for stage, count in _STAGE_WARN_COUNTERS.items():
            entry = by_stage.setdefault(
                stage,
                {
                    "events": {},
                    "last_duration_s": 0.0,
                    "total_duration_s": 0.0,
                    "warn_count": 0,
                },
            )
            entry["warn_count"] = count
    return by_stage
