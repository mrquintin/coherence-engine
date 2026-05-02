"""Read/record-only repository for portfolio state and positions.

This repository is strictly a persistence and query layer:

* No trades, transfers, or live ledger writes are performed here.
* ``set_liquidity_reserve`` and ``record_position`` only append rows; they
  never mutate existing rows.
* The "current" state is defined as the row in ``portfolio_state`` with the
  largest ``as_of`` (ties broken by ``id``).

Used by :mod:`coherence_engine.server.fund.services.decision_policy` to feed
real-portfolio values into the ``R(S, portfolio_state)`` terms of the
decision predicate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.repositories import resolve_read_session


_KNOWN_REGIMES = frozenset({"normal", "stress", "recovery"})


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _normalize_regime(raw: Optional[str]) -> str:
    k = (raw or "normal").strip().lower()
    return k if k in _KNOWN_REGIMES else "normal"


class PortfolioRepository:
    """Query + append-only writer for ``portfolio_state`` and ``positions``.

    Reads honor a caller-supplied ``session=`` override and an optional
    ``read_only=True`` routing flag. With ``read_only=True`` and no
    explicit session, a fresh session is opened against the read replica
    (or the primary, if no replica is configured) for the call's
    duration. Writes always use the constructor's primary session.
    """

    def __init__(self, db: Optional[Session] = None):
        self.db = db

    def _require_primary(self) -> Session:
        if self.db is None:
            raise RuntimeError(
                "PortfolioRepository write methods require a primary Session "
                "passed to the constructor"
            )
        return self.db

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def latest_state(
        self,
        *,
        session: Optional[Session] = None,
        read_only: bool = False,
    ) -> Optional[models.PortfolioState]:
        """Return the most recent portfolio snapshot, or ``None`` if empty."""
        stmt = (
            select(models.PortfolioState)
            .order_by(
                models.PortfolioState.as_of.desc(),
                models.PortfolioState.id.desc(),
            )
            .limit(1)
        )
        with resolve_read_session(session, self.db, read_only=read_only) as db:
            return db.execute(stmt).scalar_one_or_none()

    def active_positions_by_domain(
        self,
        *,
        session: Optional[Session] = None,
        read_only: bool = False,
    ) -> Dict[str, float]:
        """Return ``{domain: total_active_invested_usd}``.

        Aggregates ``invested_usd`` over rows with ``status == "active"``.
        Missing domain rows are skipped; the result is deterministic and
        sorted by key (handy for stable serialization in the CLI).
        """
        stmt = (
            select(
                models.Position.domain,
                func.coalesce(func.sum(models.Position.invested_usd), 0.0),
            )
            .where(models.Position.status == "active")
            .group_by(models.Position.domain)
        )
        with resolve_read_session(session, self.db, read_only=read_only) as db:
            rows = db.execute(stmt).all()
        return {str(domain): float(total or 0.0) for domain, total in sorted(rows)}

    def active_positions_total(
        self,
        *,
        session: Optional[Session] = None,
        read_only: bool = False,
    ) -> float:
        stmt = (
            select(func.coalesce(func.sum(models.Position.invested_usd), 0.0))
            .where(models.Position.status == "active")
        )
        with resolve_read_session(session, self.db, read_only=read_only) as db:
            return float(db.execute(stmt).scalar_one() or 0.0)

    # ------------------------------------------------------------------
    # Append-only write side
    # ------------------------------------------------------------------

    def set_liquidity_reserve(
        self,
        usd: float,
        *,
        note: Optional[str] = None,
    ) -> models.PortfolioState:
        """Append a new snapshot with the liquidity reserve set to ``usd``.

        All other fields (fund NAV, drawdown proxy, regime) are carried
        forward from the previous snapshot when present, otherwise seeded
        with zeros / ``"normal"``. This is strictly an append: the previous
        row is left unchanged.
        """
        if usd < 0.0:
            raise ValueError(f"liquidity_reserve_usd must be >= 0, got {usd}")
        db = self._require_primary()
        prev = self.latest_state(session=db)
        row = models.PortfolioState(
            as_of=_utc_now(),
            fund_nav_usd=float(prev.fund_nav_usd) if prev is not None else 0.0,
            liquidity_reserve_usd=float(usd),
            drawdown_proxy=float(prev.drawdown_proxy) if prev is not None else 0.0,
            regime=str(prev.regime) if prev is not None else "normal",
            note=note,
        )
        db.add(row)
        db.flush()
        return row

    def record_state(
        self,
        *,
        fund_nav_usd: float,
        liquidity_reserve_usd: float,
        drawdown_proxy: float = 0.0,
        regime: str = "normal",
        note: Optional[str] = None,
        as_of: Optional[datetime] = None,
    ) -> models.PortfolioState:
        """Append a fully-specified portfolio-state snapshot."""
        if fund_nav_usd < 0.0:
            raise ValueError("fund_nav_usd must be >= 0")
        if liquidity_reserve_usd < 0.0:
            raise ValueError("liquidity_reserve_usd must be >= 0")
        dd = max(0.0, min(1.0, float(drawdown_proxy)))
        row = models.PortfolioState(
            as_of=as_of or _utc_now(),
            fund_nav_usd=float(fund_nav_usd),
            liquidity_reserve_usd=float(liquidity_reserve_usd),
            drawdown_proxy=dd,
            regime=_normalize_regime(regime),
            note=note,
        )
        db = self._require_primary()
        db.add(row)
        db.flush()
        return row

    def record_position(
        self,
        *,
        application_id: str,
        domain: str,
        invested_usd: float,
        status: str = "active",
    ) -> models.Position:
        """Append a new position row.

        This is an append-only record for offline audit/analytics. It does
        NOT perform any trade, wire, or ledger mutation.
        """
        if invested_usd < 0.0:
            raise ValueError("invested_usd must be >= 0")
        if status not in {"active", "wound_down", "exited"}:
            raise ValueError(
                f"status must be one of 'active' | 'wound_down' | 'exited', got {status!r}"
            )
        row = models.Position(
            application_id=str(application_id),
            domain=str(domain),
            invested_usd=float(invested_usd),
            status=str(status),
        )
        db = self._require_primary()
        db.add(row)
        db.flush()
        return row

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def latest_state_as_dict(
        self,
        *,
        session: Optional[Session] = None,
        read_only: bool = False,
    ) -> Optional[dict]:
        row = self.latest_state(session=session, read_only=read_only)
        if row is None:
            return None
        return {
            "id": int(row.id),
            "as_of": row.as_of.isoformat() if row.as_of else None,
            "fund_nav_usd": float(row.fund_nav_usd),
            "liquidity_reserve_usd": float(row.liquidity_reserve_usd),
            "drawdown_proxy": float(row.drawdown_proxy),
            "regime": str(row.regime),
            "note": row.note,
        }

    def domain_concentration_by_nav(
        self,
        *,
        session: Optional[Session] = None,
        read_only: bool = False,
    ) -> Dict[str, float]:
        """Return ``{domain: invested_usd / fund_nav_usd}`` for active positions."""
        state = self.latest_state(session=session, read_only=read_only)
        nav = float(state.fund_nav_usd) if state is not None else 0.0
        totals = self.active_positions_by_domain(session=session, read_only=read_only)
        if nav <= 0.0:
            return {k: 0.0 for k in totals}
        return {k: v / nav for k, v in totals.items()}


__all__ = ["PortfolioRepository"]
