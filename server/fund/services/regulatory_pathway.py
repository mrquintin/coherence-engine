"""Regulatory pathway classification + gating (prompt 56).

Classifies each application against the operator-configured registry
of securities pathways (Reg D 506(b), Reg D 506(c), Reg CF, Reg S)
and returns a :class:`PathwayMatch` whose ``status`` drives the
``regulatory_pathway_clear`` gate in :mod:`decision_policy`.

This module DOES NOT pick a pathway autonomously and DOES NOT
provide legal advice. The pathway registry at
``data/governed/regulatory_pathways.yaml`` is owned by the operator's
licensed securities counsel; the classifier enforces what counsel
has configured. Ambiguity (zero or multiple matches) routes to
``manual_review``; never silently defaults to a pathway.

Match algorithm (deterministic, no fallback):

1. Filter the registry by founder jurisdiction (``US`` vs ``non_US``).
2. Filter by advertising mode (``permitted`` | ``prohibited``).
   Applications that have not declared an advertising mode keep the
   full candidate set; multiple matches then surface as ``ambiguous``.
3. If the candidate set is empty or contains > 1 pathway, return
   ``ambiguous``.
4. If exactly one candidate matches but the pathway's
   ``investor_requirement`` is not satisfied (e.g. 506(c) without
   ``investor_verification_status == "verified"``) -> ``unclear``.
5. If counsel signoff is required and either absent or older than
   :data:`PathwayRegistry.counsel_signoff_ttl_days` -> ``unclear``.
6. Otherwise -> ``clear``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple


__all__ = [
    "Pathway",
    "PathwayRegistry",
    "PathwayMatch",
    "REGULATORY_PATHWAY_SCHEMA_VERSION",
    "load_pathway_registry",
    "classify",
    "regulatory_pathway_clear",
]


REGULATORY_PATHWAY_SCHEMA_VERSION = "regulatory-pathways-v1"

_DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "governed"
    / "regulatory_pathways.yaml"
)
_DEFAULT_SIGNOFF_TTL_DAYS = 90

_ALLOWED_INVESTOR_REQS = frozenset(
    {"accredited_verified", "self_certified", "none"}
)
_ALLOWED_ADVERTISING = frozenset({"permitted", "prohibited"})
_ALLOWED_JURISDICTIONS = frozenset({"US", "non_US"})


class RegulatoryPathwayError(Exception):
    """Raised on malformed registry input."""


@dataclass(frozen=True)
class Pathway:
    id: str
    jurisdiction: str
    investor_requirement: str
    advertising: str
    max_investors: Optional[int]
    integration_window_days: int
    counsel_signoff_required: bool
    counsel_signoff_at: Optional[datetime]
    counsel_signoff_by: str


@dataclass(frozen=True)
class PathwayRegistry:
    schema_version: str
    pathways: Tuple[Pathway, ...]
    counsel_signoff_ttl_days: int = _DEFAULT_SIGNOFF_TTL_DAYS

    def by_id(self, pathway_id: str) -> Optional[Pathway]:
        for p in self.pathways:
            if p.id == pathway_id:
                return p
        return None


@dataclass(frozen=True)
class PathwayMatch:
    """Result of :func:`classify`.

    ``status`` is one of ``clear`` | ``unclear`` | ``ambiguous``.
    ``pathway_id`` is set when a single candidate matched (even if
    later rejected for missing prerequisite). ``reason`` is the
    decision-policy reason code or, for ``clear``, an empty string.
    """

    status: str
    pathway_id: Optional[str]
    reason: str
    candidates: Tuple[str, ...] = field(default_factory=tuple)


def _coerce_jurisdiction(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    upper = s.upper()
    if upper == "US" or upper == "USA":
        return "US"
    if upper == "NON_US" or upper.lower() == "non_us":
        return "non_US"
    return "non_US"


def _coerce_advertising(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in {"permitted", "general_solicitation", "general"}:
        return "permitted"
    if s in {"prohibited", "private", "no_solicitation"}:
        return "prohibited"
    return "unspecified"


def _coerce_signoff_at(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise RegulatoryPathwayError(
            f"invalid_counsel_signoff_at:{value!r}"
        ) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_pathway(raw: Mapping[str, Any]) -> Pathway:
    pid = str(raw.get("id") or "").strip()
    if not pid:
        raise RegulatoryPathwayError("pathway_missing_id")
    jurisdiction = str(raw.get("jurisdiction") or "").strip()
    if jurisdiction not in _ALLOWED_JURISDICTIONS:
        raise RegulatoryPathwayError(
            f"pathway_jurisdiction_invalid:{pid}:{jurisdiction!r}"
        )
    inv_req = str(raw.get("investor_requirement") or "").strip().lower()
    if inv_req not in _ALLOWED_INVESTOR_REQS:
        raise RegulatoryPathwayError(
            f"pathway_investor_requirement_invalid:{pid}:{inv_req!r}"
        )
    adv = str(raw.get("advertising") or "").strip().lower()
    if adv not in _ALLOWED_ADVERTISING:
        raise RegulatoryPathwayError(
            f"pathway_advertising_invalid:{pid}:{adv!r}"
        )
    max_inv_raw = raw.get("max_investors")
    max_investors: Optional[int]
    if max_inv_raw is None:
        max_investors = None
    else:
        max_investors = int(max_inv_raw)
        if max_investors < 0:
            raise RegulatoryPathwayError(
                f"pathway_max_investors_negative:{pid}"
            )
    integration_days = int(raw.get("integration_window_days") or 0)
    counsel_required = bool(raw.get("counsel_signoff_required", False))
    return Pathway(
        id=pid,
        jurisdiction=jurisdiction,
        investor_requirement=inv_req,
        advertising=adv,
        max_investors=max_investors,
        integration_window_days=integration_days,
        counsel_signoff_required=counsel_required,
        counsel_signoff_at=_coerce_signoff_at(raw.get("counsel_signoff_at")),
        counsel_signoff_by=str(raw.get("counsel_signoff_by") or "").strip(),
    )


def load_pathway_registry(
    path: Optional[Path | str] = None,
) -> PathwayRegistry:
    """Load and validate the YAML registry.

    Lazily imports ``yaml`` so the module is importable in
    environments where PyYAML is absent (the default registry path
    falls back to a minimal hard-coded registry only when explicitly
    requested via the env var ``REGULATORY_PATHWAY_ALLOW_FALLBACK=1``;
    otherwise we raise so misconfiguration is loud).
    """
    target = Path(path) if path is not None else _DEFAULT_REGISTRY_PATH
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dep
        raise RegulatoryPathwayError(
            "pyyaml_required_for_regulatory_pathways"
        ) from exc

    if not target.exists():
        raise RegulatoryPathwayError(
            f"regulatory_pathways_file_missing:{target}"
        )

    with target.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, Mapping):
        raise RegulatoryPathwayError("regulatory_pathways_must_be_mapping")

    schema = str(raw.get("schema_version") or "").strip()
    if schema != REGULATORY_PATHWAY_SCHEMA_VERSION:
        raise RegulatoryPathwayError(
            f"regulatory_pathways_schema_mismatch:"
            f"want={REGULATORY_PATHWAY_SCHEMA_VERSION} got={schema!r}"
        )

    ttl = int(
        raw.get("counsel_signoff_ttl_days") or _DEFAULT_SIGNOFF_TTL_DAYS
    )
    if ttl <= 0:
        raise RegulatoryPathwayError(
            "counsel_signoff_ttl_days_must_be_positive"
        )

    pathways_raw = raw.get("pathways") or []
    if not isinstance(pathways_raw, list):
        raise RegulatoryPathwayError("pathways_must_be_list")

    pathways = tuple(_parse_pathway(p) for p in pathways_raw)

    seen_ids: set[str] = set()
    for p in pathways:
        if p.id in seen_ids:
            raise RegulatoryPathwayError(
                f"duplicate_pathway_id:{p.id}"
            )
        seen_ids.add(p.id)

    return PathwayRegistry(
        schema_version=schema,
        pathways=pathways,
        counsel_signoff_ttl_days=ttl,
    )


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(tz=timezone.utc)


def _extract_jurisdiction(application: Mapping[str, Any]) -> str:
    raw = (
        application.get("regulatory_jurisdiction")
        or application.get("founder_country")
        or application.get("country")
    )
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    upper = s.upper()
    if upper in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        return "US"
    return "non_US"


def _extract_advertising(application: Mapping[str, Any]) -> str:
    return _coerce_advertising(
        application.get("advertising_mode")
        or application.get("solicitation_mode")
    )


def _extract_investor_status(application: Mapping[str, Any]) -> str:
    raw = (
        application.get("investor_verification_status")
        or application.get("investor_status")
        or ""
    )
    return str(raw).strip().lower()


def _signoff_fresh(
    pathway: Pathway, ttl_days: int, now: datetime
) -> bool:
    if not pathway.counsel_signoff_required:
        return True
    if pathway.counsel_signoff_at is None:
        return False
    if not pathway.counsel_signoff_by:
        return False
    age = now - pathway.counsel_signoff_at
    return age <= timedelta(days=ttl_days)


def _investor_requirement_satisfied(
    pathway: Pathway, application: Mapping[str, Any]
) -> bool:
    req = pathway.investor_requirement
    if req == "none":
        return True
    status = _extract_investor_status(application)
    if req == "accredited_verified":
        return status == "verified"
    if req == "self_certified":
        return status in {"verified", "self_certified", "self-attested"}
    return False


def classify(
    application: Mapping[str, Any],
    *,
    registry: PathwayRegistry,
    now: Optional[datetime] = None,
) -> PathwayMatch:
    """Classify an application against the operator-configured registry.

    See module docstring for the deterministic match algorithm. This
    function does NOT mutate ``application`` and does NOT pick a
    pathway autonomously: when the application has not declared an
    advertising mode (or jurisdiction) and multiple pathways match,
    the result is ``ambiguous``.
    """
    juris = _extract_jurisdiction(application)
    advertising = _extract_advertising(application)
    current = _now(now)

    if not juris:
        return PathwayMatch(
            status="ambiguous",
            pathway_id=None,
            reason="REGULATORY_PATHWAY_AMBIGUOUS",
            candidates=tuple(),
        )

    candidates: List[Pathway] = [
        p for p in registry.pathways if p.jurisdiction == juris
    ]

    if advertising != "unspecified":
        candidates = [p for p in candidates if p.advertising == advertising]

    candidate_ids = tuple(p.id for p in candidates)

    if len(candidates) == 0:
        return PathwayMatch(
            status="ambiguous",
            pathway_id=None,
            reason="REGULATORY_PATHWAY_AMBIGUOUS",
            candidates=candidate_ids,
        )
    if len(candidates) > 1:
        return PathwayMatch(
            status="ambiguous",
            pathway_id=None,
            reason="REGULATORY_PATHWAY_AMBIGUOUS",
            candidates=candidate_ids,
        )

    chosen = candidates[0]

    if not _investor_requirement_satisfied(chosen, application):
        return PathwayMatch(
            status="unclear",
            pathway_id=chosen.id,
            reason="REGULATORY_PATHWAY_UNCLEAR",
            candidates=candidate_ids,
        )

    if not _signoff_fresh(
        chosen, registry.counsel_signoff_ttl_days, current
    ):
        return PathwayMatch(
            status="unclear",
            pathway_id=chosen.id,
            reason="REGULATORY_PATHWAY_UNCLEAR",
            candidates=candidate_ids,
        )

    return PathwayMatch(
        status="clear",
        pathway_id=chosen.id,
        reason="",
        candidates=candidate_ids,
    )


def regulatory_pathway_clear(
    application: Mapping[str, Any],
    *,
    registry: PathwayRegistry,
    now: Optional[datetime] = None,
) -> bool:
    """Boolean form of :func:`classify` for ``decision_policy``.

    Returns ``True`` iff the classifier resolved to a single pathway
    and all gating prerequisites (investor verification, counsel
    signoff freshness) are satisfied.
    """
    return classify(application, registry=registry, now=now).status == "clear"
