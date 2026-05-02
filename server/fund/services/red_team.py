"""Red-team adversarial harness for the scoring + decision pipeline.

This module provides a deterministic, offline harness that replays a curated
corpus of labeled adversarial pitches through the production
``ScoringService`` + ``DecisionPolicyService`` chain (the same chain used by
``ApplicationService.process_next_scoring_job``), compares each case's
canonical verdict against the fixture's ground-truth label, and aggregates a
confusion matrix + false-pass / false-reject / false-review counters.

The harness is intended as a regression gate (prompt 13 of 20, Wave 5):

* Ground-truth labels live in ``tests/adversarial/labels.json`` and identify
  the human-expected verdict for each fixture (``pass``, ``reject``, or
  ``manual_review``).
* The harness emits a deterministic :class:`RedTeamReport` (byte-identical
  across runs given identical fixtures and labels) so downstream tooling can
  diff output files.
* The test file ``tests/test_red_team_harness.py`` pins the confusion matrix
  so any drift in the scoring / decision-policy logic immediately fails a
  loud regression test with a clear diff.

Design notes and prohibitions (prompt 13):

* **Fully offline.** No network I/O, no live portfolio reads, no external
  dependencies. The scoring stack is instantiated with the deterministic
  ``tfidf`` embedder + ``heuristic`` contradiction backend already used by
  :class:`~coherence_engine.server.fund.services.scoring.ScoringService`.
* **Synthetic fixtures.** Fixtures MUST NOT identify any real founder or
  company; fixture authoring guidance is in ``docs/specs/red_team_harness.md``.
* **No mutation of governed data.** The harness never writes to
  ``data/governed/*`` and never touches the live production DB.
* **Canonical verdict vocabulary.** The policy's internal vocabulary
  (``fail``) is mapped to the canonical external verdict (``reject``)
  before comparison against labels, matching the translation applied by
  :class:`~coherence_engine.server.fund.services.application_service.ApplicationService`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from coherence_engine.server.fund.services.decision_policy import (
    DECISION_POLICY_VERSION,
    DecisionPolicyService,
)
from coherence_engine.server.fund.services.scoring import ScoringService


__all__ = [
    "CANONICAL_VERDICTS",
    "RedTeamError",
    "RedTeamCaseResult",
    "RedTeamReport",
    "canonicalize_verdict",
    "load_labels",
    "load_fixtures",
    "run_adversarial_suite",
]


# ---------------------------------------------------------------------------
# Vocabulary + helpers
# ---------------------------------------------------------------------------


CANONICAL_VERDICTS: Tuple[str, ...] = ("pass", "reject", "manual_review")
_CANONICAL_SET = frozenset(CANONICAL_VERDICTS)
_INTERNAL_TO_CANONICAL = {"fail": "reject"}


class RedTeamError(Exception):
    """Raised for fixture-loading / schema errors surfaced to the CLI.

    The CLI exits with code ``2`` on any such error (see ``cli.py``). The
    exception message is deliberately operator-readable — it is the
    single signal the CLI writes to ``stderr``.
    """


def canonicalize_verdict(decision: str) -> str:
    """Map the decision-policy internal vocabulary to the external canon.

    The decision policy emits ``fail`` for hard-reject outcomes; the
    canonical external verdict (schema-version-pinned in
    ``decision_issued.v1.json`` and consumed by the rest of the
    platform) is ``reject``. Any other value (``pass``,
    ``manual_review``) passes through unchanged.
    """
    value = str(decision)
    return _INTERNAL_TO_CANONICAL.get(value, value)


# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------


_REQUIRED_FIXTURE_FIELDS: Tuple[str, ...] = (
    "id",
    "one_liner",
    "transcript_text",
    "use_of_funds_summary",
    "requested_check_usd",
    "domain_primary",
    "compliance_status",
    "category",
)

_ALLOWED_CATEGORIES: Tuple[str, ...] = (
    "incoherent",
    "coherent_evidenced",
    "template_echo",
    "borderline",
)


def load_labels(labels_path: Path) -> Dict[str, Dict[str, str]]:
    """Load ``labels.json`` mapping fixture basename -> {expected_verdict, rationale}.

    Raises :class:`RedTeamError` with an operator-readable message on any
    schema / file-format problem.
    """
    if not labels_path.exists():
        raise RedTeamError(f"labels_file_not_found: {labels_path}")
    try:
        raw = json.loads(labels_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RedTeamError(f"labels_invalid_json: {labels_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RedTeamError(f"labels_must_be_object: {labels_path}")
    out: Dict[str, Dict[str, str]] = {}
    for basename, entry in raw.items():
        if not isinstance(entry, dict):
            raise RedTeamError(f"labels_entry_not_object: {basename}")
        expected = entry.get("expected_verdict")
        if expected not in _CANONICAL_SET:
            raise RedTeamError(
                f"labels_entry_bad_verdict: {basename}: "
                f"expected_verdict={expected!r}; "
                f"must be one of {sorted(_CANONICAL_SET)}"
            )
        out[str(basename)] = {
            "expected_verdict": str(expected),
            "rationale": str(entry.get("rationale", "")),
        }
    return out


def _validate_fixture(path: Path, payload: Mapping[str, Any]) -> None:
    missing = [k for k in _REQUIRED_FIXTURE_FIELDS if k not in payload]
    if missing:
        raise RedTeamError(
            f"fixture_missing_fields: {path.name}: {missing}"
        )
    category = payload["category"]
    if category not in _ALLOWED_CATEGORIES:
        raise RedTeamError(
            f"fixture_bad_category: {path.name}: category={category!r}; "
            f"must be one of {list(_ALLOWED_CATEGORIES)}"
        )
    try:
        float(payload["requested_check_usd"])
    except (TypeError, ValueError) as exc:
        raise RedTeamError(
            f"fixture_bad_requested_check_usd: {path.name}: {exc}"
        ) from exc


def load_fixtures(fixtures_dir: Path) -> List[Dict[str, Any]]:
    """Load and validate every ``*.json`` fixture in ``fixtures_dir``.

    Returns the fixtures ordered by filename (deterministic) with a
    ``"_path"`` key carrying the resolved :class:`~pathlib.Path` so the
    caller can correlate with ``labels.json`` keys.

    Raises :class:`RedTeamError` on any IO / schema error.
    """
    if not fixtures_dir.exists() or not fixtures_dir.is_dir():
        raise RedTeamError(f"fixtures_dir_not_found: {fixtures_dir}")
    fixtures: List[Dict[str, Any]] = []
    paths = sorted(fixtures_dir.glob("*.json"))
    if not paths:
        raise RedTeamError(f"fixtures_dir_empty: {fixtures_dir}")
    for p in paths:
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RedTeamError(f"fixture_invalid_json: {p.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RedTeamError(f"fixture_not_object: {p.name}")
        _validate_fixture(p, payload)
        payload["_path"] = p
        fixtures.append(payload)
    return fixtures


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RedTeamCaseResult:
    """Per-fixture replay result.

    All fields are rounded / cast to JSON-serializable primitives so the
    parent :class:`RedTeamReport` can be emitted byte-identically across
    runs.
    """

    fixture_id: str
    fixture_filename: str
    category: str
    expected_verdict: str
    actual_verdict: str
    matches_label: bool
    coherence_superiority: float
    coherence_superiority_ci95_lower: float
    coherence_superiority_ci95_upper: float
    anti_gaming_score: float
    anti_gaming_flags: Tuple[str, ...]
    transcript_quality_score: float
    failed_gate_codes: Tuple[str, ...]
    threshold_required: float
    coherence_observed: float
    margin: float
    rationale: str

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Canonical dict form with sorted flag/code tuples for determinism."""
        return {
            "fixture_id": self.fixture_id,
            "fixture_filename": self.fixture_filename,
            "category": self.category,
            "expected_verdict": self.expected_verdict,
            "actual_verdict": self.actual_verdict,
            "matches_label": bool(self.matches_label),
            "coherence_superiority": round(float(self.coherence_superiority), 6),
            "coherence_superiority_ci95": {
                "lower": round(float(self.coherence_superiority_ci95_lower), 6),
                "upper": round(float(self.coherence_superiority_ci95_upper), 6),
            },
            "anti_gaming_score": round(float(self.anti_gaming_score), 6),
            "anti_gaming_flags": list(self.anti_gaming_flags),
            "transcript_quality_score": round(
                float(self.transcript_quality_score), 6
            ),
            "failed_gate_codes": list(self.failed_gate_codes),
            "threshold_required": round(float(self.threshold_required), 6),
            "coherence_observed": round(float(self.coherence_observed), 6),
            "margin": round(float(self.margin), 6),
            "rationale": str(self.rationale),
        }


