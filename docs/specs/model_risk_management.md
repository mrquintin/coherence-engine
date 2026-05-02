# Model Risk Management — Quarterly Report

> **Status:** internal practice document, *informed by* (not legally
> compliant with) the OCC / Federal Reserve **SR 11-7 Supervisory
> Guidance on Model Risk Management** (April 2011). No regulatory
> filing, third-party attestation, or legal compliance claim is made
> or implied. This page documents how we structure our quarterly
> review so an outside reviewer who *does* operate under SR 11-7 can
> map our artifacts onto theirs without having to invent the shape.

## Purpose

The quarterly Model-Risk-Management (MRM) report consolidates
governance evidence about the Coherence Engine's underwriting model:
what it is for, where its limits are, what the latest validation
study says, how it has drifted, how often partners override it, how
often anti-gaming alerts fire, whether decisions can be reproduced,
and what we know is wrong that we have not yet fixed.

The report exists so that, every three months, a reviewer who has
not been deep in the implementation can read one document and form
an opinion about whether the model is being operated responsibly.

## Why SR 11-7 (informed by, not compliant with)

SR 11-7 is the supervisory text U.S. banks use when describing model
risk. We are not a bank, do not file with a federal regulator, and
make no compliance claim. But the framework's spine is useful and
broadly portable:

1. **Model definition & limitations.** What the model is, what it is
   not, where its assumptions break.
2. **Ongoing validation.** Independent re-examination of the model
   on data it has not seen.
3. **Outcome analysis & monitoring.** Drift, calibration, override
   rates, alert rates.
4. **Governance & controls.** Reproducibility, change logs, an
   accountable owner for known weaknesses.

We mirror that spine here so future reviewers do not have to invent
the shape.

## Pipeline overview

```
data/governed/model_risk/backlog.yaml         ─┐
validation-study report (prompt 44)            │
calibration-drift telemetry (prompt 18)        │      assemble_quarterly_report
override aggregates (prompt 35)                ├──▶   ─────────────────────────────▶ MRMReportData
anti-gaming alert aggregates (prompt 11)       │
reproducibility audit results                  │
                                              ─┘
                                                                │
                                                                ▼
                                                quarterly.tex.j2 (Jinja2 + LaTeX)
                                                                │
                                                                ▼
                                                  pdflatex (two passes, isolated tempdir)
                                                                │
                                                                ▼
                                                            PDF report
                                                                │
                                                                ▼
                                                  object-storage put + outbox event
                                                  (event_type=mrm_report_published)
```

## Components

### `server/fund/services/model_risk_report.py`

Pure-Python assembler. Defines `QuarterRef`, `MRMReportInputs`, and
the `MRMReportData` dataclass. The single public entry point is
`assemble_quarterly_report(inputs)` → `MRMReportData`.

* Each source path is **optional**. A missing file produces an empty
  section rather than aborting the report (early quarters legitimately
  have no data for some surfaces).
* The assembler is **deterministic**: same inputs → same canonical
  bytes via `MRMReportData.to_canonical_bytes()`. The `input_digest`
  hashes everything that determines the report payload.
* PII is **never** included in raw form. Partner identifiers are
  hashed with a stable salt; override `reason_text` is intentionally
  omitted; only aggregates flow through.

### `server/fund/services/model_risk_renderer_pdf.py`

LaTeX → PDF pipeline.

* `render_tex(data)` — pure function from `MRMReportData` to a
  deterministic `.tex` source string.
* `render_pdf(data)` — runs `pdflatex` twice in an isolated temp
  directory and returns `(pdf_bytes, log_text, pages)`.
* Failure surfaces as `PdflatexNotInstalled` (binary missing) or
  `PdflatexRenderError` (compilation failed; `.log_text` attached).

### `data/governed/model_risk/templates/quarterly.tex.j2`

Jinja2 template using **square-bracket delimiters** (`[[ … ]]` for
variables, `[% … %]` for blocks) so the curly braces stay free for
LaTeX. Uses `lmodern` + `microtype` per the existing Guides
convention. Renders sections for purpose, limitations, validation
summary, drift indicators, override stats, anti-gaming, reproducibility,
known weaknesses, and remediation backlog. Empty sections render as
`(no data this quarter)` so a reviewer notices the absence rather
than mistaking it for a clean bill of health.

