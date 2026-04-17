"""Environment-configurable alert routing for worker ops telemetry warnings (Alertmanager-style hooks).

Default mode is ``none`` (no network, no extra I/O). When ``warn`` tags are present and routing is
enabled, alerts are delivered per mode: local file, generic webhook, PagerDuty Events API v2,
Opsgenie REST, or Alertmanager-compatible JSON. Misconfigured provider modes fall back to generic
webhook or file when those are set. Failures are swallowed on the worker path so workers keep
running; use :func:`drill_route_worker_ops_alert` or the deploy drill script for verification.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Literal, Optional, Tuple

_LOG = logging.getLogger(__name__)

_ENV_MODE = "COHERENCE_FUND_OPS_ALERT_ROUTER_MODE"
_ENV_FILE_PATH = "COHERENCE_FUND_OPS_ALERT_FILE_PATH"
_ENV_WEBHOOK_URL = "COHERENCE_FUND_OPS_ALERT_WEBHOOK_URL"
_ENV_WEBHOOK_TOKEN = "COHERENCE_FUND_OPS_ALERT_WEBHOOK_TOKEN"
_ENV_WEBHOOK_TIMEOUT = "COHERENCE_FUND_OPS_ALERT_WEBHOOK_TIMEOUT_SECONDS"
_ENV_COOLDOWN = "COHERENCE_FUND_OPS_ALERT_COOLDOWN_SECONDS"
_ENV_DEDUPE = "COHERENCE_FUND_OPS_ALERT_DEDUPE_KEY"
_ENV_SEVERITY_MAP = "COHERENCE_FUND_OPS_ALERT_SEVERITY_MAP"

_ENV_PD_KEY = "COHERENCE_FUND_OPS_ALERT_PAGERDUTY_ROUTING_KEY"
_ENV_PD_URL = "COHERENCE_FUND_OPS_ALERT_PAGERDUTY_EVENTS_URL"
_ENV_OPSGENIE_KEY = "COHERENCE_FUND_OPS_ALERT_OPSGENIE_API_KEY"
_ENV_OPSGENIE_URL = "COHERENCE_FUND_OPS_ALERT_OPSGENIE_API_URL"
_ENV_ALERTMANAGER_URL = "COHERENCE_FUND_OPS_ALERT_ALERTMANAGER_WEBHOOK_URL"

_DEFAULT_PD_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
_DEFAULT_OPSGENIE_URL = "https://api.opsgenie.com/v2/alerts"

RouterMode = Literal["none", "file", "webhook", "pagerduty", "opsgenie", "alertmanager"]

_last_fired_at: Dict[str, float] = {}
_last_lock = threading.Lock()


def _strip_env(name: str) -> str:
    return os.getenv(name, "").strip()


def _float_env(name: str, default: float) -> float:
    raw = _strip_env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = _strip_env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_mode(raw: str) -> RouterMode:
    low = raw.lower()
    if low in ("file", "webhook", "none", "pagerduty", "opsgenie", "alertmanager"):
        return low  # type: ignore[return-value]
    return "none"


def _parse_severity_map(raw: str) -> Dict[str, str]:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in obj.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


@dataclass
class AlertRouterConfig:
    mode: RouterMode = "none"
    file_path: str = ""
    webhook_url: str = ""
    webhook_token: str = ""
    webhook_timeout_seconds: float = 5.0
    cooldown_seconds: int = 300
    dedupe_key: str = "component_tags"
    severity_by_tag: Dict[str, str] = field(default_factory=dict)
    pagerduty_routing_key: str = ""
    pagerduty_events_url: str = _DEFAULT_PD_EVENTS_URL
    opsgenie_api_key: str = ""
    opsgenie_api_url: str = _DEFAULT_OPSGENIE_URL
    alertmanager_webhook_url: str = ""


def load_alert_router_config() -> AlertRouterConfig:
    mode = _parse_mode(_strip_env(_ENV_MODE) or "none")
    severity_raw = os.getenv(_ENV_SEVERITY_MAP, "") or ""
    pd_url = _strip_env(_ENV_PD_URL) or _DEFAULT_PD_EVENTS_URL
    og_url = _strip_env(_ENV_OPSGENIE_URL) or _DEFAULT_OPSGENIE_URL
    return AlertRouterConfig(
        mode=mode,
        file_path=_strip_env(_ENV_FILE_PATH),
        webhook_url=_strip_env(_ENV_WEBHOOK_URL),
        webhook_token=_strip_env(_ENV_WEBHOOK_TOKEN),
        webhook_timeout_seconds=max(0.1, _float_env(_ENV_WEBHOOK_TIMEOUT, 5.0)),
        cooldown_seconds=max(0, _int_env(_ENV_COOLDOWN, 300)),
        dedupe_key=(_strip_env(_ENV_DEDUPE) or "component_tags").lower(),
        severity_by_tag=_parse_severity_map(severity_raw),
        pagerduty_routing_key=_strip_env(_ENV_PD_KEY),
        pagerduty_events_url=pd_url or _DEFAULT_PD_EVENTS_URL,
        opsgenie_api_key=_strip_env(_ENV_OPSGENIE_KEY),
        opsgenie_api_url=og_url or _DEFAULT_OPSGENIE_URL,
        alertmanager_webhook_url=_strip_env(_ENV_ALERTMANAGER_URL),
    )


def _default_severity_for_tag(tag: str) -> str:
    return "warning"


def _severities_for_tags(tags: List[str], cfg: AlertRouterConfig) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for t in tags:
        out[t] = cfg.severity_by_tag.get(t, _default_severity_for_tag(t))
    return out


def _dedupe_key_for_payload(warn_tags: List[str], payload: dict, cfg: AlertRouterConfig) -> str:
    component = str(payload.get("component") or "unknown")
    tags = sorted(warn_tags)
    mode = cfg.dedupe_key
    if mode == "component":
        return component
    if mode == "payload_hash":
        stable = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"{component}:{hash(stable)}"
    # component_tags (default)
    return f"{component}:{'|'.join(tags)}"


def _within_cooldown(key: str, cfg: AlertRouterConfig, now: float) -> bool:
    if cfg.cooldown_seconds <= 0:
        return False
    with _last_lock:
        last = _last_fired_at.get(key)
        return last is not None and (now - last) < cfg.cooldown_seconds


def _record_alert_success(key: str, now: float) -> None:
    with _last_lock:
        _last_fired_at[key] = now


def _append_alert_jsonl(path: str, body: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(body, sort_keys=True, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("\n")


def _post_json(
    url: str,
    body: bytes,
    *,
    timeout: float,
    extra_headers: Optional[Dict[str, str]] = None,
    bearer_token: str = "",
) -> None:
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if bearer_token:
        req.add_header("Authorization", f"Bearer {bearer_token}")
    if extra_headers:
        for hk, hv in extra_headers.items():
            req.add_header(hk, hv)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def _post_webhook(url: str, token: str, timeout: float, body: bytes) -> None:
    _post_json(url, body, timeout=timeout, bearer_token=token)


def _pd_severity_from_envelope(severities: Dict[str, str]) -> str:
    order = ("critical", "error", "warning", "info")
    present = [severities.get(t, "warning") for t in severities] if severities else ["warning"]
    low = [str(s).lower() for s in present]
    for o in order:
        if o in low:
            return o
    return "warning"


def _build_pagerduty_body(envelope: dict, cfg: AlertRouterConfig) -> dict:
    sev = _pd_severity_from_envelope(envelope.get("severities") or {})
    comp = str((envelope.get("payload") or {}).get("component") or "coherence-fund-worker")
    summary = f"[{comp}] worker ops alert: {','.join(envelope.get('warn_tags') or [])}"
    return {
        "routing_key": cfg.pagerduty_routing_key,
        "event_action": "trigger",
        "payload": {
            "summary": summary,
            "severity": sev,
            "source": comp,
            "custom_details": envelope,
        },
    }


def _opsgenie_priority(severities: Dict[str, str]) -> str:
    s = _pd_severity_from_envelope(severities)
    if s == "critical":
        return "P1"
    if s == "error":
        return "P2"
    if s == "info":
        return "P5"
    return "P3"


def _build_opsgenie_body(envelope: dict, cfg: AlertRouterConfig) -> dict:
    comp = str((envelope.get("payload") or {}).get("component") or "worker")
    tags = list(envelope.get("warn_tags") or [])
    return {
        "message": f"Coherence fund ops: {comp} ({','.join(tags)})",
        "alias": str(envelope.get("dedupe_key") or f"coherence-{comp}"),
        "description": json.dumps(envelope, sort_keys=True),
        "tags": ["coherence-fund", "worker-ops", comp],
        "priority": _opsgenie_priority(envelope.get("severities") or {}),
    }


def _rfc3339_utc(ts: float) -> str:
    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_alertmanager_body(envelope: dict, fired_at: float) -> dict:
    comp = str((envelope.get("payload") or {}).get("component") or "unknown")
    name = "CoherenceFundWorkerOps"
    labels = {
        "alertname": name,
        "component": comp,
        "coherence_schema": str(envelope.get("schema") or ""),
    }
    summary = f"Worker ops warning ({','.join(envelope.get('warn_tags') or [])}) on {comp}"
    alert = {
        "status": "firing",
        "labels": labels,
        "annotations": {"summary": summary, "description": json.dumps(envelope, sort_keys=True)},
        "startsAt": _rfc3339_utc(fired_at),
        "endsAt": _rfc3339_utc(fired_at),
    }
    return {
        "version": "4",
        "status": "firing",
        "receiver": "coherence-fund-ops",
        "alerts": [alert],
    }


def build_alert_envelope(
    warn_tags: List[str], payload: dict, cfg: AlertRouterConfig, *, fired_at_unix: float
) -> dict:
    return {
        "schema": "coherence_fund_worker_ops_alert/v1",
        "fired_at_unix": fired_at_unix,
        "warn_tags": list(warn_tags),
        "severities": _severities_for_tags(warn_tags, cfg),
        "dedupe_key": _dedupe_key_for_payload(warn_tags, payload, cfg),
        "payload": payload,
    }


DeliveryChannel = Literal["file", "webhook", "pagerduty", "opsgenie", "alertmanager"]


def _resolve_delivery(
    cfg: AlertRouterConfig, envelope: dict, body_bytes: bytes, fired_at: float
) -> Optional[Tuple[DeliveryChannel, Callable[[], None]]]:
    """Return (channel_name, callable_that_delivers) or None if nothing to do."""

    def do_file() -> None:
        if not cfg.file_path:
            raise ValueError("file_path not set")
        _append_alert_jsonl(cfg.file_path, envelope)

    def do_webhook() -> None:
        if not cfg.webhook_url:
            raise ValueError("webhook_url not set")
        _post_webhook(cfg.webhook_url, cfg.webhook_token, cfg.webhook_timeout_seconds, body_bytes)

    def do_pagerduty() -> None:
        if not cfg.pagerduty_routing_key:
            raise ValueError("pagerduty routing_key not set")
        pd_body = json.dumps(
            _build_pagerduty_body(envelope, cfg), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        _post_json(cfg.pagerduty_events_url, pd_body, timeout=cfg.webhook_timeout_seconds)

    def do_opsgenie() -> None:
        if not cfg.opsgenie_api_key:
            raise ValueError("opsgenie api key not set")
        og_body = json.dumps(
            _build_opsgenie_body(envelope, cfg), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        _post_json(
            cfg.opsgenie_api_url,
            og_body,
            timeout=cfg.webhook_timeout_seconds,
            extra_headers={"Authorization": f"GenieKey {cfg.opsgenie_api_key}"},
        )

    def do_alertmanager() -> None:
        url = cfg.alertmanager_webhook_url or cfg.webhook_url
        if not url:
            raise ValueError("alertmanager webhook url not set")
        am_body = json.dumps(
            _build_alertmanager_body(envelope, fired_at), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        _post_json(url, am_body, timeout=cfg.webhook_timeout_seconds, bearer_token=cfg.webhook_token)

    mode = cfg.mode
    if mode == "none":
        return None
    if mode == "file":
        return ("file", do_file)
    if mode == "webhook":
        return ("webhook", do_webhook)
    if mode == "pagerduty":
        if cfg.pagerduty_routing_key:
            return ("pagerduty", do_pagerduty)
        if cfg.webhook_url:
            return ("webhook", do_webhook)
        if cfg.file_path:
            return ("file", do_file)
        return None
    if mode == "opsgenie":
        if cfg.opsgenie_api_key:
            return ("opsgenie", do_opsgenie)
        if cfg.webhook_url:
            return ("webhook", do_webhook)
        if cfg.file_path:
            return ("file", do_file)
        return None
    if mode == "alertmanager":
        if cfg.alertmanager_webhook_url or cfg.webhook_url:
            return ("alertmanager", do_alertmanager)
        if cfg.file_path:
            return ("file", do_file)
        return None
    return None


def verify_alert_router_config(cfg: AlertRouterConfig | None = None) -> List[str]:
    """Static checks for recurring route verification (no network). Returns issue descriptions."""
    cfg = cfg or load_alert_router_config()
    issues: List[str] = []
    if cfg.mode == "none":
        return issues
    if cfg.mode == "file" and not cfg.file_path:
        issues.append("mode=file but COHERENCE_FUND_OPS_ALERT_FILE_PATH is unset")
    if cfg.mode == "webhook" and not cfg.webhook_url:
        issues.append("mode=webhook but COHERENCE_FUND_OPS_ALERT_WEBHOOK_URL is unset")
    if cfg.mode == "pagerduty":
        if not cfg.pagerduty_routing_key and not cfg.webhook_url and not cfg.file_path:
            issues.append(
                "mode=pagerduty but routing key, webhook URL, and file path are all unset (no fallback)"
            )
        elif not cfg.pagerduty_routing_key and not cfg.webhook_url and cfg.file_path:
            issues.append(
                "mode=pagerduty: routing key unset; alerts will fall back to file only"
            )
    if cfg.mode == "opsgenie":
        if not cfg.opsgenie_api_key and not cfg.webhook_url and not cfg.file_path:
            issues.append(
                "mode=opsgenie but API key, webhook URL, and file path are all unset (no fallback)"
            )
    if cfg.mode == "alertmanager":
        if not (cfg.alertmanager_webhook_url or cfg.webhook_url) and not cfg.file_path:
            issues.append(
                "mode=alertmanager but ALERTMANAGER_WEBHOOK_URL/WEBHOOK_URL and file path unset"
            )
    return issues


@dataclass
class SyntheticRouteResult:
    ok: bool
    detail: str = ""
    channel: str = ""


def _route_once(
    warn_tags: List[str],
    payload: dict,
    cfg: AlertRouterConfig,
    *,
    now: float,
    bypass_cooldown: bool,
    record_dedupe_state: bool,
) -> SyntheticRouteResult:
    if not warn_tags:
        return SyntheticRouteResult(ok=True, detail="no warn tags; nothing routed")
    if cfg.mode == "none":
        return SyntheticRouteResult(ok=True, detail="router mode none")

    dedupe = _dedupe_key_for_payload(warn_tags, payload, cfg)
    if not bypass_cooldown and _within_cooldown(dedupe, cfg, now):
        return SyntheticRouteResult(ok=True, detail="suppressed by cooldown", channel="cooldown")

    envelope = build_alert_envelope(warn_tags, payload, cfg, fired_at_unix=now)
    body_bytes = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")

    resolved = _resolve_delivery(cfg, envelope, body_bytes, now)
    if resolved is None:
        return SyntheticRouteResult(
            ok=False,
            detail="no delivery target resolved for current mode and env (misconfiguration)",
        )
    channel, deliver = resolved
    try:
        deliver()
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return SyntheticRouteResult(ok=False, detail=str(exc), channel=channel)
    except Exception as exc:  # pragma: no cover - defensive
        return SyntheticRouteResult(ok=False, detail=str(exc), channel=channel)

    if record_dedupe_state:
        _record_alert_success(dedupe, now)
    return SyntheticRouteResult(ok=True, detail="delivered", channel=channel)


def route_worker_ops_alert(
    warn_tags: List[str],
    payload: dict,
    cfg: AlertRouterConfig | None = None,
    *,
    bypass_cooldown: bool = False,
    record_dedupe_state: bool = True,
) -> None:
    """Route one ops warning alert according to ``cfg`` (defaults to env). Never raises."""
    try:
        cfg = cfg or load_alert_router_config()
        now = time.time()
        _route_once(
            warn_tags,
            payload,
            cfg,
            now=now,
            bypass_cooldown=bypass_cooldown,
            record_dedupe_state=record_dedupe_state,
        )
    except OSError as exc:
        _LOG.debug("ops alert routing file error: %s", exc, exc_info=True)
    except urllib.error.URLError as exc:
        _LOG.debug("ops alert routing webhook error: %s", exc, exc_info=True)
    except Exception as exc:  # pragma: no cover - defensive
        _LOG.debug("ops alert routing error: %s", exc, exc_info=True)


def drill_route_worker_ops_alert(
    warn_tags: List[str] | None = None,
    payload: dict | None = None,
    cfg: AlertRouterConfig | None = None,
) -> SyntheticRouteResult:
    """Send one synthetic alert for on-call verification; surfaces delivery failures (no swallow)."""
    cfg = cfg or load_alert_router_config()
    tags = list(warn_tags) if warn_tags is not None else ["synthetic_drill"]
    body = payload if payload is not None else {
        "component": "ops_drill",
        "warn": tags,
        "drill": True,
    }
    now = time.time()
    return _route_once(
        tags,
        body,
        cfg,
        now=now,
        bypass_cooldown=True,
        record_dedupe_state=False,
    )


def reset_alert_routing_state_for_tests() -> None:
    """Clear in-process cooldown state (tests only)."""
    with _last_lock:
        _last_fired_at.clear()