@dataclass(frozen=True)
class RedTeamReport:
    """Aggregate outcome of running the adversarial suite.

    ``confusion_matrix`` is a nested dict keyed by
    ``expected_verdict -> actual_verdict -> count``, with every cell in
    ``CANONICAL_VERDICTS x CANONICAL_VERDICTS`` populated (zero-filled)
    so downstream diffing is easy.

    ``counts`` records the three headline error modes callers typically
    track:

    * ``false_pass`` — label is ``reject`` or ``manual_review`` but the
      pipeline returned ``pass``.
    * ``false_reject`` — label is ``pass`` or ``manual_review`` but the
      pipeline returned ``reject``.
    * ``false_review`` — label is ``pass`` or ``reject`` but the
      pipeline returned ``manual_review``.
    """

    schema_version: str
    decision_policy_version: str
    total_cases: int
    matches: int
    mismatches: int
    counts: Dict[str, int]
    confusion_matrix: Dict[str, Dict[str, int]]
    cases: List[RedTeamCaseResult] = field(default_factory=list)

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_policy_version": self.decision_policy_version,
            "total_cases": int(self.total_cases),
            "matches": int(self.matches),
            "mismatches": int(self.mismatches),
            "counts": {
                k: int(self.counts[k]) for k in sorted(self.counts)
            },
            "confusion_matrix": {
                exp: {
                    act: int(self.confusion_matrix[exp][act])
                    for act in CANONICAL_VERDICTS
                }
                for exp in CANONICAL_VERDICTS
            },
            "cases": [c.to_canonical_dict() for c in self.cases],
        }

    def to_canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_canonical_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


