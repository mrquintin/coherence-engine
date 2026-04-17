#!/usr/bin/env python3
"""Tracker handoff governance: contract validation, optional policy overlays, bounded retry/idempotency.

No third-party deps. Use ``ci-check`` in scheduled CI (deterministic). Use ``run`` in GitHub Actions
optional-tracker-handoff with repository secrets/vars for policy (JSON secret or path in repo).

Policy overlay precedence: ``ONCALL_TRACKER_HANDOFF_POLICY_JSON`` (non-empty) >
``ONCALL_TRACKER_HANDOFF_POLICY_PATH`` (repo-relative path) > built-in defaults.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

RESULTS_SCHEMA = "oncall_tracker_handoff_results/v2"
POLICY_SCHEMA = "oncall_tracker_handoff_policy/v1"
RECONCILIATION_SCHEMA = "oncall_tracker_handoff_reconciliation/v1"

# Cap response bytes read for reconciliation parsing (avoid huge bodies in memory / artifacts).
MAX_RECONCILIATION_RESPONSE_BYTES = 65536

# Hard bounds so overlays cannot request unbounded retry storms.
MIN_ATTEMPTS = 1
MAX_ATTEMPTS_CAP = 10
MIN_BACKOFF_INITIAL = 0.1
MAX_BACKOFF_INITIAL = 60.0
MIN_BACKOFF_MAX = 1.0
MAX_BACKOFF_MAX_CAP = 120.0
ALLOWED_RETRY_STATUS_MIN = 400
ALLOWED_RETRY_STATUS_MAX = 599

DEFAULT_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})
IDEMPOTENCY_MODES = frozenset({"run_env_payload", "off"})

BUILTIN_DEFAULTS: dict[str, Any] = {
    "max_attempts": 4,
    "retryable_http_statuses": sorted(DEFAULT_RETRYABLE),
    "backoff_initial_seconds": 1.0,
    "backoff_max_seconds": 8.0,
    "idempotency_mode": "run_env_payload",
}

TICKET_TEMPLATE_SCHEMA = "oncall_live_drill_ticket_template/v1"


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def load_policy_doc(
    policy_json_env: str | None,
    policy_path: str | None,
    repo_root: Path,
) -> tuple[dict[str, Any] | None, str]:
    """Returns (doc_or_none, source_label)."""
    raw = (policy_json_env or "").strip()
    if raw:
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"ONCALL_TRACKER_HANDOFF_POLICY_JSON is not valid JSON: {e}") from e
        return doc, "secret_json"

    p = (policy_path or "").strip()
    if p:
        path = (repo_root / p).resolve()
        try:
            path.relative_to(repo_root.resolve())
        except ValueError:
            raise SystemExit(f"Policy path escapes repository root: {p}") from None
        if not path.is_file():
            raise SystemExit(f"Policy file not found: {path}")
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"Policy file is not valid JSON ({path}): {e}") from e
        return doc, f"file:{p}"

    return None, "builtin_defaults"


def builtin_effective_policy() -> dict[str, Any]:
    """Effective policy when no overlay document is loaded (clamped built-ins only)."""
    return clamp_effective_policy({})


def policy_drift_vs_builtin(effective: dict[str, Any]) -> dict[str, Any]:
    """Field-by-field diff between effective policy and built-in defaults (audit / drift monitoring)."""
    baseline = builtin_effective_policy()
    differences: list[dict[str, Any]] = []
    for key in sorted(BUILTIN_DEFAULTS.keys()):
        a, b = baseline.get(key), effective.get(key)
        if a != b:
            differences.append({"field": key, "builtin": a, "effective": b})
    return {"has_drift": bool(differences), "differences": differences}


def policy_resolution_governance(
    policy_json_nonempty: bool,
    policy_path_nonempty: bool,
    selected_source: str,
) -> dict[str, Any]:
    """Explicit precedence chain for CI governance (secret > repo var path > built-ins)."""
    return {
        "precedence_order": [
            "ONCALL_TRACKER_HANDOFF_POLICY_JSON",
            "ONCALL_TRACKER_HANDOFF_POLICY_PATH",
            "builtin_defaults",
        ],
        "selected_source": selected_source,
        "inputs_evaluated": {
            "ONCALL_TRACKER_HANDOFF_POLICY_JSON_non_empty": policy_json_nonempty,
            "ONCALL_TRACKER_HANDOFF_POLICY_PATH_non_empty": policy_path_nonempty,
        },
    }


def redact_url_for_audit(url: str | None) -> dict[str, Any] | None:
    """Host + truncated path only (no query/fragment); safe for workflow artifacts."""
    if not _is_non_empty_str(url):
        return None
    p = urlparse(url.strip())
    path = p.path or ""
    if len(path) > 160:
        path = path[:157] + "..."
    return {
        "url_scheme": p.scheme or None,
        "url_host": p.hostname,
        "url_path_prefix": path or None,
    }


def extract_tracker_reconciliation(
    provider: str,
    http_status: int | None,
    body_bytes: bytes | None,
) -> dict[str, Any]:
    """Parse tracker POST response for closure identifiers; never embed raw bodies or secrets."""
    base: dict[str, Any] = {
        "schema": RECONCILIATION_SCHEMA,
        "applicable": False,
    }
    if http_status is None or not (200 <= http_status < 300):
        base["skip_reason"] = "non_success_http" if http_status is not None else "no_response"
        return base
    if not body_bytes:
        base["skip_reason"] = "empty_response_body"
        return base
    try:
        text = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        base["skip_reason"] = "invalid_utf8"
        base["response_prefix_digest_sha256"] = hashlib.sha256(body_bytes[:2048]).hexdigest()
        return base
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        base["skip_reason"] = "response_not_json"
        base["response_prefix_digest_sha256"] = hashlib.sha256(body_bytes[:4096]).hexdigest()
        return base
    if not isinstance(doc, dict):
        base["skip_reason"] = "response_json_not_object"
        return base

    base["applicable"] = True
    prov = (provider or "").strip().lower()

    def _recon_has_ids(d: dict[str, Any]) -> bool:
        return any(
            k in d
            for k in (
                "tracker_issue_key",
                "tracker_issue_number",
                "tracker_resource_hint",
                "tracker_issue_id_suffix",
            )
        )

    def _issue_id_suffix(raw: Any) -> str | None:
        if raw is None:
            return None
        s = str(raw).strip()
        if not s:
            return None
        return s[-12:] if len(s) > 12 else s

    if prov == "jira":
        key = doc.get("key")
        if _is_non_empty_str(key):
            base["tracker_issue_key"] = key.strip()[:80]
        suf = _issue_id_suffix(doc.get("id"))
        if suf:
            base["tracker_issue_id_suffix"] = suf
        self_u = doc.get("self")
        hint = redact_url_for_audit(self_u if isinstance(self_u, str) else None)
        if hint:
            base["tracker_resource_hint"] = hint
        if not _recon_has_ids(base):
            base["applicable"] = False
            base["skip_reason"] = "jira_response_missing_identifiers"
        return base

    if prov == "github":
        num = doc.get("number")
        if isinstance(num, int) and not isinstance(num, bool):
            base["tracker_issue_number"] = num
        elif num is not None and not isinstance(num, bool):
            ns = str(num).strip()[:20]
            if ns:
                base["tracker_issue_number"] = ns
        suf = _issue_id_suffix(doc.get("id"))
        if suf:
            base["tracker_issue_id_suffix"] = suf
        html_u = doc.get("html_url")
        hint = redact_url_for_audit(html_u if isinstance(html_u, str) else None)
        if hint:
            base["tracker_resource_hint"] = hint
        if not _recon_has_ids(base):
            base["applicable"] = False
            base["skip_reason"] = "github_response_missing_identifiers"
        return base

    # generic: best-effort keys seen across trackers / proxies
    for k in ("key", "issue_key", "issueKey"):
        v = doc.get(k)
        if _is_non_empty_str(v):
            base["tracker_issue_key"] = str(v).strip()[:80]
            break
    if "tracker_issue_key" not in base:
        for k in ("issue", "data"):
            inner = doc.get(k)
            if isinstance(inner, dict):
                ik = inner.get("key") or inner.get("issue_key")
                if _is_non_empty_str(ik):
                    base["tracker_issue_key"] = str(ik).strip()[:80]
                    break
    for uk in ("html_url", "url", "self", "browseUrl"):
        v = doc.get(uk)
        if _is_non_empty_str(v):
            hint = redact_url_for_audit(str(v))
            if hint:
                base["tracker_resource_hint"] = hint
                break
    suf = _issue_id_suffix(doc.get("id") or doc.get("issue_id"))
    if suf:
        base["tracker_issue_id_suffix"] = suf
    if not _recon_has_ids(base):
        base["applicable"] = False
        base["skip_reason"] = "no_identifiers_in_generic_response"
    return base


def closure_artifacts_from_ticket(ticket: dict[str, Any], payload_artifact_name: str) -> dict[str, Any]:
    """Link tracker handoff row to follow-up markdown + evidence names (from ticket template)."""
    ev = ticket.get("evidence_artifacts")
    if not isinstance(ev, list):
        ev_clean: list[str] = []
    else:
        ev_clean = [str(x) for x in ev if isinstance(x, str) and x.strip()][:50]
    imb = ticket.get("issue_body_markdown_artifact")
    return {
        "ticket_payload_source_artifact": payload_artifact_name,
        "issue_body_markdown_artifact": imb.strip() if _is_non_empty_str(imb) else None,
        "evidence_artifact_refs": ev_clean,
    }


def clamp_effective_policy(overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge overlay into builtins and clamp to sane bounds."""
    out = dict(BUILTIN_DEFAULTS)
    for k, v in overlay.items():
        if k in BUILTIN_DEFAULTS:
            out[k] = v

    ma = out["max_attempts"]
    if not isinstance(ma, int) or isinstance(ma, bool):
        ma = BUILTIN_DEFAULTS["max_attempts"]
    ma = max(MIN_ATTEMPTS, min(int(ma), MAX_ATTEMPTS_CAP))
    out["max_attempts"] = ma

    rs = out.get("retryable_http_statuses")
    if not isinstance(rs, list):
        rs = list(BUILTIN_DEFAULTS["retryable_http_statuses"])
    clean: list[int] = []
    seen: set[int] = set()
    for x in rs:
        if isinstance(x, bool) or not isinstance(x, int):
            continue
        if x < ALLOWED_RETRY_STATUS_MIN or x > ALLOWED_RETRY_STATUS_MAX:
            continue
        if x not in seen:
            seen.add(x)
            clean.append(x)
    if not clean:
        clean = sorted(DEFAULT_RETRYABLE)
    out["retryable_http_statuses"] = sorted(clean)

    bi = out.get("backoff_initial_seconds")
    try:
        bi_f = float(bi)
    except (TypeError, ValueError):
        bi_f = float(BUILTIN_DEFAULTS["backoff_initial_seconds"])
    bi_f = max(MIN_BACKOFF_INITIAL, min(bi_f, MAX_BACKOFF_INITIAL))
    out["backoff_initial_seconds"] = bi_f

    bm = out.get("backoff_max_seconds")
    try:
        bm_f = float(bm)
    except (TypeError, ValueError):
        bm_f = float(BUILTIN_DEFAULTS["backoff_max_seconds"])
    bm_f = max(MIN_BACKOFF_MAX, min(bm_f, MAX_BACKOFF_MAX_CAP))
    bm_f = max(bm_f, bi_f)
    out["backoff_max_seconds"] = bm_f

    mode = out.get("idempotency_mode")
    if not _is_non_empty_str(mode) or str(mode).strip().lower() not in IDEMPOTENCY_MODES:
        out["idempotency_mode"] = BUILTIN_DEFAULTS["idempotency_mode"]
    else:
        out["idempotency_mode"] = str(mode).strip().lower()

    return out


