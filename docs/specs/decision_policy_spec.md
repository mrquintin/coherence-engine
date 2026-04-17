# Decision Policy Spec (v1)

- policy_series: `decision-policy-v1`
- schema_version: `1`
- canonical constant: `server.fund.services.decision_policy.DECISION_POLICY_VERSION` (`"decision-policy-v1"`)

The series identifier `decision-policy-v1` is the stable contract surface downstream auditors, reviewers, and the `fund_decisions.decision_policy_version` column reference. Point-versions (`decision-policy-v1.0.0`, `decision-policy-v1.1.0`) label deployed parameter/behavior cuts *within* this series. Any of the conditions enumerated under "Versioning Rules" below require bumping to the next series, `decision-policy-v2`.

## Purpose

Define a deterministic, auditable policy for automated pre-seed routing from `application_submitted` to `pass`, `fail`, or `manual_review`.

This policy is the single source of truth for:

- threshold mathematics
- gate ordering
- parameter configuration
- decision explainability artifact

---

## Policy Scope

In scope:

- coherence-superiority thresholding by requested funding amount
- quality/confidence/anti-gaming/compliance/portfolio gates
- deterministic outputs

Out of scope:

- legal document generation
- final funds transfer authorization
- discretionary partner override logic (tracked separately)

---

## Inputs

Required policy inputs (`DecisionInput`):

```json
{
  "application_id": "app_01J...",
  "founder_id": "fnd_01J...",
  "requested_check_usd": 250000,
  "domain_primary": "market_economics",
  "domain_mix": [
    {"domain": "market_economics", "weight": 0.68},
    {"domain": "governance", "weight": 0.20}
  ],
  "coherence_superiority": 0.31,
  "coherence_superiority_ci95": {"lower": 0.22, "upper": 0.40},
  "transcript_quality_score": 0.91,
  "anti_gaming_score": 0.18,
  "compliance_status": "clear",
  "portfolio_state": {
    "fund_nav_usd": 12000000,
    "dry_powder_usd": 6100000,
    "domain_exposure": {"market_economics": 0.26, "governance": 0.09},
    "max_single_check_fraction": 0.05,
    "drawdown_regime": "normal"
  },
  "policy_version": "decision-policy-v1.0.0"
}
```

---

## Core Equations

## 1) Required coherence superiority

`CS_required(S, d) = CS0_d + alpha_d * log2(S / S_min_d) + R(S, portfolio_state)`

Where:

- `S`: requested check (USD), `S > 0`
- `d`: primary domain
- `CS0_d`: base threshold at `S_min_d`
- `alpha_d`: slope (default `1 / (2 * gamma_d)`)
- `S_min_d`: domain minimum check used for normalization
- `R(...)`: portfolio risk adjustment

## 2) Portfolio risk adjustment

`R = R_concentration + R_drawdown + R_liquidity`

Recommended v1 definitions:

- `R_concentration = k1 * max(0, projected_domain_exposure_d - domain_cap_d)`
- `R_drawdown = k2 * regime_multiplier(drawdown_regime)`
- `R_liquidity = k3 * max(0, S - dry_powder_soft_limit)`

Default multipliers:

- `regime_multiplier(normal)=0.0`
- `regime_multiplier(caution)=0.25`
- `regime_multiplier(stressed)=0.50`

Clamp:

- `R` is clamped to `[0.0, 0.35]`

## Domain Parameter Table (defaults, ranges, units)

These are the defaults implemented in `DecisionPolicyService._params_for_domain` and the portfolio-delta terms in `_portfolio_cs_delta`. Ranges are the allowed configuration envelope; values outside must bump to `decision-policy-v2`.

| Symbol        | Meaning                                          | Unit               | Default (market_economics) | Default (governance) | Default (public_health) | Allowed range |
|---------------|--------------------------------------------------|--------------------|----------------------------|----------------------|-------------------------|---------------|
| `CS0_d`       | Base threshold at `S_min_d`                      | coherence (0–1)    | 0.18                       | 0.20                 | 0.22                    | [0.05, 0.40]  |
| `gamma_d`     | Domain log-slope scale (derives `alpha_d`)       | dimensionless      | 2.0                        | 2.2                  | 2.4                     | [1.0, 5.0]    |
| `alpha_d`     | Log2 slope = `1 / (2 * gamma_d)`                 | coherence per log2 | 0.25                       | ≈0.227               | ≈0.208                  | derived       |
| `S_min_d`     | Domain minimum check used for normalization      | USD                | 50_000                     | 50_000               | 50_000                  | [10_000, 250_000] |

