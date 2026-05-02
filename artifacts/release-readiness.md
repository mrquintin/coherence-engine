# Coherence Engine Release Readiness Report

**Exit code:** `0`  (pass=11, fail=0, error=0, total=11)

| Check | Status | Reason | Detail |
|-------|--------|--------|--------|
| `alembic_head` | PASS | `-` | single alembic head resolved to 20260425_000019 |
| `decision_policy_version` | PASS | `-` | DECISION_POLICY_VERSION == 'decision-policy-v1' |
| `event_schemas` | PASS | `-` | validated 4 event example(s) |
| `prompt_registry` | PASS | `-` | prompt registry verified ok |
| `e2e_integration_test` | PASS | `-` | e2e integration test present with @pytest.mark.e2e |
| `backtest_spec` | PASS | `-` | backtest spec present at docs/specs/backtest_spec.md |
| `red_team_expected_matrix` | PASS | `-` | red-team expected matrix present (12 cases) |
| `admin_dashboard_router` | PASS | `-` | admin_ui router prefix='/admin' and mounted in create_app() |
| `migration_ci_gate` | PASS | `migration_ci_gate_skipped` | MIGRATION_GATE_PG_URL unset — gate skipped (CI runs the live cycle). |
| `status_doc_prompt_recap` | PASS | `-` | COHERENCE_ENGINE_PROJECT_STATUS.txt references prompts 01-20 |
| `continuation_doc_prompt_recap` | PASS | `-` | COHERENCE_ENGINE_CONTINUATION_PROMPT.txt references prompts 01-20 |

Full machine-readable rows (including per-check `evidence`) live in the JSON report.