def effective_policy_for_environment(
    policy_doc: dict[str, Any] | None,
    environment: str,
) -> dict[str, Any]:
    """Resolve defaults + per-environment overlay from policy doc."""
    overlay: dict[str, Any] = {}
    if policy_doc and isinstance(policy_doc, dict):
        d = policy_doc.get("defaults")
        if isinstance(d, dict):
            overlay.update(d)
        envs = policy_doc.get("environments")
        if isinstance(envs, dict):
            row = envs.get(environment)
            if isinstance(row, dict):
                overlay.update(row)
    return clamp_effective_policy(overlay)


def validate_ticket_contract(provider: str, ticket: dict[str, Any]) -> tuple[bool, list[str]]:
    """Required fields and types for source ticket JSON before adapter-specific POST body build."""
    errors: list[str] = []

    if not isinstance(ticket, dict):
        return False, ["ticket_root: must be a JSON object"]

    sch = ticket.get("schema")
    if sch != TICKET_TEMPLATE_SCHEMA:
        errors.append(
            f"schema: expected {TICKET_TEMPLATE_SCHEMA!r}, got {sch!r}"
        )

    if not _is_non_empty_str(ticket.get("environment")):
        errors.append("environment: required non-empty string")

    if not _is_non_empty_str(ticket.get("issue_title_suggested")):
        errors.append("issue_title_suggested: required non-empty string")

    labels = ticket.get("labels")
    if not _is_str_list(labels):
        errors.append("labels: must be a JSON array of strings")
    elif len(labels) > 100:
        errors.append("labels: at most 100 entries")

    ga = ticket.get("github_actions")
    if not isinstance(ga, dict):
        errors.append("github_actions: must be a JSON object")
    else:
        if not _is_non_empty_str(ga.get("repository")):
            errors.append("github_actions.repository: required non-empty string (owner/repo)")
        for opt_key in ("workflow", "run_url", "run_id", "sha"):
            val = ga.get(opt_key)
            if val is not None and val != "" and not isinstance(val, str):
                errors.append(f"github_actions.{opt_key}: must be string if set")

    prov = (provider or "").strip().lower() or "generic"

    if prov == "jira":
        if not _is_non_empty_str(ticket.get("tracker_project_key")):
            errors.append("tracker_project_key: required non-empty string for jira adapter")

    if prov == "github":
        repo = (ga or {}).get("repository") if isinstance(ga, dict) else None
        if not _is_non_empty_str(repo) or "/" not in str(repo).strip():
            errors.append(
                "github_actions.repository: must look like OWNER/REPO for github adapter"
            )

    if prov == "generic":
        # Raw POST must still be a well-formed ticket template for governance.
        pass

    ok = len(errors) == 0
    return ok, errors


