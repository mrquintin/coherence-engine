"""Coherence-vs-outcome regression study harness (prompt 44, Wave 12).

This module is the deterministic, replayable core of the predictive-validity
study. It joins the historical-pitch corpus (prompt 42) with the realized
outcome labels (prompt 43), pulls the *current* coherence_score for each
pitch, and fits a logistic regression of

    survival_5yr ~ coherence_score + domain_primary + log(check_size_usd)

reporting point estimates, 99 / 95 percent bootstrap CIs (n=10000 resamples
by default), Brier score, AUC, a 10-bin reliability curve, and per-domain
sub-models with Bonferroni-corrected alphas. The pre-registration document
that pins the hypotheses, alpha levels, and the stopping rule lives at
``data/governed/validation/preregistration.yaml`` and is loaded into every
report so a reader can verify that the hypotheses were not rewritten after
seeing the data.

Determinism guarantees
----------------------

* Same ``StudyConfig`` (seed + same data hash) -> byte-identical report
  bytes after :meth:`StudyReport.to_canonical_bytes`.
* Bootstrap resampling uses ``random.Random(config.seed)`` from the
  standard library — *not* numpy — so the harness runs in the project's
  baseline environment with no third-party dependencies. ``numpy`` /
  ``statsmodels`` / ``sklearn`` are *only* consulted opportunistically and
  recorded in ``generated_with`` if available; the math is identical
  either way.
* No wall-clock reads, no live database reads, no network calls.
* ``run_study`` raises :class:`InsufficientSampleError` when the joined
  frame has fewer known-outcome rows than the pre-registered
  ``stopping_rule.minimum_n_with_known_outcome``. The harness will *not*
  emit a partial report.

The on-disk preregistration is parsed with a deliberately tiny stdlib YAML
reader (``_parse_preregistration``) — the schema is fixed and small enough
that pulling in PyYAML for it would be a needless dependency.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)


VALIDATION_STUDY_SCHEMA_VERSION = "validation-study-report-v1"

_DEFAULT_BOOTSTRAP_ITERS = 10_000
_DEFAULT_RELIABILITY_BINS = 10
_DEFAULT_QUINTILES = 5
_PER_DOMAIN_MIN_N = 30
_DEFAULT_CHECK_SIZE_USD = 50_000.0
_LOGIT_MAX_ITERS = 50
_LOGIT_TOL = 1e-8
_RIDGE_LAMBDA = 1e-6  # tiny ridge to stabilize singular designs


_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[3]
DEFAULT_PREREGISTRATION_PATH = (
    _REPO_ROOT / "data" / "governed" / "validation" / "preregistration.yaml"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ValidationStudyError(RuntimeError):
    """Base class for validation-study failures."""


class InsufficientSampleError(ValidationStudyError):
    """Raised when N(known outcomes) < pre-registered minimum.

    The error code ``INSUFFICIENT_SAMPLE`` is included in the message so
    operator tooling can grep for it without matching the human-readable
    prefix.
    """

    def __init__(self, n: int, minimum: int):
        self.n = int(n)
        self.minimum = int(minimum)
        super().__init__(
            f"INSUFFICIENT_SAMPLE: study requires N>={minimum} known-outcome "
            f"rows but the joined frame has N={n}. Refusing to emit a report."
        )


class PreregistrationError(ValidationStudyError):
    """Raised when the preregistration document fails the integrity check."""


# ---------------------------------------------------------------------------
# Tiny YAML parser
# ---------------------------------------------------------------------------


_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+(?:[eE][+-]?\d+)?$")


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _coerce_scalar(raw: str) -> Any:
    s = raw.strip()
    if s == "":
        return ""
    if s.lower() in {"true", "yes"}:
        return True
    if s.lower() in {"false", "no"}:
        return False
    if s.lower() in {"null", "none", "~"}:
        return None
    if s[0] in {'"', "'"} and s[-1] == s[0]:
        return _strip_quotes(s)
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s


def _read_pre_lines(text: str) -> List[Tuple[int, str]]:
    """Yield (indent, content) for each non-comment, non-blank line.

    Folded scalars (``>``, ``>-``) and literal scalars (``|``, ``|-``) are
    flattened on the fly into the line they belong to so the parser below
    only deals with simple ``key: value`` and ``- value`` shapes.
    """

    raw_lines: List[str] = []
    pending_fold: Optional[Tuple[int, str, int, str]] = None
    # pending_fold = (parent_indent, key_prefix, fold_indent, accumulator)
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if stripped == "":
            if pending_fold is not None:
                # blank line inside fold — treat as paragraph break (single space)
                pi, kp, fi, acc = pending_fold
                pending_fold = (pi, kp, fi, acc.rstrip() + " ")
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if pending_fold is not None:
            pi, kp, fi, acc = pending_fold
            if indent > pi:
                acc = (acc + " " + stripped.strip()).strip()
                pending_fold = (pi, kp, fi, acc)
                continue
            raw_lines.append((pi, kp + json.dumps(acc.strip())))
            pending_fold = None
        line = stripped
        # Detect the start of a fold. We only support `key: >-` / `key: >` /
        # `key: |` / `key: |-` because those are what appear in
        # preregistration.yaml.
        m = re.match(r"^([^#]+?):\s*([>|])-?\s*$", line.strip())
        if m and not line.strip().startswith("- "):
            key_part = line.split(":", 1)[0]
            pending_fold = (indent, f"{key_part}: ", indent + 2, "")
            continue
        raw_lines.append((indent, line.strip()))
    if pending_fold is not None:
        pi, kp, _fi, acc = pending_fold
        raw_lines.append((pi, kp + json.dumps(acc.strip())))
    return raw_lines


def _parse_preregistration(text: str) -> Dict[str, Any]:
    """Parse the small subset of YAML used by ``preregistration.yaml``.

    Supported shapes:
      * top-level scalar mapping ``key: value``
      * one level of nested mapping
      * lists of scalars: ``- foo``
      * lists of mappings (each item starts with ``- key: value``)
      * folded scalars introduced by ``>``, ``>-``, ``|`` or ``|-``.

    The parser is intentionally strict; any unexpected token raises
    :class:`PreregistrationError` so silent corruption of the
    pre-registration document is impossible.
    """

    lines = _read_pre_lines(text)
    pos = 0

    def parse_block(min_indent: int) -> Tuple[Any, int]:
        nonlocal pos
        # Decide list vs mapping by inspecting the first entry at min_indent.
        if pos >= len(lines):
            return {}, pos
        first_indent, first = lines[pos]
        if first_indent < min_indent:
            return {}, pos
        if first.lstrip().startswith("- "):
            return parse_list(min_indent)
        return parse_mapping(min_indent)

    def parse_mapping(indent_target: int) -> Tuple[Dict[str, Any], int]:
        nonlocal pos
        out: Dict[str, Any] = {}
        while pos < len(lines):
            indent, content = lines[pos]
            if indent < indent_target:
                break
            if indent > indent_target:
                raise PreregistrationError(
                    f"unexpected indentation {indent} (expected {indent_target}) "
                    f"at: {content!r}"
                )
            if content.startswith("- "):
                raise PreregistrationError(
                    f"unexpected list marker at indent {indent}: {content!r}"
                )
            if ":" not in content:
                raise PreregistrationError(
                    f"expected 'key: value' at indent {indent}: {content!r}"
                )
            key_part, _, val_part = content.partition(":")
            key = key_part.strip()
            val = val_part.strip()
            pos += 1
            if val == "":
                # nested block
                if pos < len(lines) and lines[pos][0] > indent_target:
                    nested_indent = lines[pos][0]
                    nested, pos2 = parse_block(nested_indent)
                    pos = pos2
                    out[key] = nested
                else:
                    out[key] = None
            else:
                out[key] = _coerce_scalar(val)
        return out, pos

    def parse_list(indent_target: int) -> Tuple[List[Any], int]:
        nonlocal pos
        out: List[Any] = []
        while pos < len(lines):
            indent, content = lines[pos]
            if indent < indent_target:
                break
            if indent > indent_target:
                raise PreregistrationError(
                    f"unexpected indentation {indent} inside list (expected "
                    f"{indent_target}): {content!r}"
                )
            if not content.startswith("- "):
                break
            inner = content[2:].strip()
            pos += 1
            if inner == "":
                # block mapping under this dash
                if pos < len(lines) and lines[pos][0] > indent_target:
                    nested_indent = lines[pos][0]
                    nested, pos2 = parse_block(nested_indent)
                    pos = pos2
                    out.append(nested)
                else:
                    out.append(None)
            elif ":" in inner:
                key_part, _, val_part = inner.partition(":")
                key = key_part.strip()
                val = val_part.strip()
                item: Dict[str, Any] = {}
                if val == "":
                    if pos < len(lines) and lines[pos][0] > indent_target:
                        nested_indent = lines[pos][0]
                        nested, pos2 = parse_block(nested_indent)
                        pos = pos2
                        item[key] = nested
                    else:
                        item[key] = None
                else:
                    item[key] = _coerce_scalar(val)
                # extra keys at child indent
                if pos < len(lines) and lines[pos][0] > indent_target:
                    nested_indent = lines[pos][0]
                    nested, pos2 = parse_mapping(nested_indent)
                    pos = pos2
                    for k, v in nested.items():
                        item[k] = v
                out.append(item)
            else:
                out.append(_coerce_scalar(inner))
        return out, pos

    result, _ = parse_mapping(0)
    return result


def load_preregistration(
    path: Optional[os.PathLike[str] | str] = None,
) -> Dict[str, Any]:
    """Load and validate the preregistration document.

    The returned dict has all top-level keys required by the study report.
    Missing keys raise :class:`PreregistrationError` *before* any data is
    touched, which guarantees a corrupted preregistration cannot quietly
    propagate into the report.
    """

    target = Path(path) if path else DEFAULT_PREREGISTRATION_PATH
    if not target.is_file():
        raise PreregistrationError(f"preregistration file not found: {target}")
    text = target.read_text(encoding="utf-8")
    parsed = _parse_preregistration(text)
    required = {
        "version",
        "study_name",
        "primary_hypothesis",
        "stopping_rule",
        "bootstrap",
        "negative_results_policy",
    }
    missing = required - set(parsed.keys())
    if missing:
        raise PreregistrationError(
            f"preregistration missing required keys: {sorted(missing)}"
        )
    sr = parsed.get("stopping_rule") or {}
    if not isinstance(sr, Mapping) or "minimum_n_with_known_outcome" not in sr:
        raise PreregistrationError(
            "preregistration.stopping_rule.minimum_n_with_known_outcome required"
        )
    return parsed


# ---------------------------------------------------------------------------
# Config / data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StudyRow:
    """One pitch row prepared for the study.

    The frame is intentionally narrow: the pure-Python regression below
    only consumes the four primitives ``coherence_score``, ``domain``,
    ``check_size_usd``, and ``survival_5yr``. ``pitch_id`` is preserved
    so reports can be reproduced row-for-row.
    """

    pitch_id: str
    domain: str
    coherence_score: float
    check_size_usd: float
    survival_5yr: int  # 0 or 1


@dataclass(frozen=True)
class StudyConfig:
    """Inputs to a single deterministic study run.

    All paths are converted to absolute when serialized into the report's
    ``config`` block so two runs from different working directories yield
    identical bytes only when the resolved paths agree.
    """

    preregistration_path: Path = DEFAULT_PREREGISTRATION_PATH
    corpus_manifest_path: Optional[Path] = None
    outcomes_path: Optional[Path] = None
    coherence_scores_path: Optional[Path] = None
    output_path: Optional[Path] = None
    seed: int = 0
    bootstrap_iters: int = _DEFAULT_BOOTSTRAP_ITERS
    default_check_size_usd: float = _DEFAULT_CHECK_SIZE_USD
    training_artifacts_index_path: Optional[Path] = None
    audit_feature_extractors: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CoefficientEstimate:
    name: str
    point: float
    ci_lower_95: float
    ci_upper_95: float
    ci_lower_99: float
    ci_upper_99: float


@dataclass(frozen=True)
class CalibrationBin:
    bin_index: int
    bin_lower: float
    bin_upper: float
    count: int
    mean_predicted: float
    mean_realized: float


@dataclass(frozen=True)
class StudyReport:
    schema_version: str
    generated_with: Dict[str, str]
    config: Dict[str, Any]
    preregistration: Dict[str, Any]
    n_total: int
    n_known_outcome: int
    n_excluded_unknown: int
    coefficients: Tuple[CoefficientEstimate, ...]
    primary_hypothesis_result: Dict[str, Any]
    secondary_hypothesis_result: Dict[str, Any]
    metrics: Dict[str, float]
    calibration_curve: Tuple[CalibrationBin, ...]
    domain_breakdown: Dict[str, Dict[str, Any]]
    insufficient_subgroups: Tuple[str, ...]
    data_hash: str

    def to_canonical_dict(self) -> Dict[str, Any]:
        return _canonical_report_dict(self)

    def to_canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.to_canonical_dict(), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")

    def report_digest(self) -> str:
        return hashlib.sha256(self.to_canonical_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Frame loading
# ---------------------------------------------------------------------------


def _hash_frame(rows: Sequence[StudyRow]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(
            json.dumps(
                {
                    "pid": r.pitch_id,
                    "d": r.domain,
                    "c": round(r.coherence_score, 9),
                    "k": round(r.check_size_usd, 6),
                    "s": int(r.survival_5yr),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return h.hexdigest()


def _coerce_survival(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return 1 if value else 0
    if value is True:  # pragma: no cover - bool covers this
        return 1
    if value is False:  # pragma: no cover - bool covers this
        return 0
    return None


def load_frame(
    *,
    corpus_manifest_path: Optional[os.PathLike[str] | str],
    outcomes_path: Optional[os.PathLike[str] | str],
    coherence_scores_path: Optional[os.PathLike[str] | str],
    default_check_size_usd: float,
) -> Tuple[List[StudyRow], int, int]:
    """Join corpus + outcomes + scores into a list of :class:`StudyRow`.

    ``coherence_scores_path`` points at a JSON object of shape
    ``{pitch_id: {"coherence_score": float, "check_size_usd": float?}}``.
    Missing rows in the score file drop the corresponding pitch from the
    frame (after counting it in ``n_total``). Pitches whose latest
    outcome is ``unknown`` for survival_5yr are excluded and counted
    separately so the report can disclose how many rows the stopping
    rule had to weigh against.
    """

    from coherence_engine.server.fund.services import outcome_labeling as ol

    export = ol.export(
        manifest_path=corpus_manifest_path,
        outcomes_path=outcomes_path,
        include_unknown=True,
    )
    all_rows = list(export.get("rows") or [])
    n_total = len(all_rows)

    scores: Dict[str, Dict[str, Any]] = {}
    if coherence_scores_path is not None:
        sp = Path(coherence_scores_path)
        if not sp.is_file():
            raise ValidationStudyError(
                f"coherence_scores_path file not found: {sp}"
            )
        try:
            payload = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationStudyError(
                f"coherence_scores_path is not valid JSON: {sp}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ValidationStudyError(
                f"coherence_scores_path must be a JSON object: {sp}"
            )
        for pid, blob in payload.items():
            if isinstance(blob, Mapping):
                scores[str(pid)] = dict(blob)
            elif isinstance(blob, (int, float)):
                scores[str(pid)] = {"coherence_score": float(blob)}

    rows: List[StudyRow] = []
    excluded_unknown = 0
    for raw in all_rows:
        pid = str(raw.get("pitch_id"))
        survived = _coerce_survival(raw.get("survival_5yr"))
        if survived is None:
            excluded_unknown += 1
            continue
        score_info = scores.get(pid)
        if score_info is None:
            continue
        try:
            coh = float(score_info.get("coherence_score"))
        except (TypeError, ValueError):
            continue
        check = score_info.get("check_size_usd")
        if check is None:
            check = raw.get("check_size_usd")
        if check is None:
            check = default_check_size_usd
        try:
            check_f = float(check)
        except (TypeError, ValueError):
            check_f = default_check_size_usd
        if check_f <= 0:
            check_f = default_check_size_usd
        rows.append(
            StudyRow(
                pitch_id=pid,
                domain=str(raw.get("domain_primary") or "unknown"),
                coherence_score=coh,
                check_size_usd=check_f,
                survival_5yr=int(survived),
            )
        )

    rows.sort(key=lambda r: r.pitch_id)
    return rows, n_total, excluded_unknown


# ---------------------------------------------------------------------------
# Pure-Python logistic regression
# ---------------------------------------------------------------------------


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _design_matrix(
    rows: Sequence[StudyRow],
    *,
    domain_levels: Sequence[str],
) -> Tuple[List[List[float]], List[int], List[str]]:
    """Build [intercept, coherence_score, log_check_size, *domain_dummies]."""

    feature_names = ["intercept", "coherence_score", "log_check_size"]
    # use the first level as the baseline to avoid dummy-variable trap
    baseline = domain_levels[0] if domain_levels else None
    dummies = [d for d in domain_levels if d != baseline]
    feature_names.extend([f"domain[{d}]" for d in dummies])

    X: List[List[float]] = []
    y: List[int] = []
    for r in rows:
        row = [
            1.0,
            float(r.coherence_score),
            math.log(max(1.0, float(r.check_size_usd))),
        ]
        for d in dummies:
            row.append(1.0 if r.domain == d else 0.0)
        X.append(row)
        y.append(int(r.survival_5yr))
    return X, y, feature_names


def _matvec(M: Sequence[Sequence[float]], v: Sequence[float]) -> List[float]:
    n = len(M)
    out = [0.0] * n
    for i in range(n):
        row = M[i]
        s = 0.0
        for j, x in enumerate(v):
            s += row[j] * x
        out[i] = s
    return out


def _solve_psd(A: List[List[float]], b: List[float]) -> List[float]:
    """Solve A x = b for a symmetric positive-definite A via Cholesky.

    Returns the solution vector. Raises ValueError if A is not numerically
    PD (caller adds ridge regularization in that case).
    """

    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = A[i][j]
            for k in range(j):
                s -= L[i][k] * L[j][k]
            if i == j:
                if s <= 0.0:
                    raise ValueError("matrix is not positive-definite")
                L[i][j] = math.sqrt(s)
            else:
                L[i][j] = s / L[j][j]
    # forward substitution L y = b
    y = [0.0] * n
    for i in range(n):
        s = b[i]
        for k in range(i):
            s -= L[i][k] * y[k]
        y[i] = s / L[i][i]
    # back substitution L^T x = y
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = y[i]
        for k in range(i + 1, n):
            s -= L[k][i] * x[k]
        x[i] = s / L[i][i]
    return x


def fit_logit(
    X: Sequence[Sequence[float]],
    y: Sequence[int],
    *,
    max_iters: int = _LOGIT_MAX_ITERS,
    tol: float = _LOGIT_TOL,
    ridge: float = _RIDGE_LAMBDA,
) -> Tuple[List[float], bool]:
    """Newton-Raphson IRLS fit. Returns (beta, converged).

    A small ridge penalty (``lambda=1e-6``) is added to the Hessian
    diagonal so degenerate designs (e.g., a fully separable bootstrap
    resample) still produce a finite solution rather than blowing up.
    The penalty is small enough that the point estimate is stable on
    well-conditioned data.
    """

    if not X:
        return [], False
    n = len(X)
    p = len(X[0])
    beta = [0.0] * p

    converged = False
    for _ in range(max_iters):
        # eta = X beta; mu = sigmoid(eta); W = diag(mu * (1 - mu))
        eta = _matvec(X, beta)
        mu = [_sigmoid(e) for e in eta]
        # gradient g = X^T (y - mu) - ridge * beta
        g = [0.0] * p
        for i in range(n):
            r = y[i] - mu[i]
            xi = X[i]
            for j in range(p):
                g[j] += xi[j] * r
        for j in range(p):
            g[j] -= ridge * beta[j]
        # Hessian H = X^T W X + ridge * I (positive-definite of the *negative*
        # log-likelihood, so the Newton step is +H^-1 g)
        H = [[0.0] * p for _ in range(p)]
        for i in range(n):
            w = mu[i] * (1.0 - mu[i])
            xi = X[i]
            for a in range(p):
                xa = xi[a] * w
                for b in range(a, p):
                    H[a][b] += xa * xi[b]
        for a in range(p):
            H[a][a] += ridge
            for b in range(a + 1, p):
                H[b][a] = H[a][b]
        try:
            step = _solve_psd(H, g)
        except ValueError:
            # add a larger ridge bump and retry once
            for a in range(p):
                H[a][a] += 1e-3
            try:
                step = _solve_psd(H, g)
            except ValueError:
                return beta, False
        max_step = max(abs(s) for s in step) if step else 0.0
        for j in range(p):
            beta[j] += step[j]
        if max_step < tol:
            converged = True
            break
    return beta, converged


def predict_proba(
    beta: Sequence[float], X: Sequence[Sequence[float]]
) -> List[float]:
    return [_sigmoid(z) for z in _matvec(X, beta)]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def brier_score(probas: Sequence[float], y: Sequence[int]) -> float:
    n = len(probas)
    if n == 0:
        return 0.0
    s = 0.0
    for p, yi in zip(probas, y, strict=True):
        s += (p - yi) ** 2
    return s / n


def auc_roc(probas: Sequence[float], y: Sequence[int]) -> float:
    """Mann-Whitney U based AUC (handles ties via average ranks)."""

    n = len(probas)
    if n == 0:
        return 0.5
    pos = sum(1 for v in y if v == 1)
    neg = n - pos
    if pos == 0 or neg == 0:
        return 0.5
    # average-rank assignment
    order = sorted(range(n), key=lambda i: probas[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and probas[order[j + 1]] == probas[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # ranks are 1-based, average over [i+1, j+1]
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    sum_pos_ranks = sum(ranks[i] for i in range(n) if y[i] == 1)
    auc = (sum_pos_ranks - pos * (pos + 1) / 2.0) / (pos * neg)
    return auc


def calibration_curve(
    probas: Sequence[float],
    y: Sequence[int],
    *,
    n_bins: int = _DEFAULT_RELIABILITY_BINS,
) -> Tuple[CalibrationBin, ...]:
    if n_bins <= 0:
        return ()
    buckets: List[List[int]] = [[] for _ in range(n_bins)]
    for idx, p in enumerate(probas):
        pp = max(0.0, min(1.0, p))
        if pp >= 1.0:
            b = n_bins - 1
        else:
            b = min(n_bins - 1, int(math.floor(pp * n_bins)))
        buckets[b].append(idx)
    out: List[CalibrationBin] = []
    for k, bucket in enumerate(buckets):
        lo = k / n_bins
        hi = (k + 1) / n_bins
        if not bucket:
            out.append(
                CalibrationBin(
                    bin_index=k,
                    bin_lower=round(lo, 6),
                    bin_upper=round(hi, 6),
                    count=0,
                    mean_predicted=0.0,
                    mean_realized=0.0,
                )
            )
            continue
        mp = sum(probas[i] for i in bucket) / len(bucket)
        mr = sum(y[i] for i in bucket) / len(bucket)
        out.append(
            CalibrationBin(
                bin_index=k,
                bin_lower=round(lo, 6),
                bin_upper=round(hi, 6),
                count=len(bucket),
                mean_predicted=round(mp, 6),
                mean_realized=round(mr, 6),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Lower-end interpolated percentile so ties are deterministic.

    Uses the same convention as numpy's default ('linear') with no
    third-party dependency.
    """

    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _bootstrap_betas(
    rows: Sequence[StudyRow],
    *,
    seed: int,
    iters: int,
    domain_levels: Sequence[str],
) -> List[List[float]]:
    rng = random.Random(seed)
    n = len(rows)
    if n == 0:
        return []
    betas: List[List[float]] = []
    for _ in range(iters):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
        X, y, _names = _design_matrix(sample, domain_levels=domain_levels)
        beta, _ok = fit_logit(X, y)
        betas.append(beta)
    return betas