# ---------------------------------------------------------------------------
# Pipeline replay
# ---------------------------------------------------------------------------


class _SyntheticApplication:
    """Minimal application stand-in used by ``ScoringService.score_application``.

    The scoring + decision path reads a small, documented set of
    attributes off the application object; see ``scoring.py`` and
    ``decision_policy.py``. This class intentionally mirrors that shape
    without requiring any DB persistence.
    """

    __slots__ = (
        "compliance_status",
        "domain_primary",
        "founder_id",
        "id",
        "one_liner",
        "preferred_channel",
        "requested_check_usd",
        "transcript_text",
        "transcript_uri",
        "use_of_funds_summary",
    )

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        fixture_id = str(fixture["id"])
        self.id = f"app_redteam_{fixture_id}"
        self.founder_id = f"fnd_redteam_{fixture_id}"
        self.one_liner = str(fixture.get("one_liner", "") or "")
        self.requested_check_usd = int(float(fixture.get("requested_check_usd", 0) or 0))
        self.use_of_funds_summary = str(fixture.get("use_of_funds_summary", "") or "")
        self.preferred_channel = "web_voice"
        self.transcript_text = str(fixture.get("transcript_text", "") or "")
        self.transcript_uri = ""
        self.domain_primary = str(fixture.get("domain_primary", "market_economics"))
        self.compliance_status = str(fixture.get("compliance_status", "clear"))


def _empty_confusion_matrix() -> Dict[str, Dict[str, int]]:
    return {
        expected: {actual: 0 for actual in CANONICAL_VERDICTS}
        for expected in CANONICAL_VERDICTS
    }


def _classify_error(expected: str, actual: str) -> Optional[str]:
    """Return the error-category key for a mismatched (expected, actual)."""
    if expected == actual:
        return None
    if actual == "pass":
        return "false_pass"
    if actual == "reject":
        return "false_reject"
    if actual == "manual_review":
        return "false_review"
    return None