def parse_ticket_json(raw_bytes: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, f"invalid_ticket_encoding:{type(e).__name__}"
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"invalid_ticket_json:{type(e).__name__}"
    if not isinstance(doc, dict):
        return None, "ticket_root_not_object"
    return doc, None


def url_meta(url: str) -> dict[str, Any]:
    p = urlparse(url)
    path = p.path or ""
    if len(path) > 120:
        path = path[:117] + "..."
    return {
        "url_scheme": p.scheme or None,
        "url_host": p.hostname,
        "url_path_prefix": path or None,
    }


def normalize_provider(raw: str) -> tuple[str, str | None]:
    s = (raw or "").strip().lower()
    if not s or s == "generic":
        return "generic", None
    if s in ("jira", "github"):
        return s, None
    return "generic", f"unknown_provider_{s}_using_generic_adapter"


def idempotency_key(
    mode: str,
    run_id_: str,
    environment: str,
    source_payload_sha256: str,
) -> str | None:
    if mode == "off":
        return None
    seed = f"{run_id_}|{environment}|{source_payload_sha256}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()


def adf_paragraph(text: str) -> dict[str, Any]:
    t = text if len(text) <= 32000 else text[:31997] + "..."
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": t}],
            }
        ],
    }


def build_post_body(
    provider: str, raw_ticket_bytes: bytes, environment: str
) -> tuple[bytes, str | None]:
    """Returns (body_bytes, adapter_error) — adapter_error set => do not POST."""
    if provider == "generic":
        return raw_ticket_bytes, None
    ticket, err = parse_ticket_json(raw_ticket_bytes)
    if err:
        return b"", err
    assert ticket is not None
    if provider == "jira":
        proj = (ticket.get("tracker_project_key") or "").strip()
        if not proj:
            return b"", "jira_missing_tracker_project_key"
        title = (ticket.get("issue_title_suggested") or "oncall drill follow-up").strip()
        labels = ticket.get("labels") or []
        if not isinstance(labels, list):
            labels = []
        ga = ticket.get("github_actions") or {}
        desc_lines = [
            f"Environment: {ticket.get('environment', environment)}",
            f"Workflow run: {ga.get('run_url')}",
            f"Suggested body artifact: {ticket.get('issue_body_markdown_artifact', '')}",
            "",
            "Created by oncall-route-verification optional-tracker-handoff (jira adapter).",
        ]
        issue = {
            "fields": {
                "project": {"key": proj},
                "summary": title[:255],
                "issuetype": {"name": "Task"},
                "labels": [str(x) for x in labels][:50],
                "description": adf_paragraph("\n".join(desc_lines)),
            }
        }
        return json.dumps(issue, separators=(",", ":")).encode("utf-8"), None
    if provider == "github":
        title = (ticket.get("issue_title_suggested") or "oncall drill follow-up").strip()
        labels = ticket.get("labels") or []
        if not isinstance(labels, list):
            labels = []
        ga = ticket.get("github_actions") or {}
        body_md = "\n".join(
            [
                f"**Environment**: {ticket.get('environment', environment)}",
                f"**Run**: {ga.get('run_url')}",
                f"**Repository**: {ga.get('repository')}",
                "",
                "Generated by `oncall-route-verification` optional-tracker-handoff (github adapter).",
            ]
        )
        gh = {
            "title": title[:200],
            "body": body_md,
            "labels": [str(x) for x in labels][:30],
        }
        return json.dumps(gh, separators=(",", ":")).encode("utf-8"), None
    return raw_ticket_bytes, None