## `R(S, portfolio_state)` Additive Terms (code-symbol mapping)

Implemented in `DecisionPolicyService._portfolio_cs_delta` (`server/fund/services/decision_policy.py`). The `r_term_audit` dict emitted on `portfolio_adjustments.r_term_audit` uses exactly these keys:

| Code symbol       | Category                          | Driver                                                                 | Step contribution(s)    |
|-------------------|-----------------------------------|------------------------------------------------------------------------|-------------------------|
| `r_utilization`   | Liquidity reserve pressure        | `(committed_pass_usd_excl_current + S) / notional_capacity_usd`        | +0.01 @ 0.88, +0.01 @ 0.93, +0.01 @ 0.97 |
| `r_domain_count`  | Domain-count concentration        | `domain_pass_count_excl_current`                                       | +0.015 @ ≥25            |
| `r_pipeline`      | Pipeline congestion               | `open_pipeline_count_excl_current`                                     | +0.01 @ ≥40             |
| `r_domain_usd`    | Domain-USD concentration          | `(domain_pass_committed_usd_excl_current + S) / notional_capacity_usd` | +0.005 @ 0.28, +0.005 @ 0.36 |
| `r_liquidity`     | Liquidity reserve pressure (tail) | `remaining_after / notional_capacity_usd` (post-request headroom)      | +0.005 @ <0.08, +0.005 @ <0.05 |
| `r_drawdown`      | Drawdown proxy                    | `portfolio_drawdown_proxy`                                             | +0.01 @ ≥0.12, +0.01 @ ≥0.22 |
| `r_regime`        | Regime-aware threshold delta      | `portfolio_regime_code` ∈ {stress, defensive}                          | +0.015 (stress), +0.01 (defensive) |

`R` as exposed in the `CS_required` equation is the sum of these terms (audited field `portfolio_adjustments.cs_required_delta` carries the total).

## 3) Confidence-adjusted superiority

Conservative enforcement uses lower confidence bound:

`CS_observed = coherence_superiority_ci95.lower`

Policy compares `CS_observed` against `CS_required`.

---

## Gate Order (Deterministic)

The system must evaluate gates in this exact order:

1. `quality_gate`
2. `compliance_gate`
3. `anti_gaming_gate`
4. `portfolio_gate`
5. `coherence_gate`
6. `confidence_gate`

Rationale: prevent expensive scoring/false certainty when hard blockers already fail.

---

## Gate Definitions

## quality_gate

Pass if:

- `transcript_quality_score >= quality_min`

Else: `fail`

## compliance_gate

Pass if:

- `compliance_status == "clear"`

If `"review_required"` -> `manual_review`  
If `"blocked"` -> `fail`

## anti_gaming_gate

Pass if:

- `anti_gaming_score <= anti_gaming_max`

If in gray band (`anti_gaming_warn_min` to `anti_gaming_max`) -> `manual_review`

## portfolio_gate

Pass if:

- `S <= S_max`
- projected domain exposure <= hard cap
- dry powder post-check >= minimum liquidity floor

If soft breach only -> `manual_review`  
If hard breach -> `fail`

## coherence_gate

Pass if:

- `CS_observed >= CS_required(S, d)`

Else -> `fail`

## confidence_gate

Pass if:

- `ci_width <= ci_width_max`
- model disagreement <= disagreement_max

Else -> `manual_review`

---

## Policy Output Contract

`DecisionOutput`:

```json
{
  "application_id": "app_01J...",
  "decision": "pass|fail|manual_review",
  "threshold_required": 0.27,
  "coherence_observed": 0.22,
  "margin": -0.05,
  "failed_gates": [
    {"gate": "coherence_gate", "reason_code": "COHERENCE_BELOW_THRESHOLD", "message": "Lower CI bound below required threshold"}
  ],
  "audit": {
    "policy_version": "decision-policy-v1.0.0",
    "equation_hash": "sha256:...",
    "parameter_set_id": "params_2026_04_v1",
    "evaluated_at": "2026-04-07T20:30:00Z"
  }
}
```

---

## Parameter Registry

Parameters are stored in a versioned registry keyed by:

- `policy_version`
- `effective_from`
- `domain`
- `stage` (`shadow`, `canary`, `prod`)

Example parameter record:

