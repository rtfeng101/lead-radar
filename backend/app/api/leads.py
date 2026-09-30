from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.db import get_session
from app.matching.bands import HOT_MIN, WARM_MIN, band
from app.models import Contractor, Evidence, Lead, Match, utcnow
from app.pipeline import queue
from app.schemas import (
    EvidenceOut, LeadActivity, LeadList, LeadOut, LeadPatch, LeadStatus, MatchOut, Priority, RefreshResponse,
)

router = APIRouter(prefix="/api/leads", tags=["leads"])

SORTS = {
    "match": Match.match_pct.desc().nulls_last(),
    "score": Lead.score.desc(),
    "distance": Contractor.distance_mi.asc(),
    "reviews": Contractor.review_count.desc().nulls_last(),
    "recent": Lead.generated_at.desc(),
}


# The LLM match, when there is one, decides priority; the rule-based fit score is only the fallback.
EFFECTIVE_PRIORITY = case(
    (Match.match_pct >= HOT_MIN, "hot"),
    (Match.match_pct >= WARM_MIN, "warm"),
    (Match.match_pct.is_not(None), "cold"),
    else_=Lead.priority,
)


def _out(lead: Lead, match: Match | None) -> LeadOut:
    out = LeadOut.model_validate(lead)
    out.match = MatchOut.model_validate(match) if match else None
    if match is not None and match.match_pct is not None:
        out.priority = band(match.match_pct)
    return out


def _match_for(session: Session, lead: Lead, manufacturer: str) -> Match | None:
    return session.scalar(
        select(Match).where(Match.contractor_id == lead.contractor_id, Match.manufacturer == manufacturer)
    )


@router.get("", response_model=LeadList)
def list_leads(
    priority: Priority | None = None,
    status: LeadStatus | None = None,
    q: str | None = Query(None, description="substring match on name / city"),
    min_score: int = Query(0, ge=0, le=100),
    min_match: int = Query(0, ge=0, le=100, description="hide leads below this match % (unmatched leads are hidden when > 0)"),
    manufacturer: str = settings.default_manufacturer,
    sort: Literal["match", "score", "distance", "reviews", "recent"] = "match",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> LeadList:
    # Only the configured source: switching LEAD_SOURCE must not mix demo data into real leads.
    where = [Lead.score >= min_score, Contractor.source == settings.lead_source]
    if priority:
        where.append(EFFECTIVE_PRIORITY == priority)
    if status:
        where.append(Lead.status == status)
    if q:
        like = f"%{q}%"
        where.append(or_(Contractor.name.ilike(like), Contractor.city.ilike(like)))
    if min_match:
        where.append(Match.match_pct >= min_match)

    base = (
        select(Lead, Match)
        .join(Lead.contractor)
        .outerjoin(Match, and_(Match.contractor_id == Lead.contractor_id, Match.manufacturer == manufacturer))
        .where(*where)
    )
    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    rows = session.execute(
        base.options(joinedload(Lead.contractor))
        .order_by(SORTS[sort], Lead.score.desc(), Lead.id)  # Lead.id tiebreaker keeps pagination stable
        .limit(limit)
        .offset(offset)  # fine for thousands of rows; move to keyset pagination beyond that
    ).all()
    return LeadList(items=[_out(lead, match) for lead, match in rows], total=total)


def _get(session: Session, lead_id: int) -> Lead:
    lead = session.get(Lead, lead_id, options=[joinedload(Lead.contractor)])
    if lead is None:
        raise HTTPException(404, "lead not found")
    return lead


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(
    lead_id: int, manufacturer: str = settings.default_manufacturer, session: Session = Depends(get_session)
) -> LeadOut:
    lead = _get(session, lead_id)
    return _out(lead, _match_for(session, lead, manufacturer))


@router.get("/{lead_id}/evidence", response_model=list[EvidenceOut])
def lead_evidence(lead_id: int, session: Session = Depends(get_session)) -> list[EvidenceOut]:
    """Everything a match can cite. `ref` is the id that appears in a criterion's evidence_ids."""
    lead = _get(session, lead_id)
    rows = session.scalars(
        select(Evidence).where(Evidence.contractor_id == lead.contractor_id).order_by(Evidence.kind, Evidence.id)
    )
    return [
        EvidenceOut(ref=f"E{e.id}", kind=e.kind, source=e.source, url=e.url, occurred_at=e.occurred_at,
                    payload=e.payload, facts=e.facts)
        for e in rows
    ]


@router.get("/{lead_id}/activity", response_model=LeadActivity)
def lead_activity(lead_id: int, session: Session = Depends(get_session)) -> LeadActivity:
    lead = _get(session, lead_id)
    active = queue.contractor_jobs(session, lead.contractor_id, ("queued", "running"))
    failed = sorted(queue.contractor_jobs(session, lead.contractor_id, ("failed",)), key=lambda j: j.id)
    return LeadActivity(
        queued=sum(j.status == "queued" for j in active),
        running=sum(j.status == "running" for j in active),
        last_error=f"{failed[-1].kind}: {failed[-1].last_error}"[:300] if failed else None,
    )


@router.post("/{lead_id}/refresh", response_model=RefreshResponse, status_code=202)
def refresh_lead(lead_id: int, session: Session = Depends(get_session)) -> RefreshResponse:
    """Re-fetch one contractor's data, ignoring freshness windows, and re-grade if anything changed. The grade is
    only re-run when the evidence differs (same rule as every other run), so this never wastes LLM calls."""
    lead = _get(session, lead_id)
    c = lead.contractor
    if queue.contractor_jobs(session, c.id, ("queued", "running")):
        raise HTTPException(409, "this contractor is already being refreshed")
    if c.source == "gaf" and c.profile_url:
        kind = "profile"  # re-reads the GAF page, then re-scores and collects permits/licenses, then matches
    elif settings.matching_enabled:
        kind = "collect"
    else:
        raise HTTPException(422, "nothing to refresh for this contractor (no live source and matching is off)")
    job = queue.enqueue(
        session, kind, {"contractor_id": c.id, "refresh": True},
        dedupe_key=f"refresh:{c.id}:{utcnow().timestamp()}",
    )
    session.commit()
    return RefreshResponse(job_id=job.id)


@router.patch("/{lead_id}", response_model=LeadOut)
def patch_lead(
    lead_id: int,
    patch: LeadPatch,
    manufacturer: str = settings.default_manufacturer,
    session: Session = Depends(get_session),
) -> LeadOut:
    lead = _get(session, lead_id)
    for field, value in patch.model_dump(exclude_unset=True).items():
        setattr(lead, field, value)
    session.commit()
    return _out(lead, _match_for(session, lead, manufacturer))
