"""Reproducible decision artifact builder + persistence (decision_artifact.v1).

This module produces a single bundled JSON artifact per application that pins
all inputs, per-layer scores, domain mix, ontology references, and decision
outcome for downstream audit. Running ``build_decision_artifact`` twice on the
same ``app_state`` yields a byte-identical dict (after canonical
``json.dumps(..., sort_keys=True, separators=(",", ":"))``).

The builder is deterministic: it never reads wall-clock time. The
``occurred_at`` value comes from the inputs (the application's authored
timestamps), not ``datetime.now()``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.decision_policy import (
    DECISION_POLICY_VERSION,
)
from coherence_engine.server.fund.services import object_storage as _object_storage


_LOG = logging.getLogger(__name__)

ARTIFACT_KIND = "decision_artifact"
SCHEMA_VERSION = "1"
DEFAULT_SCORING_VERSION = "scoring-v1.0.0"
DEFAULT_DOMAIN_MIX_SCHEMA_VERSION = "domain-mix-v1"

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "artifacts"
_SCHEMA_FILENAME = "decision_artifact.v1.json"

_schema_cache: Optional[Dict[str, Any]] = None
_schema_lock = threading.Lock()
_fallback_warning_lock = threading.Lock()
_fallback_warning_emitted = False

# Internal policy vocabulary -> canonical artifact verdict (matches the
# decision_issued.v1 event enum: pass | reject | manual_review).
_VERDICT_MAP = {
    "pass": "pass",
    "fail": "reject",
    "reject": "reject",
    "manual_review": "manual_review",
}


class DecisionArtifactValidationError(ValueError):
    """Raised when a decision artifact dict fails schema validation."""


def _canonical_dumps(obj: Any) -> str:
    """Stable serialization used for digests and persistence bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_artifact_bytes(artifact: Dict[str, Any]) -> bytes:
    """Return UTF-8 bytes of the canonical artifact serialization."""
    return _canonical_dumps(artifact).encode("utf-8")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_inputs(inputs: Any) -> str:
    return _sha256_hex(_canonical_dumps(inputs))


def _emit_fallback_warning_once() -> None:
    global _fallback_warning_emitted
    with _fallback_warning_lock:
        if _fallback_warning_emitted:
            return
        _fallback_warning_emitted = True
    _LOG.warning(
        "jsonschema not installed; decision_artifact validator using minimal "
        "required-keys fallback. Install 'jsonschema' for full validation."
    )


def _load_schema() -> Dict[str, Any]:
    global _schema_cache
    with _schema_lock:
        if _schema_cache is not None:
            return _schema_cache
        path = _SCHEMA_DIR / _SCHEMA_FILENAME
        if not path.exists():
            raise DecisionArtifactValidationError(
                f"decision_artifact schema missing at {path}"
            )
        with path.open("r", encoding="utf-8") as f:
            _schema_cache = json.load(f)
        return _schema_cache


def _fallback_validate(schema: Dict[str, Any], artifact: Dict[str, Any]) -> None:
    required = schema.get("required") or []
    if isinstance(required, list):
        missing = [k for k in required if k not in artifact]
        if missing:
            raise DecisionArtifactValidationError(
                f"decision_artifact missing required fields: {sorted(missing)}"
            )
    if schema.get("additionalProperties") is False:
        declared = set((schema.get("properties") or {}).keys())
        extras = [k for k in artifact.keys() if k not in declared]
        if extras:
            raise DecisionArtifactValidationError(
                f"decision_artifact has unexpected fields: {sorted(extras)}"
            )


def validate_artifact(artifact: Dict[str, Any]) -> None:
    """Validate an artifact dict against decision_artifact.v1.json.

    Raises :class:`DecisionArtifactValidationError` on any failure.
    """
    schema = _load_schema()
    try:
        from jsonschema import Draft202012Validator  # type: ignore
    except ImportError:
        _emit_fallback_warning_once()
        _fallback_validate(schema, artifact)
        return
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(artifact), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        path = ".".join(str(p) for p in first.absolute_path) or "<root>"
        raise DecisionArtifactValidationError(
            f"decision_artifact invalid at {path}: {first.message}"
        )


def _coerce_per_layer(per_layer: Dict[str, Any]) -> Dict[str, float]:
    keys = ("contradiction", "argumentation", "embedding", "compression", "structural")
    return {k: float(per_layer.get(k, 0.0)) for k in keys}


def _coerce_uncertainty(unc: Any) -> Dict[str, float]:
    if not isinstance(unc, dict):
        return {"lower": 0.0, "upper": 0.0}
    return {"lower": float(unc.get("lower", 0.0)), "upper": float(unc.get("upper", 0.0))}


