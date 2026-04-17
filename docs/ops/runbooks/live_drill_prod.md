# Runbook: production on-call live drill (closure)

Use this runbook when running a **live** provider webhook drill against **production** (or production-equivalent) routing: PagerDuty, Opsgenie, Alertmanager receiver, or Slack bridge fed by the same path real pages use.

## Preconditions

- Production observability rollout phases through alert routing are complete; see `production_observability_rollout.md`.
- `deploy/ops/oncall-route-policy` (private overlay) documents the prod row: `escalation_policy_ref`, `escalation_rotation_ref`, and fresh `escalation_ownership_reviewed_at`.
- Secret `ONCALL_ROUTE_VERIFICATION_WEBHOOK_URL` points at the **production** integration you intend to exercise (not staging). Optional `ONCALL_ROUTE_VERIFICATION_WEBHOOK_TOKEN` matches that endpoint.
- Stakeholders know a labeled **drill** (`drill: true` in envelope) may page or notify; schedule during a low-risk window unless your provider supports drill incident types.

## Execute the drill

1. Prefer **manual** `workflow_dispatch` with `run_live_webhook_drill: true` on `.github/workflows/oncall-route-verification.yml` so you control timing. Manual runs **ignore** scheduled quiet-window variables.
2. **Scheduled** live POSTs on the weekly cron are **opt-in** only (`ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL=true`); default scheduled behavior remains safe-mode only. If you enable scheduled prod drills, set `ONCALL_DRILL_QUIET_UTC_*` to avoid unwanted hours.
3. After the run, download artifacts:
   - `oncall-release-readiness` (includes `oncall-incident-followup-checklist.md`, `oncall-drill-evidence.jsonl`, policy summaries, **`oncall-live-drill-followup-production.md`**, **`oncall-ticket-payload-production.json`**, plus staging templates **`oncall-live-drill-followup-staging.md`** and **`oncall-ticket-payload-staging.json`** for cross-checks).
   - `oncall-live-webhook-drill` when the live job POSTed (`oncall-live-webhook-drill.json`).

## Operational closure (same shift)

1. Confirm the synthetic `coherence_fund_worker_ops_alert/v1` notification arrived at the **intended** prod receiver and was **acknowledged** within your SLO; record responder and time.
2. If routing was wrong or noisy: note root cause and open a tracking item before closing the drill.
3. Complete **`oncall-incident-followup-checklist.md`** from the **`oncall-release-readiness`** bundle first (same checklist text the workflow generates in CI).
4. **Tracked follow-up (automation hook):** **Issues → New issue → On-call live drill follow-up** (`.github/ISSUE_TEMPLATE/oncall-live-drill-followup.yml`). Set environment **production**, paste the Actions run URL, and copy delivery/ack + evidence links from the checklist.

### Post-drill checklist → issue template (quick path)

| Step | Action |
|------|--------|
| 1 | Download **`oncall-release-readiness`**; work through `oncall-incident-followup-checklist.md` and **`oncall-live-drill-followup-production.md`** (or **`oncall-ticket-payload-production.json`** for Jira/Linear-style APIs). |
| 2 | **Issues → New issue → On-call live drill follow-up** — set **production**, required fields: run URL, delivery/ack narrative, closure checkboxes (generated `.md` includes a starter run link when produced in Actions). |
| 3 | Link **`oncall-drill-evidence.jsonl`** and **`oncall-live-webhook-drill/oncall-live-webhook-drill.json`** when the live job completed. |

### Optional: POST ticket JSON to your tracker API

With **`post_tracker_handoff`** or **`ONCALL_POST_TRACKER_HANDOFF=true`**, configure **`ONCALL_TRACKER_PRODUCTION_HANDOFF_URL`** (optional **`ONCALL_TRACKER_PRODUCTION_HANDOFF_TOKEN`**) so job **`optional-tracker-handoff`** can POST the production row. Use **`ONCALL_TRACKER_PRODUCTION_HANDOFF_PROVIDER`** (`generic` / `jira` / `github`) to match your endpoint; **`generic`** sends raw **`oncall-ticket-payload-production.json`**. Staging uses **`ONCALL_TRACKER_STAGING_HANDOFF_*`** when set. Per-environment rows are **skipped** if the URL secret is missing. Outcomes: artifact **`oncall-tracker-handoff`** (`oncall-tracker-handoff-results.json`) including idempotency key and retry metadata. Details: `production_observability_rollout.md`.

## Related

- Staging-oriented drill steps: `live_drill_staging.md`
- Rollout, CI cadence, secrets, and variables: `production_observability_rollout.md`
- Operator env reference: `../README.md` and `deploy/README.md`
