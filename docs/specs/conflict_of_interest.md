# Conflict-of-Interest Registry + Automated Gate (prompt 59)

`schema_version: coi-v1`

## Summary

Before any partner meeting is auto-booked or a `pass` decision is
finalized, the engine MUST consult the conflict-of-interest registry
and refuse to route the application to a partner with a declared
relationship to the founder or company. The registry is a simple two-
table append-only store (`fund_coi_declarations`,
`fund_coi_checks`) plus an admin-issued override table
(`fund_coi_overrides`). All evaluation flows through one entrypoint —
`services.conflict_of_interest.check_coi`.

The decision policy (`decision-policy-v1`) was extended with a
single gate, `coi_clear`, that emits one of two reason codes:

* `COI_CONFLICT` — at least one *hard* relationship matched
  (`employed`, `family`, `invested`, `board`, `founder`).
* `COI_DISCLOSURE_REQUIRED` — at least one *soft* relationship
  matched (`advisor`).

Both downgrade `pass` → `manual_review`. Neither is an automatic
hard fail; the operator decides whether to re-route to a different
partner or attach an explicit override + disclosure.

## Tables

### `fund_coi_declarations`

| column | type | notes |
| --- | --- | --- |
| `id` | `string(40)` PK | `coid_<uuid>` |
| `partner_id` | `string(128)` | indexed |
| `party_kind` | `string(16)` | `person` \| `company` |
| `party_id_ref` | `string(128)` | CRM-side ref; matched against application |
| `relationship` | `string(32)` | `employed` \| `family` \| `invested` \| `advisor` \| `board` \| `founder` |
| `period_start` | `datetime tz` | inclusive |
| `period_end` | `datetime tz` nullable | exclusive; `NULL` = open-ended |
| `evidence_uri` | `text` | optional pointer at supporting memo |
| `note` | `text` | free-form |
| `status` | `string(16)` | `active` \| `revoked` |
| `created_at`, `updated_at` | `datetime tz` | |

### `fund_coi_checks`

Append-only. One row per `check_coi(application, partner)`
evaluation. Columns: `id`, `application_id` (FK to
`fund_applications.id`), `partner_id`, `status`
(`clear` \| `conflicted` \| `requires_disclosure`),
`evidence_json`, `disclosure_uri`, `override_id`, `checked_at`.

### `fund_coi_overrides`

Admin-issued release valve. Columns: `id`, `application_id`,
`partner_id`, `justification` (≥ 50 chars), `overridden_by`,
`created_at`. **Auto-clearing is forbidden** — every override
carries a justification of at least 50 characters and is audited
(prompt 59 prohibition).

## Matching algorithm

`check_coi` collects candidate party refs from the application:

* `founder_id`
* `founder_user_id` (Supabase sub)
* `founder_email_token` (tokenized email)
* `company_name` (case-insensitive)
* explicit `party_id_ref` if the caller threads one

A declaration matches when:

1. `partner_id` equals the partner under consideration, AND
2. `party_id_ref` matches a candidate ref (case-sensitive *or*
   the lower-cased form), AND
3. `status == 'active'`, AND
4. `period_start <= now < period_end` (open-ended `period_end`
   counts as still active).

Status resolution:

* any hard relationship matches → `conflicted`
* otherwise any soft relationship matches → `requires_disclosure`
* nothing matched → `clear`

## Gate placement

| Path | When `check_coi` runs |
| --- | --- |
| `Scheduler.propose` | Caller must call `route_for_application` with the candidate partner list and use the returned partner. Conflicted candidates are skipped (prompt 59 prohibition: do NOT auto-route a conflicted application to the same partner). |
| `DecisionPolicyService.evaluate` | Caller threads `coi_clear` (bool) + `coi_status` (string) onto the `application` mapping. The policy module never imports the registry directly so the import graph stays acyclic. |

## Override flow

1. Admin reviews the conflicted check via `GET /coi/declarations`
   and `POST /coi/check`.
2. Admin decides the partner can still take the meeting because
   (e.g.) the conflict is sufficiently mitigated. They call
   `POST /coi/override` with a justification of at least 50
   characters.
3. The next `check_coi` for that pair surfaces `override_id` on
   the result. For `requires_disclosure` results that promotes
   the gate to clear; for `conflicted` results the gate remains
   blocked but the override is recorded for audit. Operators
   re-route or close the application — the engine never
   auto-promotes a hard conflict.

## API surface

* `POST /coi/declarations` — partner / admin. Partners may only
  declare on themselves.
* `GET /coi/declarations` — partner sees own; admin sees all
  (optional `partner_id` filter).
* `POST /coi/check` — partner / analyst / admin. Runs the gate
  on demand and persists a `COICheck` row.
* `POST /coi/override` — admin only. 422 on a justification
  shorter than 50 characters.

## Invariants & prohibitions

* Conflicted applications are NEVER auto-routed to the same
  partner.
* Every override carries a justification ≥ 50 chars and is
  audited via `audit_log("coi_override", ...)`.
* `fund_coi_checks` is append-only by convention so the
  disclosure trail is canonical.
* The decision policy module does NOT import
  `services.conflict_of_interest`; the caller threads
  `coi_clear` + `coi_status` instead.
