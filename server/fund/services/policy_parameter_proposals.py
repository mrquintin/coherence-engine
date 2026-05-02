"""Policy parameter proposal service (prompt 70, Wave 18).

The reserve-allocation optimizer (prompt 70 part 1) emits a *proposal*
parameter set. This module is the persistence + lifecycle layer for
those proposals: it writes a row, runs the deterministic 90-day
backtest replay, and serializes the diff for operator review. Approval
is **explicit** -- this module never auto-promotes to the running
decision policy.

Lifecycle:

::

    proposed --(operator review)--> under_review --(admin approve)--> approved
                                                  \\
                                                   +--(admin reject)--> rejected

Constraints:

* A new proposal cannot be created for a domain that already has a
  proposal (any status) less than ``MIN_PROPOSAL_INTERVAL_DAYS`` old
  -- the rate limit guards against parameter churn between committee
  cycles.
* Approval requires the principal to carry the ``admin`` role.
* The actual decision-policy promotion is a separate, explicit step
  outside this module's surface; approval emits a
  ``policy_parameter_approved.v1`` event so any downstream consumer
  (e.g. an operator runbook executor) can pick the change up.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.event_publisher import EventPublisher


_LOG = logging.getLogger(__name__)


PROPOSAL_STATUS_PROPOSED = "proposed"
PROPOSAL_STATUS_UNDER_REVIEW = "under_review"
PROPOSAL_STATUS_APPROVED = "approved"
PROPOSAL_STATUS_REJECTED = "rejected"

VALID_PROPOSAL_STATUSES = frozenset(
    {
        PROPOSAL_STATUS_PROPOSED,
        PROPOSAL_STATUS_UNDER_REVIEW,
        PROPOSAL_STATUS_APPROVED,
        PROPOSAL_STATUS_REJECTED,
    }
)

PROPOSAL_APPROVED_EVENT_TYPE = "policy_parameter_approved"

# Rate-limit guard: a domain cannot be proposed against more than once
# per calendar month (30 days), matching the spec's stated cadence.
MIN_PROPOSAL_INTERVAL_DAYS = 30


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProposalError(ValueError):
    """Raised on validation / state errors during proposal write or transition.

    The ``code`` attribute is a stable short string the router / CLI
    map onto the user-facing error envelope.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ProposalRateLimited(ProposalError):
    """Raised when a domain has been proposed against too recently."""

    def __init__(self, message: str):
        super().__init__("PROPOSAL_RATE_LIMITED", message)


class ProposalNotFound(ProposalError):
    """Raised when ``proposal_id`` does not match a row."""

    def __init__(self, proposal_id: str):
        super().__init__(
            "PROPOSAL_NOT_FOUND",
            f"proposal {proposal_id!r} does not exist",
        )


class ProposalForbidden(ProposalError):
    """Raised when the caller lacks the ``admin`` role for approval."""

    def __init__(self, message: str = "admin role required"):
        super().__init__("PROPOSAL_FORBIDDEN", message)


class ProposalInvalidTransition(ProposalError):
    """Raised on an illegal status transition."""

    def __init__(self, current: str, requested: str):
        super().__init__(
            "PROPOSAL_INVALID_TRANSITION",
            f"cannot transition proposal from {current!r} to {requested!r}",
        )


# ---------------------------------------------------------------------------
# Result wrappers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalDiff:
    """Operator-facing diff between current and proposed parameters."""

    summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.summary)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _principal_has_admin_role(principal: Any) -> bool:
    """Best-effort role check.

    Accepts any principal-shaped object: a duck-typed object with a
    ``role`` attribute, a mapping with a ``role`` key, or a plain
    string. Unknown shapes fall back to ``False`` -- approval is
    deny-by-default.
    """

    if principal is None:
        return False
    role: Optional[str] = None
    if isinstance(principal, str):
        role = principal
    elif isinstance(principal, Mapping):
        role = str(principal.get("role") or "")
    else:
        role = getattr(principal, "role", None)
    return bool(role and str(role).strip().lower() == "admin")


