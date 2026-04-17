"""Argument parser: decompose raw text into claims, premises, and relations."""

import re
from coherence_engine.core.types import Proposition, Relation, ArgumentStructure, Transcript


def parse_transcript(transcript: Transcript):
    """Public wrapper: compile a diarized Transcript into a CompiledArgument.

    Delegates to core.transcript_compiler.compile_transcript so existing
    parser behavior is untouched.
    """
    from coherence_engine.core.transcript_compiler import compile_transcript
    return compile_transcript(transcript)

# ── Discourse markers with semantic roles ────────────────────────────

CLAIM_INDICATORS = [
    "therefore", "thus", "hence", "consequently", "so ",
    "i argue", "we argue", "i believe", "we believe",
    "we conclude", "i conclude", "this shows", "this proves",
    "this means", "it follows", "in conclusion", "the point is",
    "my thesis", "our thesis", "the conclusion",
    "must be", "should be", "clearly",
    # Extended set
    "as a result", "for this reason", "it is clear that",
    "this demonstrates", "this implies", "accordingly",
    "we can see that", "this establishes", "the upshot is",
    "the implication is", "this entails", "this suggests that",
    "in sum", "in summary", "to summarize", "to sum up",
    "all things considered", "on balance",
    "the evidence shows", "the data shows",
]

EVIDENCE_INDICATORS = [
    "studies show", "research shows", "according to", "data indicates",
    "evidence suggests", "statistics show", "surveys find",
    "experiments demonstrate", "results show", "findings suggest",
    "for example", "for instance", "such as",
    "percent", "%", "in 20", "million", "billion",
    # Extended set
    "a study by", "research by", "analysis of", "a survey of",
    "the report found", "data from", "evidence from",
    "as demonstrated by", "as shown by", "as illustrated by",
    "the numbers show", "empirical data", "case study",
    "meta-analysis", "peer-reviewed", "published in",
    "measured at", "observed that", "documented",
]

QUALIFIER_INDICATORS = [
    "however", "although", "though", "nevertheless", "nonetheless",
    "on the other hand", "except", "unless", "but ", "yet ",
    "admittedly", "granted", "while it is true",
    "some might argue", "critics say", "one objection",
    # Extended set
    "to be fair", "it must be noted", "it should be noted",
    "with the caveat", "notwithstanding", "that said",
    "even so", "all the same", "be that as it may",
    "having said that", "at the same time", "while this is true",
    "opponents argue", "skeptics point out", "detractors claim",
    "a counterargument", "one limitation", "a caveat",
    "it is worth noting", "we must acknowledge",
]

ATTACK_INDICATORS = [
    "but ", "however", "yet ", "although", "despite",
    "on the contrary", "in contrast", "conversely",
    "this contradicts", "this undermines", "this challenges",
    "this is false", "this is wrong", "this fails",
    "not true", "incorrect", "mistaken",
    # Extended set
    "this refutes", "this disproves", "this negates",
    "this invalidates", "this weakens", "this rebuts",
    "contrary to", "in opposition to", "against this",
    "this overlooks", "this ignores", "this neglects",
    "this is flawed", "this is misleading",
]

SUPPORT_INDICATORS = [
    "because", "since ", "given that", "due to",
    "as a consequence", "in support of", "this supports",
    "this confirms", "this reinforces", "furthermore",
    "moreover", "in addition", "additionally",
    "what is more", "also ", "equally important",
    "this is consistent with", "this aligns with",
    "building on this", "to elaborate",
]

COREFERENCE_PRONOUNS = {
    "it", "this", "that", "they", "these", "those",
    "its", "their", "them", "itself", "themselves",
    "the former", "the latter", "such",
}


