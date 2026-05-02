# Historical-startups validation corpus (prompt 42)

The historical-startups corpus is the data layer for the predictive-validity
study: 500 anonymized founder pitches drawn from heterogeneous sources, each
one paired with the source artifacts (transcript, deck, memo) and a set of
eligibility flags that gate it into the cohort. Outcome labels are attached
separately by prompt 43; this document covers schema, ingestion, and the
consent invariant.

## On-disk layout

```
data/historical_corpus/
  manifest.jsonl           # one row per accepted pitch (append-only)
  seeds/
    seed_00_*.json … seed_24_*.json   # 25 synthetic seed rows shipped in-tree
    README.md
```

The schema lives at
`server/fund/schemas/datasets/historical_pitch.v1.json` and is mirrored
by a hand-written validator in
`server/fund/services/historical_corpus.py::_validate_row`. Bumping the
schema requires updating both files in the same commit.

## Row shape

Every row is a JSON object with these fields:

| Field | Type | Purpose |
| --- | --- | --- |
| `schema_version` | `"1"` | Pinned. Bumping is a versioning event. |
| `pitch_id` | UUIDv7 (lowercase hex) | Stable identity. |
| `company_name` | `^anon_[0-9a-f]{16}$` | Anonymized hash; never cleartext. |
| `domain_primary` | enum (10 values) | `fintech \| healthtech \| biotech \| deeptech \| consumer \| enterprise_saas \| marketplace \| climate \| edtech \| other`. |
| `pitch_year` | int [2005, 2030] | Year the pitch was originally delivered. |
| `country` | ISO 3166-1 alpha-2 | Two uppercase letters. |
| `transcript_uri` | `coh://...` | Object-storage URI for the transcript. |
| `deck_uri` | `coh://...` | Object-storage URI for the slide deck. |
| `memo_uri` | `coh://...` | Object-storage URI for the investor memo. |
| `evidence_floor.{n_propositions, n_metrics_cited, n_sources_cited}` | int ≥ 0 | Used to compute `evidence_floor_ok`. |
| `eligibility.*` | bool | Stored flags (re-derivable from content). |
| `provenance.{source, ingested_at, ingestion_run_id, consent_documented}` | mixed | Where the row came from and whether consent is on file. |

`additionalProperties: false` is enforced everywhere — the row shape is closed.

## Eligibility

Four flags determine whether a row enters the cohort. All four must be `true`:

| Flag | Rule |
| --- | --- |
| `date_window_ok` | `2005 ≤ pitch_year ≤ 2024` |
| `evidence_floor_ok` | `n_propositions ≥ 10`, `n_metrics_cited ≥ 3`, `n_sources_cited ≥ 2` |
| `no_training_overlap_ok` | `pitch_id` not in `TRAINING_CORPUS_PITCH_IDS` |
| `consent_documented` | `provenance.source == "synthetic"` OR `provenance.consent_documented == true` |

`compute_eligibility` is a pure function. The harness recomputes flags at
ingest and at validate; drift between stored and recomputed values is logged
in the `ValidationReport.eligibility_drift` list. Drift never fails
validation — it surfaces rows that need re-ingest after a content change.

## Consent invariant

**Every real founder pitch in the corpus must have written consent recorded
in `provenance.consent_documented = true`.** Synthetic rows are exempt
(`provenance.source = "synthetic"`) but every other source — `crunchbase`,
`cb_insights`, `operator_archive`, `public_filings` — must carry documented
consent on every row. The ingestion harness refuses any non-synthetic row
with `consent_documented = false` and emits a `consent_missing` rejection.

This is the load-bearing rule that lets the corpus be used in published
validation work. Removing or weakening it requires legal and IRB sign-off.

## Ingestion

```
python -m coherence_engine historical-corpus ingest \
    --source operator_archive --path /tmp/pitches/ [--apply]
```

`ingest`:

1. Walks the path (single file or directory of `*.json`).
2. Stamps each row with `provenance.source`, `provenance.ingested_at`, and
   `provenance.ingestion_run_id`.
3. Refuses non-synthetic rows lacking documented consent.
4. Recomputes and writes back the canonical `eligibility` block.
5. Validates against the v1 schema.
6. Skips rows whose `pitch_id` is already in the manifest (idempotent).
7. Appends accepted rows to `manifest.jsonl` (only when `--apply` is passed).

Without `--apply` the harness runs in dry-run mode: it computes the report
but writes nothing. Always preview real ingests before applying.

## Validation

```
python -m coherence_engine historical-corpus validate
```

Re-validates every row in the manifest against the v1 schema and recomputes
eligibility. Exit 0 if every row is schema-valid; exit 2 otherwise.
Eligibility drift never fails the run; it is reported separately.

## Stat

```
python -m coherence_engine historical-corpus stat
```

Prints a deterministic JSON summary: total rows, counts by source, by
domain, by year, and per-flag eligibility counts. The seeded corpus has a
pinned report — the test suite asserts on it. Regenerating the seeds
requires updating the pin.

## Seed corpus (PR 1)

The initial PR ships 25 fully-fabricated synthetic seeds (`provenance.source
= "synthetic"`) that span four coherence bands and exercise the
eligibility logic end-to-end. The operator backfills the remaining
real-source rows.

Synthetic seeds must never be conflated with real founder data: the
`source = "synthetic"` flag is what enables the consent exemption, and
mixing the two would silently leak past the consent invariant.
