"""End-to-end reproducibility integration test (prompt 16).

Drives a synthetic founder application through every pipeline stage via
the workflow orchestrator (:func:`server.fund.services.workflow.run_workflow`)
and asserts four invariants:

1. Every ``WorkflowStep`` row lands in ``succeeded`` status.
2. Exactly one persisted ``decision_artifact`` row exists whose
   ``decision.decision_policy_version`` equals the pinned
   ``DECISION_POLICY_VERSION`` constant (``"decision-policy-v1"``).
3. The ``DecisionIssued`` payload recorded in the outbox validates
   against ``decision_issued.v1.json`` when reconstructed into a full
   canonical event object.
4. Running the same pipeline a second time against a fresh application
   row seeded from the identical transcript / domain / ask yields a
   decision-artifact whose canonically-serialized bytes match the
   first run after explicitly excluding the fields that legitimately
   vary per run:

      * ``application_id``      — new row per run
      * ``session_id``          — derived from the application id
      * ``occurred_at``         — sourced from the application's
                                  ``updated_at`` timestamp
      * ``artifact_id``         — deterministic hash that depends on
                                  ``application_id``; recomputed per
                                  run
      * ``inputs.application.id`` /
        ``inputs.session_id``   — same reason as above; embedded in
                                  the inputs digest

   Exclusion of these fields is documented both here and inside the
   helper that performs the byte comparison. Any other field
   difference between the two runs is a reproducibility regression
   and fails the test.

Prohibitions honored
--------------------

* Uses the throwaway fund SQLite DB via ``Base.metadata.drop_all`` +
  ``create_all`` (production DB credentials never touched).
* No network I/O: the workflow dispatches notifications through a
  ``DryRunBackend`` rooted in ``tmp_path``; no real SMTP/SES/Sendgrid.
* Byte comparison explicitly excludes fields that must vary per run
  (UUIDs, timestamps, derived identifiers). The exclusion list lives
  in one place (``_REPRODUCIBILITY_EXCLUDED_PATHS``) so future
  maintenance is obvious.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import SessionLocal
from coherence_engine.server.fund.services.decision_artifact import (
    ARTIFACT_KIND,
    canonical_artifact_bytes,
    validate_artifact,
)
from coherence_engine.server.fund.services.decision_policy import (
    DECISION_POLICY_VERSION,
)
from coherence_engine.server.fund.services.event_schemas import validate_event
from coherence_engine.server.fund.services.notification_backends import (
    DryRunBackend,
)
from coherence_engine.server.fund.services.workflow import (
    STATUS_SUCCEEDED,
    STEP_NAMES,
    run_workflow,
)


# ---------------------------------------------------------------------------
# Fields that legitimately vary per run. Excluded from byte-comparison in
# ``test_decision_artifact_is_byte_reproducible``. Documented in the module
# docstring above; changing this set silently would hide reproducibility
# regressions, so any new entry must include a rationale comment.
# ---------------------------------------------------------------------------

_REPRODUCIBILITY_EXCLUDED_PATHS: Tuple[Tuple[str, ...], ...] = (
    # Surface identity + provenance (new row per run, derived from app id)
    ("artifact_id",),
    ("application_id",),
    ("session_id",),
    ("occurred_at",),
    # Digest over inputs that embed the application id + session id
    # (itself derived from the application id). The per-field content
    # equality below catches real drift; the top-level digest would
    # flag the expected id churn.
    ("inputs_digest",),
    # The same identifiers echoed inside the pinned inputs subtree.
    ("inputs", "application", "id"),
    ("inputs", "session_id"),
)


def _strip_paths(obj: Any, paths: Iterable[Tuple[str, ...]]) -> Any:
    """Return a deep copy of ``obj`` with each path removed.

    ``paths`` is an iterable of tuples: each tuple is a sequence of
    mapping keys descending into ``obj``. Missing paths are silently
    skipped so the helper stays robust if the artifact schema adds a
    new top-level field between runs.
    """
    out = copy.deepcopy(obj)
    for path in paths:
        cursor: Any = out
        for key in path[:-1]:
            if not isinstance(cursor, dict) or key not in cursor:
                cursor = None
                break
            cursor = cursor[key]
        if isinstance(cursor, dict):
            cursor.pop(path[-1], None)
    return out


def _reconstruct_decision_issued_event(
    payload: Dict[str, Any], row: models.EventOutbox
) -> Dict[str, Any]:
    """Rebuild the full canonical event object validated against the schema.

    The outbox row stores only the inner ``payload`` dict. The schema
    validator (:func:`validate_event`) consumes the envelope-plus-
    payload object built by :meth:`EventPublisher._build_event_object`.
    This helper reproduces that assembly so we can validate directly
    against the raw payload as persisted by the workflow.
    """
    merged: Dict[str, Any] = {
        "event_id": str(row.id),
        "event_name": "decision_issued",
        "schema_version": 1,
        "occurred_at": row.occurred_at.isoformat().replace("+00:00", "Z"),
    }
    for key, value in payload.items():
        merged.setdefault(key, value)
    return merged


def _fetch_decision_artifact_payload(db, application_id: str) -> Dict[str, Any]:
    rows = (
        db.query(models.ArgumentArtifact)
        .filter(models.ArgumentArtifact.application_id == application_id)
        .filter(models.ArgumentArtifact.kind == ARTIFACT_KIND)
        .all()
    )
    assert len(rows) == 1, (
        f"expected exactly one decision_artifact row for {application_id}, "
        f"found {len(rows)}"
    )
    row = rows[0]
    assert row.payload_json, "decision_artifact payload_json must be non-empty"
    return json.loads(row.payload_json)


# ---------------------------------------------------------------------------
# The integration test
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_e2e_pipeline_is_byte_reproducible_and_schema_valid(
    e2e_app_factory, tmp_path: Path
) -> None:
    """Offline reproducibility integration test (prompt 16)."""

    # --- Run 1 ------------------------------------------------------------
    seeded_first = e2e_app_factory(suffix="first")
    backend = DryRunBackend(tmp_path / "notifications")

    db = SessionLocal()
    try:
        run_one = run_workflow(
            db,
            seeded_first["application_id"],
            notification_backend=backend,
            idempotency_prefix="e2e_run1",
        )
        db.commit()

        # (a) Every checkpoint row is succeeded.
        assert run_one.status == STATUS_SUCCEEDED
        run_one_steps = (
            db.query(models.WorkflowStep)
            .filter(models.WorkflowStep.workflow_run_id == run_one.id)
            .all()
        )
        assert len(run_one_steps) == len(STEP_NAMES)
        assert {s.name for s in run_one_steps} == set(STEP_NAMES)
        for step in run_one_steps:
            assert step.status == STATUS_SUCCEEDED, (
                f"step {step.name!r} expected succeeded, got {step.status!r} "
                f"(error={step.error!r})"
            )

        # (b) Exactly one decision_artifact row + pinned policy version.
        artifact_one = _fetch_decision_artifact_payload(
            db, seeded_first["application_id"]
        )
        validate_artifact(artifact_one)
        assert (
            artifact_one["decision"]["decision_policy_version"]
            == DECISION_POLICY_VERSION
        ), (
            "decision_artifact decision_policy_version must equal the "
            f"running constant {DECISION_POLICY_VERSION!r}"
        )
        assert DECISION_POLICY_VERSION == "decision-policy-v1", (
            "regression guard: the e2e reproducibility test pins the "
            "policy version string; update this assertion deliberately "
            "if the fund policy is rev'd."
        )

        # (c) DecisionIssued outbox payload validates against its v1 schema.
        decision_issued_rows = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "DecisionIssued")
            .order_by(models.EventOutbox.occurred_at.asc())
            .all()
        )
        assert len(decision_issued_rows) == 1, (
            f"expected exactly one DecisionIssued event, got "
            f"{len(decision_issued_rows)}"
        )
        decision_issued_row = decision_issued_rows[0]
        decision_issued_payload = json.loads(
            decision_issued_row.payload_json
        )
        event_object = _reconstruct_decision_issued_event(
            decision_issued_payload, decision_issued_row
        )
        validate_event("decision_issued", event_object)
    finally:
        db.close()

    # --- Run 2 (fresh application, identical inputs) ---------------------
    seeded_second = e2e_app_factory(suffix="second")
    assert (
        seeded_second["application_id"] != seeded_first["application_id"]
    ), "factory must mint a distinct application id on the second invocation"
    assert (
        seeded_second["transcript_text"] == seeded_first["transcript_text"]
    ), "reproducibility comparison assumes byte-identical transcript input"
    assert (
        seeded_second["requested_check_usd"]
        == seeded_first["requested_check_usd"]
    )
    assert seeded_second["domain_primary"] == seeded_first["domain_primary"]

    db = SessionLocal()
    try:
        run_two = run_workflow(
            db,
            seeded_second["application_id"],
            notification_backend=DryRunBackend(tmp_path / "notifications_b"),
            idempotency_prefix="e2e_run2",
        )
        db.commit()
        assert run_two.status == STATUS_SUCCEEDED

        artifact_two = _fetch_decision_artifact_payload(
            db, seeded_second["application_id"]
        )
    finally:
        db.close()

    # --- Reproducibility byte-comparison ---------------------------------
    stripped_one = _strip_paths(artifact_one, _REPRODUCIBILITY_EXCLUDED_PATHS)
    stripped_two = _strip_paths(artifact_two, _REPRODUCIBILITY_EXCLUDED_PATHS)
    bytes_one = canonical_artifact_bytes(stripped_one)
    bytes_two = canonical_artifact_bytes(stripped_two)
    if bytes_one != bytes_two:
        # Produce an actionable diff so a regression is easy to debug.
        # We re-dump with indentation to surface the first divergent key.
        pretty_one = json.dumps(stripped_one, sort_keys=True, indent=2)
        pretty_two = json.dumps(stripped_two, sort_keys=True, indent=2)
        diff_hint = []
        for key in sorted(set(stripped_one) | set(stripped_two)):
            if stripped_one.get(key) != stripped_two.get(key):
                diff_hint.append(key)
        pytest.fail(
            "decision_artifact bytes diverged across two runs with "
            "identical inputs (excluding "
            f"{_REPRODUCIBILITY_EXCLUDED_PATHS}); first-divergent top-"
            f"level keys: {diff_hint}\n\n"
            f"run1 stripped:\n{pretty_one}\n\n"
            f"run2 stripped:\n{pretty_two}"
        )

    # Both artifacts also survive their own validator on their own.
    validate_artifact(artifact_two)
    assert (
        artifact_two["decision"]["decision_policy_version"]
        == DECISION_POLICY_VERSION
    )