def _coerce_normative_profile(np_in: Any) -> Dict[str, float]:
    if np_in is None:
        return {"rights": 0.0, "utilitarian": 0.0, "deontic": 0.0}
    if hasattr(np_in, "rights"):
        return {
            "rights": float(getattr(np_in, "rights", 0.0)),
            "utilitarian": float(getattr(np_in, "utilitarian", 0.0)),
            "deontic": float(getattr(np_in, "deontic", 0.0)),
        }
    return {
        "rights": float(np_in.get("rights", 0.0)),
        "utilitarian": float(np_in.get("utilitarian", 0.0)),
        "deontic": float(np_in.get("deontic", 0.0)),
    }


def _coerce_domain_weights(weights: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not weights:
        return out
    for entry in weights:
        if isinstance(entry, dict):
            domain_key = str(entry.get("domain", "")).strip()
            weight_val = float(entry.get("weight", 0.0))
        else:
            try:
                domain_key, weight_val = entry  # type: ignore[misc]
            except (TypeError, ValueError):
                continue
            domain_key = str(domain_key).strip()
            weight_val = float(weight_val)
        if not domain_key:
            continue
        out.append({"domain": domain_key, "weight": weight_val})
    return out


def _coerce_domain(domain_in: Any) -> Dict[str, Any]:
    if domain_in is None:
        return {
            "weights": [],
            "normative_profile": {"rights": 0.0, "utilitarian": 0.0, "deontic": 0.0},
            "schema_version": DEFAULT_DOMAIN_MIX_SCHEMA_VERSION,
            "notes": [],
        }
    if hasattr(domain_in, "weights") and hasattr(domain_in, "normative"):
        weights = _coerce_domain_weights(getattr(domain_in, "weights", ()))
        normative = _coerce_normative_profile(getattr(domain_in, "normative", None))
        schema_version = str(getattr(domain_in, "schema_version", DEFAULT_DOMAIN_MIX_SCHEMA_VERSION))
        notes_raw = getattr(domain_in, "notes", None) or ()
        notes = [str(n) for n in notes_raw]
        return {
            "weights": weights,
            "normative_profile": normative,
            "schema_version": schema_version,
            "notes": notes,
        }
    weights = _coerce_domain_weights(domain_in.get("weights"))
    normative = _coerce_normative_profile(domain_in.get("normative_profile") or domain_in.get("normative"))
    schema_version = str(domain_in.get("schema_version") or DEFAULT_DOMAIN_MIX_SCHEMA_VERSION)
    notes_raw = domain_in.get("notes") or []
    notes = [str(n) for n in notes_raw]
    return {
        "weights": weights,
        "normative_profile": normative,
        "schema_version": schema_version,
        "notes": notes,
    }


def _deterministic_artifact_id(
    app_state: Dict[str, Any], inputs_digest: str
) -> str:
    seed = "|".join([
        str(app_state.get("application_id", "")),
        str(app_state.get("session_id", "")),
        inputs_digest,
    ])
    return f"art_{_sha256_hex(seed)[:16]}"


def build_decision_artifact(app_state: Dict[str, Any]) -> Dict[str, Any]:
    """Build the canonical decision_artifact.v1 dict from ``app_state``.

    ``app_state`` is a plain dict with the following keys:

    - application_id (str)
    - session_id (str)
    - occurred_at (str, ISO8601 from the inputs — never wall-clock)
    - inputs (any JSON-serializable structure used for the digest)
    - scoring: {composite, per_layer{...}, uncertainty{lower,upper}, scoring_version?}
    - domain:  DomainMix-shaped dict OR DomainMix dataclass instance
    - ontology_graph_id (str), ontology_graph_digest (str)
    - decision: {verdict, cs_superiority, cs_required, reason_codes,
                 decision_policy_version?}
    - prompt_registry_digest (optional str)
    - artifact_id (optional override; default deterministic from inputs)

    The returned dict is suitable for direct ``json.dumps(sort_keys=True)``.
    Calling this function twice with the same ``app_state`` returns equal
    dicts whose canonical serialization is byte-identical.
    """
    if not isinstance(app_state, dict):
        raise TypeError("app_state must be a dict")

    application_id = str(app_state["application_id"])
    session_id = str(app_state["session_id"])
    occurred_at = str(app_state["occurred_at"])

    inputs = app_state.get("inputs", {})
    inputs_digest = _digest_inputs(inputs)

    scoring_in = app_state.get("scoring") or {}
    scoring_version = str(scoring_in.get("scoring_version") or DEFAULT_SCORING_VERSION)
    scoring = {
        "composite": float(scoring_in.get("composite", 0.0)),
        "per_layer": _coerce_per_layer(scoring_in.get("per_layer") or {}),
        "uncertainty": _coerce_uncertainty(scoring_in.get("uncertainty")),
        "scoring_version": scoring_version,
    }

    domain = _coerce_domain(app_state.get("domain"))

    ontology_graph_id = str(app_state.get("ontology_graph_id") or "")
    ontology_graph_digest = str(app_state.get("ontology_graph_digest") or "")

    decision_in = app_state.get("decision") or {}
    raw_verdict = str(decision_in.get("verdict", "manual_review")).strip().lower()
    verdict = _VERDICT_MAP.get(raw_verdict, raw_verdict)
    if verdict not in ("pass", "reject", "manual_review"):
        verdict = "manual_review"
    decision_policy_version = str(
        decision_in.get("decision_policy_version") or DECISION_POLICY_VERSION
    )
    reason_codes = sorted({str(c) for c in (decision_in.get("reason_codes") or [])})
    decision = {
        "verdict": verdict,
        "cs_superiority": float(decision_in.get("cs_superiority", 0.0)),
        "cs_required": float(decision_in.get("cs_required", 0.0)),
        "reason_codes": reason_codes,
        "decision_policy_version": decision_policy_version,
    }

    pins: Dict[str, str] = {
        "scoring_version": scoring_version,
        "decision_policy_version": decision_policy_version,
    }
    prompt_registry_digest = app_state.get("prompt_registry_digest")
    if prompt_registry_digest is not None and str(prompt_registry_digest) != "":
        pins["prompt_registry_digest"] = str(prompt_registry_digest)

    artifact_id_override = app_state.get("artifact_id")
    if artifact_id_override:
        artifact_id = str(artifact_id_override)
    else:
        artifact_id = _deterministic_artifact_id(app_state, inputs_digest)

    artifact: Dict[str, Any] = {
        "artifact_id": artifact_id,
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": SCHEMA_VERSION,
        "application_id": application_id,
        "session_id": session_id,
        "occurred_at": occurred_at,
        "inputs_digest": inputs_digest,
        "scoring": scoring,
        "domain": domain,
        "ontology_graph_id": ontology_graph_id,
        "ontology_graph_digest": ontology_graph_digest,
        "decision": decision,
        "pins": pins,
    }
    return artifact


def persist_decision_artifact(
    session: Session,
    app_id: str,
    artifact_dict: Dict[str, Any],
    *,
    scoring_job_id: Optional[str] = None,
) -> models.ArgumentArtifact:
    """Persist an artifact dict into the artifact table with kind='decision_artifact'.

    Validates against the v1 schema before writing. Returns the persisted ORM
    row (the existing :class:`~coherence_engine.server.fund.models.ArgumentArtifact`
    table is reused as the generic artifact store; this row carries
    ``kind="decision_artifact"`` and the canonical JSON in ``payload_json``).

    Side effect: the same canonical bytes are also written through the
    configured :mod:`object_storage` backend at the deterministic key
    ``decision_artifacts/<application_id>/<artifact_id>.json`` so that
    downstream consumers (auditor portal, signed-URL handouts) can read the
    body without round-tripping through Postgres. The DB copy remains the
    authoritative store; storage write failures are logged but do not abort
    the persist (the workflow has retry semantics for transient blob errors).
    """
    validate_artifact(artifact_dict)
    payload_bytes = canonical_artifact_bytes(artifact_dict)
    payload_json = payload_bytes.decode("utf-8")
    artifact_id = str(artifact_dict["artifact_id"])
    rec = models.ArgumentArtifact(
        id=artifact_id,
        application_id=str(app_id),
        scoring_job_id=str(scoring_job_id or ""),
        propositions_json="[]",
        relations_json="[]",
        kind=ARTIFACT_KIND,
        payload_json=payload_json,
    )
    session.add(rec)
    session.flush()

    # Write the canonical bytes through object storage. The body is content-
    # addressable: the SHA-256 returned by ``put`` must match what we computed
    # from the canonical dict, otherwise the backend corrupted the upload.
    try:
        expected = _sha256_hex(payload_json)
        result = _object_storage.put(
            f"decision_artifacts/{app_id}/{artifact_id}.json",
            payload_bytes,
            content_type="application/json",
        )
        if result.sha256 != expected:
            raise _object_storage.StorageHashMismatch(
                f"decision_artifact body hash drift: expected={expected} "
                f"got={result.sha256} uri={result.uri}"
            )
    except _object_storage.StorageHashMismatch:
        raise
    except Exception:
        _LOG.exception(
            "decision_artifact storage upload failed application_id=%s artifact_id=%s",
            app_id,
            artifact_id,
        )

    return rec
