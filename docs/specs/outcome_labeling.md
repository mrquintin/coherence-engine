# Outcome labeling — historical-startups validation corpus (prompt 43)

## Purpose

Attach realized outcomes to each pitch in `data/historical_corpus/manifest.jsonl`
so the predictive-validity study can compare Coherence Engine scores against
ground truth. Outcomes are stored separately from the corpus rows in
`data/historical_corpus/outcomes.jsonl` and joined on `pitch_id` at export time.

## Schema

`server/fund/schemas/datasets/outcome_label.v1.json` (strict;
`additionalProperties: false` everywhere). Required top-level fields:

| Field                  | Type                              | Notes                                                                 |
|------------------------|-----------------------------------|-----------------------------------------------------------------------|
| `schema_version`       | `"1"`                             | Bumping requires a new `outcome_label.v2.json`.                       |
| `pitch_id`             | UUIDv7 string                     | Joins to `historical_pitch.v1.pitch_id`.                              |
| `survival_5yr`         | `true` \| `false` \| `"unknown"`  | Was the company still operating five years after `pitch_year`?        |
| `exit_event`           | `acquired \| ipo \| shutdown \| active \| unknown` | See corroboration rules below.                              |
| `last_known_arr_usd`   | number ≥ 0 or `null`              | Annualized recurring revenue, USD; `null` when not disclosed.         |
| `last_known_headcount` | integer ≥ 0 or `null`             | FTE headcount; `null` when not disclosed.                             |
| `outcome_as_of`        | `YYYY-MM-DD`                      | Date this label is asserted to be true.                               |
| `outcome_provenance`   | object (required)                 | See below — every sub-field is required.                              |

### Provenance (required)

`outcome_provenance` is itself a strict object with every field required:

* `source` — one of `crunchbase | pitchbook | sec_edgar | company_blog | news_archive | operator_query`.
* `url` — canonical URL of the source document; must parse with
  `urllib.parse.urlparse` and have both a `scheme` and a `netloc`.
* `retrieved_at` — ISO-8601 timestamp.
* `retrieved_by` — operator handle or service-account identifier.

**Unsourced labels are rejected at write time.** `attach_outcome()` raises
`OutcomeSchemaError` and writes nothing to disk.

## Labeling protocol

* `acquired`, `ipo`, and `shutdown` require **minimum two-source
  corroboration** — one of the two must be primary (SEC EDGAR filing, the
  company's own blog/press release, or a regulator-of-record). The second can
  be a news-archive or data-provider entry. Operators record both retrievals
  by appending two outcome rows (each with its own provenance); the latest
  `outcome_as_of` wins at export time, but the corroboration is auditable in
  the file history.
* `active` is the default for companies that are still operating at
  `outcome_as_of` and have no recorded exit event. A single primary source is
  acceptable.
* `unknown` is acceptable when the data is genuinely missing — operators must
  never default `survival_5yr` to `true`/`false` or `exit_event` to `active`
  in the absence of evidence. Rows with `unknown` for either field are
  **excluded from the default study export**; they remain in the file as a
  record that the pitch was investigated. Pass `--include-unknown` to keep
  them.

## CLI

```
# Append an outcome row (validates first; provenance required).
python -m coherence_engine outcomes attach \
    --pitch-id <uuidv7> --row outcome.json

# Audit: every pitch must have ≥ 1 outcome row with a non-null
# outcome_as_of and a parseable provenance URL. Exits 2 otherwise.
python -m coherence_engine outcomes audit

# Export the study-ready frame (latest outcome per pitch_id, unknowns
# excluded by default).
python -m coherence_engine outcomes export
```

## File format

`data/historical_corpus/outcomes.jsonl` is a JSON-lines file with one outcome
row per line. Lines beginning with `#` are treated as comments by the loader
(used for the file header). Multiple rows per `pitch_id` are allowed — the
export keeps the row with the greatest `outcome_as_of`.

## Invariants honored by the harness

* Provenance is required — there is no code path that writes an outcome row
  without a complete `outcome_provenance` block.
* Missing-data labels are explicit (`unknown`); the schema does not allow a
  silently-defaulted `survival_5yr` or `exit_event`.
* Default `export()` excludes `unknown` rows from the study frame so models
  are not biased by labels with no evidence behind them.
