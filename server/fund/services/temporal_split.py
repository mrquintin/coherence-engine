"""Strict temporal pre/post split for the historical-validation corpus.

This module implements the look-ahead-bias guard for the predictive-
validity study (prompt 45, Wave 12). The corpus is partitioned by
``pitch_year`` into a *training* window (pre-2020) and a *holdout*
window (post-2020), with a *buffer year* of 2020 itself excluded from
both partitions. The buffer is what defends the holdout from contamination
by pitches whose outcomes were still being observed when training-side
artifacts (the contradiction-direction vector ĉ, the anti-gaming
templates, the calibration curves) were last refit.

The split is intentionally based on the integer ``pitch_year`` field of
the corpus row (see ``server/fund/schemas/datasets/historical_pitch.v1
.json``). String YYYY-MM-DD inputs are also accepted so the function can
be called against outcome rows or arbitrary date-bearing dicts.

Public surface
--------------

* :class:`SplitConfig` — frozen dataclass capturing
  ``train_end_year`` / ``buffer_year`` / ``holdout_start_year``.
* :class:`SplitResult` — ``(train, holdout, buffer_excluded)`` with
  per-side pitch_id lists kept in deterministic sorted order.
* :func:`split` — apply the split to an iterable of corpus rows.
* :func:`pitch_year_of` — robust extractor; understands integer
  ``pitch_year``, ``YYYY-MM-DD`` strings, and pre-parsed dates.

The defaults (train_end="2019-12-31", buffer_year=2020,
holdout_start="2021-01-01") are pinned in the docstring of
``leakage_audit.audit`` as well; shrinking the buffer year requires an
explicit operator override and a written rationale in the study YAML
per the prompt 45 prohibitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, List, Mapping, Optional, Tuple


_DEFAULT_TRAIN_END = "2019-12-31"
_DEFAULT_BUFFER_YEAR = 2020
_DEFAULT_HOLDOUT_START = "2021-01-01"


class TemporalSplitError(ValueError):
    """Raised when the temporal-split configuration is internally inconsistent."""


@dataclass(frozen=True)
class SplitConfig:
    """Inputs that pin the temporal split.

    ``train_end_year`` is the *inclusive* upper bound of the training
    window; ``holdout_start_year`` is the *inclusive* lower bound of the
    holdout window; everything strictly between them (``buffer_year``)
    is excluded from both partitions.
    """

    train_end_year: int = 2019
    buffer_year: int = _DEFAULT_BUFFER_YEAR
    holdout_start_year: int = 2021

    def __post_init__(self) -> None:
        if self.train_end_year >= self.holdout_start_year:
            raise TemporalSplitError(
                "train_end_year must be strictly less than holdout_start_year "
                f"(got train_end_year={self.train_end_year}, "
                f"holdout_start_year={self.holdout_start_year})"
            )
        if not (self.train_end_year < self.buffer_year < self.holdout_start_year):
            raise TemporalSplitError(
                "buffer_year must lie strictly between train_end_year and "
                f"holdout_start_year (got buffer_year={self.buffer_year})"
            )


@dataclass(frozen=True)
class SplitResult:
    """Result of :func:`split`.

    ``train`` and ``holdout`` are lists of corpus rows (dicts) preserved
    as-is from the input, in pitch_id-sorted order. ``buffer_excluded``
    holds rows whose ``pitch_year`` falls inside the buffer window and
    which therefore belong to neither side.
    """

    train: Tuple[Mapping[str, Any], ...]
    holdout: Tuple[Mapping[str, Any], ...]
    buffer_excluded: Tuple[Mapping[str, Any], ...]
    config: SplitConfig
    undated_excluded: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def counts(self) -> dict:
        return {
            "train": len(self.train),
            "holdout": len(self.holdout),
            "buffer_excluded": len(self.buffer_excluded),
            "undated_excluded": len(self.undated_excluded),
        }


def pitch_year_of(row: Mapping[str, Any]) -> Optional[int]:
    """Best-effort extractor for the year a pitch was recorded.

    Recognized fields, in order: ``pitch_year`` (int), ``pitch_date``,
    ``date``, ``outcome_as_of`` — for the latter three, the value may be
    a ``YYYY-MM-DD`` string, a ``date``, or a ``datetime``. Returns
    ``None`` when no usable year can be derived; the caller decides
    whether that is fatal (the split places undated rows in
    ``undated_excluded``).
    """

    yr = row.get("pitch_year")
    if isinstance(yr, int):
        return yr
    if isinstance(yr, str) and yr.isdigit():
        return int(yr)
    for key in ("pitch_date", "date", "outcome_as_of"):
        v = row.get(key)
        if v is None:
            continue
        if isinstance(v, datetime):
            return v.year
        if isinstance(v, date):
            return v.year
        if isinstance(v, str):
            try:
                return datetime.strptime(v[:10], "%Y-%m-%d").year
            except ValueError:
                continue
    return None


def _parse_year_arg(value: Any, *, label: str) -> int:
    """Accept either an integer year or a ``YYYY-...`` string; return year."""

    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value[:4])
        except ValueError as exc:
            raise TemporalSplitError(
                f"{label}: cannot parse year from {value!r}"
            ) from exc
    raise TemporalSplitError(f"{label}: unsupported type {type(value).__name__}")


def split(
    corpus: Iterable[Mapping[str, Any]],
    *,
    train_end: Any = _DEFAULT_TRAIN_END,
    buffer_year: int = _DEFAULT_BUFFER_YEAR,
    holdout_start: Any = _DEFAULT_HOLDOUT_START,
) -> SplitResult:
    """Partition ``corpus`` into pre-/post-buffer subsets.

    Parameters
    ----------
    corpus:
        Iterable of dict-like corpus rows. Rows are not mutated.
    train_end:
        Inclusive upper edge of the training window. Either an integer
        year (``2019``) or a ``YYYY-MM-DD`` string (``"2019-12-31"``).
    buffer_year:
        Year *fully excluded* from both partitions. Defaults to 2020 —
        the look-ahead-bias buffer pinned in
        ``docs/specs/leakage_audit.md``.
    holdout_start:
        Inclusive lower edge of the holdout window. Either an integer
        year or a ``YYYY-MM-DD`` string.

    Returns
    -------
    SplitResult
        Train / holdout / buffer-excluded / undated buckets.
    """

    train_end_year = _parse_year_arg(train_end, label="train_end")
    holdout_start_year = _parse_year_arg(holdout_start, label="holdout_start")
    config = SplitConfig(
        train_end_year=train_end_year,
        buffer_year=int(buffer_year),
        holdout_start_year=holdout_start_year,
    )

    train: List[Mapping[str, Any]] = []
    holdout: List[Mapping[str, Any]] = []
    buffered: List[Mapping[str, Any]] = []
    undated: List[Mapping[str, Any]] = []

    for row in corpus:
        if not isinstance(row, Mapping):
            raise TemporalSplitError(
                f"corpus rows must be Mapping; got {type(row).__name__}"
            )
        year = pitch_year_of(row)
        if year is None:
            undated.append(row)
            continue
        if year <= config.train_end_year:
            train.append(row)
        elif year >= config.holdout_start_year:
            holdout.append(row)
        else:
            # year is in (train_end_year, holdout_start_year), i.e. the
            # buffer window. Reject the row from both partitions.
            buffered.append(row)

    def _key(r: Mapping[str, Any]) -> str:
        return str(r.get("pitch_id") or "")

    return SplitResult(
        train=tuple(sorted(train, key=_key)),
        holdout=tuple(sorted(holdout, key=_key)),
        buffer_excluded=tuple(sorted(buffered, key=_key)),
        config=config,
        undated_excluded=tuple(sorted(undated, key=_key)),
    )


__all__ = [
    "SplitConfig",
    "SplitResult",
    "TemporalSplitError",
    "pitch_year_of",
    "split",
]