# ---------------------------------------------------------------------------
# Hypothesis testing
# ---------------------------------------------------------------------------


def _evaluate_primary(
    coherence_estimate: CoefficientEstimate,
    *,
    alpha: float,
) -> Dict[str, Any]:
    """Decision rule: reject H0 if 1-alpha CI excludes 0 *and* point > 0.

    For ``alpha=0.01`` we use the 99% CI bounds; for ``alpha=0.05`` we
    fall back to the 95% bounds. Anything else is reported but not used
    to make a yes/no call.
    """

    if abs(alpha - 0.01) < 1e-9:
        lo, hi = coherence_estimate.ci_lower_99, coherence_estimate.ci_upper_99
        ci_label = "ci_99"
    else:
        lo, hi = coherence_estimate.ci_lower_95, coherence_estimate.ci_upper_95
        ci_label = "ci_95"
    excludes_zero = (lo > 0) or (hi < 0)
    direction_ok = coherence_estimate.point > 0
    rejected = excludes_zero and direction_ok and lo > 0
    return {
        "alpha": alpha,
        "ci_used": ci_label,
        "ci_lower": lo,
        "ci_upper": hi,
        "point_estimate": coherence_estimate.point,
        "excludes_zero": bool(excludes_zero),
        "direction_consistent": bool(direction_ok),
        "rejected_null": bool(rejected),
    }


