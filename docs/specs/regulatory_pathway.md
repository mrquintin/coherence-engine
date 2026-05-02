# Regulatory Pathway Selector — Spec

**Status:** v1 (prompt 56). Schema version: `regulatory-pathways-v1`.

## DISCLAIMER — read first

The Coherence Engine software **does not provide legal advice** and
**does not autonomously pick a securities pathway**. The pathway
registry at `data/governed/regulatory_pathways.yaml` is owned by the
operator's licensed securities counsel. The runtime classifier in
`server/fund/services/regulatory_pathway.py` enforces what counsel
has configured — nothing more, nothing less. Ambiguity routes to
manual review; the system never silently defaults to a pathway.

If you are reading this and you are not the operator's counsel:
do **not** edit the registry. Open a ticket and route the change
through counsel signoff.

## Scope

This module classifies each application against one of the
operator-configured U.S. securities pathways:

* **Reg D 506(b)** — private placement, no general solicitation,
  capped at 35 unaccredited investors per Rule 506(b)(2)(ii).
* **Reg D 506(c)** — private placement permitting general
  solicitation; **all** investors must be verified accredited
  under SEC Rule 501. Wires to prompt 26's
  `VerificationRecord` (`status == "verified"`).
* **Reg CF** — Regulation Crowdfunding under the JOBS Act;
  permits general solicitation; aggregate offering cap and
  per-investor caps apply (operator's responsibility, not
  enforced here).
* **Reg S** — offshore offering for non-U.S. founders / issuers.
  Permits general solicitation outside the U.S. but requires
  no directed selling efforts into the U.S.

These four are starting placeholders. Counsel may add, remove, or
amend pathways by editing the YAML; the schema is intentionally
generic so additions like Reg A+ or Reg D 504 do not require code
changes.

## YAML schema

```yaml
schema_version: regulatory-pathways-v1
counsel_signoff_ttl_days: 90
pathways:
  - id: reg_d_506c
    jurisdiction: US             # US | non_US
    investor_requirement: accredited_verified  # accredited_verified | self_certified | none
    advertising: permitted       # permitted | prohibited
    max_investors: null          # null = unlimited
    integration_window_days: 30  # SEC Rule 152 safe-harbor window
    counsel_signoff_required: true
    counsel_signoff_at: 2026-04-01T00:00:00Z
    counsel_signoff_by: "Acme LLP"
```

### Field semantics

* `jurisdiction` — `US` for Reg D / Reg CF; `non_US` for Reg S.
  The classifier normalizes the application's `founder_country`
  using a simple "US vs non-US" rule. Finer-grained per-country
  selection (e.g. EU MiFID overlay) is counsel's responsibility
  and lives outside this registry.
* `investor_requirement`:
  * `accredited_verified` — must be backed by a `verified`
    `VerificationRecord` (prompt 26).
  * `self_certified` — operator-attested only; lower trust.
  * `none` — no investor-side gate (e.g. Reg CF, Reg S).
* `advertising`:
  * `permitted` — general solicitation allowed (506(c), Reg CF, Reg S).
  * `prohibited` — private placement only (506(b)). The runtime
    will not auto-promote an application from `prohibited` to
    `permitted` even if a 506(c) pathway is also configured;
    that decision is counsel's.
* `max_investors` — null = unlimited; integer otherwise.
  Surfaced via the registry but **not** enforced by this module
  (the capital-deployment service in prompt 31 owns enforcement).
* `integration_window_days` — SEC Rule 152 integration safe
  harbor (currently 30 days). Surfaced for downstream policy;
  not enforced here.
* `counsel_signoff_at` / `counsel_signoff_by` — recorded so the
  classifier can refuse to clear stale signoffs. TTL is set
  globally via `counsel_signoff_ttl_days` (default 90 days).

## Classification algorithm

The classifier in `regulatory_pathway.classify` is **deterministic**
and **non-defaulting**:

1. Filter the registry by founder jurisdiction (`US` vs `non_US`).
2. Filter by advertising mode. If the application has not declared
   an advertising mode (`unspecified`), the candidate set is left
   unfiltered — multiple matches then surface as `ambiguous`.
3. If 0 or > 1 candidate remains → `ambiguous`
   (`REGULATORY_PATHWAY_AMBIGUOUS`).
4. If exactly 1 candidate remains but its `investor_requirement`
   is not satisfied (e.g. 506(c) without
   `investor_verification_status == "verified"`) → `unclear`
   (`REGULATORY_PATHWAY_UNCLEAR`).
5. If `counsel_signoff_required=true` and the signoff is missing
   or older than `counsel_signoff_ttl_days` → `unclear`.
6. Otherwise → `clear`, with `pathway_id` set.

## Decision-policy gate

The decision-policy module
(`server/fund/services/decision_policy.py`) reads
`application["regulatory_pathway_status"]` (one of `clear` |
`unclear` | `ambiguous`). When the field is absent the gate is
silent (backwards-compatible). When the field is present:

| status      | reason code                       | decision impact                  |
| ----------- | --------------------------------- | -------------------------------- |
| `clear`     | —                                 | no effect                        |
| `unclear`   | `REGULATORY_PATHWAY_UNCLEAR`      | downgrade `pass` → `manual_review` |
| `ambiguous` | `REGULATORY_PATHWAY_AMBIGUOUS`    | downgrade `pass` → `manual_review` |

Neither status downgrades to `fail` — the operator (with counsel)
decides whether to disambiguate, refresh signoff, or close the
application.

## Persistence

`Application.regulatory_pathway_id` (nullable, indexed) records
the resolved pathway id. Migration:
`alembic/versions/20260425_000013_regulatory_pathway.py`. The
column is the *resolution*, not the *gate*: a null value does not
mean "fail"; the gate is driven by
`regulatory_pathway_status` threaded into `decision_policy.evaluate`.

## Operator obligations

* Maintain `data/governed/regulatory_pathways.yaml` under counsel
  review.
* When adding a pathway: ensure `id` is stable (it is persisted),
  set `counsel_signoff_at` to the date counsel actually signed,
  and set `counsel_signoff_by` to the firm or attorney of record.
* Refresh signoff at least every `counsel_signoff_ttl_days` (90)
  days.
* Never edit a pathway's semantics in place to "promote" an
  application from `unclear` to `clear`. If counsel changes their
  analysis, version the change in git and refresh the signoff
  timestamp.

## Prohibitions (load-bearing, prompt 56)

* Do **not** silently default to a pathway when the registry yields
  zero or multiple matches — ambiguity routes to manual review.
* Do **not** advertise or solicit on a 506(b) pathway. The runtime
  will not surface `clear` for a 506(b) candidate when the
  application's advertising mode is `permitted`.
* Do **not** permit a `pass` decision when
  `counsel_signoff_required=true` and signoff is missing or stale.
