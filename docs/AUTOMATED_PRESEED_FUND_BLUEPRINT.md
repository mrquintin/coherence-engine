# Automated Quantitative Pre-Seed Fund Blueprint

## Objective

Transform the current Coherence Engine into a fully automated pre-seed fund intake and screening platform where:

1. Founders talk to an AI interviewer (phone or web voice).
2. The conversation is transcribed into structured claims, premises, and assumptions.
3. The argument is converted into an ideological and ontological representation.
4. The system computes domain-relative coherence superiority.
5. A funding-size-adjusted threshold is applied automatically.
6. Qualified founders are escalated to you with a concise decision packet and email intro.

This document defines the target architecture, core quantitative logic, automation flow, controls, and deployment strategy.

---

## Core Quantitative Decision Model

Use your report's principle as the policy backbone:

- Required evidence/coherence increases logarithmically with ask size.
- Diligence effort increases sublinearly with ask size.

Recommended production formulas:

1) Funding-size threshold

`CS_required(S, d) = CS0_d + alpha_d * log2(S / S_min_d) + R(S, portfolio_state)`

- `S`: founder requested check
- `d`: detected domain
- `CS0_d`: base threshold for domain d
- `alpha_d`: slope (default `1/(2*gamma_d)`)
- `S_min_d`: minimum check size for domain d
- `R(...)`: concentration/risk adjustment (portfolio occupancy, exposure caps, drawdown regime)

2) Diligence compute/time budget

`Budget_tokens(S, d) = B0_d * sqrt(S / S_min_d)`

3) Position sizing guardrail

`S_max = Fund_NAV * clip((Edge_hat * payoff_hat - 1)/(gamma_portfolio * payoff_hat), 0, cap_d)`

The decision policy should approve only if:

- `CS_superiority >= CS_required(S, d)`
- confidence and anti-gaming constraints pass
- legal/compliance constraints pass
- portfolio risk constraints pass

---

## End-to-End Automated Workflow

## Stage 1: Founder Intake and Consent

1. Founder enters via phone call, web voice room, or async voice note.
2. System presents disclosure:
   - AI-assisted assessment
   - recording/transcription consent
   - no funding guarantee
   - data handling policy
3. Founder provides:
   - identity + contact
   - company basics
   - requested amount
   - target use of funds

Output: `intake_record`, legal consent log, communication channel preference.

## Stage 2: Adaptive Voice Interview

AI interviewer follows a structured script with dynamic branching:

- problem clarity
- solution mechanism
- causal chain to outcomes
- evidence quality
- market assumptions
- moat/defensibility
- execution plan and milestones
- critical self-critique

The interviewer aggressively probes contradictions and unsupported jumps.

Output: timestamped transcript + turn metadata + confidence scores.

## Stage 3: Transcript Normalization and Argument Structuring

Pipeline:

1. diarization cleanup
2. semantic de-duplication
3. proposition extraction
4. claim-premise-evidence-qualifier tagging
5. relation extraction (supports/attacks/depends-on)

Output: structured argument graph compatible with your existing parser/scorer model.

## Stage 4: Ideological Ontology and Domain Reconstruction

Build a hybrid domain profile from:

- topical embeddings
- premise overlap
- ontological commitments (entities, causal objects, actors)
- normative markers (rights/utilitarian/deontic frames)
- argumentation pattern signatures

Output:

- `domain_mix` (single domain or weighted blend)
- `ontology_graph`
- `normative_profile`
- incumbent comparison cohort

## Stage 5: Coherence Superiority Computation

Run current and extended scoring stack:

- contradiction layer
- argumentation layer
- embedding/sparsity layer
- compression layer
- structural layer
- cross-layer fusion

Then compute:

- absolute coherence score
- incumbent-domain coherence baseline
- superiority margin (`CS_superiority`)
- confidence interval
- adversarial-risk flags (possible rhetorical gaming)

## Stage 6: Decision Engine and Escalation

Apply policy gates in sequence:

1. quality gate (minimum transcript quality)
2. coherence gate (`CS_superiority` vs threshold)
3. confidence gate (uncertainty below bound)
4. anti-gaming gate
5. compliance gate
6. portfolio gate

If pass:

- create founder escalation packet
- generate intro email to you
- schedule meeting links automatically
- optionally send founder "provisional pass" notification

If fail:

- send constructive decline note with optional re-apply conditions
- log model rationale for audit trail

---

## System Architecture (Production)

## Services

1. `intake-gateway`
   - handles web, phone, and identity
2. `conversation-orchestrator`
   - drives adaptive interview prompts
