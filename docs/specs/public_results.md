# Public validation-results page (prompt 46)

The public `/results` route on the marketing site
(`apps/site`) is sourced *exclusively* from the canonical study JSON
emitted by `validation-study run` (prompt 44). The renderer is
`scripts/render_study_to_mdx.py`; it is pure (deterministic, no
network, no clock) and refuses to publish a report that did not
clear the leakage audit (prompt 45).

## Pipeline

1. Operator runs `python -m coherence_engine validation-study run --output data/governed/validation/study_<version>.json`.
2. The study harness invokes the leakage audit before writing any
   bytes; a failure raises `LEAKAGE_DETECTED` and produces no JSON.
3. Operator runs `python scripts/render_study_to_mdx.py --study-json
   data/governed/validation/study_<version>.json` which:
   - re-checks `generated_with.leakage_audit_passed == "true"`
     (RefusePublication if not),
   - writes `apps/site/src/content/results/study_<version>.mdx`
     deterministically, and
   - rebuilds `apps/site/public/results/feed.xml` (RSS) over every
     study JSON in the same directory.
4. The Astro site picks up the new MDX at the next `pnpm build` and
   exposes it at `/results/<slug>/`.

## File layout

| Path | Purpose |
| --- | --- |
| `scripts/render_study_to_mdx.py` | Pure renderer (study JSON → MDX + RSS). |
| `apps/site/src/content/results/study_<version>.mdx` | One MDX per study. |
| `apps/site/src/pages/results/index.astro` | Lists every rendered study by `published_at` desc. |
| `apps/site/src/pages/results/[slug].astro` | Renders one study's MDX. |
| `apps/site/src/pages/results/latest.astro` | Meta-refresh redirect to the most recent study (or `/results/` if empty). |
| `apps/site/public/results/feed.xml` | RSS 2.0 feed across all rendered studies. |

## Frontmatter

Every rendered MDX page has the following frontmatter (stable order,
deterministic quoting):

```yaml
title: "Validation study — <study_name> (<version>)"
published_at: "<ISO-8601 date or version string>"
version: "<pre-registration version>"
n_pitches: <int — N(known outcome)>
domain_count: <int — domains with a fitted sub-model>
headline: "<one-line summary; obeys the negative-result rule below>"
rejected_null: <true|false>
data_hash: "<sha256 of the joined frame>"
leakage_audit_digest: "<digest from generated_with>"
schema_version: "<study schema version>"
```

## Negative-result rule

The headline is composed by `headline_for(report)` from a fixed
two-branch lookup. When `primary_hypothesis_result.rejected_null`
is `false` (a null finding or a wrong-sign finding), the headline:

* states plainly that the test did **not** reject H0,
* never includes any of the words `successfully`, `confirmed`,
  `validated`,
* is rendered with the same prominence (same heading level, same
  layout) as a positive headline.

The interpretation and limitations sections also bias toward
negative-result fidelity: a null finding triggers an explicit
discussion of the two competing explanations (no real effect vs.
underpowered) and a reference to the pre-registration's
`negative_results_policy` block.

This is enforced at three layers:

1. The renderer's `headline_for` is exhaustively tested for spin-word
   absence in `tests/test_render_study_to_mdx.py`.
2. The MDX page front-matter `rejected_null` is mirrored into the
   index and detail page templates so a null finding cannot be
   visually demoted at the layout layer.
3. The pre-registration document
   (`data/governed/validation/preregistration.yaml`) declares
   `negative_results_policy.publish_when_null: true` and
   `publish_when_wrong_sign: true`; the renderer reads this block
   and pins it into the page footer.

## Publication gate

`render()` raises `PublicationRefused` (and the CLI exits 2) when
`generated_with.leakage_audit_passed` is anything other than the
literal string `"true"`. There is no bypass flag; the only way to
ship a report is to fix the audit. The published page also displays
the `leakage_audit_digest` so a reader can cross-reference the audit
record.

## Determinism

* Same study JSON ⇒ byte-identical MDX. Tests assert this with
  `test_render_is_deterministic` and `test_write_mdx_is_byte_deterministic`.
* `render_feed_xml(reports)` sorts by `(published_at, version)`
  descending so the same set of studies always yields the same feed.
* SVG plots embed a fixed `viewBox`, fixed numeric formatting (4-digit
  rounding), and no time-based attributes.

## RSS

The feed lives at `/results/feed.xml`. It is rebuilt by the renderer
every time a study is written; the on-disk template at
`apps/site/public/results/feed.xml` is the empty-channel placeholder
used until the first study is rendered. The feed advertises the same
headline as the page so a syndicating reader sees the same negative
or positive framing as a web visitor.

## Operator runbook

```sh
# 1. Run the validation study (audit runs implicitly).
PYTHONPATH=.. python3 -m coherence_engine validation-study run \
    --output data/governed/validation/study_v1.0.json

# 2. Render the page + refresh feed.xml.
python3 scripts/render_study_to_mdx.py \
    --study-json data/governed/validation/study_v1.0.json \
    --site-url https://coherence.example.com

# 3. Build the static site.
cd apps/site && pnpm install && pnpm build
```

If step 1 fails with `INSUFFICIENT_SAMPLE` or `LEAKAGE_DETECTED`, no
report exists for step 2 to render — that is the intended behaviour.

## Prohibitions (prompt 46)

* **Never alter the study report content during rendering.** The
  renderer is read-only over the JSON payload.
* **Never publish without `leakage_audit_passed == "true"`.**
* **Never bury negative findings.** The headline rule above is the
  load-bearing invariant of this surface.
* **Never edit files outside the prompt-46 scope** (the only
  exception is the deletion of the prompt-46-superseded stub
  `apps/site/src/pages/results.astro`, whose own comment marked it
  for replacement here).
