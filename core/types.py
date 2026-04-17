"""Shared data types used across the Coherence Engine."""

from dataclasses import dataclass, field
from typing import Optional
import json


@dataclass(frozen=True)
class TranscriptTurn:
    """A single ASR-decoded turn from an interview transcript."""
    speaker: str
    text: str
    confidence: float
    start_s: float
    end_s: float


@dataclass(frozen=True)
class Transcript:
    """Canonical interview transcript: ordered turns plus session metadata."""
    session_id: str
    language: str
    turns: tuple
    asr_model: Optional[str] = None


@dataclass(frozen=True)
class ProvenanceSpan:
    """Pointer from a compiled proposition back to its transcript origin."""
    session_id: str
    turn_index: int
    start_s: float
    end_s: float
    speaker: str


@dataclass
class Proposition:
    """A single unit of argumentative content."""
    id: str
    text: str
    prop_type: str = "premise"
    importance: float = 0.5
    source_span: tuple = (0, 0)
    provenance: Optional[tuple] = None


@dataclass
class Relation:
    """A directed relationship between two propositions."""
    source_id: str
    target_id: str
    relation_type: str = "supports"
    strength: float = 0.5


@dataclass(frozen=True)
class Entity:
    """A named node in the ontology graph."""
    id: str
    type: str
    surface_forms: tuple
    mentions: int


@dataclass(frozen=True)
class OntologyEdge:
    """A directed relation between two ontology entities."""
    src: str
    dst: str
    relation: str
    evidence_count: int


@dataclass(frozen=True)
class OntologyGraph:
    """Deterministic ontology extracted from a set of propositions."""
    entities: tuple
    edges: tuple
    schema_version: str = "ontology-v1"


@dataclass(frozen=True)
class NormativeProfile:
    """Weighted normative axes extracted from argument content."""
    rights: float
    utilitarian: float
    deontic: float


@dataclass(frozen=True)
class DomainMix:
    """Weighted fusion of signals assigning an argument to multiple domains."""
    weights: tuple
    normative: NormativeProfile
    ontology_graph_id: Optional[str] = None
    notes: Optional[tuple] = None
    schema_version: str = "domain-mix-v1"

    def top(self):
        """Return (domain_id, weight) for the highest-weight domain, or None."""
        if not self.weights:
            return None
        return self.weights[0]


@dataclass
class ArgumentStructure:
    """Complete parsed argument: propositions + relations."""
    propositions: list = field(default_factory=list)
    relations: list = field(default_factory=list)
    original_text: str = ""

    @property
    def claims(self):
        return [p for p in self.propositions if p.prop_type == "claim"]

    @property
    def premises(self):
        return [p for p in self.propositions if p.prop_type == "premise"]

    @property
    def n_propositions(self):
        return len(self.propositions)

    @property
    def all_pairs(self):
        pairs = []
        for i in range(len(self.propositions)):
            for j in range(i + 1, len(self.propositions)):
                pairs.append((self.propositions[i], self.propositions[j]))
        return pairs

    def get_proposition(self, prop_id: str) -> Optional[Proposition]:
        for p in self.propositions:
            if p.id == prop_id:
                return p
        return None


@dataclass
class ContradictionPair:
    """A detected contradiction between two propositions."""
    prop_a_id: str = ""
    prop_b_id: str = ""
    prop_a_text: str = ""
    prop_b_text: str = ""
    confidence: float = 0.0
    explanation: str = ""
    prop1_id: str = ""
    prop2_id: str = ""

    def __post_init__(self):
        if self.prop1_id and not self.prop_a_id:
            self.prop_a_id = self.prop1_id
        if self.prop2_id and not self.prop_b_id:
            self.prop_b_id = self.prop2_id
        if self.prop_a_id and not self.prop1_id:
            self.prop1_id = self.prop_a_id
        if self.prop_b_id and not self.prop2_id:
            self.prop2_id = self.prop_b_id

    def to_dict(self) -> dict:
        return {
            "prop_a_id": self.prop_a_id,
            "prop_b_id": self.prop_b_id,
            "prop_a_text": self.prop_a_text,
            "prop_b_text": self.prop_b_text,
            "confidence": round(self.confidence, 3),
            "explanation": self.explanation,
        }


@dataclass
class LayerResult:
    """Result from a single analysis layer."""
    name: str
    score: float
    weight: float
    details: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    @property
    def weighted_score(self):
        return self.score * self.weight


@dataclass
class CoherenceResult:
    """Complete result from the Coherence Engine."""
    composite_score: float
    layer_results: list = field(default_factory=list)
    argument_structure: Optional[ArgumentStructure] = None
    contradictions: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def report(self, fmt: str = "text") -> str:
        from coherence_engine.core.report import ReportGenerator
        gen = ReportGenerator()
        if fmt == "json":
            return gen.to_json(self)
        elif fmt == "markdown":
            return gen.to_markdown(self)
        return gen.to_text(self)

    def to_dict(self) -> dict:
        return {
            "composite_score": round(self.composite_score, 4),
            "layers": {
                r.name: {
                    "score": round(r.score, 4),
                    "weight": r.weight,
                    "weighted": round(r.weighted_score, 4),
                    "warnings": r.warnings,
                }
                for r in self.layer_results
            },
            "n_propositions": self.argument_structure.n_propositions if self.argument_structure else 0,
            "n_contradictions": len(self.contradictions),
            "contradictions": [c.to_dict() for c in self.contradictions],
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)
