# Implementation Roadmap and Backlog

This roadmap converts the automated fund blueprint into executable engineering workstreams.

> **Status note (2026-04-17, Wave 6 / prompt 20/20):** the 20-prompt Wave 1–6
> automated pre-seed pipeline program is complete. Items below whose artifacts
> are materially on disk are annotated inline with `[DONE prompt NN]`; items
> without such annotation remain open. Evidence for every `[DONE prompt NN]`
> marker is re-verified on each run of
> `deploy/scripts/release_readiness_check.py`.

---

## Program Targets

Primary KPI set:

1. End-to-end automation rate (intake to decision packet) >= 95%.
2. Median turnaround from interview completion to decision <= 30 minutes.
3. Decision reproducibility: same inputs produce same decision artifact 100%.
4. Calibration drift alerting SLA <= 24 hours.
5. False-pass rate bounded by policy.

---

## Workstream A: Platform Foundation

Goal: establish robust event-driven backbone.

Backlog:

- A1: Service skeletons (`intake`, `conversation`, `scoring`, `policy`, `notifications`) [DONE prompts 09, 14, 15] — scoring hardened (`server/fund/services/scoring.py`), notifications shipped (`server/fund/services/notifications.py`), workflow orchestrator (`server/fund/services/workflow.py`) knits them together
- A2: Shared schema contracts (JSON schemas + typed SDK) [DONE prompts 02, 17] — event schemas in `server/fund/schemas/events/*.v1.json`; Python SDK stubs in `sdk/python/coherence_fund_client/client.py` generated from `docs/specs/openapi_v1.yaml`
- A3: Workflow orchestrator with retries and idempotency keys [DONE prompt 15] — `server/fund/services/workflow.py` + `WorkflowRun` / `WorkflowStep` models + `alembic/versions/20260417_000006_workflow_checkpoints.py`
- A4: Secrets management and key rotation
- A5: Observability baseline (traces, metrics, structured logs) [DONE prompt 18] — `server/fund/services/ops_telemetry.py` (`record_stage`), `docs/ops/slo_metrics.md`, Prometheus rule stubs under `deploy/helm/templates/` and `deploy/k8s/prometheus/`

Definition of done:

- all core events published/consumed with schema validation
- failure replay works end-to-end

---

## Workstream B: Voice Intake and Interview Automation

Goal: complete autonomous founder interview flow.

Backlog:

- B1: phone + web voice ingress
- B2: consent and disclosure module
- B3: adaptive interview policy engine
- B4: interruption handling and recovery
- B5: transcript confidence scoring and quality gate [DONE prompt 03] — `server/fund/services/transcript_quality.py`, `server/fund/data/interview_topics.json`

Definition of done:

- founder can start and complete interview without human intervention
- transcript quality and metadata meet downstream requirements

---

## Workstream C: Argument, Ontology, and Domain Engine

Goal: transform raw interviews into formal decision-ready structures.

Backlog:

- C1: transcript-to-proposition compiler [DONE prompt 04] — `core/transcript_compiler.py`, `core/parser.py::parse_transcript`, `core/types.py::ProvenanceSpan`
- C2: relation extraction enhancements for spoken language artifacts
- C3: ontology entity extraction service [DONE prompt 05] — `domain/ontology.py`, `server/fund/data/ontology_lexicon.json`, `core/types.py::{Entity, OntologyEdge, OntologyGraph}`
- C4: hybrid domain reconstruction (topic + premises + ontology + normative) [DONE prompt 06] — `domain/detector.py::detect_domain_mix`, `domain/normative.py`, `core/types.py::{DomainMix, NormativeProfile}`
- C5: incumbent comparison-set retrieval service

Definition of done:

- each case yields stable argument graph, ontology graph, and domain mix

---

## Workstream D: Quant Decision and Risk Policy

Goal: productionize threshold logic and safeguards.

Backlog:

- D1: implement `CS_required(S, d)` policy service [DONE prompt 01] — `server/fund/services/decision_policy.py` (`DECISION_POLICY_VERSION = "decision-policy-v1"`), `docs/specs/decision_policy_spec.md`
- D2: implement `Budget_tokens(S, d)` compute allocator
- D3: uncertainty estimation and confidence gating
- D4: anti-gaming detector integration [DONE prompt 09] — `core/anti_gaming.py`, composite adjustment in `core/scorer.py`, plumbing in `server/fund/services/scoring.py`
- D5: portfolio concentration and exposure checks [DONE prompt 10] — `server/fund/models.py::{PortfolioState, Position}`, `server/fund/repositories/portfolio_repository.py`, `PortfolioStateProvider` in `server/fund/services/decision_policy.py`, `alembic/versions/20260417_000003_portfolio_state.py`
- D6: explainability and audit artifact generator [DONE prompt 07] — `server/fund/services/decision_artifact.py`, `server/fund/schemas/artifacts/decision_artifact.v1.json`, `alembic/versions/20260417_000002_artifact_kind.py`

Definition of done:

- deterministic pass/fail/manual-review decision with full rationale

---

## Workstream E: Founder and Partner Automation

Goal: eliminate manual ops for communication and handoff.

Backlog:

- E1: founder status email templates (pass/fail/review) [DONE prompt 14] — `server/fund/services/notifications.py`, `server/fund/services/notification_backends.py`, `server/fund/data/notification_templates/`, `alembic/versions/20260417_000005_notification_log.py`
- E2: partner escalation memo generation
- E3: calendar scheduling automation
- E4: CRM synchronization
- E5: exception inbox for manual review queue

Definition of done:

- qualified founders are automatically routed to partner-ready meetings

---

## Workstream F: Calibration, Validation, and Research Ops

