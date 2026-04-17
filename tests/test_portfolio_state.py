"""Tests for portfolio_state persistence + concentration policy (prompt 10)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.repositories.portfolio_repository import (
    PortfolioRepository,
)
from coherence_engine.server.fund.services.decision_policy import (
    DecisionPolicyService,
    PortfolioSnapshot,
    PortfolioStateProvider,
    portfolio_snapshot_from_repository,
    snapshot_to_portfolio_state,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fund_tables():
    """Recreate the schema between tests (mirrors other fund-table tests)."""
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _passing_score(ci_lower: float = 0.85) -> dict:
    return {
        "transcript_quality_score": 0.95,
        "anti_gaming_score": 0.1,
        "coherence_superiority_ci95": {"lower": ci_lower, "upper": min(ci_lower + 0.05, 0.99)},
    }


# ---------------------------------------------------------------------------
# Migration round-trip (SQLite, isolated DB)
# ---------------------------------------------------------------------------


def test_alembic_upgrade_downgrade_round_trip_sqlite(tmp_path: Path):
    """The ``20260417_000003_portfolio_state`` migration round-trips on SQLite.

    Several earlier migrations in the chain emit DDL that SQLite cannot
    execute in-place (e.g. ``ALTER TABLE ... DROP DEFAULT``); they are
    out-of-scope for this prompt, so we deliberately bypass them by:

    1. Creating the full ORM-declared schema via
       ``Base.metadata.create_all`` (which already includes the new
       ``portfolio_state`` and ``positions`` tables defined in this prompt).
    2. Stamping Alembic's revision marker at ``20260417_000003`` so the
       migration framework treats the database as fully up-to-date.
    3. Running ``downgrade -1`` to exercise *this* migration's
       ``downgrade()``.
    4. Re-running ``upgrade +1`` to exercise its ``upgrade()`` and confirm
       the tables + indexes are recreated identically.

    Because the new tables are append-only and never touched by the
    earlier migrations, this still gives us a true round-trip assertion
    for the migration introduced in prompt 10.
    """
    from alembic import command
    from alembic.config import Config

    from coherence_engine.server.fund import config as fund_config

    db_path = tmp_path / "rt.db"
    url = f"sqlite:///{db_path}"

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    prev_env = os.environ.get("COHERENCE_FUND_DATABASE_URL")
    prev_settings_url = fund_config.settings.DATABASE_URL
    os.environ["COHERENCE_FUND_DATABASE_URL"] = url
    fund_config.settings.DATABASE_URL = url
    try:
        rt_engine = create_engine(url, future=True)
        Base.metadata.create_all(bind=rt_engine)
        rt_engine.dispose()

        command.stamp(cfg, "20260417_000003")

        rt_engine = create_engine(url, future=True)
        try:
            insp = inspect(rt_engine)
            tables = set(insp.get_table_names())
            assert "portfolio_state" in tables
            assert "positions" in tables
        finally:
            rt_engine.dispose()

        command.downgrade(cfg, "-1")
        rt_engine = create_engine(url, future=True)
        try:
            insp = inspect(rt_engine)
            tables = set(insp.get_table_names())
            assert "portfolio_state" not in tables
            assert "positions" not in tables
        finally:
            rt_engine.dispose()

        command.upgrade(cfg, "+1")
        rt_engine = create_engine(url, future=True)
        try:
            insp = inspect(rt_engine)
            tables = set(insp.get_table_names())
            assert "portfolio_state" in tables
            assert "positions" in tables

            ps_idx = {idx["name"] for idx in insp.get_indexes("portfolio_state")}
            assert "ix_portfolio_state_as_of" in ps_idx

            pos_idx = {idx["name"] for idx in insp.get_indexes("positions")}
            assert "ix_positions_domain_status" in pos_idx
        finally:
            rt_engine.dispose()
    finally:
        fund_config.settings.DATABASE_URL = prev_settings_url
        if prev_env is None:
            os.environ.pop("COHERENCE_FUND_DATABASE_URL", None)
        else:
            os.environ["COHERENCE_FUND_DATABASE_URL"] = prev_env


# ---------------------------------------------------------------------------
# Repository behavior
# ---------------------------------------------------------------------------


def test_repository_records_state_and_positions_round_trip():
    session: Session = SessionLocal()
    try:
        repo = PortfolioRepository(session)
        assert repo.latest_state() is None
        assert repo.active_positions_by_domain() == {}

        repo.record_state(
            fund_nav_usd=10_000_000.0,
            liquidity_reserve_usd=500_000.0,
            drawdown_proxy=0.05,
            regime="normal",
        )
        repo.record_position(
            application_id="app_a",
            domain="market_economics",
            invested_usd=2_500_000.0,
            status="active",
        )
        repo.record_position(
            application_id="app_b",
            domain="public_health",
            invested_usd=1_500_000.0,
            status="active",
        )
        repo.record_position(
            application_id="app_c",
            domain="market_economics",
            invested_usd=999_999.0,
            status="wound_down",
        )
        session.commit()

        latest = repo.latest_state()
        assert latest is not None
        assert latest.fund_nav_usd == 10_000_000.0
        assert latest.liquidity_reserve_usd == 500_000.0

        totals = repo.active_positions_by_domain()
        assert totals == {
            "market_economics": 2_500_000.0,
            "public_health": 1_500_000.0,
        }

        concentration = repo.domain_concentration_by_nav()
        assert concentration["market_economics"] == pytest.approx(0.25)
        assert concentration["public_health"] == pytest.approx(0.15)
    finally:
        session.close()


def test_set_liquidity_reserve_appends_new_row_without_mutating_previous():
    session: Session = SessionLocal()
    try:
        repo = PortfolioRepository(session)
        first = repo.record_state(
            fund_nav_usd=8_000_000.0,
            liquidity_reserve_usd=400_000.0,
            drawdown_proxy=0.02,
            regime="normal",
        )
        session.commit()
        first_id = int(first.id)
        first_reserve = float(first.liquidity_reserve_usd)

        new_row = repo.set_liquidity_reserve(750_000.0, note="reserve up after Q3")
        session.commit()

        assert int(new_row.id) != first_id
        assert float(new_row.liquidity_reserve_usd) == 750_000.0
        assert float(new_row.fund_nav_usd) == 8_000_000.0  # carried forward
        assert new_row.regime == "normal"

        session.expire_all()
        previous = session.get(models.PortfolioState, first_id)
        assert previous is not None
        assert float(previous.liquidity_reserve_usd) == first_reserve
    finally:
        session.close()


def test_repository_rejects_invalid_inputs():
    session: Session = SessionLocal()
    try:
        repo = PortfolioRepository(session)
        with pytest.raises(ValueError):
            repo.set_liquidity_reserve(-1.0)
        with pytest.raises(ValueError):
            repo.record_state(fund_nav_usd=-1.0, liquidity_reserve_usd=0.0)
        with pytest.raises(ValueError):
            repo.record_position(
                application_id="x", domain="d", invested_usd=10.0, status="bogus"
            )
        with pytest.raises(ValueError):
            repo.record_position(
                application_id="x", domain="d", invested_usd=-5.0, status="active"
            )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Provider integration with decision_policy
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Test double satisfying the :class:`PortfolioStateProvider` Protocol."""

    def __init__(self, snapshot=None):
        self._snapshot = snapshot

    def get_snapshot(self):
        return self._snapshot