### `data/governed/model_risk/backlog.yaml`

Source of truth for the qualitative content: model purpose, model
limitations, known weaknesses, remediation backlog. Edit this file
directly; the assembler reads it on every report run.

### `cli.py`

Two subcommands under `coherence-engine mrm-report`:

* `mrm-report generate --quarter 2026Q2 --output report.pdf`
  Assembles + renders. Optional flags wire in the source artifacts
  (`--validation-study`, `--drift-telemetry`, `--override-stats`,
  `--anti-gaming-stats`, `--reproducibility-audit`). `--tex-only`
  skips pdflatex and writes the raw `.tex`. `--log-output` captures
  the `.log` next to the PDF for debugging.
* `mrm-report publish --pdf report.pdf --quarter 2026Q2`
  Uploads the PDF to object storage and emits an
  `mrm_report_published` outbox event with the storage URI, sha256,
  size, and actor.

## Determinism contract

The renderer emits **byte-identical** `.tex` source for identical
input data. This is the cheapest signal of accidental drift in
template ordering or in the assembler's aggregation logic, and is
why the test suite asserts it on every run. The contract holds
because:

* Every list passed to Jinja2 is sorted at construction time
  (drift indicators by metric, partner stats by partner hash, etc.).
* The Jinja2 environment is configured with `trim_blocks` +
  `lstrip_blocks` so whitespace handling is fixed.
* No wall-clock reads occur inside `assemble_quarterly_report`; the
  `generated_at` timestamp is supplied by the caller and tests freeze
  it.

The PDF output is *not* byte-deterministic because pdflatex stamps
its build with creation timestamps and a random ID — that is by
design upstream and is not something we try to defeat. The
`input_digest` printed on every page is the deterministic anchor a
reviewer compares against.

## Operational runbook

### Generate a quarterly report

```
python -m coherence_engine mrm-report generate \
    --quarter 2026Q2 \
    --output build/mrm-2026Q2.pdf \
    --validation-study artifacts/validation-2026Q2.json \
    --drift-telemetry artifacts/drift-2026Q2.json \
    --override-stats artifacts/overrides-2026Q2.json \
    --anti-gaming-stats artifacts/ag-2026Q2.json \
    --reproducibility-audit artifacts/repro-2026Q2.json \
    --log-output build/mrm-2026Q2.log
```

The CLI prints a JSON line on stdout with `wrote`, `input_digest`,
`report_digest`, `pages`, and `size_bytes`.

### Publish to object storage

```
python -m coherence_engine mrm-report publish \
    --pdf build/mrm-2026Q2.pdf \
    --quarter 2026Q2 \
    --actor "$USER"
```

Writes to the configured object-storage backend at key
`model_risk/2026Q2/report.pdf` (override with `--storage-key`) and
emits an `mrm_report_published` event to the outbox.

### Edit the backlog

Update `data/governed/model_risk/backlog.yaml`, commit, then re-run
the generate command. The `input_digest` will change, which a
reviewer comparing two PDFs of the same quarter can use to confirm
the underlying inputs really did update.

## Known limits of the report itself

* The assembler is *defensive about missing inputs*. A quarter where
  no source data is wired in still produces a PDF — empty sections
  display "(no data this quarter)". This is intentional but means a
  reviewer must check section emptiness, not just signature.
* Partner hashing is one-way and salted with a constant; if the salt
  is rotated, prior reports are no longer cross-referenceable. Treat
  the salt as part of the report contract.
* `pdflatex` is not pinned to a specific TeX Live version. The
  template depends only on packages present in any plausible TeX Live
  install (`lmodern`, `microtype`, `geometry`, `longtable`, `array`,
  `booktabs`, `fancyhdr`, `parskip`, `lastpage`).

## References

* OCC / Fed SR 11-7, *Supervisory Guidance on Model Risk Management*,
  April 2011 — the framework this report is informed by.
* `docs/specs/validation_study.md` — predictive-validity study used
  as a primary input.
* `docs/specs/leakage_audit.md` — the audit gate that blocks the
  validation-study renderer until it passes.
* `docs/specs/decision_policy_spec.md` — the overrides surface that
  feeds `override_partner_stats`.
