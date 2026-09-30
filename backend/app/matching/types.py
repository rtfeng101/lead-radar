"""Shapes shared by the matching stages.

Everything the LLM must *return* (ReviewBatch, Judgement) deliberately has no defaults and no numeric
bounds: OpenAI strict structured-output mode rejects both. Ranges are enforced in code instead
(see aggregate.py), which is the safer place anyway.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

EvidenceKind = Literal["review", "permit", "permit_summary", "license", "license_search", "profile"]


class EvidenceItem(BaseModel):
    """What a collector produces. Persisted as an `Evidence` row."""

    kind: EvidenceKind
    source: str
    external_id: str
    url: str | None = None
    occurred_at: datetime | None = None
    payload: dict


# ---------- stage 1: review -> facts ----------

ProjectKind = Literal["residential_replacement", "residential_repair", "commercial_or_flat", "new_construction", "other"]
Complaint = Literal[
    "leak_or_defect", "delays", "communication", "damage_or_cleanup", "pricing_or_billing",
    "warranty_not_honored", "safety_or_licensing",
]
Praise = Literal["workmanship", "communication", "timeliness", "cleanup", "fair_price", "professionalism"]


class ReviewFacts(BaseModel):
    id: str  # echoes the review id from the prompt
    sentiment: Literal["positive", "mixed", "negative"]
    project: ProjectKind
    complaints: list[Complaint]
    praises: list[Praise]
    # Roofing-product manufacturers named in the review (e.g. "GAF", "CertainTeed", "Owens Corning").
    brands: list[str]


class ReviewBatch(BaseModel):
    items: list[ReviewFacts]


# ---------- stage 2: rubric judgement ----------


class CriterionJudgement(BaseModel):
    id: str  # a rubric criterion id
    score: int | None  # 0-100; null only when the evidence says nothing relevant to this criterion
    confidence: float  # 0-1
    rationale: str
    evidence_ids: list[str]


class Judgement(BaseModel):
    criteria: list[CriterionJudgement]
    summary: str
    gaps: list[str]  # things a rep should verify before relying on the score
    next_best_action: str  # one concrete step for the rep, consistent with the score


# ---------- result ----------


class CriterionResult(BaseModel):
    id: str
    name: str
    weight: float
    score: int | None
    confidence: float
    rationale: str
    evidence_ids: list[str]
    uncited: bool = False  # scored, but cited nothing verifiable, so confidence was capped


class MatchResult(BaseModel):
    manufacturer: str
    match_pct: int | None
    confidence: float
    criteria: list[CriterionResult]
    summary: str
    gaps: list[str]
    next_best_action: str
    generated_at: datetime | None = None