def authorization_value(token: str) -> str:
    t = token.strip()
    tl = t.lower()
    if tl.startswith("basic "):
        return t
    if tl.startswith("bearer "):
        return t
    return f"Bearer {t}"


def apply_adapter_headers(
    req: urllib.request.Request,
    provider: str,
    token: str,
    idem: str | None,
) -> list[str]:
    names = ["Content-Type"]
    req.add_header("Content-Type", "application/json")
    if idem:
        req.add_header("Idempotency-Key", idem)
        names.append("Idempotency-Key")
    if provider == "jira":
        req.add_header("Accept", "application/json")
        names.append("Accept")
    if provider == "github":
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        names.extend(["Accept", "X-GitHub-Api-Version"])
    if token:
        req.add_header("Authorization", authorization_value(token))
        names.append("Authorization")
    return sorted(names)


def retry_sleep_seconds(
    attempt_index: int,
    http_code: int | None,
    headers: Any,
    initial: float,
    backoff_max: float,
) -> float:
    if http_code == 429 and headers:
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra:
            try:
                return min(float(ra), backoff_max)
            except ValueError:
                pass
    base = min(initial * (2 ** (attempt_index - 1)), backoff_max)
    jitter = random.uniform(0, 0.25 * base)
    return min(base + jitter, backoff_max)


