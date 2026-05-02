# LP Reporting Pipeline (prompt 69)

## Purpose

Produce per-LP, deterministic, audit-traceable artifacts for the three
limited-partner-facing reporting events:

1. **Quarterly NAV statements** — capital account summary, per-LP
   share of cost basis and FMV, IRR since inception.
2. **Capital-call notices** — per-LP draw notices with required LP
   acknowledgement signature.
3. **Distribution notices** — per-LP notice of intended cash
   distributions; LP acknowledgement only (the treasurer remains the
   sole party authorised to execute the wire under prompt 51).

The pipeline is a *renderer + orchestrator* layer on top of the
existing capital-deployment, cap-table, and e-signature subsystems —
it never moves money and never issues securities by itself.

## Module map

| Module | Responsibility |
| ---- | ---- |
| `server.fund.services.nav_calculator` | Pure-functional per-LP NAV math (cost basis, FMV, unrealized gain, IRR via XIRR). |
| `server.fund.services.lp_reporting` | Quarterly-statement orchestration, Jinja2 / LaTeX rendering env, content-digest seal. |
| `server.fund.services.capital_call_notice` | Capital-call payload + render + DocuSign dispatch helper. |
| `server.fund.services.distribution_notice` | Distribution-notice payload + render + DocuSign acknowledgement helper. |
| `apps/lp_portal` | Next.js (App Router) LP portal with Supabase Auth + `lp` role gate. |

Templates live under
`data/governed/lp_reports/templates/{quarterly_nav,capital_call,distribution_notice}.tex.j2`.

## NAV math

For one LP at a period close (`as_of`):

```
lp_cost_basis_i  = position.cost_basis_usd * lp.ownership_fraction
lp_fmv_i         = mark.fmv_usd            * lp.ownership_fraction
total_cost_basis = sum(lp_cost_basis_i)
total_fmv        = sum(lp_fmv_i)
unrealized_gain  = total_fmv - total_cost_basis
nav_usd          = total_fmv + lp.uncalled_capital_usd
```

`Mark` records carry `operator_signoff_at` + `operator_id`. A position
without a signed Mark raises `UnsignedMarkError`; the statement is
NOT published in that state. Marks are therefore the audit hook for
the FMV column.

### IRR

`compute_irr` builds a dated cash-flow series from the LP's capital
calls (negative) and distributions (positive), appending the LP-share
residual NAV as a positive synthetic flow on `as_of`. Solver:

* if `pyxirr` is importable, delegate to it;
* otherwise, run a Newton-Raphson XIRR with bisection fallback over
  `[-0.999, 10.0]`.

Returns `None` for degenerate (sign-stable) series.

## Quarterly statement determinism

`assemble_quarterly_statement` returns a frozen `QuarterlyStatement`
dataclass:

* `tex_source` — byte-deterministic LaTeX rendering;
* `content_digest` — SHA-256 hex digest of `tex_source`;
* `statement_id` — `stmt_<sha256(lp_id|quarter_label|digest)[:24]>`.

Determinism is enforced by:

1. Pinned `generated_at` (the orchestrator floors to seconds; tests
   pass an explicit value);
2. Sorted iteration over positions and cash flows;
3. `jinja2.StrictUndefined` so a missing key fails loudly;
4. The same LaTeX-escape filter the MRM renderer uses.

## Capital-call notices

`CapitalCallNotice` carries the per-LP draw amount, the line items
funded by the call, the wire-instructions reference (opaque token —
never raw bank details), and the contact email. `render_notice`
returns the deterministic `.tex` source plus its digest;
`dispatch_for_signature` hands the document to a caller-supplied
`ESignatureProvider` (typically the DocuSign backend, prompt 52) with
a deterministic per-(call_id, lp_id) idempotency key. PDF compilation
re-uses the MRM renderer's `pdflatex` invocation.

## Distribution notices

`DistributionNotice` carries the waterfall, the wire-instructions
reference, AND the `treasurer_approval_ref` linking back to the
prompt-51 `TreasurerApproval` row. `render_notice` rejects payloads
without that reference. `dispatch_for_acknowledgement` hands the
notice to the e-signature provider with the LP signer role marked
`lp_acknowledger` — the type-system marker that the LP is
acknowledging *receipt*, not authorising the wire.

## LP portal RBAC

`apps/lp_portal/src/lib/rbac.ts` defines the role gate:

* `app_metadata.roles` MUST include `"lp"` (legacy: `app_metadata.role === "lp"`);
* `app_metadata.lp_id` MUST be present.

The shared guard `requireLpSession()` (used by every `/lp/*` page)
constructs an `LpApiClient` keyed off the session's own `lp_id` —
no user-supplied id is ever honoured by the helper. This is the first
of two layers; the backend additionally enforces row-level security
on the LP statement and notice tables, mirroring the prompt-26 RLS
policies on the investor surface.

## Prohibitions (load-bearing)

* **No auto-execution of distributions.** This module renders and
  dispatches notices for acknowledgement; the treasurer's prompt-51
  approval + execute path is the only way money moves.
* **No statement publication without a signed Mark.**
  `nav_calculator.compute_nav` raises `UnsignedMarkError` if any held
  position lacks operator attestation.
* **No cross-LP leakage.** `assemble_batch` is a thin loop emitting
  one statement per LP; the LP portal guard never widens an LP's
  view to another LP's data; the backend RLS catches any drift.
* **No raw bank details in payloads.** Both notice payloads carry an
  opaque `wire_instructions_ref` token; raw account / routing numbers
  live in the treasurer-controlled wire register, mirroring the
  prompt-51 storage discipline.

## Tests

* `tests/test_nav_calculator.py` — NAV math, IRR (known-doubling case,
  zero, degenerate), input validation, mark-signoff gate.
* `tests/test_lp_reporting.py` — quarterly determinism, batch
  isolation across two LPs, capital-call render + dispatch idempotency,
  distribution rejection paths, treasurer-approval-ref enforcement.
* `apps/lp_portal/__tests__/rbac.test.ts` — role/claim parsing,
  cross-LP isolation guard.
* `apps/lp_portal/__tests__/lp_api.test.ts` — header stamping
  (Authorization, Accept, X-LP-ID), URL encoding of `lp_id`.

## Operator obligations

* The operator that issues a Mark MUST sign it (`operator_signoff_at`
  + `operator_id`) before the statement run; an unsigned Mark
  prevents publication and is detected loudly.
* Capital-call and distribution templates are governed assets under
  `data/governed/lp_reports/templates/` and require legal-counsel
  review before any production envelope is sent — same convention as
  the SAFE / term-sheet templates under
  `server/fund/data/legal_templates/`.
