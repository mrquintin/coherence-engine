"""Deterministic decision policy service."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple, runtime_checkable


DECISION_POLICY_VERSION = "decision-policy-v1"

_NOTIONAL_CAPACITY_USD_DEFAULT = 12_000_000.0
_LIQUIDITY_RESERVE_FRACTION_DEFAULT = 0.05

_KNOWN_REGIMES = frozenset({"neutral", "stress", "defensive", "expansion"})

_PERSISTED_REGIME_TO_POLICY: Mapping[str, str] = {
    "normal": "neutral",
    "recovery": "expansion",
    "stress": "stress",
}


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Immutable view of persisted portfolio state used by R(S, portfolio_state).

    Fields are expressed in native units (USD, dimensionless ratios in
    ``[0, 1]``). ``domain_invested_usd`` maps domain keys (e.g.
    ``"market_economics"``) to the sum of active position USD for that
    domain. ``regime`` uses the persisted vocabulary
    (``"normal" | "stress" | "recovery"``); ``decision_policy.evaluate``
    translates it into the policy regime code (``"neutral" | "stress" |
    "expansion"``) before running the ``R`` terms.

    Attributes:
        fund_nav_usd: Total notional capacity of the fund.
        liquidity_reserve_usd: USD held back as reserve (floor).
        drawdown_proxy: Portfolio drawdown proxy in ``[0, 1]``.
        regime: ``"normal" | "stress" | "recovery"``.
        domain_invested_usd: ``{domain_key: total_active_invested_usd}``.
        as_of: Optional wall-clock timestamp (for audit only — never read
            by the deterministic policy path).
    """

    fund_nav_usd: float = 0.0
    liquidity_reserve_usd: float = 0.0
    drawdown_proxy: float = 0.0
    regime: str = "normal"
    domain_invested_usd: Mapping[str, float] = field(default_factory=dict)
    as_of: Optional[datetime] = None


@runtime_checkable
class PortfolioStateProvider(Protocol):
    """Read-only source of the most recent :class:`PortfolioSnapshot`.

    Production code wires this to
    :class:`coherence_engine.server.fund.repositories.portfolio_repository.PortfolioRepository`;
    tests can inject a fake that returns a deterministic snapshot.
    """

    def get_snapshot(self) -> Optional[PortfolioSnapshot]:  # pragma: no cover - protocol
        ...


def portfolio_snapshot_from_repository(repo) -> Optional[PortfolioSnapshot]:
    """Build a :class:`PortfolioSnapshot` from a ``PortfolioRepository``.

    Returns ``None`` when the repository has no recorded state, so callers
    can fall back to the pre-persistence defaults. Accepts any duck-typed
    object exposing ``latest_state`` and ``active_positions_by_domain``
    to keep the import graph acyclic (no hard dependency on the repo
    module from the policy module).
    """
    if repo is None:
        return None
    state = getattr(repo, "latest_state")()
    if state is None:
        return None
    totals = getattr(repo, "active_positions_by_domain")()
    return PortfolioSnapshot(
        fund_nav_usd=float(getattr(state, "fund_nav_usd", 0.0) or 0.0),
        liquidity_reserve_usd=float(getattr(state, "liquidity_reserve_usd", 0.0) or 0.0),
        drawdown_proxy=float(getattr(state, "drawdown_proxy", 0.0) or 0.0),
        regime=str(getattr(state, "regime", "normal") or "normal"),
        domain_invested_usd=dict(totals or {}),
        as_of=getattr(state, "as_of", None),
    )


