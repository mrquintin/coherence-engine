# Runbook: staging on-call live drill (closure)

Use this runbook when exercising **live** webhook delivery in **staging** or other non-production environments before or after production drills.

## Preconditions

- Staging workers or alert path mirror production **labels and receiver names** closely enough that a drill proves routing config, not production load.
- `ONCALL_ROUTE_VERIFICATION_WEBHOOK_URL` targets a **staging** endpoint (separate PagerDuty service, Opsgenie team, Alertmanager receiver, or Slack test channel). Do not reuse production URLs while labeling the run “staging-only” in process docs.
- Policy JSON (private) includes a staging row with `escalation_rotation_ref` and reviewed ownership consistent with your verifier flags.

## Execute the drill

1. Run `.github/workflows/oncall-route-verification.yml` via **Run workflow** with `run_live_webhook_drill: true`. Staging drills do not require enabling `ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL`; keep that variable **unset** or `false` if you want production schedules to stay network-quiet.
2. Collect `oncall-release-readiness` and, when applicable, `oncall-live-webhook-drill` artifacts from the run. Inside `oncall-release-readiness`, use **`oncall-live-drill-followup-staging.md`** (pre-filled staging follow-up body) and **`oncall-ticket-payload-staging.json`** (external tracker template) alongside the generic checklist. The same bundle always includes production templates (**`oncall-live-drill-followup-production.md`**, **`oncall-ticket-payload-production.json`**) for the next promotion step.

## Operational closure

1. Verify notification reached the **staging** receiver; acknowledge or resolve per your staging hygiene (some teams auto-resolve drill incidents).
2. Record any mismatch between staging and prod route config; open change requests before relying on the same pattern in prod.
3. Use **`oncall-incident-followup-checklist.md`** inside the **`oncall-release-readiness`** artifact as the authoritative row-by-row closure list (delivery, noise, policy, evidence).
4. **Tracked follow-up (automation hook):** in GitHub go to **Issues → New issue → On-call live drill follow-up** (template file `.github/ISSUE_TEMPLATE/oncall-live-drill-followup.yml`). Choose environment **staging**, paste the workflow run URL, and mirror checklist items into the issue for audit.

### Post-drill checklist → issue template (quick path)

| Step | Action |
|------|--------|
| 1 | Download **`oncall-release-readiness`** from the workflow run; open `oncall-incident-followup-checklist.md` and **`oncall-live-drill-followup-staging.md`** (or import fields from **`oncall-ticket-payload-staging.json`**). |
| 2 | **Issues → New issue → On-call live drill follow-up** — choose **staging**, fill delivery/ack, paste from the generated `.md` or mirror its sections. |
| 3 | Attach or link `oncall-drill-evidence.jsonl` and `oncall-live-webhook-drill.json` (if the live job ran). |

### Optional: POST ticket JSON to your tracker API

If your org automates ticket creation outside GitHub Issues, enable **`post_tracker_handoff`** (or repository variable **`ONCALL_POST_TRACKER_HANDOFF=true`**) on the same workflow run and set secret **`ONCALL_TRACKER_STAGING_HANDOFF_URL`** (optional **`ONCALL_TRACKER_STAGING_HANDOFF_TOKEN`**). Repository variable **`ONCALL_TRACKER_STAGING_HANDOFF_PROVIDER`** chooses **`generic`** (raw **`oncall-ticket-payload-staging.json`** POST), **`jira`**, or **`github`**; **`Idempotency-Key`** and bounded retries apply on transient failures. Review **`oncall-tracker-handoff/oncall-tracker-handoff-results.json`** for HTTP status, provider, and attempts (no secrets in file). Production uses **`ONCALL_TRACKER_PRODUCTION_HANDOFF_*`** when you opt in. See `production_observability_rollout.md` for full secret/variable list.

## Promotion to production drill

After a clean staging drill, schedule a production drill using `live_drill_prod.md` and the same issue template with environment **production**.

## Related

- Production drill: `live_drill_prod.md`
- Full rollout and CI behavior: `production_observability_rollout.md`
