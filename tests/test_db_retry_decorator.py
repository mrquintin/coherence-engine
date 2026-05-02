"""Unit tests for ``retry_transient_db_errors``.

These tests are pure-Python: no real DB connection is opened. We
construct synthetic ``OperationalError`` / ``DBAPIError`` instances with
the SQLSTATEs / ``connection_invalidated`` flags the decorator inspects
and verify that:

* Transient errors trigger retries with bounded backoff (full-jitter
  capped by ``max_delay_ms``).
* The deterministic injectable clock + RNG let us assert exact call
  counts and exact recorded sleep durations.
* Logic-bug errors (``IntegrityError`` / ``DataError``) are NOT retried.
* Retry budget is bounded — exhaustion raises the last exception.
"""

from __future__ import annotations

import logging
from typing import List

import pytest
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, OperationalError

from coherence_engine.server.fund.database import (
    _is_transient_db_error,
    retry_transient_db_errors,
)


class _DeterministicRNG:
    """Mimic ``random.Random.uniform`` from a scripted sequence of values
    in [0, 1]; the decorator multiplies by the cap so we can predict the
    exact delay it produces."""

    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    def uniform(self, lo: float, hi: float) -> float:
        if self._i >= len(self._seq):
            t = 0.5
        else:
            t = self._seq[self._i]
            self._i += 1
        return lo + (hi - lo) * t


class _RecordingSleeper:
    def __init__(self):
        self.calls: List[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _make_orig(pgcode: str | None = None, sqlstate: str | None = None):
    class _Orig(Exception):
        pass

    o = _Orig("synthetic")
    if pgcode is not None:
        o.pgcode = pgcode
    if sqlstate is not None:
        o.sqlstate = sqlstate
    return o


def _serialization_failure() -> DBAPIError:
    return DBAPIError("stmt", {}, _make_orig(pgcode="40001"))


def _deadlock_detected() -> DBAPIError:
    return DBAPIError("stmt", {}, _make_orig(pgcode="40P01"))


def _admin_shutdown() -> DBAPIError:
    return DBAPIError("stmt", {}, _make_orig(pgcode="57P01"))


def _connection_invalidated() -> OperationalError:
    exc = OperationalError("stmt", {}, _make_orig())
    exc.connection_invalidated = True  # type: ignore[attr-defined]
    return exc


def _plain_operational() -> OperationalError:
    return OperationalError("stmt", {}, _make_orig())


# ----------------------------------------------------------------------
# _is_transient_db_error
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        _serialization_failure,
        _deadlock_detected,
        _admin_shutdown,
        _connection_invalidated,
        _plain_operational,
    ],
)
def test_classifier_marks_transient(factory):
    assert _is_transient_db_error(factory()) is True


def test_classifier_rejects_integrity_error():
    err = IntegrityError("stmt", {}, _make_orig())
    assert _is_transient_db_error(err) is False


def test_classifier_rejects_data_error():
    err = DataError("stmt", {}, _make_orig())
    assert _is_transient_db_error(err) is False


def test_classifier_rejects_value_error():
    assert _is_transient_db_error(ValueError("not a db error")) is False


def test_classifier_rejects_dbapi_error_with_unknown_pgcode():
    err = DBAPIError("stmt", {}, _make_orig(pgcode="22000"))
    assert _is_transient_db_error(err) is False


# ----------------------------------------------------------------------
# Retry decorator behavior
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory,name",
    [
        (_serialization_failure, "SerializationFailure(40001)"),
        (_deadlock_detected, "DeadlockDetected(40P01)"),
        (_connection_invalidated, "OperationalError(connection_invalidated)"),
    ],
)
def test_retries_then_succeeds(factory, name):
    """Function raises N-1 times, then succeeds; assert call count + delays."""
    sleeper = _RecordingSleeper()
    rng = _DeterministicRNG([1.0, 1.0, 1.0])  # max-jitter — pick the cap

    calls = {"n": 0}

    @retry_transient_db_errors(
        max_attempts=4,
        base_delay_ms=50,
        max_delay_ms=2000,
        sleeper=sleeper,
        rng=rng,
    )
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise factory()
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3, f"{name}: expected 3 calls, got {calls['n']}"
    # Two sleeps between three attempts. With t=1.0 jitter we get exactly
    # the cap: 50ms (attempt 1->2), 100ms (attempt 2->3).
    assert sleeper.calls == pytest.approx([0.050, 0.100])