def snapshot_to_portfolio_state(
    snapshot: Optional[PortfolioSnapshot],
    *,
    domain_primary: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Project a :class:`PortfolioSnapshot` into the ``portfolio_state`` mapping
    consumed by :meth:`DecisionPolicyService.evaluate`.

    Returns ``None`` if ``snapshot`` is ``None`` so the caller falls back to
    the pre-existing default path (pure backward compatibility).

    Mapping:

    * ``fund_nav_usd``              -> ``notional_capacity_usd``
    * ``liquidity_reserve_usd``     -> ``liquidity_reserve_floor_usd``
    * ``drawdown_proxy``            -> ``portfolio_drawdown_proxy``
    * ``regime`` (persisted)        -> ``portfolio_regime_code`` (policy)
    * ``sum(domain_invested_usd)``  -> ``committed_pass_usd_excl_current``
    * ``domain_invested_usd[d]``    -> ``domain_pass_committed_usd_excl_current``
      (where ``d == domain_primary``)
    """
    if snapshot is None:
        return None
    nav = float(snapshot.fund_nav_usd or 0.0)
    reserve = float(snapshot.liquidity_reserve_usd or 0.0)
    drawdown = max(0.0, min(1.0, float(snapshot.drawdown_proxy or 0.0)))
    regime = _PERSISTED_REGIME_TO_POLICY.get(
        str(snapshot.regime or "normal").strip().lower(), "neutral"
    )
    domain_totals = dict(snapshot.domain_invested_usd or {})
    total_committed = float(sum(float(v or 0.0) for v in domain_totals.values()))
    domain_committed = 0.0
    if domain_primary is not None:
        domain_committed = float(domain_totals.get(str(domain_primary), 0.0) or 0.0)

    out: Dict[str, Any] = {
        "notional_capacity_usd": nav if nav > 0.0 else _NOTIONAL_CAPACITY_USD_DEFAULT,
        "committed_pass_usd_excl_current": total_committed,
        "domain_pass_committed_usd_excl_current": domain_committed,
        "portfolio_drawdown_proxy": drawdown,
        "portfolio_regime_code": regime,
    }
    if reserve > 0.0:
        out["liquidity_reserve_floor_usd"] = reserve
    cap = float(out["notional_capacity_usd"])
    out["dry_powder_usd_excl_current"] = max(0.0, cap - total_committed)
    return out


class DecisionPolicyService:
    """Implements decision policy spec v1 with optional portfolio-aware extensions (R(S, portfolio_state)).

    The service can optionally accept a :class:`PortfolioStateProvider` via the
    constructor. When present and the caller does not pass an explicit
    ``portfolio_state`` mapping to :meth:`evaluate`, the provider's
    :class:`PortfolioSnapshot` is projected into the legacy mapping shape via
    :func:`snapshot_to_portfolio_state`. If both are absent, behavior falls
    back to the pre-provider defaults — preserving full backward
    compatibility for existing unit tests.
    """

    def __init__(self, portfolio_provider: Optional[PortfolioStateProvider] = None) -> None:
        self._portfolio_provider = portfolio_provider

    def _params_for_domain(self, domain: str) -> Dict[str, float]:
        defaults = {
            "market_economics": {"CS0_d": 0.18, "gamma_d": 2.0, "S_min_d": 50000.0},
            "governance": {"CS0_d": 0.20, "gamma_d": 2.2, "S_min_d": 50000.0},
            "public_health": {"CS0_d": 0.22, "gamma_d": 2.4, "S_min_d": 50000.0},
        }
        p = defaults.get(domain, defaults["market_economics"])
        p["alpha_d"] = 1.0 / (2.0 * p["gamma_d"])
        return p

    @staticmethod
    def _float_portfolio(ps: Mapping[str, Any], key: str, default: float) -> float:
        if ps is None or key not in ps:
            return default
        v = ps[key]
        if v is None:
            return default
        return float(v)

    @staticmethod
    def _int_portfolio(ps: Mapping[str, Any], key: str, default: int) -> int:
        if ps is None or key not in ps:
            return default
        v = ps[key]
        if v is None:
            return default
        return int(v)

    @staticmethod
    def _str_portfolio(ps: Mapping[str, Any], key: str, default: str) -> str:
        if ps is None or key not in ps:
            return default
        v = ps[key]
        if v is None:
            return default
        return str(v).strip().lower() or default

    def _default_liquidity_floor(self, capacity: float) -> float:
        frac = _LIQUIDITY_RESERVE_FRACTION_DEFAULT
        env = os.environ.get("COHERENCE_LIQUIDITY_RESERVE_FRACTION")
        if env is not None and env.strip() != "":
            try:
                frac = float(env)
            except ValueError:
                frac = _LIQUIDITY_RESERVE_FRACTION_DEFAULT
        frac = max(0.0, min(1.0, frac))
        return capacity * frac

    def _normalize_regime(self, raw: str) -> str:
        k = (raw or "neutral").strip().lower() or "neutral"
        return k if k in _KNOWN_REGIMES else "neutral"

    def _normalize_portfolio_state(
        self, portfolio_state: Optional[Mapping[str, Any]]
    ) -> Dict[str, Any]:
        base: Dict[str, Any] = {
            "notional_capacity_usd": _NOTIONAL_CAPACITY_USD_DEFAULT,
            "committed_pass_usd_excl_current": 0.0,
            "same_founder_pass_count_excl_current": 0,
            "same_founder_pass_committed_usd_excl_current": 0.0,
            "open_pipeline_count_excl_current": 0,
            "domain_pass_count_excl_current": 0,
            "domain_pass_committed_usd_excl_current": 0.0,
            "dry_powder_usd_excl_current": _NOTIONAL_CAPACITY_USD_DEFAULT,
            "liquidity_reserve_floor_usd": _NOTIONAL_CAPACITY_USD_DEFAULT * _LIQUIDITY_RESERVE_FRACTION_DEFAULT,
            "portfolio_regime_code": "neutral",
            "portfolio_drawdown_proxy": 0.0,
        }
        if not portfolio_state:
            return base
        out = dict(base)
        if "notional_capacity_usd" in portfolio_state:
            cap = self._float_portfolio(portfolio_state, "notional_capacity_usd", _NOTIONAL_CAPACITY_USD_DEFAULT)
            out["notional_capacity_usd"] = max(cap, 1.0)
        capacity = float(out["notional_capacity_usd"])
        out["committed_pass_usd_excl_current"] = self._float_portfolio(
            portfolio_state, "committed_pass_usd_excl_current", 0.0
        )
        out["same_founder_pass_count_excl_current"] = self._int_portfolio(
            portfolio_state, "same_founder_pass_count_excl_current", 0
        )
        out["same_founder_pass_committed_usd_excl_current"] = self._float_portfolio(
            portfolio_state, "same_founder_pass_committed_usd_excl_current", 0.0
        )
        out["open_pipeline_count_excl_current"] = self._int_portfolio(
            portfolio_state, "open_pipeline_count_excl_current", 0
        )
        out["domain_pass_count_excl_current"] = self._int_portfolio(
            portfolio_state, "domain_pass_count_excl_current", 0
        )
        out["domain_pass_committed_usd_excl_current"] = max(
            0.0,
            self._float_portfolio(portfolio_state, "domain_pass_committed_usd_excl_current", 0.0),
        )
        committed = float(out["committed_pass_usd_excl_current"])
        if "dry_powder_usd_excl_current" in portfolio_state:
            out["dry_powder_usd_excl_current"] = max(
                0.0, self._float_portfolio(portfolio_state, "dry_powder_usd_excl_current", 0.0)
            )
        else:
            out["dry_powder_usd_excl_current"] = max(0.0, capacity - committed)
        if "liquidity_reserve_floor_usd" in portfolio_state:
            out["liquidity_reserve_floor_usd"] = max(
                0.0, self._float_portfolio(portfolio_state, "liquidity_reserve_floor_usd", 0.0)
            )
        else:
            out["liquidity_reserve_floor_usd"] = self._default_liquidity_floor(capacity)
        raw_regime = self._str_portfolio(portfolio_state, "portfolio_regime_code", "neutral")
        out["portfolio_regime_code"] = self._normalize_regime(raw_regime)
        dd = self._float_portfolio(portfolio_state, "portfolio_drawdown_proxy", 0.0)
        out["portfolio_drawdown_proxy"] = max(0.0, min(1.0, dd))
        return out

    def _portfolio_context_active(self, ps: Mapping[str, Any]) -> bool:
        if (
            ps["committed_pass_usd_excl_current"] > 0.0
            or ps["same_founder_pass_count_excl_current"] > 0
            or ps["same_founder_pass_committed_usd_excl_current"] > 0.0
            or ps["open_pipeline_count_excl_current"] > 0
            or ps["domain_pass_count_excl_current"] > 0
        ):
            return True
        if ps["domain_pass_committed_usd_excl_current"] > 0.0:
            return True
        if ps["portfolio_regime_code"] != "neutral":
            return True
        if float(ps["portfolio_drawdown_proxy"]) > 0.0:
            return True
        cap = float(ps["notional_capacity_usd"])
        dry = float(ps["dry_powder_usd_excl_current"])
        if dry + 1e-9 < cap:
            return True
        floor = float(ps["liquidity_reserve_floor_usd"])
        default_floor = self._default_liquidity_floor(cap)
        if abs(floor - default_floor) > 1e-6:
            return True
        return False

    def _portfolio_cs_delta(
        self, ps: Mapping[str, Any], requested: float
    ) -> Tuple[float, float, Dict[str, float]]:
        """Return (delta on cs_required, utilization_ratio, r_term_audit). Step functions only."""
        capacity = float(ps["notional_capacity_usd"])
        committed = float(ps["committed_pass_usd_excl_current"])
        utilization = (committed + requested) / capacity
        delta = 0.0
        audit: Dict[str, float] = {
            "r_utilization": 0.0,
            "r_domain_count": 0.0,
            "r_domain_usd": 0.0,
            "r_pipeline": 0.0,
            "r_liquidity": 0.0,
            "r_drawdown": 0.0,
            "r_regime": 0.0,
        }

        if utilization >= 0.88:
            delta += 0.01
            audit["r_utilization"] += 0.01
        if utilization >= 0.93:
            delta += 0.01
            audit["r_utilization"] += 0.01
        if utilization >= 0.97:
            delta += 0.01
            audit["r_utilization"] += 0.01

        dcount = int(ps["domain_pass_count_excl_current"])
        if dcount >= 25:
            delta += 0.015
            audit["r_domain_count"] += 0.015
        if int(ps["open_pipeline_count_excl_current"]) >= 40:
            delta += 0.01
            audit["r_pipeline"] += 0.01

        domain_committed = float(ps["domain_pass_committed_usd_excl_current"])
        domain_share = (domain_committed + requested) / capacity
        if domain_share >= 0.28:
            delta += 0.005
            audit["r_domain_usd"] += 0.005
        if domain_share >= 0.36:
            delta += 0.005
            audit["r_domain_usd"] += 0.005

        remaining_after = capacity - committed - requested
        remaining_ratio = remaining_after / capacity if capacity > 0 else 0.0
        if remaining_ratio < 0.08 and remaining_after >= 0.0:
            delta += 0.005
            audit["r_liquidity"] += 0.005
        if remaining_ratio < 0.05 and remaining_after >= 0.0:
            delta += 0.005
            audit["r_liquidity"] += 0.005

        drawdown = float(ps["portfolio_drawdown_proxy"])
        if drawdown >= 0.12:
            delta += 0.01
            audit["r_drawdown"] += 0.01
        if drawdown >= 0.22:
            delta += 0.01
            audit["r_drawdown"] += 0.01

        regime = str(ps["portfolio_regime_code"])
        if regime == "stress":
            delta += 0.015
            audit["r_regime"] += 0.015
        elif regime == "defensive":
            delta += 0.01
            audit["r_regime"] += 0.01

        return delta, utilization, audit

    def evaluate(
        self,
        application: Dict[str, object],
        score_record: Dict[str, object],
        portfolio_state: Optional[Mapping[str, Any]] = None,
        *,
        portfolio_snapshot: Optional[PortfolioSnapshot] = None,
    ) -> Dict[str, object]:
        params = self._params_for_domain(str(application["domain_primary"]))
        failed_gates: List[Dict[str, str]] = []

        effective_state: Optional[Mapping[str, Any]] = portfolio_state
        snapshot_source = "explicit_mapping" if portfolio_state is not None else None
        resolved_snapshot: Optional[PortfolioSnapshot] = None
        if portfolio_state is None:
            if portfolio_snapshot is not None:
                resolved_snapshot = portfolio_snapshot
                snapshot_source = "explicit_snapshot"
            elif self._portfolio_provider is not None:
                try:
                    resolved_snapshot = self._portfolio_provider.get_snapshot()
                except Exception:
                    resolved_snapshot = None
                if resolved_snapshot is not None:
                    snapshot_source = "provider"
            if resolved_snapshot is not None:
                effective_state = snapshot_to_portfolio_state(
                    resolved_snapshot,
                    domain_primary=str(application.get("domain_primary", "")),
                )

        ps = self._normalize_portfolio_state(effective_state)
        context_active = self._portfolio_context_active(ps)

        transcript_quality = float(score_record["transcript_quality_score"])
        anti_gaming = float(score_record["anti_gaming_score"])
        compliance_status = str(application.get("compliance_status", "clear"))
        requested = float(application["requested_check_usd"])
        ci = score_record["coherence_superiority_ci95"]
        ci_lower = float(ci["lower"])
        ci_upper = float(ci["upper"])
        ci_width = ci_upper - ci_lower

        quality_min = 0.80
        anti_gaming_warn_min = 0.25
        anti_gaming_max = 0.35
        ci_width_max = 0.20
        max_single_check = 12000000.0 * 0.05
        capacity = float(ps["notional_capacity_usd"])
        committed_excl = float(ps["committed_pass_usd_excl_current"])
        same_founder_committed = float(ps["same_founder_pass_committed_usd_excl_current"])
        founder_concentration_cap = 0.12 * capacity
        liquidity_floor = float(ps["liquidity_reserve_floor_usd"])
        dry_after_request = capacity - committed_excl - requested
        domain_committed = float(ps["domain_pass_committed_usd_excl_current"])
        domain_share_after = (domain_committed + requested) / capacity if capacity > 0 else 0.0
        drawdown = float(ps["portfolio_drawdown_proxy"])

        if transcript_quality < quality_min:
            failed_gates.append({"gate": "quality_gate", "reason_code": "QUALITY_BELOW_MIN"})

        if compliance_status == "blocked":
            failed_gates.append({"gate": "compliance_gate", "reason_code": "COMPLIANCE_BLOCKED"})
        elif compliance_status == "review_required":
            failed_gates.append({"gate": "compliance_gate", "reason_code": "COMPLIANCE_REVIEW_REQUIRED"})

        if anti_gaming > anti_gaming_max:
            failed_gates.append({"gate": "anti_gaming_gate", "reason_code": "ANTI_GAMING_HIGH"})
        elif anti_gaming >= anti_gaming_warn_min:
            failed_gates.append({"gate": "anti_gaming_gate", "reason_code": "ANTI_GAMING_WARNING_BAND"})

        if requested > max_single_check:
            failed_gates.append({"gate": "portfolio_gate", "reason_code": "PORTFOLIO_HARD_CAP_BREACH"})

        if committed_excl + requested > capacity + 1e-9:
            failed_gates.append({"gate": "portfolio_gate", "reason_code": "PORTFOLIO_FUND_CAPACITY_EXCEEDED"})

        if same_founder_committed + requested > founder_concentration_cap + 1e-9:
            failed_gates.append({"gate": "portfolio_gate", "reason_code": "PORTFOLIO_FOUNDER_CONCENTRATION"})

        if dry_after_request + 1e-9 < liquidity_floor:
            failed_gates.append(
                {"gate": "portfolio_gate", "reason_code": "PORTFOLIO_LIQUIDITY_RESERVE_PRESSURE"}
            )

        if domain_share_after > 0.42 + 1e-12:
            failed_gates.append(
                {"gate": "portfolio_gate", "reason_code": "PORTFOLIO_DOMAIN_USD_CONCENTRATION_HIGH"}
            )

        if drawdown >= 0.30:
            failed_gates.append(
                {"gate": "portfolio_gate", "reason_code": "PORTFOLIO_DRAWDOWN_PROXY_ELEVATED"}
            )

        s_min = params["S_min_d"]
        cs_required_base = params["CS0_d"] + params["alpha_d"] * math.log2(max(requested, s_min) / s_min)
        cs_delta, utilization_ratio, r_audit = self._portfolio_cs_delta(ps, requested)
        cs_required = cs_required_base + cs_delta
        portfolio_gates_applied = (
            (committed_excl + requested > capacity + 1e-9)
            or (same_founder_committed + requested > founder_concentration_cap + 1e-9)
            or (dry_after_request + 1e-9 < liquidity_floor)
            or (domain_share_after > 0.42 + 1e-12)
            or (drawdown >= 0.30)
        )
        portfolio_touched = context_active or cs_delta > 0.0 or portfolio_gates_applied

        cs_observed = ci_lower
        margin = cs_observed - cs_required
        if margin < 0:
            failed_gates.append({"gate": "coherence_gate", "reason_code": "COHERENCE_BELOW_THRESHOLD"})

        if ci_width > ci_width_max:
            failed_gates.append({"gate": "confidence_gate", "reason_code": "CONFIDENCE_INTERVAL_TOO_WIDE"})

        hard_fail = {
            "QUALITY_BELOW_MIN",
            "COMPLIANCE_BLOCKED",
            "ANTI_GAMING_HIGH",
            "PORTFOLIO_HARD_CAP_BREACH",
            "PORTFOLIO_FUND_CAPACITY_EXCEEDED",
            "COHERENCE_BELOW_THRESHOLD",
        }
        manual_review = {
            "COMPLIANCE_REVIEW_REQUIRED",
            "ANTI_GAMING_WARNING_BAND",
            "CONFIDENCE_INTERVAL_TOO_WIDE",
            "PORTFOLIO_FOUNDER_CONCENTRATION",
            "PORTFOLIO_LIQUIDITY_RESERVE_PRESSURE",
            "PORTFOLIO_DOMAIN_USD_CONCENTRATION_HIGH",
            "PORTFOLIO_DRAWDOWN_PROXY_ELEVATED",
        }
        codes = {g["reason_code"] for g in failed_gates}
        if codes & hard_fail:
            decision = "fail"
        elif codes & manual_review:
            decision = "manual_review"
        else:
            decision = "pass"

        cs_required_r = round(cs_required, 6)
        cs_observed_r = round(cs_observed, 6)
        margin_r = round(margin, 6)

        result: Dict[str, object] = {
            "decision": decision,
            "threshold_required": cs_required_r,
            "coherence_observed": cs_observed_r,
            "margin": margin_r,
            "failed_gates": failed_gates,
            "policy_version": "decision-policy-v1.1.0" if portfolio_touched else "decision-policy-v1.0.0",
            "parameter_set_id": "params_starter_v1",
        }
        if portfolio_touched:
            adjustments: Dict[str, Any] = {
                "cs_required_delta": round(cs_delta, 6),
                "utilization_ratio": round(utilization_ratio, 6),
                "committed_pass_usd_excl_current": round(committed_excl, 2),
                "founder_concentration_cap_usd": round(founder_concentration_cap, 2),
                "dry_powder_usd_after_request": round(dry_after_request, 2),
                "domain_primary_usd_share": round(domain_share_after, 6),
                "liquidity_reserve_floor_usd": round(liquidity_floor, 2),
                "portfolio_regime_code": ps["portfolio_regime_code"],
                "portfolio_drawdown_proxy": round(drawdown, 6),
                "r_term_audit": {k: round(v, 6) for k, v in r_audit.items()},
            }
            if snapshot_source is not None:
                adjustments["portfolio_snapshot_source"] = snapshot_source
            result["portfolio_adjustments"] = adjustments
        return result