def is_retryable_http(code: int, retryable: frozenset[int]) -> bool:
    return code in retryable


def is_retryable_exception(exc: BaseException, retryable: frozenset[int]) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return is_retryable_http(int(exc.code), retryable)
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, (TimeoutError, OSError, ConnectionError))


def _read_body_limited(stream: Any, limit: int) -> bytes:
    return stream.read(limit + 1)[:limit]


def execute_post(
    url: str,
    post_bytes: bytes,
    provider: str,
    token: str,
    idem: str | None,
    max_attempts: int,
    retryable: frozenset[int],
    initial_backoff: float,
    backoff_max: float,
) -> tuple[int | None, str | None, int, list[str], bytes | None]:
    req = urllib.request.Request(url, data=post_bytes, method="POST")
    header_names = apply_adapter_headers(req, provider, token, idem)
    last_http = None
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_body = _read_body_limited(resp, MAX_RECONCILIATION_RESPONSE_BYTES)
                code = getattr(resp, "status", None) or resp.getcode()
            return int(code), None, attempt, header_names, raw_body
        except urllib.error.HTTPError as e:
            last_http = int(e.code)
            err_body: bytes | None = None
            try:
                err_body = _read_body_limited(e, MAX_RECONCILIATION_RESPONSE_BYTES)
            except Exception:
                pass
            if attempt < max_attempts and is_retryable_http(last_http, retryable):
                time.sleep(
                    retry_sleep_seconds(attempt, last_http, e.headers, initial_backoff, backoff_max)
                )
                continue
            return last_http, f"HTTPError:{e.reason}", attempt, header_names, err_body
        except Exception as e:
            last_err = type(e).__name__ + ":" + str(e)[:500]
            if attempt < max_attempts and is_retryable_exception(e, retryable):
                time.sleep(retry_sleep_seconds(attempt, None, None, initial_backoff, backoff_max))
                continue
            return None, last_err, attempt, header_names, None
    return last_http, last_err or "exhausted_retries", max_attempts, header_names, None