def test_default_fake_snapshot_is_backward_compatible_with_legacy_envelope():
    """A None-snapshot provider must yield the pre-change decision shape."""
    provider = _FakeProvider(snapshot=None)
    policy = DecisionPolicyService(portfolio_provider=provider)
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
    }

    out_with = policy.evaluate(app, _passing_score())
    out_without = DecisionPolicyService().evaluate(app, _passing_score())

    assert out_with == out_without
    assert "portfolio_adjustments" not in out_with
    assert out_with["policy_version"] == "decision-policy-v1.0.0"


def test_provider_snapshot_routes_into_portfolio_adjustments():
    snap = PortfolioSnapshot(
        fund_nav_usd=10_000_000.0,
        liquidity_reserve_usd=600_000.0,
        drawdown_proxy=0.05,
        regime="normal",
        domain_invested_usd={"market_economics": 1_000_000.0},
    )
    policy = DecisionPolicyService(portfolio_provider=_FakeProvider(snap))
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 100_000,
        "compliance_status": "clear",
    }
    out = policy.evaluate(app, _passing_score())
    assert "portfolio_adjustments" in out
    adj = out["portfolio_adjustments"]
    assert adj["portfolio_snapshot_source"] == "provider"
    assert adj["committed_pass_usd_excl_current"] == pytest.approx(1_000_000.0)


def test_explicit_portfolio_state_takes_precedence_over_provider():
    snap = PortfolioSnapshot(
        fund_nav_usd=10_000_000.0,
        liquidity_reserve_usd=600_000.0,
        domain_invested_usd={"market_economics": 1_000_000.0},
    )
    policy = DecisionPolicyService(portfolio_provider=_FakeProvider(snap))
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
    }
    explicit = {
        "notional_capacity_usd": 5_000_000.0,
        "committed_pass_usd_excl_current": 0.0,
    }
    out = policy.evaluate(app, _passing_score(), portfolio_state=explicit)
    adj = out.get("portfolio_adjustments", {})
    if adj:
        assert adj.get("portfolio_snapshot_source") == "explicit_mapping"
        assert adj["committed_pass_usd_excl_current"] == 0.0


