# Synthetic seed pitches — historical-startups validation corpus

**These 25 rows are fully fabricated and must NOT be conflated with real
founder data.**

Every file in this directory has `provenance.source = "synthetic"` and is
exempt from the consent invariant (no real founder is involved). The rows
exist only to (a) exercise the ingestion harness end-to-end, and (b) pin
the deterministic `historical-corpus stat` output that tests assert
against.

The full 500-row cohort used by the predictive-validity study is built by
the operator from real archives (`crunchbase`, `cb_insights`,
`operator_archive`, `public_filings`); each real row carries written
consent recorded in `provenance.consent_documented = true`. See
`docs/specs/historical_corpus.md` for the consent invariant and the full
eligibility rules.

## How the seeds vary

The seeds intentionally span four coherence bands (low / borderline /
medium / high) by varying the evidence-floor counts (`n_propositions`,
`n_metrics_cited`, `n_sources_cited`). Two rows
(`seed_05_fintech.json`, `seed_11_healthtech.json`) sit outside the
date window so the eligibility computation has at least one failing
flag in the seeded distribution.
