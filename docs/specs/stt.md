# Speech-to-Text (STT) abstraction

Prompt 40 — depends on prompts 38 (Twilio voice intake) and 39 (browser
WebRTC intake). The STT layer turns recorded interview audio into the
canonical `coherence_engine.core.types.Transcript` consumed by the
transcript compiler and the `transcript_quality` gate.

## Backends

Three pluggable backends sit behind one `SpeechToText` protocol:

| backend          | name        | use case                                  | network calls |
|------------------|-------------|-------------------------------------------|---------------|
| `WhisperLocal`   | `whisper`   | default; runs on the worker's own CPU/GPU | none          |
| `Deepgram`       | `deepgram`  | low-latency managed service               | `POST /v1/listen` |
| `AssemblyAI`     | `assemblyai`| diarization-heavy interviews              | `POST /upload`, `POST /transcript`, `GET /transcript/{id}` |

The Whisper backend lazy-imports `faster_whisper` first, then `whisper`
(the legacy openai package). If neither is installed,
`WhisperNotAvailable` is raised the first time `transcribe` is called —
the import never happens at module load, so deployments without the ML
wheels can still use the managed providers.

## Provider selection and fallback

`STTRouter` reads two environment variables:

* `STT_PROVIDER_PRIMARY` — defaults to `whisper`.
* `STT_PROVIDER_FALLBACK` — optional. If unset, no fallback runs.

The router calls the primary first. It falls back to the secondary when:

* the primary raises `STTError` (transport timeout, 5xx response, or
  any other recoverable provider-side error), or
* the primary returns a transcript whose **average word confidence** is
  below `STT_MIN_AVG_CONFIDENCE` (default `0.6`).

If both backends fail outright, `STTUnavailable` is raised. If both
return below-threshold transcripts, the router returns the
higher-confidence of the two and stamps `LOW_STT_CONFIDENCE` on
`STTResult.quality_flags`. That flag is consumed by
`server/fund/services/transcript_quality.py` (prompt 03), whose
existing `TQG_ASR_CONFIDENCE_LOW` check folds the per-turn confidence
values into the deterministic gating decision.

4xx responses are **not** treated as transient. A misconfigured API key
or a missing audio file should fail loudly rather than silently masking
the misconfiguration with a fallback that hides the root cause.

## Cost-per-minute (April 2026 list prices)

| provider       | tier              | $/audio-minute | notes                                     |
|----------------|-------------------|----------------|-------------------------------------------|
| Whisper local  | self-hosted CPU   | ~$0.001        | dominated by worker amortization          |
| Whisper local  | self-hosted GPU   | ~$0.002        | amortized over GPU-bound batches          |
| Deepgram       | Nova-2 General    | $0.0043        | streaming + pre-recorded; volume tiers    |
| Deepgram       | Nova-2 Phonecall  | $0.0058        | tuned for narrowband telephony            |
| AssemblyAI     | Universal-1 best  | $0.0065        | includes diarization + LeMUR add-ons      |
| AssemblyAI     | Universal-1 nano  | $0.0030        | English-only; cheaper, lower accuracy     |

Estimates assume one founder × one interview ≈ 30 minutes audio per
application. Numbers must be re-verified before any commercial launch.

## Language and accent fitness

* **Whisper local** — strongest on US/UK English; degrades on heavy
  L2 accents and code-switching. Use the `large-v3` checkpoint for
  non-English languages where latency is acceptable; `base` is fine
  for clean English.
* **Deepgram Nova-2** — competitive on English (US/UK/AU) and several
  EU languages; weak on tonal Asian languages compared to Whisper.
  Phonecall-tuned model materially outperforms general on Twilio audio.
* **AssemblyAI Universal-1** — best diarization out of the box and
  strong on English (incl. Indian English). Best choice when the
  interview has overlapping speakers or significant cross-talk.

## Per-word confidence

Every backend produces a `(turn_index, word, start_s, end_s, confidence)`
tuple per transcribed word. The router averages those confidences for
its threshold check; the `STTResult.words` field is preserved for
downstream consumers (UI highlighting, alignment to slides, etc.). When
a backend returns no per-word breakdown — e.g. a Deepgram response that
came back without `utterances` — the router falls back to per-turn
confidence as the gating signal.

## Audio fetch

Backends use the small `fetch_audio_bytes` helper from
`server.fund.services.stt.interface`, which resolves `file://` URIs and
absolute filesystem paths directly and falls through to
`server.fund.services.object_storage.get` for everything else. This
keeps the test suite working against the on-disk fixture WAV without
requiring an `coh://` round-trip and keeps production happy when audio
lives in S3 / Supabase Storage.

## Test strategy

* Each backend is tested with the HTTP transport mocked. No paid
  provider call is ever made from the default test run.
* The Whisper backend ships an in-memory `_set_fake_engine` seam so the
  per-word/per-turn assembly path can be exercised without loading any
  ML wheels.
* The router covers six paths: clean primary success; transient primary
  failure → fallback wins; low-confidence primary → fallback wins; both
  low-confidence → return best with `LOW_STT_CONFIDENCE`; both fail →
  `STTUnavailable`; unknown provider in env → `STTError`.

## Wiring

`voice_intake.transcribe_session_audio` is invoked from
`finalize_browser_session` immediately after `stitch_chunks`. It is a
no-op when `STT_PROVIDER_PRIMARY` is unset, which keeps existing test
fixtures (synthetic non-audio bytes) and any deployment that has not
yet provisioned an STT provider working unchanged. When STT is
configured, the resulting `Transcript` is stored via
`transcript_quality.store_transcript` and a small metadata block
(`stt_provider`, `model`, `avg_confidence`, `quality_flags`,
`fallback_used`, `transcript_uri`) is added to the
`interview_session_completed` event payload.