Goal: maintain quantitative integrity over time.

Backlog:

- F1: historical backtest pipeline [DONE prompt 11] — `server/fund/services/backtest.py`, `docs/specs/backtest_spec.md`, `deploy/scripts/run_backtest.py`, `tests/fixtures/backtest/`
- F2: shadow-mode evaluation before production gate changes [DONE prompt 12] — `server/fund/services/application_service.py` enforce/shadow branch, `alembic/versions/20260417_000004_scoring_mode.py`, optional `mode` on `server/fund/schemas/events/decision_issued.v1.json`
- F3: red-team adversarial prompt harness [DONE prompt 13] — `server/fund/services/red_team.py`, `tests/adversarial/fixtures/`, `tests/adversarial/labels.json`, `docs/specs/red_team_harness.md`
- F4: domain-level threshold calibration jobs
- F5: monthly model-risk report generator

Definition of done:

- policy parameters update under controlled rollout with rollback capability

---

## Suggested Milestones

## Milestone 0 (Weeks 1-2): Architecture Lock

- finalize schemas, event taxonomy, and policy formula contract
- choose infra stack and deployment topology

Exit criteria:

- approved architecture decision records
- initial CI/CD and staging environment available

## Milestone 1 (Weeks 3-6): Interview to Structured Argument

- complete voice intake, transcription, and structuring

Exit criteria:

- at least 100 dry-run interviews processed fully to argument graph

## Milestone 2 (Weeks 7-10): Domain + Ontology + Coherence Superiority

- complete domain reconstruction and superiority outputs

Exit criteria:

- superiority metrics generated with uncertainty bounds for all test cases

## Milestone 3 (Weeks 11-14): Policy Decision Automation

- deploy threshold/risk gating and founder/partner messaging

Exit criteria:

- end-to-end automated path from intake to partner escalation packet

## Milestone 4 (Weeks 15-20): Pilot Operations

- limited live pilot with capped check sizes and manual override

Exit criteria:

- stable operations, acceptable false-pass/false-reject profile

## Milestone 5 (Weeks 21-32): Scale and Governance

- expand to broader traffic, harden compliance, launch calibration loops

Exit criteria:

- formal governance package and sustained SLO performance

---

## Example Ticket Templates

## Engineering Ticket Template

- Title: `<service>: <capability>`
- Problem: what fails today
- Scope: explicit in/out
- Inputs/Outputs: schema references
- Acceptance tests: deterministic checks
- Metrics impact: expected KPI movement
- Rollback strategy: how to disable safely

## Prompt Ops Ticket Template

- Prompt ID + current version
- Failure pattern observed
- Proposed change
- Offline eval set used
- Pass/fail criteria
- Launch mode: shadow/canary/full

## Quant Policy Ticket Template

- Parameter(s) changed: `CS0_d`, `alpha_d`, `gamma_d`, confidence cutoff
- Evidence source: backtest + pilot stats
- Bias and fairness impact assessment
- Risk committee signoff required

---

## Metrics and Dashboards

Operational:

- interview completion rate
- transcription confidence distribution
- pipeline failure rate per stage
- P50/P95 end-to-end latency

Model:

- coherence score drift by domain
- superiority margin drift
- anti-gaming alert rate
- confidence calibration curve

Business:

- pass rate by ask bucket
- conversion to partner meeting
- investment close rate
- post-investment outcome correlation

---

## Hard Requirements for Production Readiness

1. Deterministic schema contracts across all services. [DONE prompt 02]
2. Immutable audit trail from audio to decision. [DONE prompt 07]
3. Prompt/version pinning and replay support. [DONE prompt 08]
4. Human override and kill switch at policy layer. [DONE prompt 12] — shadow mode routes fresh applications through the enforce/shadow branch without publishing `decision.issued` events in shadow mode; operator flip is the kill switch
5. Continuous red-team testing. [DONE prompt 13]
6. Data retention and deletion workflows.

---

## Immediate Next 10 Actions

1. Create `docs/decision_policy_spec.md` with exact equations and parameter ranges. [DONE prompt 01] — shipped as `docs/specs/decision_policy_spec.md`
2. Define event schemas (`InterviewCompleted`, `ArgumentCompiled`, `DecisionIssued`). [DONE prompt 02] — `server/fund/schemas/events/*.v1.json` with offline validator in `server/fund/services/event_schemas.py`
3. Build a thin workflow orchestrator skeleton. [DONE prompt 15] — `server/fund/services/workflow.py` (9-stage pipeline)
4. Implement transcript quality gate before scoring. [DONE prompt 03] — `server/fund/services/transcript_quality.py`
5. Add ontology/domain output fields into current result dataclasses. [DONE prompts 05, 06] — `core/types.py::{Entity, OntologyEdge, OntologyGraph, DomainMix, NormativeProfile}`
6. Implement policy microservice with dry-run mode. [DONE prompts 01, 12] — policy service lives in `server/fund/services/decision_policy.py`; shadow mode provides the dry-run branch (prompt 12)
7. Build escalation packet generator. [DONE prompts 07, 14] — decision artifact (`server/fund/services/decision_artifact.py`) plus notification templates (`server/fund/data/notification_templates/`)
8. Add prompt registry file with version metadata. [DONE prompt 08] — `data/prompts/registry.json` + `server/fund/services/prompt_registry.py`
9. Run first backtest on historical or simulated founder corpus. [DONE prompt 11] — `server/fund/services/backtest.py`, `deploy/scripts/run_backtest.py`, fixtures under `tests/fixtures/backtest/`
10. Launch shadow mode with no automated founder notifications yet. [DONE prompt 12] — `scoring_mode` plumbing + `alembic/versions/20260417_000004_scoring_mode.py`

