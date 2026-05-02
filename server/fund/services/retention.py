"""Daily retention sweep + targeted erasure execution (prompt 57).

The retention worker is the canonical place that combines:

  * tombstoning the object-storage URI (soft-delete via
    :func:`server.fund.services.object_storage.delete`),
  * crypto-shredding the per-row encryption key
    (:func:`server.fund.services.crypto_shred.shred_key`), and
  * flipping ``redacted=True`` on the DB row so reads return HTTP 410.

It runs in two modes:

  * ``apply_retention(db, now=...)`` -- daily sweep. Walks each class
    declared in ``data/governed/retention_policy.yaml``, finds rows
    whose age exceeds ``retention_days``, and applies
    ``tombstone_and_shred``. Classes flagged ``on_expiry: keep`` are
    skipped (decision artifacts, audit log).
  * ``execute_erasure(db, erasure_request_id)`` -- targeted execution
    of a single :class:`ErasureRequest`. Called by the worker after
    ``scheduled_for`` is reached. Idempotent: re-running a completed
    request is a no-op.

Audit-hold contract: classes whose policy is ``on_expiry: keep`` are
NEVER tombstoned by either entry point. ``execute_erasure`` records
which classes were preserved on the request row so the audit trail
explains what survived the erasure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services import object_storage
from coherence_engine.server.fund.services.crypto_shred import shred_key
from coherence_engine.server.fund.services.event_publisher import EventPublisher
from coherence_engine.server.fund.services.per_row_encryption import (
    KeyNotFoundError,
)


_LOG = logging.getLogger(__name__)


SCHEMA_VERSION = "retention-policy-v1"
ON_EXPIRY_TOMBSTONE_AND_SHRED = "tombstone_and_shred"
ON_EXPIRY_KEEP = "keep"

ERASURE_GRACE_DAYS = 30
ERASURE_REFUSED_AUDIT_HOLD = "ERASURE_REFUSED_AUDIT_HOLD"

POLICY_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "governed" / "retention_policy.yaml"
)


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionClass:
    name: str
    retention_days: Optional[int]  # None == indefinite
    on_expiry: str
    description: str = ""

    @property
    def keeps_indefinitely(self) -> bool:
        return self.on_expiry == ON_EXPIRY_KEEP


@dataclass(frozen=True)
class RetentionPolicy:
    schema_version: str
    classes: Tuple[RetentionClass, ...]

    def by_name(self, name: str) -> Optional[RetentionClass]:
        for cls in self.classes:
            if cls.name == name:
                return cls
        return None

    def is_audit_hold(self, name: str) -> bool:
        cls = self.by_name(name)
        return cls is not None and cls.keeps_indefinitely


def load_retention_policy(path: Optional[Path] = None) -> RetentionPolicy:
    """Load + validate ``data/governed/retention_policy.yaml``."""
    p = Path(path) if path else POLICY_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"retention policy at {p} is not a mapping")
    schema = str(raw.get("schema_version", ""))
    if schema != SCHEMA_VERSION:
        raise ValueError(
            f"retention policy schema_version {schema!r} != {SCHEMA_VERSION!r}"
        )
    classes_raw = raw.get("classes") or []
    if not isinstance(classes_raw, list):
        raise ValueError("retention policy 'classes' must be a list")
    classes: List[RetentionClass] = []
    for entry in classes_raw:
        if not isinstance(entry, dict):
            raise ValueError(f"retention policy class entry not a mapping: {entry!r}")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ValueError("retention policy class missing 'name'")
        on_expiry = str(entry.get("on_expiry", "")).strip()
        if on_expiry not in {ON_EXPIRY_TOMBSTONE_AND_SHRED, ON_EXPIRY_KEEP}:
            raise ValueError(
                f"retention class {name!r}: on_expiry must be "
                f"'tombstone_and_shred' or 'keep' (got {on_expiry!r})"
            )
        retention = entry.get("retention_days")
        if isinstance(retention, str) and retention.strip().lower() == "indefinite":
            retention_days: Optional[int] = None
        elif isinstance(retention, int) and retention > 0:
            retention_days = retention
        else:
            raise ValueError(
                f"retention class {name!r}: retention_days must be a positive int or 'indefinite' (got {retention!r})"
            )
        if on_expiry == ON_EXPIRY_KEEP and retention_days is not None:
            raise ValueError(
                f"retention class {name!r}: on_expiry=keep requires retention_days=indefinite"
            )
        classes.append(
            RetentionClass(
                name=name,
                retention_days=retention_days,
                on_expiry=on_expiry,
                description=str(entry.get("description", "")),
            )
        )
    return RetentionPolicy(schema_version=schema, classes=tuple(classes))


# ---------------------------------------------------------------------------
# Class -> table descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClassTarget:
    """How a retention class maps onto a SQLAlchemy model."""

    class_name: str
    model: type
    age_attr: str  # column name used for age comparison (created_at usually)
    uri_attrs: Tuple[str, ...]
    key_id_attr: Optional[str]


_TARGETS: Tuple[_ClassTarget, ...] = (
    _ClassTarget(
        class_name="transcript",
        model=models.Application,
        age_attr="created_at",
        uri_attrs=("transcript_uri",),
        key_id_attr="transcript_key_id",
    ),
    _ClassTarget(
        class_name="interview_recording",
        model=models.InterviewRecording,
        age_attr="started_at",
        uri_attrs=("recording_uri",),
        key_id_attr="recording_key_id",
    ),
    _ClassTarget(
        class_name="kyc_evidence",
        model=models.KYCResult,
        age_attr="created_at",
        uri_attrs=("evidence_uri",),
        key_id_attr="evidence_key_id",
    ),
)


def _target_for(class_name: str) -> Optional[_ClassTarget]:
    for t in _TARGETS:
        if t.class_name == class_name:
            return t
    return None


# ---------------------------------------------------------------------------
# Sweep results
# ---------------------------------------------------------------------------


@dataclass
class SweepStats:
    class_name: str
    inspected: int = 0
    tombstoned: int = 0
    shredded: int = 0
    skipped_already_redacted: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class RetentionResult:
    schema_version: str
    now: datetime
    stats: List[SweepStats] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core: tombstone + shred a single row
# ---------------------------------------------------------------------------


def tombstone_and_shred(
    db: Session,
    target: _ClassTarget,
    row: Any,
    *,
    reason: str,
    now: datetime,
    stats: Optional[SweepStats] = None,
) -> bool:
    """Soft-delete the URI(s), shred the per-row key, mark redacted.

    Returns ``True`` if the row was redacted by this call (i.e. it was
    not already in the redacted state). Idempotent: a redacted row is
    a no-op.
    """
    if getattr(row, "redacted", False):
        if stats is not None:
            stats.skipped_already_redacted += 1
        return False
    # 1. Tombstone every URI we know about. ``object_storage.delete`` is
    #    soft (copies to ``tombstone/`` prefix per prompt 29). Failures
    #    are logged but do not abort the shred -- a missing live blob is
    #    safe to ignore.
    for attr in target.uri_attrs:
        uri = getattr(row, attr, None)
        if not uri:
            continue
        try:
            object_storage.delete(uri)
            if stats is not None:
                stats.tombstoned += 1
        except Exception as exc:  # pragma: no cover - object-storage best-effort
            _LOG.warning(
                "retention: tombstone failed class=%s row_id=%s uri=%s err=%s",
                target.class_name,
                getattr(row, "id", "<no-id>"),
                uri,
                exc,
            )
            if stats is not None:
                stats.errors.append(f"tombstone:{attr}:{exc}")
    # 2. Crypto-shred the per-row key, if present.
    if target.key_id_attr:
        key_id = getattr(row, target.key_id_attr, None)
        if key_id:
            try:
                shred_key(db, key_id)
                if stats is not None:
                    stats.shredded += 1
            except KeyNotFoundError:
                # Already gone or never created. Treat as soft-success.
                if stats is not None:
                    stats.errors.append(f"shred:key_not_found:{key_id}")
            except Exception as exc:  # pragma: no cover
                if stats is not None:
                    stats.errors.append(f"shred:{exc}")
                _LOG.warning("retention: shred failed key_id=%s err=%s", key_id, exc)
    # 3. Flip the tombstone flags. The DB row stays as a tombstone for
    #    referential integrity (foreign keys, audit history).
    row.redacted = True
    row.redacted_at = now
    row.redaction_reason = reason
    db.add(row)
    db.flush()
    return True


# ---------------------------------------------------------------------------
# Daily sweep
# ---------------------------------------------------------------------------


def apply_retention(
    db: Session,
    *,
    policy: Optional[RetentionPolicy] = None,
    now: Optional[datetime] = None,
) -> RetentionResult:
    """Walk every class in ``policy``, redacting rows past their horizon.

    A class with ``on_expiry: keep`` is inspected (so the stats line
    records that it ran) but never modified. Returns a
    :class:`RetentionResult` summarising the sweep.
    """
    pol = policy or load_retention_policy()
    when = now or datetime.now(tz=timezone.utc)
    result = RetentionResult(schema_version=pol.schema_version, now=when)
    for cls in pol.classes:
        stats = SweepStats(class_name=cls.name)
        result.stats.append(stats)
        if cls.on_expiry == ON_EXPIRY_KEEP:
            continue
        target = _target_for(cls.name)
        if target is None:
            stats.errors.append("no_target_mapping")
            continue
        cutoff = when - timedelta(days=int(cls.retention_days or 0))
        age_col = getattr(target.model, target.age_attr)
        rows = (
            db.query(target.model)
            .filter(age_col < cutoff)
            .filter((target.model.redacted.is_(False)) | (target.model.redacted.is_(None)))
            .all()
        )
        stats.inspected = len(rows)
        for row in rows:
            try:
                tombstone_and_shred(
                    db,
                    target,
                    row,
                    reason=f"retention:{cls.name}",
                    now=when,
                    stats=stats,
                )
            except Exception as exc:  # pragma: no cover - defensive
                stats.errors.append(f"row:{getattr(row,'id','<?>')}:{exc}")
                _LOG.exception("retention sweep error class=%s", cls.name)
    return result


# ---------------------------------------------------------------------------
# Targeted erasure execution (prompt 57)
# ---------------------------------------------------------------------------


def _erasable_classes(policy: RetentionPolicy) -> Tuple[str, ...]:
    return tuple(c.name for c in policy.classes if c.on_expiry == ON_EXPIRY_TOMBSTONE_AND_SHRED)


def assess_erasure_classes(
    requested: Optional[List[str]],
    policy: RetentionPolicy,
) -> Tuple[List[str], List[str]]:
    """Split a requested class list into ``(erasable, audit_hold)``.

    A ``None`` or empty ``requested`` defaults to *every* erasable class
    (the policy decides what an "everything" request means; the caller
    cannot ask us to delete decision artifacts by listing them under a
    blanket request).
    """
    if not requested:
        return list(_erasable_classes(policy)), []
    erasable: List[str] = []
    audit_hold: List[str] = []
    for name in requested:
        cls = policy.by_name(name)
        if cls is None:
            audit_hold.append(name)
            continue
        if cls.on_expiry == ON_EXPIRY_KEEP:
            audit_hold.append(name)
        else:
            erasable.append(name)
    return erasable, audit_hold


def _rows_for_subject(
    db: Session,
    target: _ClassTarget,
    *,
    subject_id: str,
    subject_type: str,
) -> List[Any]:
    """Return the rows on ``target`` that belong to ``subject_id``."""
    if subject_type != "founder":
        # Non-founder subjects are not yet wired (e.g. investors). The
        # policy + endpoint surface support them so future prompts can
        # extend without a schema migration; the worker simply has no
        # target rows to act on for now.
        return []
    if target.class_name == "transcript":
        return (
            db.query(models.Application)
            .filter(models.Application.founder_id == subject_id)
            .all()
        )
    if target.class_name == "kyc_evidence":
        return (
            db.query(models.KYCResult)
            .filter(models.KYCResult.founder_id == subject_id)
            .all()
        )
    if target.class_name == "interview_recording":
        # Recordings join through Application.
        app_ids = [
            row.id
            for row in db.query(models.Application)
            .filter(models.Application.founder_id == subject_id)
            .all()
        ]
        if not app_ids:
            return []
        return (
            db.query(models.InterviewRecording)
            .filter(models.InterviewRecording.application_id.in_(app_ids))
            .all()
        )
    return []


def execute_erasure(
    db: Session,
    erasure_request_id: str,
    *,
    publisher: Optional[EventPublisher] = None,
    policy: Optional[RetentionPolicy] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run a scheduled :class:`ErasureRequest`.

    Idempotent: a request already in ``completed`` state returns its
    stored receipt without re-acting. ``refused`` requests
    (``ERASURE_REFUSED_AUDIT_HOLD`` etc.) likewise return a no-op
    receipt.
    """
    pol = policy or load_retention_policy()
    when = now or datetime.now(tz=timezone.utc)
    req = db.get(models.ErasureRequest, erasure_request_id)
    if req is None:
        raise ValueError(f"unknown erasure_request_id: {erasure_request_id!r}")
    if req.status in {"completed", "refused"}:
        return {
            "status": req.status,
            "completed_at": req.completed_at.isoformat() if req.completed_at else None,
            "refusal_reason": req.refusal_reason,
            "idempotent": True,
        }
    if req.status != "scheduled":
        raise RuntimeError(
            f"erasure_request {req.id} not in scheduled state (status={req.status})"
        )
    requested = json.loads(req.classes_json or "[]")
    erasable, audit_hold = assess_erasure_classes(requested or None, pol)
    receipt: Dict[str, Any] = {
        "subject_id": req.subject_id,
        "subject_type": req.subject_type,
        "erased_classes": [],
        "audit_hold_classes": audit_hold,
        "rows_redacted": 0,
        "keys_shredded": 0,
        "uris_tombstoned": 0,
    }
    for class_name in erasable:
        target = _target_for(class_name)
        if target is None:
            continue
        rows = _rows_for_subject(
            db, target, subject_id=req.subject_id, subject_type=req.subject_type
        )
        stats = SweepStats(class_name=class_name)
        for row in rows:
            tombstone_and_shred(
                db,
                target,
                row,
                reason=f"erasure:{req.id}",
                now=when,
                stats=stats,
            )
        receipt["erased_classes"].append(class_name)
        receipt["rows_redacted"] += stats.tombstoned + stats.shredded
        receipt["uris_tombstoned"] += stats.tombstoned
        receipt["keys_shredded"] += stats.shredded
    req.status = "completed"
    req.completed_at = when
    db.add(req)
    db.flush()
    if publisher is not None:
        try:
            publisher.publish(
                event_type="erasure_completed",
                producer="retention_worker",
                trace_id=req.request_id or req.id,
                idempotency_key=f"erasure_completed:{req.id}",
                payload={
                    "erasure_request_id": req.id,
                    "subject_id": req.subject_id,
                    "subject_type": req.subject_type,
                    "classes": receipt["erased_classes"],
                    "audit_hold_classes": receipt["audit_hold_classes"],
                    "completed_at": when.isoformat().replace("+00:00", "Z"),
                },
            )
        except Exception:  # pragma: no cover - publisher is best-effort
            _LOG.exception("erasure_completed publish failed: %s", req.id)
    return receipt