def _evaluate_quintile_dose_response(
    rows: Sequence[StudyRow],
) -> Dict[str, Any]:
    """Bin coherence_score into 5 equal-count quintiles and check
    monotonic non-decreasing realized survival rate (with a 0.05 slack).

    Quintiles are assigned by rank to keep the bin populations balanced
    even when the score distribution is heavy-tailed.
    """

    n = len(rows)
    if n == 0:
        return {
            "n": 0,
            "quintile_rates": [],
            "monotonic_non_decreasing": False,
            "q5_minus_q1": 0.0,
            "rejected_null": False,
        }
    indexed = sorted(range(n), key=lambda i: rows[i].coherence_score)
    rates: List[float] = []
    counts: List[int] = []
    for k in range(_DEFAULT_QUINTILES):
        lo = (k * n) // _DEFAULT_QUINTILES
        hi = ((k + 1) * n) // _DEFAULT_QUINTILES
        bucket = [rows[indexed[i]].survival_5yr for i in range(lo, hi)]
        if not bucket:
            rates.append(0.0)
            counts.append(0)
            continue
        rates.append(sum(bucket) / len(bucket))
        counts.append(len(bucket))
    monotonic = True
    for k in range(1, len(rates)):
        if rates[k] + 0.05 < rates[k - 1]:
            monotonic = False
            break
    diff = rates[-1] - rates[0] if rates else 0.0
    rejected = monotonic and diff >= 0.05
    return {
        "n": n,
        "quintile_rates": [round(r, 6) for r in rates],
        "quintile_counts": counts,
        "monotonic_non_decreasing": monotonic,
        "q5_minus_q1": round(diff, 6),
        "rejected_null": bool(rejected),
    }


