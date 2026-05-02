"""Conflict-of-interest registry + automated gate (prompt 59).

The fund engine routes ``pass`` decisions to partner meetings via the
scheduler (prompt 54) and to the partner-dashboard finalization flow
(prompt 35). Both paths must consult the conflict-of-interest registry
*before* sending the application to a partner: a partner who has a
prior employment, family, invested, advisor, board, or founder
relationship with the applicant cannot be the one evaluating it.

This module exposes:

* :class:`COIStatus` -- the three terminal states of a check
  (``clear``, ``conflicted``, ``requires_disclosure``). The constants
  are also exposed at module scope (``COI_CLEAR`` etc.) for
  ``decision_policy`` to import without pulling the dataclass.
* :class:`COICheckResult` -- result envelope returned by
  :func:`check_coi`. Carries the resolved status, the evidence list
  (one entry per matched declaration), and the persisted
  :class:`models.COICheck` row id once written.
* :func:`check_coi` -- evaluates the registry against an
  ``(application, partner_id)`` pair, persists a :class:`COICheck`
  row, and returns the result. Conflicted applications are NEVER
  auto-routed back to the same partner (prompt 59 prohibition).
* :func:`route_for_application` -- helper for the meeting-proposal
  path: given a ranked list of candidate partners, returns the first
  one that ``check_coi`` clears (or returns ``None`` so the caller
  falls back to manual review).

The decision-policy gate is intentionally a thin reason-code
contributor: the heavy lifting (matching declarations, writing
audit rows) lives here so the policy module stays pure / portable.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models


__all__ = [
    "COI_CLEAR",
    "COI_CONFLICTED",
    "COI_REQUIRES_DISCLOSURE",
    "HARD_CONFLICT_RELATIONSHIPS",
    "SOFT_CONFLICT_RELATIONSHIPS",
    "VALID_RELATIONSHIPS",
    "VALID_PARTY_KINDS",
    "MIN_OVERRIDE_JUSTIFICATION_LENGTH",
    "COIError",
    "COICheckResult",
    "check_coi",
    "is_clear",
    "route_for_application",
    "record_override",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


COI_CLEAR = "clear"
COI_CONFLICTED = "conflicted"
COI_REQUIRES_DISCLOSURE = "requires_disclosure"


# Hard relationships block routing entirely.
HARD_CONFLICT_RELATIONSHIPS = frozenset(
    {"employed", "family", "invested", "board", "founder"}
)

# Soft relationships permit routing only with a disclosure attached.
SOFT_CONFLICT_RELATIONSHIPS = frozenset({"advisor"})

VALID_RELATIONSHIPS = HARD_CONFLICT_RELATIONSHIPS | SOFT_CONFLICT_RELATIONSHIPS

VALID_PARTY_KINDS = frozenset({"person", "company"})

MIN_OVERRIDE_JUSTIFICATION_LENGTH = 50


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class COIError(ValueError):
    """Validation / state error raised by the COI service.

    ``code`` is a short stable string the router maps onto the JSON
    error envelope ``error.code`` field. ``http_status`` mirrors the
    HTTP status the router should emit -- 422 for validation
    failures, 404 for unknown rows, 409 for conflict, and so on.
    """

    def __init__(self, code: str, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass
class COICheckResult:
    status: str
    application_id: str
    partner_id: str
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    check_id: Optional[str] = None
    disclosure_uri: str = ""
    override_id: Optional[str] = None

    @property
    def coi_clear(self) -> bool:
        """True iff the gate is satisfied (``clear`` or override-attached)."""
        if self.status == COI_CLEAR:
            return True
        if self.status == COI_REQUIRES_DISCLOSURE and (
            self.override_id or self.disclosure_uri
        ):
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "application_id": self.application_id,
            "partner_id": self.partner_id,
            "evidence": list(self.evidence),
            "check_id": self.check_id,
            "disclosure_uri": self.disclosure_uri,
            "override_id": self.override_id,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_clear(result: COICheckResult) -> bool:
    """Convenience predicate matching :attr:`COICheckResult.coi_clear`."""
    return result.coi_clear


def _candidate_party_refs(application: Mapping[str, Any]) -> Tuple[str, ...]:
    """Refs that a partner could declare a conflict against.

    The CRM uses one id space for the founder (person) and another for
    the company they're founding. ``check_coi`` matches a declaration
    against any of: ``founder_id``, the founder's ``email_token``, the
    company id, or the company name (case-insensitive). Empty values
    are filtered out so a row with ``party_id_ref=""`` cannot collide.
    """
    refs: List[str] = []
    for key in (
        "founder_id",
        "founder_user_id",
        "founder_email_token",
        "founder_email",
        "company_id",
        "company_name",
        "party_id_ref",
    ):
        v = application.get(key)
        if v:
            s = str(v).strip()
            if s:
                refs.append(s)
    # Case-insensitive match on company name; preserve the originals
    # too so deterministic-id refs (founder_id) are not mangled.
    lowered = {r.lower() for r in refs}
    return tuple(sorted(set(refs) | lowered))


def _declaration_active_at(
    decl: models.COIDeclaration, when: datetime
) -> bool:
    if decl.status != "active":
        return False
    start = _ensure_utc(decl.period_start)
    if when < start:
        return False
    if decl.period_end is not None:
        end = _ensure_utc(decl.period_end)
        if when >= end:
            return False
    return True


def _classify(declarations: Sequence[models.COIDeclaration]) -> str:
    if not declarations:
        return COI_CLEAR
    has_hard = any(
        d.relationship in HARD_CONFLICT_RELATIONSHIPS for d in declarations
    )
    if has_hard:
        return COI_CONFLICTED
    has_soft = any(
        d.relationship in SOFT_CONFLICT_RELATIONSHIPS for d in declarations
    )
    if has_soft:
        return COI_REQUIRES_DISCLOSURE
    # Unknown relationship value (shouldn't pass declaration validation)
    # treated conservatively as a hard conflict.
    return COI_CONFLICTED


def _evidence_for(
    matches: Sequence[models.COIDeclaration],
) -> List[Dict[str, Any]]:
    return [
        {
            "declaration_id": d.id,
            "party_kind": d.party_kind,
            "party_id_ref": d.party_id_ref,
            "relationship": d.relationship,
            "period_start": _ensure_utc(d.period_start).isoformat(),
            "period_end": (
                _ensure_utc(d.period_end).isoformat()
                if d.period_end is not None
                else None
            ),
        }
        for d in matches
    ]


def _load_active_override(
    db: Session, application_id: str, partner_id: str
) -> Optional[models.COIOverride]:
    return (
        db.query(models.COIOverride)
        .filter(models.COIOverride.application_id == application_id)
        .filter(models.COIOverride.partner_id == partner_id)
        .order_by(models.COIOverride.created_at.desc())
        .first()
    )


# ---------------------------------------------------------------------------
# Public API: check_coi
# ---------------------------------------------------------------------------


def check_coi(
    db: Session,
    application: Mapping[str, Any],
    partner_id: str,
    *,
    now: Optional[datetime] = None,
    persist: bool = True,
    disclosure_uri: str = "",
) -> COICheckResult:
    """Evaluate the COI registry for ``(application, partner_id)``.

    Always writes a :class:`models.COICheck` row when ``persist`` is
    true (default) so the disclosure trail is canonical. The caller
    is responsible for committing the surrounding transaction.

    A pre-existing :class:`models.COIOverride` row covering the same
    ``(application_id, partner_id)`` pair is honored: a hard conflict
    surfaces as ``conflicted`` (the override does NOT auto-clear it
    -- prompt 59 prohibition: do NOT auto-route a conflicted
    application to the same partner) but the resulting ``COICheck``
    carries the override_id so the caller can decide whether to
    proceed under the explicit admin disclosure. ``requires_disclosure``
    + override = clear.
    """
    if not partner_id or not str(partner_id).strip():
        raise COIError("MISSING_PARTNER", "partner_id is required")
    application_id = str(application.get("id") or application.get("application_id") or "").strip()
    if not application_id:
        raise COIError("MISSING_APPLICATION_ID", "application id is required")

    when = _ensure_utc(now or _utc_now())
    partner_id_norm = str(partner_id).strip()

    refs = _candidate_party_refs(application)

    # Empty refs short-circuits to clear -- there is nothing the
    # partner could have declared a conflict against.
    matches: List[models.COIDeclaration] = []
    if refs:
        rows = (
            db.query(models.COIDeclaration)
            .filter(models.COIDeclaration.partner_id == partner_id_norm)
            .filter(models.COIDeclaration.party_id_ref.in_(list(refs)))
            .all()
        )
        matches = [r for r in rows if _declaration_active_at(r, when)]

    status = _classify(matches)
    evidence = _evidence_for(matches)

    override = _load_active_override(db, application_id, partner_id_norm)
    override_id = override.id if override is not None else None

    check_id: Optional[str] = None
    if persist:
        check = models.COICheck(
            id=f"coic_{uuid.uuid4().hex[:24]}",
            application_id=application_id,
            partner_id=partner_id_norm,
            status=status,
            evidence_json=json.dumps(evidence),
            disclosure_uri=disclosure_uri or "",
            override_id=override_id,
            checked_at=when,
        )
        db.add(check)
        db.flush()
        check_id = check.id

    return COICheckResult(
        status=status,
        application_id=application_id,
        partner_id=partner_id_norm,
        evidence=evidence,
        check_id=check_id,
        disclosure_uri=disclosure_uri or "",
        override_id=override_id,
    )


# ---------------------------------------------------------------------------
# Public API: route_for_application
# ---------------------------------------------------------------------------


def route_for_application(
    db: Session,
    application: Mapping[str, Any],
    candidate_partner_ids: Iterable[str],
    *,
    now: Optional[datetime] = None,
) -> Tuple[Optional[str], List[COICheckResult]]:
    """Pick the first conflict-clear partner from ``candidate_partner_ids``.

    Conflicted candidates are skipped (NEVER auto-routed -- prompt 59
    prohibition). ``requires_disclosure`` candidates are skipped too;
    routing them requires an admin override + disclosure handled
    elsewhere. Returns ``(partner_id_or_None, results)`` where
    ``results`` is the per-candidate :class:`COICheckResult` list in
    the same order as the input.
    """
    results: List[COICheckResult] = []
    chosen: Optional[str] = None
    for pid in candidate_partner_ids:
        result = check_coi(db, application, pid, now=now)
        results.append(result)
        if chosen is None and result.coi_clear:
            chosen = result.partner_id
    return chosen, results


# ---------------------------------------------------------------------------
# Public API: record_override
# ---------------------------------------------------------------------------


def record_override(
    db: Session,
    *,
    application_id: str,
    partner_id: str,
    justification: str,
    overridden_by: str,
) -> models.COIOverride:
    """Persist an admin override allowing ``partner_id`` to handle the
    application despite a flagged COI.

    Validates that ``justification`` is at least
    :data:`MIN_OVERRIDE_JUSTIFICATION_LENGTH` characters of meaningful
    prose. Auto-clearing is forbidden (prompt 59 prohibition: every
    override carries a justification ≥ 50 chars and is audited).
    """
    if not str(application_id or "").strip():
        raise COIError("MISSING_APPLICATION_ID", "application_id is required")
    if not str(partner_id or "").strip():
        raise COIError("MISSING_PARTNER", "partner_id is required")
    cleaned = (justification or "").strip()
    if len(cleaned) < MIN_OVERRIDE_JUSTIFICATION_LENGTH:
        raise COIError(
            "JUSTIFICATION_TOO_SHORT",
            (
                "override justification must be at least "
                f"{MIN_OVERRIDE_JUSTIFICATION_LENGTH} characters"
            ),
        )
    if not str(overridden_by or "").strip():
        raise COIError("MISSING_ACTOR", "overridden_by is required")

    row = models.COIOverride(
        id=f"coio_{uuid.uuid4().hex[:24]}",
        application_id=application_id.strip(),
        partner_id=partner_id.strip(),
        justification=cleaned,
        overridden_by=overridden_by.strip(),
        created_at=_utc_now(),
    )
    db.add(row)
    db.flush()
    return row
