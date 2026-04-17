"""Tests for the deterministic transcript -> proposition compiler."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coherence_engine.core.parser import parse_transcript
from coherence_engine.core.scorer import CoherenceScorer
from coherence_engine.core.transcript_compiler import (
    CompiledArgument,
    compile_transcript,
)
from coherence_engine.core.types import (
    Proposition,
    ProvenanceSpan,
    Transcript,
    TranscriptTurn,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"


def _load_transcript(name: str) -> Transcript:
    payload = json.loads((_FIXTURES / name).read_text())
    turns = tuple(TranscriptTurn(**t) for t in payload["turns"])
    return Transcript(
        session_id=payload["session_id"],
        language=payload["language"],
        turns=turns,
        asr_model=payload.get("asr_model"),
    )


def test_happy_path_yields_propositions_with_provenance():
    transcript = _load_transcript("happy_path.json")
    compiled = compile_transcript(transcript)

    assert isinstance(compiled, CompiledArgument)
    assert len(compiled.propositions) >= 5
    for prop in compiled.propositions:
        assert isinstance(prop, Proposition)
        assert prop.provenance is not None
        assert len(prop.provenance) >= 1
        span = prop.provenance[0]
        assert isinstance(span, ProvenanceSpan)
        assert span.session_id == "ivw_happy_01"
        assert span.speaker == "founder"
        assert span.end_s >= span.start_s


def test_dedup_removes_repeated_sentence_exactly_once():
    transcript = _load_transcript("happy_path.json")
    compiled = compile_transcript(transcript)

    repeated = "our product normalizes lab results across clinics."
    matches = [
        p for p in compiled.propositions
        if p.text.strip().lower() == repeated
    ]
    assert len(matches) == 1


def test_low_quality_drops_turns_but_returns_valid_argument():
    transcript = _load_transcript("low_quality.json")
    compiled = compile_transcript(transcript)

    assert isinstance(compiled, CompiledArgument)
    assert compiled.dropped_turn_count > 0
    # The one high-confidence founder turn contributes propositions.
    assert len(compiled.propositions) >= 1
    for prop in compiled.propositions:
        assert prop.provenance is not None


def test_non_founder_turns_are_excluded():
    transcript = _load_transcript("happy_path.json")
    compiled = compile_transcript(transcript)
    for prop in compiled.propositions:
        assert prop.provenance[0].speaker == "founder"


def test_parse_transcript_wrapper_delegates():
    transcript = _load_transcript("happy_path.json")
    compiled_direct = compile_transcript(transcript)
    compiled_wrapped = parse_transcript(transcript)
    assert len(compiled_wrapped.propositions) == len(compiled_direct.propositions)


def test_empty_transcript_returns_empty_argument():
    empty = Transcript(session_id="empty", language="en-US", turns=())
    compiled = compile_transcript(empty)
    assert compiled.propositions == ()
    assert compiled.relations == ()
    assert compiled.dropped_turn_count == 0


def test_roundtrip_into_scorer_runs_without_exception():
    transcript = _load_transcript("happy_path.json")
    compiled = compile_transcript(transcript)
    text = " ".join(p.text for p in compiled.propositions)
    scorer = CoherenceScorer()
    result = scorer.score(text)
    assert result is not None