def cmd_ci_check(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    policy_json_env = os.environ.get("ONCALL_TRACKER_HANDOFF_POLICY_JSON")
    policy_path_env = os.environ.get("ONCALL_TRACKER_HANDOFF_POLICY_PATH")
    if (policy_json_env or "").strip():
        policy_doc, policy_src = load_policy_doc(policy_json_env, policy_path_env, repo_root)
    elif (policy_path_env or "").strip():
        policy_doc, policy_src = load_policy_doc(None, policy_path_env, repo_root)
    else:
        policy_doc, policy_src = load_policy_doc(None, args.policy, repo_root)
    if policy_doc is not None:
        sch = policy_doc.get("schema")
        if sch is not None and sch != POLICY_SCHEMA:
            print(
                f"warning: policy schema {sch!r} is not {POLICY_SCHEMA!r} (continuing)",
                file=sys.stderr,
            )

    pres = policy_resolution_governance(
        bool((policy_json_env or "").strip()),
        bool((policy_path_env or "").strip()),
        policy_src,
    )
    print(f"GOVERNANCE policy_resolution={json.dumps(pres, sort_keys=True)}")

    failures = 0
    checks = [
        ("staging", args.staging_payload, args.staging_provider),
        ("production", args.production_payload, args.production_provider),
    ]
    for env_name, payload_path, prov_raw in checks:
        path = Path(payload_path)
        if not path.is_file():
            print(f"FAIL {env_name}: missing payload {path}", file=sys.stderr)
            failures += 1
            continue
        raw = path.read_bytes()
        ticket, perr = parse_ticket_json(raw)
        provider, _ = normalize_provider(prov_raw)
        if perr:
            print(f"FAIL {env_name}: {perr}", file=sys.stderr)
            failures += 1
            continue
        assert ticket is not None
        ok, errs = validate_ticket_contract(provider, ticket)
        if not ok:
            print(f"FAIL {env_name} contract ({provider}): " + "; ".join(errs), file=sys.stderr)
            failures += 1
        else:
            eff = effective_policy_for_environment(policy_doc, env_name)
            drift = policy_drift_vs_builtin(eff)
            print(
                "OK "
                f"{env_name} provider={provider} policy_source={policy_src} "
                f"policy_drift_vs_builtin_defaults={json.dumps(drift, sort_keys=True)} "
                f"effective={eff!r}"
            )

    return 1 if failures else 0


def format_reconciliation_writeback_markdown(recon: dict[str, Any], env: str) -> str:
    """Append-only block for follow-up markdown (no secrets)."""
    lines = [
        "## Tracker reconciliation (automation write-back)",
        "",
        f"_Environment_: **{env}**",
        "",
    ]
    if not recon.get("applicable"):
        lines.append(
            f"_No applicable tracker identifiers captured_ (`{recon.get('skip_reason', 'n/a')}`)."
        )
        lines.append("")
        return "\n".join(lines)

    for label, key in (
        ("Issue key", "tracker_issue_key"),
        ("Issue number", "tracker_issue_number"),
        ("Issue id (suffix)", "tracker_issue_id_suffix"),
    ):
        v = recon.get(key)
        if v is not None:
            lines.append(f"- **{label}**: `{v}`")
    hint = recon.get("tracker_resource_hint")
    if isinstance(hint, dict) and hint:
        host = hint.get("url_host") or ""
        path = hint.get("url_path_prefix") or ""
        sch = hint.get("url_scheme") or ""
        lines.append(f"- **Resource (redacted)**: `{sch}://{host}{path}`")
    lines.append("")
    return "\n".join(lines)


def cmd_writeback_reconciliation(args: argparse.Namespace) -> int:
    """Append reconciliation section to follow-up markdown artifacts (local files only)."""
    results_path = Path(args.results_json).resolve()
    art_dir = Path(args.artifacts_dir).resolve()
    if not results_path.is_file():
        print(f"missing results JSON: {results_path}", file=sys.stderr)
        return 1
    try:
        doc = json.loads(results_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"invalid results JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(doc, dict):
        print("results root must be object", file=sys.stderr)
        return 1

    heading = "## Tracker reconciliation (automation write-back)"
    updated = 0
    for row in doc.get("environments") or []:
        if not isinstance(row, dict):
            continue
        env = str(row.get("environment", "")).strip() or "unknown"
        closure = row.get("closure_artifacts")
        if not isinstance(closure, dict):
            continue
        md_name = closure.get("issue_body_markdown_artifact")
        if not isinstance(md_name, str) or not md_name.strip():
            continue
        md_path = (art_dir / md_name).resolve()
        try:
            md_path.relative_to(art_dir)
        except ValueError:
            print(f"skip path outside artifacts dir: {md_name}", file=sys.stderr)
            continue
        if not md_path.is_file():
            print(f"skip missing markdown: {md_path}", file=sys.stderr)
            continue
        text = md_path.read_text(encoding="utf-8")
        if heading in text:
            continue
        resp = row.get("response") if isinstance(row.get("response"), dict) else {}
        recon = resp.get("reconciliation") if isinstance(resp.get("reconciliation"), dict) else {}
        block = format_reconciliation_writeback_markdown(recon, env)
        md_path.write_text(text.rstrip() + "\n\n" + block + "\n", encoding="utf-8")
        updated += 1

    print(f"writeback-reconciliation: updated {updated} markdown file(s)", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    policy_json = os.environ.get("ONCALL_TRACKER_HANDOFF_POLICY_JSON")
    policy_path = os.environ.get("ONCALL_TRACKER_HANDOFF_POLICY_PATH")
    policy_doc, policy_source = load_policy_doc(policy_json, policy_path, repo_root)
    policy_json_nonempty = bool((policy_json or "").strip())
    policy_path_nonempty = bool((policy_path or "").strip())
    policy_resolution = policy_resolution_governance(
        policy_json_nonempty,
        policy_path_nonempty,
        policy_source,
    )
    policy_drift_by_env = {
        env: policy_drift_vs_builtin(effective_policy_for_environment(policy_doc, env))
        for env in ("staging", "production")
    }

    art = Path(os.environ.get("HANDOFF_ARTIFACTS_DIR", "artifacts/oncall")).resolve()
    out_dir = Path(os.environ.get("HANDOFF_OUT_DIR", "artifacts/oncall-tracker-handoff"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "oncall-tracker-handoff-results.json"

    event = (os.environ.get("GITHUB_EVENT_NAME") or "").strip()
    wfd = event == "workflow_dispatch"
    post_in = str(os.environ.get("POST_TRACKER_HANDOFF_INPUT") or "").strip().lower() in (
        "true",
        "1",
    )
    var_on = (os.environ.get("ONCALL_POST_TRACKER_HANDOFF_VAR") or "").strip() == "true"
    if wfd and post_in:
        trigger = "workflow_dispatch_post_tracker_handoff_input"
    elif var_on:
        trigger = "repository_variable_ONCALL_POST_TRACKER_HANDOFF"
    else:
        trigger = "configured_gate_matched"

    run_id = (os.environ.get("GITHUB_RUN_ID") or "").strip()

    specs = [
        {
            "environment": "staging",
            "payload_file": art / "oncall-ticket-payload-staging.json",
            "url": (os.environ.get("STAGING_URL") or "").strip(),
            "token": (os.environ.get("STAGING_TOKEN") or "").strip(),
            "provider_raw": os.environ.get("STAGING_PROVIDER") or "",
        },
        {
            "environment": "production",
            "payload_file": art / "oncall-ticket-payload-production.json",
            "url": (os.environ.get("PRODUCTION_URL") or "").strip(),
            "token": (os.environ.get("PRODUCTION_TOKEN") or "").strip(),
            "provider_raw": os.environ.get("PRODUCTION_PROVIDER") or "",
        },
    ]

    governance_environments: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    any_contract_fail_with_url = False

    for spec in specs:
        env_name = spec["environment"]
        pf = spec["payload_file"]
        url = spec["url"]
        token = spec["token"]
        provider, adapter_note = normalize_provider(spec["provider_raw"])
        eff = effective_policy_for_environment(policy_doc, env_name)
        retryable_fro = frozenset(int(x) for x in eff["retryable_http_statuses"])

        base_req: dict[str, Any] = {
            "method": "POST",
            "payload_source_artifact": pf.name,
            "provider": provider,
            "provider_raw": (spec["provider_raw"] or "").strip() or None,
            "adapter_note": adapter_note,
            "authorization_header": "present" if token else "absent",
        }

        contract_ok = False
        contract_errors: list[str] = []
        raw_ticket_bytes: bytes | None = None
        ticket: dict[str, Any] | None = None
        if pf.is_file():
            raw_ticket_bytes = pf.read_bytes()
            ticket, perr = parse_ticket_json(raw_ticket_bytes)
            if perr:
                contract_errors = [perr]
            else:
                assert ticket is not None
                contract_ok, contract_errors = validate_ticket_contract(provider, ticket)
        else:
            contract_errors = [f"missing_payload_file:{pf.name}"]

        closure_art = (
            closure_artifacts_from_ticket(ticket, pf.name)
            if ticket is not None
            else None
        )

        governance_environments[env_name] = {
            "effective_policy": eff,
            "policy_overlay_source": policy_source,
            "contract_validation": {
                "ok": contract_ok,
                "errors": list(contract_errors),
            },
        }

        if not url:
            rows.append(
                {
                    "environment": env_name,
                    "status": "skipped",
                    "skip_reason": "missing_handoff_url_secret",
                    "closure_artifacts": closure_art,
                    "contract_validation": {
                        "ok": contract_ok,
                        "errors": list(contract_errors),
                    },
                    "request": {
                        **base_req,
                        "url_scheme": None,
                        "url_host": None,
                        "url_path_prefix": None,
                        "payload_sha256": None,
                        "idempotency_key": None,
                        "idempotency_mode": eff["idempotency_mode"],
                        "adapter_request_headers": None,
                        "attempts": None,
                    },
                    "response": {
                        "http_status": None,
                        "error": None,
                        "attempts": None,
                        "reconciliation": None,
                    },
                }
            )
            continue

        if raw_ticket_bytes is None:
            rows.append(
                {
                    "environment": env_name,
                    "status": "failed",
                    "skip_reason": None,
                    "closure_artifacts": closure_art,
                    "contract_validation": {
                        "ok": False,
                        "errors": list(contract_errors),
                    },
                    "request": {
                        **base_req,
                        **url_meta(url),
                        "payload_sha256": None,
                        "idempotency_key": None,
                        "idempotency_mode": eff["idempotency_mode"],
                        "adapter_request_headers": None,
                        "attempts": None,
                    },
                    "response": {
                        "http_status": None,
                        "error": f"missing_payload_file:{pf.name}",
                        "attempts": None,
                        "reconciliation": None,
                    },
                }
            )
            any_contract_fail_with_url = True
            continue

        payload_sha256 = hashlib.sha256(raw_ticket_bytes).hexdigest()
        idem = idempotency_key(
            str(eff["idempotency_mode"]), run_id, env_name, payload_sha256
        )

        if not contract_ok:
            rows.append(
                {
                    "environment": env_name,
                    "status": "failed",
                    "skip_reason": None,
                    "closure_artifacts": closure_art,
                    "contract_validation": {"ok": False, "errors": list(contract_errors)},
                    "request": {
                        **base_req,
                        **url_meta(url),
                        "content_type": "application/json",
                        "payload_sha256": payload_sha256,
                        "idempotency_key": idem,
                        "idempotency_mode": eff["idempotency_mode"],
                        "adapter_request_headers": None,
                        "attempts": None,
                    },
                    "response": {
                        "http_status": None,
                        "error": "contract_validation_failed",
                        "attempts": None,
                        "reconciliation": None,
                    },
                }
            )
            any_contract_fail_with_url = True
            continue

        post_bytes, adapter_err = build_post_body(provider, raw_ticket_bytes, env_name)
        if adapter_err:
            rows.append(
                {
                    "environment": env_name,
                    "status": "failed",
                    "skip_reason": None,
                    "closure_artifacts": closure_art,
                    "contract_validation": {"ok": True, "errors": []},
                    "request": {
                        **base_req,
                        **url_meta(url),
                        "content_type": "application/json",
                        "payload_sha256": payload_sha256,
                        "idempotency_key": idem,
                        "idempotency_mode": eff["idempotency_mode"],
                        "adapter_request_headers": None,
                        "attempts": None,
                    },
                    "response": {
                        "http_status": None,
                        "error": adapter_err,
                        "attempts": None,
                        "reconciliation": None,
                    },
                }
            )
            continue

        http_code, err, attempts, header_names, resp_body = execute_post(
            url,
            post_bytes,
            provider,
            token,
            idem,
            int(eff["max_attempts"]),
            retryable_fro,
            float(eff["backoff_initial_seconds"]),
            float(eff["backoff_max_seconds"]),
        )
        ok = err is None and http_code is not None and 200 <= http_code < 300
        recon = extract_tracker_reconciliation(provider, http_code, resp_body)
        rows.append(
            {
                "environment": env_name,
                "status": "success" if ok else "failed",
                "skip_reason": None,
                "closure_artifacts": closure_art,
                "contract_validation": {"ok": True, "errors": []},
                "request": {
                    **base_req,
                    **url_meta(url),
                    "content_type": "application/json",
                    "payload_sha256": payload_sha256,
                    "idempotency_key": idem,
                    "idempotency_mode": eff["idempotency_mode"],
                    "adapter_request_headers": header_names,
                    "attempts": attempts,
                },
                "response": {
                    "http_status": http_code,
                    "error": err,
                    "attempts": attempts,
                    "reconciliation": recon,
                },
            }
        )

    now = time.time()
    summary_parts = [f"{r['environment']}:{r['status']}" for r in rows]
    doc: dict[str, Any] = {
        "schema": RESULTS_SCHEMA,
        "ts_unix": now,
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "github_event": os.environ.get("GITHUB_EVENT_NAME"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_sha": os.environ.get("GITHUB_SHA"),
        "github_repository": os.environ.get("GITHUB_REPOSITORY"),
        "trigger_detail": trigger,
        "governance_audit": {
            "policy_source": policy_source,
            "policy_resolution": policy_resolution,
            "policy_schema": policy_doc.get("schema") if isinstance(policy_doc, dict) else None,
            "builtin_defaults_reference": BUILTIN_DEFAULTS,
            "policy_drift_vs_builtin_defaults": policy_drift_by_env,
            "per_environment": governance_environments,
        },
        "handoff_retry_policy": effective_policy_for_environment(policy_doc, "staging"),
        "environments": rows,
        "summary": "; ".join(summary_parts),
    }
    out_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if any_contract_fail_with_url:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ci = sub.add_parser("ci-check", help="Validate ticket payloads and policy shape (CI, no network)")
    p_ci.add_argument("--repo-root", default=".", help="Repository root (default: cwd)")
    p_ci.add_argument(
        "--policy",
        default="deploy/ops/oncall_tracker_handoff_policy.example.json",
        help="Repo-relative policy path (optional overlay for effective_policy sanity)",
    )
    p_ci.add_argument("--staging-payload", required=True)
    p_ci.add_argument("--production-payload", required=True)
    p_ci.add_argument("--staging-provider", default="generic")
    p_ci.add_argument("--production-provider", default="generic")
    p_ci.set_defaults(func=cmd_ci_check)

    p_run = sub.add_parser("run", help="Execute tracker handoff (reads environment variables)")
    p_run.add_argument("--repo-root", default=".", help="Repository root for policy path resolution")
    p_run.set_defaults(func=cmd_run)

    p_wb = sub.add_parser(
        "writeback-reconciliation",
        help="Append reconciliation block to follow-up markdown files from handoff results",
    )
    p_wb.add_argument(
        "--results-json",
        type=Path,
        required=True,
        help="Path to oncall-tracker-handoff-results.json",
    )
    p_wb.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path("artifacts/oncall"),
        help="Directory containing oncall-live-drill-followup-*.md (default: artifacts/oncall)",
    )
    p_wb.set_defaults(func=cmd_writeback_reconciliation)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
