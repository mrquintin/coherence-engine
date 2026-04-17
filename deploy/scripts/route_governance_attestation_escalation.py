#!/usr/bin/env python3
"""Optional post-step router for governance attestation escalation JSON (CI/local).

Reads a machine-readable escalation record (same schema as report_governance_attestation_age
validate-ownership-attestation) and optionally delivers a copy to a per-environment sink.

Commands:
  route (default) — deliver per GOVERNANCE_ESCALATION_SINK / map (exits 0 even on delivery warnings).
  emit-routing-proof — write artifacts/governance_escalation_routing_proof.json (or
    GOVERNANCE_ESCALATION_ROUTING_PROOF_OUT) documenting canary/prod channel resolution from the
    routing map; no network, no escalation file required.
  emit-webhook-delivery-receipt — per canary/prod, build a canonical probe payload, optional
    HMAC-SHA256 receipt (GOVERNANCE_ESCALATION_RECEIPT_HMAC_KEY), dry-run by default; optional
    live POST when GOVERNANCE_ESCALATION_WEBHOOK_VERIFY_LIVE_POST=true and URL is configured.

Safe-by-default: no outbound network unless GOVERNANCE_ESCALATION_SINK=webhook and a webhook
URL is resolved. Malformed map JSON or routing errors are logged; the process exits 0 so CI
does not attribute job failure to this hook (primary attestation errors remain the root cause).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Default canary/prod pairs for recurring routing proofs (explicit, auditable).
_DEFAULT_PROOF_CHANNELS: tuple[tuple[str, str], ...] = (
    ("uncertainty-governance-canary", "canary"),
    ("uncertainty-governance-prod", "prod"),
)


def _truthy(raw: str | None) -> bool:
    s = (raw or "").strip().lower()
    return s in ("1", "true", "yes", "on")


def _norm_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", (s or "").strip()).strip("_").upper()


def _load_json_file(path: Path) -> Any | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _load_routing_map(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    data = _load_json_file(path)
    if not isinstance(data, dict):
        return {}
    return data


def _map_webhook_var(
    mapping: dict[str, Any],
    *,
    github_environment: str,
    promotion_env: str,
) -> str | None:
    """Return name of environment variable holding webhook URL from optional map."""
    by_gh = mapping.get("by_github_environment")
    if isinstance(by_gh, dict) and github_environment:
        entry = by_gh.get(github_environment)
        if isinstance(entry, dict):
            for k in ("webhook_environment_variable", "webhook_env", "webhook_url_env"):
                v = entry.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    by_promo = mapping.get("by_promotion_environment")
    if isinstance(by_promo, dict) and promotion_env:
        entry = by_promo.get(promotion_env.strip().lower())
        if isinstance(entry, dict):
            for k in ("webhook_environment_variable", "webhook_env", "webhook_url_env"):
                v = entry.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    # Flat legacy: routes["uncertainty-governance-canary"] = {"webhook_environment_variable": "..."}
    routes = mapping.get("routes")
    if isinstance(routes, dict):
        for key in (github_environment, promotion_env):
            if not key:
                continue
            entry = routes.get(key)
            if isinstance(entry, dict):
                for kk in ("webhook_environment_variable", "webhook_env", "webhook_url_env"):
                    vv = entry.get(kk)
                    if isinstance(vv, str) and vv.strip():
                        return vv.strip()
    return None


def _webhook_url_configured(
    mapping: dict[str, Any],
    *,
    github_environment: str,
    promotion_env: str,
) -> bool:
    u = _resolve_webhook_url(
        mapping,
        github_environment=github_environment,
        promotion_env=promotion_env,
    )
    return bool(u and u.strip())


def _resolve_webhook_url(
    mapping: dict[str, Any],
    *,
    github_environment: str,
    promotion_env: str,
) -> str | None:
    mapped_var = _map_webhook_var(
        mapping,
        github_environment=github_environment,
        promotion_env=promotion_env,
    )
    if mapped_var:
        u = (os.environ.get(mapped_var) or "").strip()
        if u:
            return u

    direct = (os.environ.get("GOVERNANCE_ESCALATION_WEBHOOK_URL") or "").strip()
    if direct:
        return direct

    pe = _norm_key(promotion_env)
    if pe:
        u = (os.environ.get(f"GOVERNANCE_ESCALATION_WEBHOOK_URL_{pe}") or "").strip()
        if u:
            return u

    ge = _norm_key(github_environment)
    if ge:
        u = (os.environ.get(f"GOVERNANCE_ESCALATION_WEBHOOK_URL_{ge}") or "").strip()
        if u:
            return u

    return None


def _resolve_file_path(
    mapping: dict[str, Any],
    *,
    github_environment: str,
    promotion_env: str,
) -> str | None:
    env_out = (os.environ.get("GOVERNANCE_ESCALATION_FILE_PATH") or "").strip()
    if env_out:
        return env_out

    by_gh = mapping.get("by_github_environment")
    if isinstance(by_gh, dict) and github_environment:
        entry = by_gh.get(github_environment)
        if isinstance(entry, dict):
            fp = entry.get("file_path") or entry.get("path")
            if isinstance(fp, str) and fp.strip():
                return fp.strip()
    by_promo = mapping.get("by_promotion_environment")
    if isinstance(by_promo, dict) and promotion_env:
        entry = by_promo.get(promotion_env.strip().lower())
        if isinstance(entry, dict):
            fp = entry.get("file_path") or entry.get("path")
            if isinstance(fp, str) and fp.strip():
                return fp.strip()
    routes = mapping.get("routes")
    if isinstance(routes, dict):
        for key in (github_environment, promotion_env):
            if not key:
                continue
            entry = routes.get(key)
            if isinstance(entry, dict):
                fp = entry.get("file_path") or entry.get("path")
                if isinstance(fp, str) and fp.strip():
                    return fp.strip()
    return None


def _channel_metadata(
    mapping: dict[str, Any],
    *,
    github_environment: str,
    promotion_env: str,
) -> dict[str, Any | None]:
    """Labels from map entry (if any) for audit trails; does not expose secrets."""
    meta: dict[str, Any | None] = {
        "escalation_channel": None,
    }
    by_gh = mapping.get("by_github_environment")
    if isinstance(by_gh, dict) and github_environment:
        entry = by_gh.get(github_environment)
        if isinstance(entry, dict):
            ch = entry.get("escalation_channel") or entry.get("channel")
            if isinstance(ch, str) and ch.strip():
                meta["escalation_channel"] = ch.strip()
    if meta["escalation_channel"] is None:
        by_promo = mapping.get("by_promotion_environment")
        if isinstance(by_promo, dict) and promotion_env:
            entry = by_promo.get(promotion_env.strip().lower())
            if isinstance(entry, dict):
                ch = entry.get("escalation_channel") or entry.get("channel")
                if isinstance(ch, str) and ch.strip():
                    meta["escalation_channel"] = ch.strip()
    return meta


def build_routing_resolution(
    mapping: dict[str, Any],
    *,
    github_environment: str,
    promotion_env: str,
) -> dict[str, Any]:
    """Auditable resolution for one (github_environment, promotion_environment) pair."""
    wh_var = _map_webhook_var(
        mapping,
        github_environment=github_environment,
        promotion_env=promotion_env,
    )
    fp = _resolve_file_path(
        mapping,
        github_environment=github_environment,
        promotion_env=promotion_env,
    )
    meta = _channel_metadata(
        mapping,
        github_environment=github_environment,
        promotion_env=promotion_env,
    )
    return {
        "github_environment": github_environment or None,
        "promotion_environment": (promotion_env or "").strip().lower() or None,
        "escalation_channel": meta.get("escalation_channel"),
        "webhook_environment_variable": wh_var,
        "webhook_url_configured": _webhook_url_configured(
            mapping,
            github_environment=github_environment,
            promotion_env=promotion_env,
        ),
        "resolved_file_path": fp,
    }


def emit_routing_proof(
    mapping: dict[str, Any],
    *,
    map_path: Path | None,
    out_path: Path,
    channels: tuple[tuple[str, str], ...] | None = None,
) -> None:
    """Write a machine-readable proof of canary/prod (and similar) channel resolution. No I/O except the proof file."""
    pairs = channels if channels is not None else _DEFAULT_PROOF_CHANNELS
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows = [
        build_routing_resolution(
            mapping,
            github_environment=gh,
            promotion_env=promo,
        )
        for gh, promo in pairs
    ]
    proof = {
        "schema_version": 1,
        "record_type": "governance_escalation_routing_proof",
        "generated_at": now,
        "routing_map_path": str(map_path) if map_path else None,
        "channels": rows,
        "notes": (
            "webhook_url_configured is true only when the resolved env var or fallback "
            "GOVERNANCE_ESCALATION_WEBHOOK_URL* is set in the runner environment; URLs are never emitted."
        ),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _canonical_probe_bytes(
    github_environment: str,
    promotion_env: str,
    *,
    generated_at: str,
    run_id: str,
    repository: str,
) -> bytes:
    probe: dict[str, Any] = {
        "schema_version": 1,
        "record_type": "governance_escalation_webhook_delivery_probe",
        "probe_version": 1,
        "github_environment": github_environment,
        "promotion_environment": promotion_env.strip().lower(),
        "generated_at_utc": generated_at,
        "github_run_id": run_id or None,
        "github_repository": repository or None,
    }
    return json.dumps(probe, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _post_webhook_with_status(
    url: str, payload: bytes, *, timeout_s: float
) -> tuple[int | None, str | None]:
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            _ = resp.read()
            code = getattr(resp, "status", None) or resp.getcode()
        return int(code), None
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        return int(e.code), f"HTTPError:{e.reason}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, type(e).__name__ + ":" + str(e)[:500]


def emit_webhook_delivery_receipt(
    mapping: dict[str, Any],
    *,
    map_path: Path | None,
    out_path: Path,
    channels: tuple[tuple[str, str], ...] | None = None,
) -> None:
    """Write signed (optional) webhook delivery receipts for canary/prod probe envelopes."""
    pairs = channels if channels is not None else _DEFAULT_PROOF_CHANNELS
    ref_time = (os.environ.get("GOVERNANCE_ESCALATION_RECEIPT_REFERENCE_TIME_UTC") or "").strip()
    if ref_time:
        gen_at = ref_time
    else:
        gen_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    run_id = (os.environ.get("GITHUB_RUN_ID") or "").strip()
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    hmac_key = (os.environ.get("GOVERNANCE_ESCALATION_RECEIPT_HMAC_KEY") or "").strip()
    key_ok = bool(hmac_key)
    live_post = _truthy(os.environ.get("GOVERNANCE_ESCALATION_WEBHOOK_VERIFY_LIVE_POST"))
    if _truthy(os.environ.get("GOVERNANCE_ESCALATION_WEBHOOK_VERIFY_FORCE_DRY_RUN")):
        live_post = False
    try:
        timeout_s = float(os.environ.get("GOVERNANCE_ESCALATION_WEBHOOK_TIMEOUT_SECONDS") or "15")
    except ValueError:
        timeout_s = 15.0

    rows: list[dict[str, Any]] = []
    for gh, promo in pairs:
        routing = build_routing_resolution(
            mapping,
            github_environment=gh,
            promotion_env=promo,
        )
        body = _canonical_probe_bytes(
            gh, promo, generated_at=gen_at, run_id=run_id, repository=repo
        )
        digest = hashlib.sha256(body).hexdigest()
        receipt_hmac: str | None = None
        if hmac_key:
            receipt_hmac = hmac.new(
                hmac_key.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()

        url = _resolve_webhook_url(
            mapping,
            github_environment=gh,
            promotion_env=promo,
        )
        delivery: dict[str, Any]

        if live_post and url:
            http_code, err = _post_webhook_with_status(url, body, timeout_s=timeout_s)
            ok = err is None and http_code is not None and 200 <= http_code < 300
            delivery = {
                "mode": "live_post",
                "http_status": http_code,
                "error": err,
                "success": ok,
            }
        elif live_post and not url:
            delivery = {
                "mode": "skipped_no_url",
                "http_status": None,
                "error": None,
                "success": False,
            }
        else:
            delivery = {
                "mode": "dry_run",
                "http_status": None,
                "error": None,
                "success": True,
            }

        rows.append(
            {
                "routing": routing,
                "canonical_payload_sha256": digest,
                "receipt_hmac_sha256": receipt_hmac,
                "delivery": delivery,
            }
        )

    doc: dict[str, Any] = {
        "schema_version": 1,
        "record_type": "governance_escalation_webhook_delivery_receipt",
        "generated_at": gen_at,
        "routing_map_path": str(map_path) if map_path else None,
        "hmac_key_configured": key_ok,
        "live_post_requested": _truthy(
            os.environ.get("GOVERNANCE_ESCALATION_WEBHOOK_VERIFY_LIVE_POST")
        ),
        "force_dry_run": _truthy(
            os.environ.get("GOVERNANCE_ESCALATION_WEBHOOK_VERIFY_FORCE_DRY_RUN")
        ),
        "channels": rows,
        "notes": (
            "receipt_hmac_sha256 is HMAC-SHA256(key, canonical UTF-8 probe bytes) when "
            "GOVERNANCE_ESCALATION_RECEIPT_HMAC_KEY is set; probe JSON uses sort_keys and "
            "compact separators. Scheduled runs should keep live_post_requested=false unless "
            "you intentionally enable GOVERNANCE_ESCALATION_WEBHOOK_VERIFY_LIVE_POST."
        ),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sink_from_env() -> str:
    raw = (os.environ.get("GOVERNANCE_ESCALATION_SINK") or "").strip().lower()
    if raw in ("webhook", "file", "noop", ""):
        return raw or "noop"
    return "noop"


def _post_webhook(url: str, payload: bytes, *, timeout_s: float) -> None:
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        _ = resp.read()


def route_escalation(
    escalation: dict[str, Any],
    *,
    sink: str,
    mapping: dict[str, Any],
    github_environment: str,
    promotion_env: str,
    dry_run: bool,
    timeout_s: float,
) -> tuple[bool, str]:
    """Apply sink. Returns (ok, message)."""
    sink_l = sink.strip().lower()
    envelope = {
        "schema_version": 1,
        "sink": sink_l,
        "github_environment": github_environment or None,
        "promotion_environment": promotion_env or None,
        "escalation": escalation,
    }
    body = json.dumps(envelope, sort_keys=True).encode("utf-8")

    if sink_l == "noop":
        return True, "noop (no sink configured or explicit noop)"

    if sink_l == "file":
        out = _resolve_file_path(
            mapping,
            github_environment=github_environment,
            promotion_env=promotion_env,
        )
        if not out:
            return True, "file sink selected but no GOVERNANCE_ESCALATION_FILE_PATH (or map path); skip"
        p = Path(out)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(envelope, sort_keys=True) + "\n")
        except OSError as e:
            return False, f"file sink error: {e}"
        return True, f"appended to {p}"

    if sink_l == "webhook":
        url = _resolve_webhook_url(
            mapping,
            github_environment=github_environment,
            promotion_env=promotion_env,
        )
        if not url:
            return True, "webhook sink selected but no URL resolved; skip"
        if dry_run:
            return True, f"dry-run: would POST {len(body)} bytes to webhook"
        try:
            _post_webhook(url, body, timeout_s=timeout_s)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, f"webhook POST failed: {e}"
        return True, "webhook POST ok"

    return True, f"unknown sink {sink!r} treated as noop"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="route",
        choices=("route", "emit-routing-proof", "emit-webhook-delivery-receipt"),
        help="route | emit-routing-proof | emit-webhook-delivery-receipt",
    )
    parser.add_argument(
        "--escalation-json",
        type=Path,
        default=None,
        help="Path to governance_attestation_escalation.json "
        "(default: GOVERNANCE_ATTESTATION_ESCALATION_IN or artifacts/...)",
    )
    parser.add_argument(
        "--map-json",
        type=Path,
        default=None,
        help="Optional routing map JSON (default: GOVERNANCE_ESCALATION_ROUTING_MAP_JSON path)",
    )
    parser.add_argument(
        "--sink",
        choices=("noop", "webhook", "file", "auto"),
        default="auto",
        help="auto: from GOVERNANCE_ESCALATION_SINK env, else noop",
    )
    parser.add_argument(
        "--github-environment",
        default=None,
        help="Override GITHUB_ENVIRONMENT / GOVERNANCE_GITHUB_ENVIRONMENT",
    )
    parser.add_argument(
        "--promotion-environment",
        default=None,
        help="Override GOVERNANCE_PROMOTION_ENV (e.g. canary, prod)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=15.0,
        help="Webhook timeout (default 15)",
    )
    parser.add_argument(
        "--routing-proof-out",
        type=Path,
        default=None,
        help="With emit-routing-proof: output JSON path "
        "(default: GOVERNANCE_ESCALATION_ROUTING_PROOF_OUT or artifacts/governance_escalation_routing_proof.json)",
    )
    parser.add_argument(
        "--webhook-receipt-out",
        type=Path,
        default=None,
        help="With emit-webhook-delivery-receipt: output JSON path "
        "(default: GOVERNANCE_ESCALATION_WEBHOOK_RECEIPT_OUT or "
        "artifacts/governance_escalation_webhook_delivery_receipt.json)",
    )
    args = parser.parse_args(argv)

    if args.command == "emit-routing-proof":
        map_path = args.map_json
        if map_path is None:
            mp = (os.environ.get("GOVERNANCE_ESCALATION_ROUTING_MAP_JSON") or "").strip()
            map_path = Path(mp) if mp else None
        if map_path is None or not map_path.is_file():
            print(
                "::error::emit-routing-proof requires --map-json or GOVERNANCE_ESCALATION_ROUTING_MAP_JSON "
                f"pointing to an existing file (got {map_path!r})",
                file=sys.stderr,
            )
            return 1
        mapping = _load_routing_map(map_path)
        out = args.routing_proof_out
        if out is None:
            raw = (os.environ.get("GOVERNANCE_ESCALATION_ROUTING_PROOF_OUT") or "").strip()
            out = Path(raw) if raw else Path("artifacts/governance_escalation_routing_proof.json")
        emit_routing_proof(mapping, map_path=map_path, out_path=out)
        print(f"route_governance_attestation_escalation: wrote routing proof to {out}", file=sys.stderr)
        return 0

    if args.command == "emit-webhook-delivery-receipt":
        map_path = args.map_json
        if map_path is None:
            mp = (os.environ.get("GOVERNANCE_ESCALATION_ROUTING_MAP_JSON") or "").strip()
            map_path = Path(mp) if mp else None
        if map_path is None or not map_path.is_file():
            print(
                "::error::emit-webhook-delivery-receipt requires --map-json or "
                "GOVERNANCE_ESCALATION_ROUTING_MAP_JSON pointing to an existing file "
                f"(got {map_path!r})",
                file=sys.stderr,
            )
            return 1
        mapping = _load_routing_map(map_path)
        out = args.webhook_receipt_out
        if out is None:
            raw = (os.environ.get("GOVERNANCE_ESCALATION_WEBHOOK_RECEIPT_OUT") or "").strip()
            out = (
                Path(raw)
                if raw
                else Path("artifacts/governance_escalation_webhook_delivery_receipt.json")
            )
        emit_webhook_delivery_receipt(mapping, map_path=map_path, out_path=out)
        print(
            f"route_governance_attestation_escalation: wrote webhook delivery receipt to {out}",
            file=sys.stderr,
        )
        return 0

    esc_path = args.escalation_json
    if esc_path is None:
        raw = (os.environ.get("GOVERNANCE_ATTESTATION_ESCALATION_IN") or "").strip()
        if raw:
            esc_path = Path(raw)
        else:
            esc_path = Path("artifacts/governance_attestation_escalation.json")

    if not esc_path.is_file():
        print(f"route_governance_attestation_escalation: no file at {esc_path}; skip", file=sys.stderr)
        return 0

    data = _load_json_file(esc_path)
    if not isinstance(data, dict):
        print(
            f"::warning::route_governance_attestation_escalation: invalid JSON at {esc_path}; skip",
            file=sys.stderr,
        )
        return 0

    if (data.get("record_type") or "") != "governance_attestation_escalation":
        print(
            "::warning::route_governance_attestation_escalation: unexpected record_type; routing anyway",
            file=sys.stderr,
        )

    ctx = data.get("promotion_context")
    map_path = args.map_json
    if map_path is None:
        mp = (os.environ.get("GOVERNANCE_ESCALATION_ROUTING_MAP_JSON") or "").strip()
        map_path = Path(mp) if mp else None

    mapping = _load_routing_map(map_path) if map_path else {}

    sink = args.sink
    if sink == "auto":
        sink = _sink_from_env()
        if sink == "noop" and (os.environ.get("GOVERNANCE_ESCALATION_FILE_PATH") or "").strip():
            sink = "file"

    gh_env = (args.github_environment or os.environ.get("GOVERNANCE_GITHUB_ENVIRONMENT") or "").strip()
    if not gh_env:
        gh_env = (os.environ.get("GITHUB_ENVIRONMENT") or "").strip()

    promo = (args.promotion_environment or os.environ.get("GOVERNANCE_PROMOTION_ENV") or "").strip()

    if isinstance(ctx, dict):
        c_gh = ctx.get("github_environment")
        if isinstance(c_gh, str) and c_gh.strip() and not gh_env:
            gh_env = c_gh.strip()
        c_pr = ctx.get("promotion_environment")
        if isinstance(c_pr, str) and c_pr.strip() and not promo:
            promo = c_pr.strip()

    dry_run = _truthy(os.environ.get("GOVERNANCE_ESCALATION_DRY_RUN"))
    try:
        timeout_s = float(os.environ.get("GOVERNANCE_ESCALATION_WEBHOOK_TIMEOUT_SECONDS") or args.timeout_seconds)
    except ValueError:
        timeout_s = args.timeout_seconds

    ok, msg = route_escalation(
        data,
        sink=sink,
        mapping=mapping,
        github_environment=gh_env,
        promotion_env=promo,
        dry_run=dry_run,
        timeout_s=timeout_s,
    )
    log_line = f"route_governance_attestation_escalation: {msg}"
    if ok:
        print(log_line, file=sys.stderr)
    else:
        print(f"::warning::{log_line}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
