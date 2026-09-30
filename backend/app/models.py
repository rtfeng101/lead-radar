"""Relational model.

Three concerns, deliberately separated:
  * Contractor - what the outside world says (owned by the pipeline, overwritten on every re-scrape)
  * Lead       - what *we* derived + what reps did about it (regenerating a score must never wipe
                 a rep's status / notes, so those live on a different row than the raw source data)
  * Job        - the work queue that drives the pipeline
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Contractor(Base):
    __tablename__ = "contractors"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_contractor_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # "gaf", "fixture", later "google", "linkedin"...
    source_id: Mapped[str] = mapped_column(String(128))  # stable id within the source -> idempotent upserts

    name: Mapped[str] = mapped_column(String(256))
    legal_name: Mapped[Optional[str]] = mapped_column(String(256))  # registered business name; what permits/licenses use
    employees: Mapped[Optional[str]] = mapped_column(String(32))  # size band as the source states it, e.g. "1-5"
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    website: Mapped[Optional[str]] = mapped_column(String(512))
    address: Mapped[Optional[str]] = mapped_column(String(256))
    city: Mapped[Optional[str]] = mapped_column(String(128))
    state: Mapped[Optional[str]] = mapped_column(String(2))
    zip_code: Mapped[Optional[str]] = mapped_column(String(10))
    search_zip: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    distance_mi: Mapped[Optional[float]]

    rating: Mapped[Optional[float]]
    review_count: Mapped[Optional[int]]
    cert_tier: Mapped[Optional[str]] = mapped_column(String(64))
    years_in_business: Mapped[Optional[int]]
    services: Mapped[list] = mapped_column(JSON, default=list)
    profile_url: Mapped[Optional[str]] = mapped_column(String(512))
    google_place_id: Mapped[Optional[str]] = mapped_column(String(128))  # from the profile page's "See All Reviews" link

    raw: Mapped[dict] = mapped_column(JSON, default=dict)  # untouched source payload, for reprocessing
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Optional["Lead"]] = relationship(back_populates="contractor", uselist=False)


class Evidence(Base):
    """One citable fact about a contractor from an outside source: a review, a permit, a license...

    `payload` is what the source said; `facts` is what the LLM extracted from it (reviews only) and is
    reset whenever the payload changes, so extraction is paid for once per distinct piece of text."""

    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("contractor_id", "source", "external_id", name="uq_evidence_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contractor_id: Mapped[int] = mapped_column(ForeignKey("contractors.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # review | permit | permit_summary | license | license_search | profile
    source: Mapped[str] = mapped_column(String(48))  # e.g. nyc_dob_now, nyc_dca, google_reviews, fixture
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[Optional[str]] = mapped_column(String(512))
    occurred_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    facts: Mapped[Optional[dict]] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Match(Base):
    """How well a contractor fits one manufacturer's program. One row per (contractor, manufacturer)."""

    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("contractor_id", "manufacturer", name="uq_match_contractor_mfr"),
        Index("ix_matches_mfr_pct", "manufacturer", "match_pct"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contractor_id: Mapped[int] = mapped_column(ForeignKey("contractors.id"), index=True)
    manufacturer: Mapped[str] = mapped_column(String(32))

    match_pct: Mapped[Optional[int]]  # None when no criterion could be scored
    confidence: Mapped[float]  # 0-1: how much of the rubric is backed by cited evidence
    criteria: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    gaps: Mapped[list] = mapped_column(JSON, default=list)
    next_action: Mapped[Optional[str]] = mapped_column(Text)

    input_hash: Mapped[str] = mapped_column(String(64))  # rubric + model + evidence; unchanged => skip the LLM
    model: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    contractor: Mapped[Contractor] = relationship()


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_priority_score", "priority", "score"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contractor_id: Mapped[int] = mapped_column(ForeignKey("contractors.id"), unique=True)

    # --- system-generated (overwritten each time the pipeline re-runs) ---
    score: Mapped[int] = mapped_column(index=True)
    priority: Mapped[str] = mapped_column(String(8))  # hot | warm | cold
    score_breakdown: Mapped[list] = mapped_column(JSON, default=list)
    insights: Mapped[dict] = mapped_column(JSON, default=dict)
    insights_model: Mapped[Optional[str]] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # --- rep-owned (never touched by the pipeline) ---
    # NOTE: single-owner for now. With many reps, move to a per-(rep, lead) table + territories.
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(128))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    contractor: Mapped[Contractor] = relationship(back_populates="lead")


class Job(Base):
    """Postgres-backed queue. Workers claim rows with FOR UPDATE SKIP LOCKED, so N workers can
    poll the same table without blocking each other. See app/pipeline/queue.py."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(256), unique=True)

    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | done | failed
    attempts: Mapped[int] = mapped_column(default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