```json
{
  "policy_version": "decision-policy-v1.0.0",
  "domain": "market_economics",
  "CS0_d": 0.18,
  "gamma_d": 2.0,
  "alpha_d": 0.25,
  "S_min_d": 50000,
  "quality_min": 0.80,
  "anti_gaming_max": 0.35,
  "anti_gaming_warn_min": 0.25,
  "ci_width_max": 0.20,
  "disagreement_max": 0.30,
  "domain_cap_d": 0.30,
  "k1": 0.20,
  "k2": 0.08,
  "k3": 0.05
}
```

---

## Decision Reason Codes

Mandatory reason codes:

- `QUALITY_BELOW_MIN`
- `COMPLIANCE_BLOCKED`
- `COMPLIANCE_REVIEW_REQUIRED`
- `ANTI_GAMING_HIGH`
- `ANTI_GAMING_WARNING_BAND`
- `PORTFOLIO_HARD_CAP_BREACH`
- `PORTFOLIO_SOFT_CAP_BREACH`
- `LIQUIDITY_FLOOR_BREACH`
- `COHERENCE_BELOW_THRESHOLD`
- `CONFIDENCE_INTERVAL_TOO_WIDE`
- `MODEL_DISAGREEMENT_TOO_HIGH`

---

## Determinism and Reproducibility Rules

1. Decision service must be pure for identical input + parameter set.
2. All floating-point comparisons use fixed precision of 1e-6.
3. Equation implementation must be referenced by immutable `equation_hash`.
4. Every decision persists an `input_hash` and `output_hash`.
5. Replays must return byte-identical JSON except timestamp fields.

---

## Deterministic Approval Predicate

A decision resolves to `pass` only if all of the following hold. The predicate is the conjunction of five gate families — coherence superiority, confidence, anti-gaming, portfolio, and compliance — evaluated in the gate order above:

```
approve(application) :=
      (transcript_quality_score >= quality_min)                              # quality
  AND (compliance_status == "clear")                                         # compliance
  AND (anti_gaming_score < anti_gaming_warn_min)                             # anti-gaming
  AND (requested_check_usd <= S_max)                                         # portfolio (hard caps)
      AND (committed_pass_usd_excl_current + S <= notional_capacity_usd)
      AND (same_founder_pass_committed_usd_excl_current + S
           <= founder_concentration_cap_usd)
      AND (dry_powder_usd_after_request >= liquidity_reserve_floor_usd)
      AND (domain_primary_usd_share_after <= domain_usd_hard_cap)
      AND (portfolio_drawdown_proxy < drawdown_hard_cap)
  AND (coherence_superiority_ci95.lower
       >= CS0_d + alpha_d * log2(max(S, S_min_d) / S_min_d)
               + R(S, portfolio_state))                                      # coherence
  AND (coherence_superiority_ci95.upper - .lower <= ci_width_max)            # confidence
```

Any failed gate routes to `fail` (for hard-fail codes) or `manual_review` (for soft-fail codes); the canonical code sets are enumerated in `DecisionPolicyService.evaluate` and mirrored in "Decision Reason Codes" above.

---

## Versioning Rules

The `decision-policy-v1` series is stable. The following changes require bumping the series identifier to `decision-policy-v2` (and writing a new `DECISION_POLICY_VERSION` constant):

1. Changing any default in the Domain Parameter Table (`CS0_d`, `gamma_d`, `alpha_d` derivation, or `S_min_d`).
2. Adding, removing, or renaming any additive term inside `R(S, portfolio_state)` (the `r_*` keys enumerated above).
3. Changing the gate order, the deterministic approval predicate, or the hard-fail / manual-review reason-code partitioning.
4. Changing the `CS_required` functional form (log base, normalization, or clamp range for `R`).

Non-breaking changes that stay within `decision-policy-v1` (point-release only, e.g. `v1.x.y`):

- Tightening or loosening a numeric threshold *inside* an already-enumerated step function (e.g. shifting `r_utilization`'s 0.93 breakpoint) is a point-release, not a series bump — so long as the term, its units, and its audit key are unchanged.
- Adding new observability fields under `portfolio_adjustments.r_term_audit` that do not feed back into `cs_required_delta`.

The persisted `fund_decisions.decision_policy_version` column always carries the current series (`decision-policy-v1`), while `policy_version` continues to carry the point-release label emitted by the service.

---

## Rollout Strategy

1. `shadow`: compute decisions without founder-visible effects.
2. `canary`: apply to <=10% traffic by domain.
3. `prod`: full enforcement after KPI thresholds pass.

Exit criteria from shadow to canary:

- reproducibility 100%
- observed false-pass within target
- no critical compliance misses