def test_high_domain_concentration_raises_cs_required_by_measurable_delta():
    """A snapshot with high single-domain USD exposure must lift cs_required.

    We compare two snapshots that are identical except for the per-domain
    USD exposure of ``market_economics``: a low-concentration baseline
    (~5% of NAV) and a high-concentration variant (~35% of NAV). The
    high-concentration variant must produce a strictly larger
    ``threshold_required`` and a non-zero ``r_domain_usd`` audit term.
    """
    nav = 10_000_000.0
    requested = 250_000  # small enough to keep gates passing on the low side
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": requested,
        "compliance_status": "clear",
    }

    low_snap = PortfolioSnapshot(
        fund_nav_usd=nav,
        liquidity_reserve_usd=nav * 0.05,
        drawdown_proxy=0.0,
        regime="normal",
        domain_invested_usd={"market_economics": 500_000.0},  # 5%
    )
    high_snap = PortfolioSnapshot(
        fund_nav_usd=nav,
        liquidity_reserve_usd=nav * 0.05,
        drawdown_proxy=0.0,
        regime="normal",
        domain_invested_usd={"market_economics": 3_500_000.0},  # 35%
    )

    policy_low = DecisionPolicyService(portfolio_provider=_FakeProvider(low_snap))
    policy_high = DecisionPolicyService(portfolio_provider=_FakeProvider(high_snap))

    out_low = policy_low.evaluate(app, _passing_score(ci_lower=0.95))
    out_high = policy_high.evaluate(app, _passing_score(ci_lower=0.95))

    assert out_high["threshold_required"] > out_low["threshold_required"]
    delta = out_high["threshold_required"] - out_low["threshold_required"]
    assert delta >= 0.005  # at least the first r_domain_usd step

    high_audit = out_high["portfolio_adjustments"]["r_term_audit"]
    assert high_audit["r_domain_usd"] > 0.0


def test_portfolio_snapshot_from_repository_reads_real_state():
    session: Session = SessionLocal()
    try:
        repo = PortfolioRepository(session)
        repo.record_state(
            fund_nav_usd=4_000_000.0,
            liquidity_reserve_usd=200_000.0,
            drawdown_proxy=0.1,
            regime="stress",
        )
        repo.record_position(
            application_id="a1",
            domain="market_economics",
            invested_usd=750_000.0,
            status="active",
        )
        session.commit()

        snap = portfolio_snapshot_from_repository(repo)
        assert snap is not None
        assert isinstance(snap, PortfolioSnapshot)
        assert snap.fund_nav_usd == 4_000_000.0
        assert snap.liquidity_reserve_usd == 200_000.0
        assert snap.regime == "stress"
        assert snap.domain_invested_usd == {"market_economics": 750_000.0}

        ps = snapshot_to_portfolio_state(snap, domain_primary="market_economics")
        assert ps is not None
        assert ps["notional_capacity_usd"] == 4_000_000.0
        assert ps["domain_pass_committed_usd_excl_current"] == 750_000.0
        assert ps["portfolio_regime_code"] == "stress"
    finally:
        session.close()


def test_snapshot_to_portfolio_state_passes_none_through():
    assert snapshot_to_portfolio_state(None) is None


def test_portfolio_state_provider_is_protocol_compliant():
    assert isinstance(_FakeProvider(snapshot=None), PortfolioStateProvider)


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def _run_cli(*args: str, cwd: Path | None = None, db_url: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    parent = str(REPO_ROOT.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = parent + (os.pathsep + existing if existing else "")
    if db_url is not None:
        env["COHERENCE_FUND_DATABASE_URL"] = db_url
    return subprocess.run(
        [sys.executable, "-m", "coherence_engine", "portfolio-state", *args],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_show_on_empty_database_returns_null_state(tmp_path: Path):
    db_path = tmp_path / "cli_show.db"
    proc = _run_cli("show", db_url=f"sqlite:///{db_path}", cwd=tmp_path)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout)
    assert payload["state"] is None
    assert payload["active_positions_by_domain_usd"] == {}
    assert payload["domain_concentration_by_nav"] == {}


def test_cli_set_reserve_then_show_round_trip(tmp_path: Path):
    db_path = tmp_path / "cli_set.db"
    db_url = f"sqlite:///{db_path}"

    set_proc = _run_cli("set-reserve", "--usd", "1234.5", "--note", "smoke", db_url=db_url, cwd=tmp_path)
    assert set_proc.returncode == 0, f"stdout={set_proc.stdout!r} stderr={set_proc.stderr!r}"
    set_payload = json.loads(set_proc.stdout)
    assert set_payload["liquidity_reserve_usd"] == 1234.5
    assert set_payload["note"] == "smoke"

    show_proc = _run_cli("show", db_url=db_url, cwd=tmp_path)
    assert show_proc.returncode == 0, f"stdout={show_proc.stdout!r} stderr={show_proc.stderr!r}"
    show_payload = json.loads(show_proc.stdout)
    assert show_payload["state"]["liquidity_reserve_usd"] == 1234.5
    assert show_payload["state"]["regime"] == "normal"


def test_cli_set_reserve_rejects_negative(tmp_path: Path):
    db_path = tmp_path / "cli_neg.db"
    proc = _run_cli("set-reserve", "--usd", "-1", db_url=f"sqlite:///{db_path}", cwd=tmp_path)
    assert proc.returncode == 2
    assert "must be >= 0" in proc.stderr