def run_adversarial_suite(
    fixtures_dir: Path,
    labels_path: Path,
    policy_version: str = DECISION_POLICY_VERSION,
    *,
    scoring: Optional[ScoringService] = None,
    policy: Optional[DecisionPolicyService] = None,
) -> RedTeamReport:
    """Replay every fixture through scoring + decision policy and score it.

    Args:
        fixtures_dir: Directory containing ``*.json`` fixtures.
        labels_path: Path to ``labels.json``.
        policy_version: Pinned decision-policy version string. Raised
            as :class:`RedTeamError` if it does not match the running
            ``DECISION_POLICY_VERSION`` (defensive regression check so a
            stale pin in CI surfaces immediately).
        scoring: Optional injected :class:`ScoringService` (primarily for
            tests). Defaults to a fresh instance per call.
        policy: Optional injected :class:`DecisionPolicyService`.

    Returns:
        A fully-populated :class:`RedTeamReport`.

    Raises:
        RedTeamError: for any fixture/label IO or schema failure, or a
            policy-version-pin mismatch. (The CLI converts these into
            exit code ``2``.)
    """
    if str(policy_version) != DECISION_POLICY_VERSION:
        raise RedTeamError(
            "decision_policy_version_pin_mismatch: "
            f"requested={policy_version!r} actual={DECISION_POLICY_VERSION!r}"
        )

    fixtures = load_fixtures(Path(fixtures_dir))
    labels = load_labels(Path(labels_path))

    scoring_svc = scoring or ScoringService()
    policy_svc = policy or DecisionPolicyService()

    cases: List[RedTeamCaseResult] = []
    counts: Dict[str, int] = {
        "false_pass": 0,
        "false_reject": 0,
        "false_review": 0,
    }
    confusion: Dict[str, Dict[str, int]] = _empty_confusion_matrix()
    matches = 0

    # Fixture ordering is already deterministic (sorted by filename in
    # ``load_fixtures``); we re-affirm it here so the emitted report is
    # byte-stable regardless of OS directory iteration order.
    for fixture in sorted(fixtures, key=lambda f: f["_path"].name):
        path: Path = fixture["_path"]
        basename = path.name
        label_entry = labels.get(basename)
        if label_entry is None:
            raise RedTeamError(f"fixture_missing_label: {basename}")
        expected = str(label_entry["expected_verdict"])
        rationale = str(label_entry.get("rationale", ""))

        app = _SyntheticApplication(fixture)
        score_record = scoring_svc.score_application(application=app)
        decision_payload = policy_svc.evaluate(
            application={
                "domain_primary": app.domain_primary,
                "requested_check_usd": app.requested_check_usd,
                "compliance_status": app.compliance_status,
            },
            score_record=score_record,
        )
        actual = canonicalize_verdict(str(decision_payload["decision"]))
        if actual not in _CANONICAL_SET:
            # Defensive: a brand-new policy state should fail loudly
            # rather than silently collapse into the ``false_*`` buckets.
            raise RedTeamError(
                f"unknown_actual_verdict: fixture={basename} "
                f"decision={decision_payload.get('decision')!r}"
            )

        ci = score_record.get("coherence_superiority_ci95") or {}
        failed_gate_codes = tuple(
            sorted(
                {
                    str(g.get("reason_code", ""))
                    for g in (decision_payload.get("failed_gates") or [])
                    if g.get("reason_code")
                }
            )
        )
        flags = tuple(sorted(score_record.get("anti_gaming_flags") or []))

        case = RedTeamCaseResult(
            fixture_id=str(fixture["id"]),
            fixture_filename=basename,
            category=str(fixture["category"]),
            expected_verdict=expected,
            actual_verdict=actual,
            matches_label=(expected == actual),
            coherence_superiority=float(score_record.get("coherence_superiority", 0.0)),
            coherence_superiority_ci95_lower=float(ci.get("lower", 0.0)),
            coherence_superiority_ci95_upper=float(ci.get("upper", 0.0)),
            anti_gaming_score=float(score_record.get("anti_gaming_score", 0.0)),
            anti_gaming_flags=flags,
            transcript_quality_score=float(
                score_record.get("transcript_quality_score", 0.0)
            ),
            failed_gate_codes=failed_gate_codes,
            threshold_required=float(decision_payload.get("threshold_required", 0.0)),
            coherence_observed=float(decision_payload.get("coherence_observed", 0.0)),
            margin=float(decision_payload.get("margin", 0.0)),
            rationale=rationale,
        )
        cases.append(case)

        confusion[expected][actual] += 1
        if expected == actual:
            matches += 1
        else:
            err = _classify_error(expected, actual)
            if err is not None:
                counts[err] += 1

    total = len(cases)
    report = RedTeamReport(
        schema_version="red-team-report-v1",
        decision_policy_version=DECISION_POLICY_VERSION,
        total_cases=total,
        matches=matches,
        mismatches=total - matches,
        counts=counts,
        confusion_matrix=confusion,
        cases=cases,
    )
    return report
