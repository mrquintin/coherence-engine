"""Deterministic compiler from a diarized Transcript to proposition/relation structures."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from coherence_engine.core.types import (
    Proposition,
    ProvenanceSpan,
    Relation,
    Transcript,
    TranscriptTurn,
)

_FOUNDER_SPEAKER = "founder"
_MIN_CONFIDENCE = 0.5
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z]|$)")


@dataclass
class CompiledArgument:
    """Output of compile_transcript: propositions + relations with diagnostics."""
    propositions: tuple = ()
    relations: tuple = ()
    dropped_turn_count: int = 0
    notes: tuple = ()


def _split_sentences(text: str) -> list:
    text = (text or "").strip()
    if not text:
        return []
    pieces = _SENTENCE_SPLIT.split(text)
    return [p.strip() for p in pieces if p and p.strip()]


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def compile_transcript(transcript: Transcript) -> CompiledArgument:
    """Compile a diarized Transcript into propositions + relations.

    Deterministic rules:
      - Keep only founder turns.
      - Drop turns with confidence < 0.5 (counted in dropped_turn_count).
      - Split each kept turn into sentences via regex.
      - Dedup exact sentences (case-insensitive, whitespace-normalized).
      - One Proposition per surviving sentence with ProvenanceSpan provenance.
      - Relations: reuse ArgumentParser's relation inference when available,
        otherwise emit an empty relations tuple and record a note.
    """
    if transcript is None or not transcript.turns:
        return CompiledArgument(
            propositions=(),
            relations=(),
            dropped_turn_count=0,
            notes=("empty_transcript",),
        )

    dropped = 0
    propositions: list = []
    seen_keys: set = set()
    notes: list = []

    for turn_index, turn in enumerate(transcript.turns):
        if not isinstance(turn, TranscriptTurn):
            continue
        if turn.speaker != _FOUNDER_SPEAKER:
            continue
        if turn.confidence < _MIN_CONFIDENCE:
            dropped += 1
            continue

        sentences = _split_sentences(turn.text)
        if not sentences:
            continue

        span = ProvenanceSpan(
            session_id=transcript.session_id,
            turn_index=turn_index,
            start_s=turn.start_s,
            end_s=turn.end_s,
            speaker=turn.speaker,
        )

        for sentence in sentences:
            key = _fold(sentence)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            prop_id = f"P{len(propositions) + 1}"
            propositions.append(
                Proposition(
                    id=prop_id,
                    text=sentence,
                    prop_type="premise",
                    importance=0.5,
                    source_span=(0, len(sentence)),
                    provenance=(span,),
                )
            )

    relations = _infer_relations(propositions, notes)

    return CompiledArgument(
        propositions=tuple(propositions),
        relations=tuple(relations),
        dropped_turn_count=dropped,
        notes=tuple(notes),
    )


def _infer_relations(propositions, notes: list):
    """Reuse ArgumentParser's relation inference when accessible.

    Falls back to an empty tuple + note if parser internals are not present.
    """
    if len(propositions) < 2:
        return []
    try:
        from coherence_engine.core.parser import ArgumentParser
        parser = ArgumentParser()
        relations = parser._infer_relations(propositions, [])
        return list(relations) if relations else []
    except Exception:
        notes.append("no_relations_extracted")
        return []