def _principal_id(principal: Any) -> str:
    if principal is None:
        return ""
    if isinstance(principal, str):
        return principal
    if isinstance(principal, Mapping):
        for key in ("id", "principal_id", "subject", "key_id"):
            if key in principal:
                return str(principal[key])
        return ""
    for attr in ("id", "principal_id", "subject", "key_id"):
        v = getattr(principal, attr, None)
        if v:
            return str(v)
    return ""


def _domains_in(parameters: Mapping[str, Any]) -> Tuple[str, ...]:
    """Extract the domain keys present in a proposed/current parameter set."""

    domains = parameters.get("domains") if isinstance(parameters, Mapping) else None
    if not isinstance(domains, Mapping):
        return ()
    return tuple(sorted(domains.keys()))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PolicyParameterProposalService:
    """Persistence + lifecycle for :class:`models.PolicyParameterProposal`."""

    def __init__(self, db: Session, publisher: Optional[EventPublisher] = None):
        self.db = db
        self.publisher = publisher or EventPublisher(db)

    # ------------------------------------------------------------------
    # Rate-limit
    # ------------------------------------------------------------------

    def _check_rate_limit(
        self,
        *,
        domains: Sequence[str],
        now: datetime,
    ) -> None:
        if not domains:
            return
        cutoff = now - timedelta(days=MIN_PROPOSAL_INTERVAL_DAYS)
        recent = (
            self.db.query(models.PolicyParameterProposal)
            .filter(models.PolicyParameterProposal.created_at >= cutoff)
            .all()
        )
        for row in recent:
            try:
                row_params = json.loads(row.parameters_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                row_params = {}
            # Persisted blob is the canonical OptimizerResult dict --
            # domains live under ``proposed.domains``. Fall back to
            # the top-level shape for hand-built test payloads.
            proposed_block = row_params.get("proposed") if isinstance(row_params, Mapping) else None
            if isinstance(proposed_block, Mapping):
                row_domains = set(_domains_in(proposed_block))
            else:
                row_domains = set(_domains_in(row_params))
            collision = sorted(row_domains.intersection(domains))
            if collision:
                raise ProposalRateLimited(
                    f"proposal already exists within {MIN_PROPOSAL_INTERVAL_DAYS} "
                    f"days for domains {collision} (id={row.id}, created_at="
                    f"{row.created_at.isoformat() if row.created_at else ''})"
                )

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    @staticmethod
    def render_diff(
        current: Mapping[str, Any],
        proposed: Mapping[str, Any],
        backtest_delta: Mapping[str, Any],
    ) -> ProposalDiff:
        """Render a flat operator-facing diff.

        The diff is structured (machine-readable) -- the CLI / router
        is responsible for human formatting. Keys at the top level
        always include ``per_domain``, ``liquidity_reserve``,
        ``pipeline_volume_cap``, and ``backtest`` so consumers can
        rely on the shape.
        """

        per_domain: Dict[str, Dict[str, Any]] = {}
        cur_domains = current.get("domains") if isinstance(current, Mapping) else {}
        prop_domains = proposed.get("domains") if isinstance(proposed, Mapping) else {}
        for d in sorted(set(list(cur_domains or {}) + list(prop_domains or {}))):
            cur = (cur_domains or {}).get(d, {}) or {}
            prop = (prop_domains or {}).get(d, {}) or {}
            per_domain[d] = {
                "CS0_d": {
                    "current": cur.get("CS0_d"),
                    "proposed": prop.get("CS0_d"),
                },
                "alpha_d": {
                    "current": cur.get("alpha_d"),
                    "proposed": prop.get("alpha_d"),
                },
            }
        return ProposalDiff(
            summary={
                "per_domain": per_domain,
                "liquidity_reserve": {
                    "current_fraction": current.get("liquidity_reserve_fraction"),
                    "proposed_fraction": proposed.get("liquidity_reserve_fraction"),
                    "current_target_usd": current.get("liquidity_reserve_target_usd"),
                    "proposed_target_usd": proposed.get("liquidity_reserve_target_usd"),
                },
                "pipeline_volume_cap": {
                    "current": current.get("pipeline_volume_cap"),
                    "proposed": proposed.get("pipeline_volume_cap"),
                },
                "backtest": dict(backtest_delta or {}),
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        proposed_by: str,
        parameters: Mapping[str, Any],
        rationale: str,
        backtest_report_uri: str = "",
        now: Optional[datetime] = None,
    ) -> models.PolicyParameterProposal:
        """Persist a new proposal.

        ``parameters`` is the canonical
        :meth:`reserve_optimizer.OptimizerResult.to_canonical_dict`
        payload (or any structurally-equivalent mapping with
        ``proposed`` / ``current`` / ``delta`` keys). ``rationale`` is
        a free-form operator note (>= 20 chars). The row is inserted in
        ``proposed`` status -- the operator must explicitly transition
        it via :meth:`mark_under_review` / :meth:`approve` /
        :meth:`reject`.
        """

        if not isinstance(parameters, Mapping):
            raise ProposalError(
                "INVALID_PARAMETERS", "parameters must be a JSON-serializable mapping"
            )
        if not isinstance(rationale, str) or len(rationale.strip()) < 20:
            raise ProposalError(
                "RATIONALE_TOO_SHORT", "rationale must be at least 20 characters"
            )
        proposed_section = parameters.get("proposed")
        if not isinstance(proposed_section, Mapping):
            raise ProposalError(
                "INVALID_PARAMETERS",
                "parameters.proposed must be present and a mapping",
            )

        domains = _domains_in(proposed_section)
        if not domains:
            raise ProposalError(
                "INVALID_PARAMETERS",
                "parameters.proposed.domains must contain at least one domain",
            )

        ts = now or _utc_now()
        self._check_rate_limit(domains=domains, now=ts)

        row = models.PolicyParameterProposal(
            id=str(uuid.uuid4()),
            proposed_by=str(proposed_by or ""),
            parameters_json=json.dumps(parameters, sort_keys=True, separators=(",", ":")),
            rationale=str(rationale.strip()),
            backtest_report_uri=str(backtest_report_uri or ""),
            status=PROPOSAL_STATUS_PROPOSED,
            created_at=ts,
            updated_at=ts,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def get(self, proposal_id: str) -> models.PolicyParameterProposal:
        row = (
            self.db.query(models.PolicyParameterProposal)
            .filter(models.PolicyParameterProposal.id == str(proposal_id))
            .one_or_none()
        )
        if row is None:
            raise ProposalNotFound(str(proposal_id))
        return row

    def list_recent(
        self, *, limit: int = 20
    ) -> list[models.PolicyParameterProposal]:
        rows: list[models.PolicyParameterProposal] = (
            self.db.query(models.PolicyParameterProposal)
            .order_by(models.PolicyParameterProposal.created_at.desc())
            .limit(int(limit))
            .all()
        )
        return rows

    def render_review(self, proposal_id: str) -> Dict[str, Any]:
        """Return a serializable review payload (diff + status)."""

        row = self.get(proposal_id)
        try:
            params = json.loads(row.parameters_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            params = {}
        diff = self.render_diff(
            params.get("current") or {},
            params.get("proposed") or {},
            params.get("delta") or {},
        )
        return {
            "id": row.id,
            "status": row.status,
            "proposed_by": row.proposed_by,
            "rationale": row.rationale,
            "backtest_report_uri": row.backtest_report_uri or "",
            "created_at": (
                row.created_at.isoformat().replace("+00:00", "Z")
                if row.created_at
                else ""
            ),
            "approved_by": row.approved_by or "",
            "approved_at": (
                row.approved_at.isoformat().replace("+00:00", "Z")
                if row.approved_at
                else ""
            ),
            "diff": diff.to_dict(),
        }

    def mark_under_review(self, proposal_id: str) -> models.PolicyParameterProposal:
        row = self.get(proposal_id)
        if row.status != PROPOSAL_STATUS_PROPOSED:
            raise ProposalInvalidTransition(row.status, PROPOSAL_STATUS_UNDER_REVIEW)
        row.status = PROPOSAL_STATUS_UNDER_REVIEW
        row.updated_at = _utc_now()
        self.db.flush()
        return row

    def approve(
        self,
        proposal_id: str,
        *,
        principal: Any,
        trace_id: Optional[str] = None,
    ) -> models.PolicyParameterProposal:
        """Approve a proposal. Admin role required.

        Emits ``policy_parameter_approved.v1``. **Does NOT promote**
        the running decision policy -- the operator runbook is
        responsible for the explicit promotion step.
        """

        if not _principal_has_admin_role(principal):
            raise ProposalForbidden()
        row = self.get(proposal_id)
        if row.status not in (PROPOSAL_STATUS_PROPOSED, PROPOSAL_STATUS_UNDER_REVIEW):
            raise ProposalInvalidTransition(row.status, PROPOSAL_STATUS_APPROVED)
        actor = _principal_id(principal) or "admin"
        now = _utc_now()
        row.status = PROPOSAL_STATUS_APPROVED
        row.approved_by = actor
        row.approved_at = now
        row.updated_at = now
        self.db.flush()

        try:
            params = json.loads(row.parameters_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            params = {}
        proposed_section = params.get("proposed") or {}
        payload = {
            "proposal_id": row.id,
            "approved_by": actor,
            "approved_at": row.approved_at.isoformat().replace("+00:00", "Z")
            if row.approved_at
            else "",
            "parameter_set_digest": _hash_payload(proposed_section),
            "domains": list(_domains_in(proposed_section)),
            "rationale": row.rationale,
        }
        try:
            self.publisher.publish(
                event_type=PROPOSAL_APPROVED_EVENT_TYPE,
                producer="policy_parameter_proposals",
                trace_id=trace_id or str(uuid.uuid4()),
                idempotency_key=f"proposal:{row.id}:approved",
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.error(
                "policy_parameter_approved_event_publish_failed proposal_id=%s err=%s",
                row.id,
                exc,
            )
            raise
        return row

    def reject(
        self,
        proposal_id: str,
        *,
        principal: Any,
        reason: str = "",
    ) -> models.PolicyParameterProposal:
        if not _principal_has_admin_role(principal):
            raise ProposalForbidden()
        row = self.get(proposal_id)
        if row.status not in (PROPOSAL_STATUS_PROPOSED, PROPOSAL_STATUS_UNDER_REVIEW):
            raise ProposalInvalidTransition(row.status, PROPOSAL_STATUS_REJECTED)
        now = _utc_now()
        row.status = PROPOSAL_STATUS_REJECTED
        row.approved_by = _principal_id(principal) or "admin"
        row.approved_at = now
        row.updated_at = now
        if reason:
            existing = row.rationale or ""
            row.rationale = f"{existing}\n\nrejected: {reason.strip()}".strip()
        self.db.flush()
        return row


def _hash_payload(payload: Mapping[str, Any]) -> str:
    import hashlib

    canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "MIN_PROPOSAL_INTERVAL_DAYS",
    "PROPOSAL_APPROVED_EVENT_TYPE",
    "PROPOSAL_STATUS_APPROVED",
    "PROPOSAL_STATUS_PROPOSED",
    "PROPOSAL_STATUS_REJECTED",
    "PROPOSAL_STATUS_UNDER_REVIEW",
    "PolicyParameterProposalService",
    "ProposalDiff",
    "ProposalError",
    "ProposalForbidden",
    "ProposalInvalidTransition",
    "ProposalNotFound",
    "ProposalRateLimited",
    "VALID_PROPOSAL_STATUSES",
]
