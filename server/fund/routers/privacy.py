"""GDPR / CCPA right-to-delete endpoints (prompt 57).

The privacy router exposes:

* ``POST /api/v1/privacy/erasure`` -- subject-facing entry point. The
  body carries ``subject_id`` and a one-shot ``verification_token``
  issued out-of-band by support staff. The handler hashes the token
  and matches it against a pending :class:`ErasureRequest` row; on a
  match the request transitions to ``scheduled`` with
  ``scheduled_for = requested_at + 30 days``. ``immediate=true``
  collapses the buffer but is admin-only.

* ``POST /api/v1/privacy/erasure/issue`` -- admin/support endpoint
  used to issue a verification token before the subject calls in. The
  plaintext token is returned **once** in the response body and never
  persisted anywhere else (only its SHA-256 hash is stored).

Audit-hold contract (prompt 57 prohibition): a request that targets a
class flagged ``on_expiry: keep`` (decision artifact, audit log) is
refused with ``ERASURE_REFUSED_AUDIT_HOLD``. The handler records the
refusal on the row and emits an ``erasure_refused`` event.

The handler MUST NOT confirm erasure to the requestor before the
deletion job actually runs. The success response surfaces ``status =
"scheduled"`` only -- the daily worker flips the row to ``completed``
and emits ``erasure_completed`` once the rows are actually redacted.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import envelope, error_response, new_request_id
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.security import audit_log, enforce_roles
from coherence_engine.server.fund.services.event_publisher import EventPublisher
from coherence_engine.server.fund.services.retention import (
    ERASURE_GRACE_DAYS,
    ERASURE_REFUSED_AUDIT_HOLD,
    assess_erasure_classes,
    load_retention_policy,
)


router = APIRouter(prefix="/privacy", tags=["privacy"])


_VERIFICATION_TOKEN_BYTES = 32
_SUBJECT_TYPES = {"founder", "investor"}


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def hash_verification_token(token: str) -> str:
    """Return the SHA-256 hex digest of ``token`` (server-side validator)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_verification_token(
    db: Session,
    *,
    subject_id: str,
    subject_type: str,
    issued_by: str,
    classes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Mint a one-shot token for a future erasure request.

    The plaintext token is returned to the caller (admin/support) and
    is never persisted. Only its SHA-256 hash is stored on the
    :class:`ErasureRequest` row in ``pending_subject_request`` state.
    """
    if subject_type not in _SUBJECT_TYPES:
        raise ValueError(f"unsupported subject_type: {subject_type!r}")
    if not subject_id:
        raise ValueError("subject_id is required")
    plaintext = secrets.token_urlsafe(_VERIFICATION_TOKEN_BYTES)
    row = models.ErasureRequest(
        id=f"era_{uuid.uuid4().hex[:24]}",
        subject_id=subject_id,
        subject_type=subject_type,
        status="pending_subject_request",
        verification_token_hash=hash_verification_token(plaintext),
        issued_by=issued_by,
        classes_json=json.dumps(classes or []),
    )
    db.add(row)
    db.flush()
    return {
        "erasure_request_id": row.id,
        "verification_token": plaintext,  # ONLY surfaced in this response
        "subject_id": subject_id,
        "subject_type": subject_type,
        "expires_in_days": 30,
    }


@router.post("/erasure/issue")
def post_issue_token(
    request: Request,
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Admin / support: issue a verification token to a subject.

    Gated to ``admin`` or ``support`` roles. The plaintext token is
    returned exactly once in the response body. The endpoint MUST be
    called only after the requestor's identity has been validated
    out-of-band (phone callback, in-person, video KYC) -- this server
    does not perform that step.
    """
    request_id = request.headers.get("x-request-id") or new_request_id()
    deny = enforce_roles(request, ("admin", "support"))
    if deny is not None:
        return deny
    subject_id = str(body.get("subject_id", "")).strip()
    subject_type = str(body.get("subject_type", "founder")).strip()
    classes = body.get("classes")
    if not subject_id:
        return error_response(
            request_id, 400, "BAD_REQUEST", "subject_id is required"
        )
    if subject_type not in _SUBJECT_TYPES:
        return error_response(
            request_id, 400, "BAD_REQUEST", f"unsupported subject_type: {subject_type!r}"
        )
    principal = getattr(request.state, "principal", None) or {}
    issued_by = str(principal.get("fingerprint") or principal.get("role") or "support")
    try:
        result = issue_verification_token(
            db,
            subject_id=subject_id,
            subject_type=subject_type,
            issued_by=issued_by,
            classes=classes if isinstance(classes, list) else None,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return error_response(request_id, 400, "BAD_REQUEST", str(exc))
    audit_log(
        event="erasure_token_issued",
        request=request,
        outcome="allowed",
        details={
            "subject_id": subject_id,
            "subject_type": subject_type,
            "erasure_request_id": result["erasure_request_id"],
        },
    )
    return envelope(request_id=request_id, data=result)


@router.post("/erasure")
def post_erasure(
    request: Request,
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Subject-facing: schedule an erasure against a previously-issued token.

    Body::

        {
          "subject_id": "fnd_...",
          "verification_token": "<one-shot token issued by support>",
          "classes": ["transcript", ...]   // optional; defaults to all erasable
          "immediate": false               // admin-only; collapses 30-day buffer
        }

    The handler:

    1. Hashes the token server-side and looks up the matching row.
       Rejects with ``UNAUTHORIZED`` on no match.
    2. If ``classes`` includes any audit-hold class (decision_artifact,
       audit_log), refuses with ``ERASURE_REFUSED_AUDIT_HOLD`` and
       transitions the row to ``refused``.
    3. Otherwise transitions to ``scheduled`` with
       ``scheduled_for = now + 30 days`` (or ``now`` if ``immediate``
       and the caller is ``admin``).
    4. Emits ``erasure_scheduled`` (or ``erasure_refused``) and returns
       ``status="scheduled"`` (or ``"refused"``). The endpoint never
       returns ``"completed"`` -- only the worker flips that.
    """
    request_id = request.headers.get("x-request-id") or new_request_id()
    subject_id = str(body.get("subject_id", "")).strip()
    token = body.get("verification_token")
    if not subject_id or not isinstance(token, str) or not token.strip():
        return error_response(
            request_id, 400, "BAD_REQUEST",
            "subject_id and verification_token are required",
        )
    requested_classes = body.get("classes")
    if requested_classes is not None and not isinstance(requested_classes, list):
        return error_response(
            request_id, 400, "BAD_REQUEST",
            "classes must be a list of strings if provided",
        )
    immediate = bool(body.get("immediate", False))

    token_hash = hash_verification_token(token.strip())
    row = (
        db.query(models.ErasureRequest)
        .filter(models.ErasureRequest.verification_token_hash == token_hash)
        .one_or_none()
    )
    if row is None or row.subject_id != subject_id:
        audit_log(
            event="erasure_request_unauthorized",
            request=request,
            outcome="denied",
            details={"subject_id": subject_id, "reason": "token_mismatch"},
        )
        return error_response(
            request_id, 401, "UNAUTHORIZED",
            "verification token does not match a pending erasure request",
        )

    # Idempotency: replay returns the existing record without side-effects.
    if row.status in {"scheduled", "completed", "refused"}:
        return envelope(
            request_id=request_id,
            data=_serialize_erasure(row, idempotent=True),
        )

    if row.status != "pending_subject_request":
        return error_response(
            request_id, 409, "INVALID_STATE",
            f"erasure request in unexpected state: {row.status}",
        )

    policy = load_retention_policy()
    erasable, audit_hold = assess_erasure_classes(requested_classes, policy)
    now = _utc_now()
    publisher = EventPublisher(db)

    principal = getattr(request.state, "principal", None) or {}
    actor_role = str(principal.get("role", "")).lower()
    actor_fp = str(principal.get("fingerprint") or "")

    if audit_hold:
        row.status = "refused"
        row.refusal_reason = ERASURE_REFUSED_AUDIT_HOLD
        row.requested_at = now
        row.requested_by = actor_fp or "subject"
        row.classes_json = json.dumps(requested_classes or [])
        row.request_id = request_id
        db.add(row)
        try:
            publisher.publish(
                event_type="erasure_refused",
                producer="privacy_router",
                trace_id=request_id,
                idempotency_key=f"erasure_refused:{row.id}",
                payload={
                    "erasure_request_id": row.id,
                    "subject_id": row.subject_id,
                    "subject_type": row.subject_type,
                    "refusal_reason": ERASURE_REFUSED_AUDIT_HOLD,
                    "audit_hold_classes": audit_hold,
                },
            )
        except Exception:  # pragma: no cover - publisher best-effort
            pass
        db.commit()
        audit_log(
            event="erasure_refused",
            request=request,
            outcome="denied",
            details={
                "erasure_request_id": row.id,
                "refusal_reason": ERASURE_REFUSED_AUDIT_HOLD,
                "audit_hold_classes": audit_hold,
            },
        )
        return envelope(
            request_id=request_id,
            data=_serialize_erasure(row),
        )

    if immediate and actor_role != "admin":
        return error_response(
            request_id, 403, "FORBIDDEN",
            "immediate=true requires admin role",
        )

    scheduled_for = now if immediate else now + timedelta(days=ERASURE_GRACE_DAYS)
    row.status = "scheduled"
    row.requested_at = now
    row.requested_by = actor_fp or "subject"
    row.scheduled_for = scheduled_for
    row.immediate = immediate
    row.classes_json = json.dumps(erasable)
    row.request_id = request_id
    db.add(row)
    try:
        publisher.publish(
            event_type="erasure_scheduled",
            producer="privacy_router",
            trace_id=request_id,
            idempotency_key=f"erasure_scheduled:{row.id}",
            payload={
                "erasure_request_id": row.id,
                "subject_id": row.subject_id,
                "subject_type": row.subject_type,
                "scheduled_for": scheduled_for.isoformat().replace("+00:00", "Z"),
                "classes": erasable,
                "immediate": immediate,
            },
        )
    except Exception:  # pragma: no cover - publisher best-effort
        pass
    db.commit()
    audit_log(
        event="erasure_scheduled",
        request=request,
        outcome="allowed",
        details={
            "erasure_request_id": row.id,
            "scheduled_for": scheduled_for.isoformat(),
            "classes": erasable,
            "immediate": immediate,
        },
    )
    return envelope(
        request_id=request_id,
        data=_serialize_erasure(row),
    )


def _serialize_erasure(row: models.ErasureRequest, *, idempotent: bool = False) -> Dict[str, Any]:
    return {
        "erasure_request_id": row.id,
        "subject_id": row.subject_id,
        "subject_type": row.subject_type,
        "status": row.status,
        "scheduled_for": row.scheduled_for.isoformat() if row.scheduled_for else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "refusal_reason": row.refusal_reason or None,
        "classes": json.loads(row.classes_json or "[]"),
        "immediate": bool(row.immediate),
        "idempotent": idempotent,
    }