# ---------------------------------------------------------------------------
# Per-domain models
# ---------------------------------------------------------------------------


def _per_domain_models(
    rows: Sequence[StudyRow],
    *,
    seed: int,
    bootstrap_iters: int,
    family_alpha: float,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Fit a logit per domain with N >= _PER_DOMAIN_MIN_N.

    Each sub-model uses a tiny design (intercept + coherence_score +
    log_check_size) — domain dummies are unnecessary because the
    sub-model is conditioned on a single domain. The Bonferroni
    correction divides ``family_alpha`` by the number of fitted
    sub-models.
    """

    by_domain: Dict[str, List[StudyRow]] = {}
    for r in rows:
        by_domain.setdefault(r.domain, []).append(r)
    eligible: List[Tuple[str, List[StudyRow]]] = [
        (d, sub) for d, sub in sorted(by_domain.items()) if len(sub) >= _PER_DOMAIN_MIN_N
    ]
    insufficient = sorted(
        d for d, sub in by_domain.items() if len(sub) < _PER_DOMAIN_MIN_N
    )
    k = len(eligible)
    if k == 0:
        return {}, insufficient
    corrected_alpha = family_alpha / k
    out: Dict[str, Dict[str, Any]] = {}
    for d, sub in eligible:
        # design without domain dummies
        X = []
        y = []
        for r in sub:
            X.append(
                [
                    1.0,
                    float(r.coherence_score),
                    math.log(max(1.0, float(r.check_size_usd))),
                ]
            )
            y.append(int(r.survival_5yr))
        beta, converged = fit_logit(X, y)
        coh_idx = 1
        # bootstrap
        rng = random.Random(_compose_seed(seed, d))
        n_sub = len(sub)
        boot_coh: List[float] = []
        for _ in range(bootstrap_iters):
            samp_idx = [rng.randrange(n_sub) for _ in range(n_sub)]
            Xs = [X[i] for i in samp_idx]
            ys = [y[i] for i in samp_idx]
            bs_beta, _ok = fit_logit(Xs, ys)
            if len(bs_beta) > coh_idx:
                boot_coh.append(bs_beta[coh_idx])
        boot_coh.sort()
        lo95 = _percentile(boot_coh, 0.025) if boot_coh else 0.0
        hi95 = _percentile(boot_coh, 0.975) if boot_coh else 0.0
        ci_lo_corrected = (
            _percentile(boot_coh, corrected_alpha / 2.0) if boot_coh else 0.0
        )
        ci_hi_corrected = (
            _percentile(boot_coh, 1 - corrected_alpha / 2.0) if boot_coh else 0.0
        )
        out[d] = {
            "n": n_sub,
            "converged": bool(converged),
            "beta_coherence": round(beta[coh_idx], 6) if len(beta) > coh_idx else 0.0,
            "ci_95_lower": round(lo95, 6),
            "ci_95_upper": round(hi95, 6),
            "alpha_bonferroni": round(corrected_alpha, 6),
            "ci_corrected_lower": round(ci_lo_corrected, 6),
            "ci_corrected_upper": round(ci_hi_corrected, 6),
            "rejected_null_corrected": bool(
                ci_lo_corrected > 0 or ci_hi_corrected < 0
            ),
        }
    return out, insufficient


def _compose_seed(parent_seed: int, label: str) -> int:
    """Derive a child seed from a parent seed and a label.

    Same parent + same label always yields the same child, so per-domain
    bootstraps are themselves replayable.
    """

    h = hashlib.sha256(f"{parent_seed}:{label}".encode("utf-8")).hexdigest()
    return int(h[:16], 16)


# ---------------------------------------------------------------------------
# Optional library detection
# ---------------------------------------------------------------------------


def _detect_optional_libs() -> Dict[str, str]:
    """Record numpy / statsmodels / sklearn versions if importable.

    The functions in this module never *use* these libraries; they are
    captured purely so the report can disclose what was available in
    the runtime environment. The math is pure stdlib regardless.
    """

    out: Dict[str, str] = {}
    for name in ("numpy", "statsmodels", "sklearn"):
        try:
            mod = __import__(name)
            ver = getattr(mod, "__version__", "")
            out[name] = str(ver)
        except Exception:
            out[name] = "unavailable"
    return out


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------


def _canonical_coef(c: CoefficientEstimate) -> Dict[str, Any]:
    return {
        "ci_lower_95": round(c.ci_lower_95, 6),
        "ci_lower_99": round(c.ci_lower_99, 6),
        "ci_upper_95": round(c.ci_upper_95, 6),
        "ci_upper_99": round(c.ci_upper_99, 6),
        "name": c.name,
        "point": round(c.point, 6),
    }


def _canonical_bin(b: CalibrationBin) -> Dict[str, Any]:
    return {
        "bin_index": b.bin_index,
        "bin_lower": b.bin_lower,
        "bin_upper": b.bin_upper,
        "count": b.count,
        "mean_predicted": b.mean_predicted,
        "mean_realized": b.mean_realized,
    }


def _canonical_report_dict(report: StudyReport) -> Dict[str, Any]:
    """Return a fully-detached canonical dict.

    Inner dicts (``config``, ``preregistration``, ``metrics``, etc.) are
    deep-copied via a JSON round-trip so that callers may mutate the
    returned dict without corrupting the report's own state — a property
    the byte-determinism guarantee relies on. The round-trip also
    happens to normalize tuple/list edge cases so two runs produce the
    same bytes regardless of construction path.
    """

    raw = {
        "calibration_curve": [_canonical_bin(b) for b in report.calibration_curve],
        "coefficients": [_canonical_coef(c) for c in report.coefficients],
        "config": report.config,
        "data_hash": report.data_hash,
        "domain_breakdown": report.domain_breakdown,
        "generated_with": report.generated_with,
        "insufficient_subgroups": list(report.insufficient_subgroups),
        "metrics": report.metrics,
        "n_excluded_unknown": report.n_excluded_unknown,
        "n_known_outcome": report.n_known_outcome,
        "n_total": report.n_total,
        "preregistration": report.preregistration,
        "primary_hypothesis_result": report.primary_hypothesis_result,
        "schema_version": report.schema_version,
        "secondary_hypothesis_result": report.secondary_hypothesis_result,
    }
    return json.loads(json.dumps(raw, sort_keys=True, separators=(",", ":")))


def _config_audit(config: StudyConfig) -> Dict[str, Any]:
    def resolve(p: Optional[Path]) -> Optional[str]:
        return str(Path(p).resolve()) if p else None

    return {
        "audit_feature_extractors": list(config.audit_feature_extractors),
        "bootstrap_iters": int(config.bootstrap_iters),
        "coherence_scores_path": resolve(config.coherence_scores_path),
        "corpus_manifest_path": resolve(config.corpus_manifest_path),
        "default_check_size_usd": float(config.default_check_size_usd),
        "outcomes_path": resolve(config.outcomes_path),
        "output_path": resolve(config.output_path),
        "preregistration_path": resolve(config.preregistration_path),
        "seed": int(config.seed),
        "training_artifacts_index_path": resolve(
            config.training_artifacts_index_path
        ),
    }


def _load_corpus_for_audit(
    manifest_path: Optional[Path],
) -> Tuple[Mapping[str, Any], ...]:
    """Best-effort manifest loader for the leakage audit.

    The manifest is JSONL of historical_pitch.v1 rows. The audit only
    needs ``pitch_id`` and ``pitch_year`` plus any feature columns the
    operator wires in via ``audit_feature_extractors``. A missing
    manifest yields an empty corpus — the leakage audit then runs in
    its trivial-pass mode (no overlap can exist when no holdout can be
    inferred). Production runs MUST provide a manifest path; the
    operator runbook is in ``docs/specs/leakage_audit.md``.
    """

    if manifest_path is None:
        return ()
    p = Path(manifest_path)
    if not p.is_file():
        return ()
    rows: List[Mapping[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return tuple(rows)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_study(
    config: StudyConfig,
    *,
    frame: Optional[Sequence[StudyRow]] = None,
    n_total_override: Optional[int] = None,
    excluded_unknown_override: Optional[int] = None,
) -> StudyReport:
    """Run the deterministic validation study.

    Either ``frame`` is provided directly (tests inject a synthetic
    frame) or it is loaded from disk via :func:`load_frame` using the
    paths in ``config``. The pre-registered stopping rule is checked
    *before* any model is fit; failure raises
    :class:`InsufficientSampleError`.
    """

    prereg = load_preregistration(config.preregistration_path)
    sr = prereg["stopping_rule"]
    if not isinstance(sr, Mapping):
        raise PreregistrationError("stopping_rule must be a mapping")
    minimum_n = int(sr.get("minimum_n_with_known_outcome", 0))
    primary = prereg.get("primary_hypothesis") or {}
    if not isinstance(primary, Mapping):
        raise PreregistrationError("primary_hypothesis must be a mapping")
    primary_alpha = float(primary.get("alpha", 0.05))
    family_alpha = 0.05
    pdm = prereg.get("per_domain_models")
    if isinstance(pdm, Mapping):
        family_alpha = float(pdm.get("family_alpha", 0.05))

    if frame is None:
        rows, n_total, excluded_unknown = load_frame(
            corpus_manifest_path=config.corpus_manifest_path,
            outcomes_path=config.outcomes_path,
            coherence_scores_path=config.coherence_scores_path,
            default_check_size_usd=float(config.default_check_size_usd),
        )
    else:
        rows = sorted(list(frame), key=lambda r: r.pitch_id)
        n_total = (
            int(n_total_override) if n_total_override is not None else len(rows)
        )
        excluded_unknown = (
            int(excluded_unknown_override)
            if excluded_unknown_override is not None
            else 0
        )

    n_known = len(rows)
    if n_known < minimum_n:
        raise InsufficientSampleError(n=n_known, minimum=minimum_n)

    domain_levels = sorted({r.domain for r in rows})
    X, y, feature_names = _design_matrix(rows, domain_levels=domain_levels)
    beta, converged = fit_logit(X, y)
    if not converged:  # pragma: no cover - defensive
        # Continue with the last beta but flag in metrics; the report
        # still contains the bootstrap CIs which carry the uncertainty.
        pass
    probas = predict_proba(beta, X)

    boot_betas = _bootstrap_betas(
        rows,
        seed=config.seed,
        iters=int(config.bootstrap_iters),
        domain_levels=domain_levels,
    )

    coefficients: List[CoefficientEstimate] = []
    for j, name in enumerate(feature_names):
        col = sorted(b[j] for b in boot_betas if len(b) > j)
        if not col:
            coefficients.append(
                CoefficientEstimate(
                    name=name,
                    point=round(beta[j] if len(beta) > j else 0.0, 6),
                    ci_lower_95=0.0,
                    ci_upper_95=0.0,
                    ci_lower_99=0.0,
                    ci_upper_99=0.0,
                )
            )
            continue
        coefficients.append(
            CoefficientEstimate(
                name=name,
                point=round(beta[j], 6),
                ci_lower_95=round(_percentile(col, 0.025), 6),
                ci_upper_95=round(_percentile(col, 0.975), 6),
                ci_lower_99=round(_percentile(col, 0.005), 6),
                ci_upper_99=round(_percentile(col, 0.995), 6),
            )
        )

    coh_estimate = next(
        (c for c in coefficients if c.name == "coherence_score"), coefficients[0]
    )
    primary_result = _evaluate_primary(coh_estimate, alpha=primary_alpha)
    secondary_result = _evaluate_quintile_dose_response(rows)

    metrics = {
        "auc_roc": round(auc_roc(probas, y), 6),
        "brier_score": round(brier_score(probas, y), 6),
        "convergence": "converged" if converged else "not_converged",
        "mean_predicted_probability": round(sum(probas) / len(probas), 6)
        if probas
        else 0.0,
        "realized_positive_rate": round(sum(y) / len(y), 6) if y else 0.0,
    }
    calibration = calibration_curve(probas, y)

    domain_break, insufficient = _per_domain_models(
        rows,
        seed=config.seed,
        bootstrap_iters=int(config.bootstrap_iters),
        family_alpha=family_alpha,
    )

    data_hash = _hash_frame(rows)
    generated_with = _detect_optional_libs()
    generated_with["validation_study_schema"] = VALIDATION_STUDY_SCHEMA_VERSION

    # Mandatory leakage audit: refuse to render a final report unless
    # the audit passes (prompt 45). Empty corpus ⇒ trivial pass; the
    # production path always provides a corpus_manifest_path.
    from coherence_engine.server.fund.services import leakage_audit as _la

    audit_corpus = _load_corpus_for_audit(config.corpus_manifest_path)
    audit_cfg = _la.AuditConfig(
        corpus=audit_corpus,
        feature_extractors=tuple(config.audit_feature_extractors),
        training_artifacts_index_path=config.training_artifacts_index_path,
    )
    leakage_report = _la.audit(audit_cfg)
    _la.enforce(leakage_report)
    generated_with["leakage_audit_digest"] = leakage_report.audit_digest
    generated_with["leakage_audit_passed"] = "true"

    report = StudyReport(
        schema_version=VALIDATION_STUDY_SCHEMA_VERSION,
        generated_with=generated_with,
        config=_config_audit(config),
        preregistration=prereg,
        n_total=int(n_total),
        n_known_outcome=int(n_known),
        n_excluded_unknown=int(excluded_unknown),
        coefficients=tuple(coefficients),
        primary_hypothesis_result=primary_result,
        secondary_hypothesis_result=secondary_result,
        metrics=metrics,
        calibration_curve=calibration,
        domain_breakdown=domain_break,
        insufficient_subgroups=tuple(insufficient),
        data_hash=data_hash,
    )

    if config.output_path is not None:
        out = Path(config.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(report.to_canonical_bytes())

    return report


__all__ = [
    "DEFAULT_PREREGISTRATION_PATH",
    "InsufficientSampleError",
    "PreregistrationError",
    "StudyConfig",
    "StudyReport",
    "StudyRow",
    "VALIDATION_STUDY_SCHEMA_VERSION",
    "ValidationStudyError",
    "auc_roc",
    "brier_score",
    "calibration_curve",
    "fit_logit",
    "load_frame",
    "load_preregistration",
    "predict_proba",
    "run_study",
]