def test_retry_exhaustion_raises_last_exception(caplog):
    sleeper = _RecordingSleeper()
    rng = _DeterministicRNG([0.0, 0.0, 0.0])  # zero jitter

    @retry_transient_db_errors(
        max_attempts=3,
        base_delay_ms=50,
        max_delay_ms=2000,
        sleeper=sleeper,
        rng=rng,
    )
    def always_fails():
        raise _serialization_failure()

    caplog.set_level(logging.ERROR)
    with pytest.raises(DBAPIError):
        always_fails()
    # Two sleeps between 3 attempts.
    assert len(sleeper.calls) == 2
    # Exhaustion log is emitted exactly once.
    exhausted = [r for r in caplog.records if r.message == "db.retry.exhausted"]
    assert len(exhausted) == 1
    assert getattr(exhausted[0], "attempts", None) == 3


def test_non_transient_error_is_not_retried():
    sleeper = _RecordingSleeper()

    @retry_transient_db_errors(
        max_attempts=4,
        base_delay_ms=50,
        max_delay_ms=2000,
        sleeper=sleeper,
        rng=_DeterministicRNG([0.0]),
    )
    def bad_logic():
        raise IntegrityError("stmt", {}, _make_orig())

    with pytest.raises(IntegrityError):
        bad_logic()
    # Zero retries — non-transient errors short-circuit immediately.
    assert sleeper.calls == []


def test_backoff_is_capped_by_max_delay():
    """Even with maximum jitter, the delay never exceeds ``max_delay_ms``."""
    sleeper = _RecordingSleeper()
    rng = _DeterministicRNG([1.0] * 10)

    calls = {"n": 0}

    @retry_transient_db_errors(
        max_attempts=8,
        base_delay_ms=200,
        max_delay_ms=500,
        sleeper=sleeper,
        rng=rng,
    )
    def flaky():
        calls["n"] += 1
        if calls["n"] < 8:
            raise _deadlock_detected()
        return "ok"

    assert flaky() == "ok"
    # 7 sleeps; each <= max_delay_ms / 1000.
    assert all(s <= 0.500 + 1e-9 for s in sleeper.calls)
    # And growth honors the exponential schedule until the cap kicks in.
    # Expected caps: 200, 400, 500, 500, 500, 500, 500 (in ms).
    assert sleeper.calls == pytest.approx([0.200, 0.400, 0.500, 0.500, 0.500, 0.500, 0.500])


def test_decorator_validates_arguments():
    with pytest.raises(ValueError):
        retry_transient_db_errors(max_attempts=0)
    with pytest.raises(ValueError):
        retry_transient_db_errors(base_delay_ms=-1)
    with pytest.raises(ValueError):
        retry_transient_db_errors(base_delay_ms=100, max_delay_ms=50)


def test_max_attempts_one_means_no_retry():
    sleeper = _RecordingSleeper()

    @retry_transient_db_errors(
        max_attempts=1,
        base_delay_ms=50,
        max_delay_ms=2000,
        sleeper=sleeper,
        rng=_DeterministicRNG([0.0]),
    )
    def flaky():
        raise _serialization_failure()

    with pytest.raises(DBAPIError):
        flaky()
    assert sleeper.calls == []


def test_decorator_preserves_function_metadata():
    @retry_transient_db_errors(max_attempts=2)
    def my_named_function(x):
        """Doc string."""
        return x * 2

    assert my_named_function.__name__ == "my_named_function"
    assert my_named_function.__doc__ == "Doc string."
    assert my_named_function(3) == 6
