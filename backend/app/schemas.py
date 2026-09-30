from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.matching.types import CriterionResult

LeadStatus = Literal["new", "reviewing", "contacted", "qualified", "won", "dismissed"]
Priority = Literal["hot", "warm", "cold"]


class ContractorRecord(BaseModel):
    """Source-agnostic shape every LeadSource must produce, and what scoring / insights consume."""

    model_config = ConfigDict(from_attributes=True)

    source: str
    source_id: str
    name: str
    legal_name: str | None = None
    employees: str | None = None
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    distance_mi: float | None = None
    rating: float | None = None
    review_count: int | None = None
    cert_tier: str | None = None
    years_in_business: int | None = None
    services: list[str] = Field(default_factory=list)
    profile_url: str | None = None
    google_place_id: str | None = None
    raw: dict = Field(default_factory=dict)


class ScoreFactor(BaseModel):
    factor: str
    points: float
    max_points: float
    note: str


class Insights(BaseModel):
    summary: str
    talking_points: list[str]
    risks: list[str]
    next_best_action: str


# ---------- API ----------


class ContractorOut(ContractorRecord):
    id: int
    raw: dict = Field(default_factory=dict, exclude=True)


class MatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    manufacturer: str
    match_pct: int | None
    confidence: float
    criteria: list[CriterionResult]
    summary: str
    gaps: list[str]
    next_action: str | None
    model: str
    generated_at: datetime


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ref: str  # the id the judge cites, e.g. "E12"
    kind: str
    source: str
    url: str | None
    occurred_at: datetime | None
    payload: dict
    facts: dict | None


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    score: int
    priority: Priority
    score_breakdown: list[ScoreFactor]
    insights: Insights | None
    insights_model: str | None
    status: LeadStatus
    assigned_to: str | None
    notes: str | None
    generated_at: datetime
    updated_at: datetime
    contractor: ContractorOut
    match: MatchOut | None = None  # for the requested manufacturer; None until the match job has run


class LeadList(BaseModel):
    items: list[LeadOut]
    total: int


class LeadPatch(BaseModel):
    status: LeadStatus | None = None
    assigned_to: str | None = None
    notes: str | None = None


class RunRequest(BaseModel):
    zip_code: str = Field(pattern=r"^\d{5}$")
    radius_mi: int = Field(default=25, ge=1, le=100)
    refresh: bool = Field(default=False, description="ignore freshness windows and re-fetch every source")


class RunResponse(BaseModel):
    job_id: int


class PipelineStatus(BaseModel):
    queued: int
    running: int
    done: int
    failed: int


class RefreshResponse(BaseModel):
    job_id: int


class LeadActivity(BaseModel):
    """What the pipeline is doing for one contractor right now."""

    queued: int
    running: int
    last_error: str | None  # most recent failure that has not since been superseded by a success
