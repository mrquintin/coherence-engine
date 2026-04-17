"""Tests for the deterministic transcript quality gate."""

from __future__ import annotations

import pytest

from coherence_engine.config import TranscriptQualityThresholds
from coherence_engine.core.types import Transcript, TranscriptTurn
from coherence_engine.server.fund.services.transcript_quality import (
    TranscriptQualityReport,
    evaluate_transcript,
)


_FOUNDER_PARAGRAPH = (
    "Our problem is the pain point that small clinics face when they need "
    "to reconcile lab results across systems. Our solution is a mechanism "
    "that ingests HL7 feeds and normalizes them. We have evidence from a "
    "pilot study with three clinics. The market is large with a clear "
    "addressable segment. Our moat comes from proprietary network effects "
    "and high switching costs once a clinic is wired in. On execution we "
    "have shipped milestone one and the team has delivered the roadmap on "
    "schedule. The main risk and tradeoff is that integration timelines "
    "for new clinics can be a challenge."
)


def _founder_turn(text: str, *, confidence: float = 0.85, start: float = 0.0, end: float = 5.0) -> TranscriptTurn:
    return TranscriptTurn(speaker="founder", text=text, confidence=confidence, start_s=start, end_s=end)


def _interviewer_turn(text: str, *, confidence: float = 0.9, start: float = 0.0, end: float = 2.0) -> TranscriptTurn:
    return TranscriptTurn(speaker="interviewer", text=text, confidence=confidence, start_s=start, end_s=end)


def _build_transcript(turns: list[TranscriptTurn]) -> Transcript:
    return Transcript(
        session_id="ivw_test",
        language="en-US",
        turns=tuple(turns),
        asr_model="whisper-test",
    )


def _happy_path_transcript() -> Transcript:
    turns: list[TranscriptTurn] = []
    # 25 turns: alternating interviewer/founder; founder paragraphs supply ~480 words and full topic coverage.
    for i in range(13):
        turns.append(_interviewer_turn(f"Question {i}", start=i * 30, end=i * 30 + 2))
        turns.append(_founder_turn(_FOUNDER_PARAGRAPH, start=i * 30 + 2, end=i * 30 + 28))
    # Drop one to reach exactly 25 turns.
    turns = turns[:25]
    return _build_transcript(turns)


def test_happy_path_passes_all_thresholds():
    transcript = _happy_path_transcript()
    report = evaluate_transcript(transcript)
    assert isinstance(report, TranscriptQualityReport)
    assert report.passed, f"expected pass, got reasons={report.reason_codes} metrics={report.metrics}"
    assert report.reason_codes == ()
    assert 0.95 <= report.score <= 1.0
    assert report.metrics["founder_words"] >= 400
    assert report.metrics["total_turns"] >= 20


def test_short_founder_transcript_is_flagged():
    turns = [_founder_turn("We solve a problem.", confidence=0.9) for _ in range(25)]
    # Add interviewer turns so total turns count is fine but founder words remain low.
    transcript = _build_transcript(turns)
    report = evaluate_transcript(transcript)
    assert not report.passed
    assert "TQG_FOUNDER_WORDS_LOW" in report.reason_codes


def test_low_asr_confidence_is_flagged():
    transcript = _happy_path_transcript()
    # Replace all turns with low-confidence variants.
    degraded = tuple(
        TranscriptTurn(
            speaker=t.speaker,
            text=t.text,
            confidence=0.3,
            start_s=t.start_s,
            end_s=t.end_s,
        )
        for t in transcript.turns
    )
    bad = Transcript(
        session_id=transcript.session_id,
        language=transcript.language,
        turns=degraded,
        asr_model=transcript.asr_model,
    )
    report = evaluate_transcript(bad)
    assert not report.passed
    assert "TQG_ASR_CONFIDENCE_LOW" in report.reason_codes
    assert "TQG_LOW_CONFIDENCE_RATIO_HIGH" in report.reason_codes


def test_missing_topics_is_flagged():
    # Founder text has plenty of words but mentions zero rubric topics.
    filler = " ".join(["lorem ipsum dolor sit amet consectetur adipiscing elit"] * 100)
    turns: list[TranscriptTurn] = []
    for i in range(13):
        turns.append(_interviewer_turn(f"Question {i}"))
        turns.append(_founder_turn(filler))
    turns = turns[:25]
    transcript = _build_transcript(turns)
    report = evaluate_transcript(transcript)
    assert not report.passed
    assert "TQG_TOPIC_COVERAGE_LOW" in report.reason_codes


def test_too_few_turns_is_flagged():
    transcript = _build_transcript([_founder_turn(_FOUNDER_PARAGRAPH * 5)])
    report = evaluate_transcript(transcript)
    assert not report.passed
    assert "TQG_TURNS_LOW" in report.reason_codes


def test_threshold_override_via_env(monkeypatch):
    transcript = _build_transcript([_founder_turn("problem solution evidence market moat execution risk", confidence=0.95)])
    # Default config rejects this (way too few words/turns).
    default_report = evaluate_transcript(transcript)
    assert not default_report.passed

    # Loosen every threshold via env so the same transcript now passes.
    monkeypatch.setenv("COHERENCE_TQG_MIN_FOUNDER_WORDS", "1")
    monkeypatch.setenv("COHERENCE_TQG_MIN_TURNS", "1")
    monkeypatch.setenv("COHERENCE_TQG_MIN_AVG_CONFIDENCE", "0.5")
    monkeypatch.setenv("COHERENCE_TQG_MAX_LOW_CONF_RATIO", "0.9")
    monkeypatch.setenv("COHERENCE_TQG_MIN_TOPIC_COVERAGE", "0.5")

    cfg = TranscriptQualityThresholds.from_env()
    assert cfg.min_founder_words == 1
    assert cfg.min_avg_confidence == pytest.approx(0.5)

    relaxed_report = evaluate_transcript(transcript, config=cfg)
    assert relaxed_report.passed, f"reasons={relaxed_report.reason_codes}"


def test_score_is_deterministic():
    transcript = _happy_path_transcript()
    a = evaluate_transcript(transcript)
    b = evaluate_transcript(transcript)
    assert a == b


def test_report_metrics_shape():
    transcript = _happy_path_transcript()
    report = evaluate_transcript(transcript)
    expected_keys = {"founder_words", "total_turns", "avg_confidence", "low_confidence_ratio", "topic_coverage"}
    assert expected_keys == set(report.metrics.keys())
    assert 0.0 <= report.score <= 1.0