3. `speech-stack`
   - ASR, diarization, transcript confidence
4. `argument-compiler`
   - transforms transcript into formal argument objects
5. `ontology-domain-engine`
   - domain reconstruction + ontology graphing
6. `coherence-engine-service`
   - your existing scoring system plus extensions
7. `decision-policy-engine`
   - applies threshold and risk logic
8. `notification-service`
   - email, CRM push, calendar handoff
9. `audit-observability-service`
   - immutable logs, score provenance, model versions

## Data Stores

- Postgres: canonical records (founders, applications, decisions)
- Object storage: raw audio, transcripts, reports
- Vector store: semantic retrieval for domain comparisons
- Graph store (optional but useful): ontology and contradiction graphs
- Feature store: calibration features for risk/slope updates

## Message and Workflow Layer

- Durable queue + orchestrator (Temporal, Celery, or managed workflow service)
- Every stage idempotent with retry policy
- Dead-letter handling for failed cases

## Security and Governance

- Encrypt audio/transcripts at rest and in transit
- Fine-grained RBAC for staff/admin access
- model + prompt version pinning for reproducibility
- founder data retention policy
- decision explainability artifact per case

---

## Suggested Tech Stack

- Voice ingress: Twilio Voice + webRTC app
- ASR: Deepgram or Whisper-hosted
- LLM orchestration: provider-agnostic abstraction
- API: FastAPI
- Queue/workflow: Temporal
- DB: Postgres
- Object store: S3-compatible
- Vector DB: pgvector or dedicated service
- Graph: Neo4j (optional first, useful later)
- Monitoring: OpenTelemetry + Prometheus + Grafana

Keep vendor abstraction at boundaries to reduce lock-in.

---

## Automation Design Principles

1. Human-in-the-loop only at final investment close.
2. Every automated decision must have replayable evidence.
3. Never use a single model output as final truth; use multi-signal consensus.
4. Separate evaluation from persuasion quality (anti-charisma bias).
5. Calibrate by domain, not globally.
6. Detect and penalize contradiction laundering (rephrasing inconsistency).
7. Enforce uncertainty-aware gating; uncertain cases route to manual review.

---

## Model and Policy Calibration Program

## Inputs for calibration

- historical startups + outcomes
- synthetic adversarial pitches
- blinded human expert scoring
- domain-specific base rates

## Outputs to calibrate

- `CS0_d`, `alpha_d`, `gamma_d`
- confidence cutoffs
- anti-gaming detector thresholds
- expected false-pass / false-reject rates

## Cadence

- weekly drift checks
- monthly policy recalibration
- quarterly red-team evaluation

---

## Operational Playbook

## Escalation packet contents

- founder identity + contact
- one-page thesis summary
- requested amount + intended use
- domain and ontology profile
- coherence layer breakdown
- superiority margin and threshold comparison
- key contradictions resolved/unresolved
- top risks + recommended diligence focus
- auto-generated meeting brief

## Notifications

- to you: high-signal concise email + dashboard link
- to founder: status + timeline + next steps
- internal logs: immutable decision event

---

## 12-Month Build Plan (High Level)

Phase 1 (Weeks 1-4): Foundation

- event-driven architecture skeleton
- intake + consent + transcript pipeline
- initial dashboard

Phase 2 (Weeks 5-10): Argument and Ontology

- transcript-to-argument compiler
- domain reconstruction v1
- ontology graph pipeline

Phase 3 (Weeks 11-16): Quant Decision Engine

- implement `CS_required(S, d)` policy service
- implement confidence/anti-gaming gates
- escalation automation

Phase 4 (Weeks 17-24): Calibration + Validation

- historical backtesting
- threshold tuning
- pilot with bounded check sizes

Phase 5 (Weeks 25-36): Reliability + Compliance

- formal audit trails
- red-team stress testing
- governance and exception tooling

Phase 6 (Weeks 37-52): Scale

- multi-domain specialization
- active learning loops
- partner/LP reporting automation

---

## Risk Register (Top Items)

1. Persuasion-over-substance bias in voice interviews.
2. Domain misclassification causing wrong thresholding.
3. Transcript errors cascading into false contradiction flags.
4. Adversarially optimized founder scripts.
5. Overconfidence in early-stage calibration.
6. Regulatory/legal constraints for automated investment workflows.

Each risk needs an owner, metric, and mitigation test.

---

## Definition of "Fully Automated" in This Context

Automated means:

- founder intake to scored recommendation runs without manual intervention
- threshold policy and routing are automatic
- only final close and funds transfer remain human-authorized

This keeps the system high-automation while maintaining prudent legal and fiduciary controls.