class ArgumentParser:
    """Parse raw text into argumentative structure."""

    def __init__(self, max_propositions: int = 200):
        self.max_propositions = max_propositions

    def parse(self, text: str) -> ArgumentStructure:
        """Decompose text into propositions and relations."""
        if not text or not text.strip():
            return ArgumentStructure(original_text=text)

        paragraphs = self._detect_paragraphs(text)
        sentences = self._split_sentences(text)
        sentences = self._deduplicate(sentences)

        if len(sentences) > self.max_propositions:
            sentences = sentences[:self.max_propositions]

        propositions = []
        for i, (sent, span) in enumerate(sentences):
            prop_type = self._classify(sent)
            prop = Proposition(
                id=f"P{i+1}",
                text=sent,
                prop_type=prop_type,
                importance=self._base_importance(prop_type),
                source_span=span,
            )
            propositions.append(prop)

        self._detect_multi_sentence_claims(propositions)

        relations = self._infer_relations(propositions, paragraphs)

        self._add_coreference_links(propositions, relations)

        self._adjust_importance(propositions, relations)

        return ArgumentStructure(
            propositions=propositions,
            relations=relations,
            original_text=text,
        )

    def _detect_paragraphs(self, text: str) -> list:
        """Detect paragraph boundary offsets (positions of double-newlines)."""
        boundaries = []
        for m in re.finditer(r'\n\s*\n', text):
            boundaries.append(m.start())
        return boundaries

    def _spans_paragraph_boundary(self, span_a, span_b, paragraph_boundaries):
        """Check whether two spans are separated by a paragraph break."""
        if not paragraph_boundaries:
            return False
        end_a = span_a[1]
        start_b = span_b[0]
        return any(end_a <= b <= start_b for b in paragraph_boundaries)

    def _split_sentences(self, text: str):
        """Split text into sentences, preserving source spans."""
        text_clean = text.replace("Dr.", "Dr").replace("Mr.", "Mr")
        text_clean = text_clean.replace("Mrs.", "Mrs").replace("Ms.", "Ms")
        text_clean = text_clean.replace("vs.", "vs").replace("etc.", "etc")
        text_clean = text_clean.replace("e.g.", "eg").replace("i.e.", "ie")

        pattern = r'(?<=[.!?])\s+'
        raw_sentences = re.split(pattern, text_clean)

        results = []
        offset = 0
        for sent in raw_sentences:
            sent = sent.strip()
            if len(sent) < 10:
                offset += len(sent) + 1
                continue
            start = text.find(sent[:20], max(0, offset - 5))
            if start == -1:
                start = offset
            results.append((sent, (start, start + len(sent))))
            offset = start + len(sent)

        return results

    def _deduplicate(self, sentences):
        """Remove near-duplicate sentences."""
        seen = set()
        unique = []
        for sent, span in sentences:
            key = re.sub(r'\s+', ' ', sent.lower().strip())
            if key not in seen:
                seen.add(key)
                unique.append((sent, span))
        return unique

    def _classify(self, text: str) -> str:
        """Classify proposition type by keyword indicators."""
        lower = text.lower()

        for indicator in CLAIM_INDICATORS:
            if indicator in lower:
                return "claim"

        for indicator in EVIDENCE_INDICATORS:
            if indicator in lower:
                return "evidence"

        for indicator in QUALIFIER_INDICATORS:
            if indicator in lower:
                return "qualifier"

        return "premise"

    def _base_importance(self, prop_type: str) -> float:
        return {"claim": 1.0, "premise": 0.7, "evidence": 0.5, "qualifier": 0.3}.get(prop_type, 0.5)

    def _detect_multi_sentence_claims(self, propositions):
        """Upgrade premises that immediately follow a claim into claims
        when they continue the conclusion (no discourse marker of their own)."""
        for i in range(1, len(propositions)):
            prev = propositions[i - 1]
            curr = propositions[i]
            if prev.prop_type == "claim" and curr.prop_type == "premise":
                lower = curr.text.lower()
                has_own_marker = any(
                    ind in lower
                    for ind in EVIDENCE_INDICATORS + QUALIFIER_INDICATORS + ATTACK_INDICATORS
                )
                continues_claim = any(
                    lower.startswith(w) for w in (
                        "this ", "it ", "such ", "the result", "we must",
                        "we should", "we need", "that is",
                    )
                )
                if not has_own_marker and continues_claim:
                    curr.prop_type = "claim"
                    curr.importance = max(curr.importance, 0.9)

    def _infer_relations(self, propositions, paragraph_boundaries=None):
        """Infer support/attack relations from adjacency, content, and paragraphs."""
        if paragraph_boundaries is None:
            paragraph_boundaries = []

        relations = []
        for i in range(len(propositions) - 1):
            curr = propositions[i]
            next_prop = propositions[i + 1]

            is_attack = any(ind in next_prop.text.lower() for ind in ATTACK_INDICATORS[:6])
            has_support_marker = any(
                ind in next_prop.text.lower() for ind in SUPPORT_INDICATORS
            )

            if is_attack:
                rel_type = "attacks"
            elif next_prop.prop_type == "qualifier":
                rel_type = "qualifies"
            else:
                rel_type = "supports"

            cross_paragraph = self._spans_paragraph_boundary(
                curr.source_span, next_prop.source_span, paragraph_boundaries
            )
            strength = 0.3 if cross_paragraph else 0.5
            if has_support_marker and rel_type == "supports":
                strength = min(1.0, strength + 0.2)

            relations.append(Relation(
                source_id=curr.id,
                target_id=next_prop.id,
                relation_type=rel_type,
                strength=strength,
            ))

            if curr.prop_type == "evidence":
                for j in range(i - 1, -1, -1):
                    if propositions[j].prop_type == "claim":
                        relations.append(Relation(
                            source_id=curr.id,
                            target_id=propositions[j].id,
                            relation_type="supports",
                            strength=0.7,
                        ))
                        break

        return relations

    def _add_coreference_links(self, propositions, relations):
        """Add relations when a proposition starts with a pronoun
        likely referring to the prior sentence's topic."""
        existing_pairs = {(r.source_id, r.target_id) for r in relations}

        for i in range(1, len(propositions)):
            curr = propositions[i]
            first_word = curr.text.split()[0].lower().rstrip(".,;:") if curr.text.split() else ""

            if first_word in COREFERENCE_PRONOUNS:
                prev = propositions[i - 1]
                pair = (curr.id, prev.id)
                if pair not in existing_pairs:
                    relations.append(Relation(
                        source_id=curr.id,
                        target_id=prev.id,
                        relation_type="references",
                        strength=0.4,
                    ))
                    existing_pairs.add(pair)

    def _adjust_importance(self, propositions, relations):
        """Adjust importance by connectivity — more connected = more important."""
        connection_count = {}
        for r in relations:
            connection_count[r.source_id] = connection_count.get(r.source_id, 0) + 1
            connection_count[r.target_id] = connection_count.get(r.target_id, 0) + 1

        if not connection_count:
            return

        max_conn = max(connection_count.values())
        for p in propositions:
            conn = connection_count.get(p.id, 0)
            connectivity_bonus = 0.2 * (conn / max(max_conn, 1))
            p.importance = min(1.0, p.importance + connectivity_bonus)
