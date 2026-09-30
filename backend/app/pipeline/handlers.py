"""Pipeline stages. Every handler is idempotent: running it twice yields the same rows, which is what
makes at-least-once queue delivery safe.

    discover(zip) --fan-out--> enrich(contractor_id)    x N   fit score + insight brief
                          \\--> profile(contractor_id)   x N   GAF profile page (browser): address, legal name,
                                                              years in business, reviews  (GAF source only)
                                    \\--> collect(contractor_id)   permits / licenses (+ reviews for other sources)
                                              \\--> match(contractor_id, manufacturer)   LLM rubric score
"""

from datetime import timedelta
from itertools import islice

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import db
from app.config import settings
from app.evidence import get_collectors
from app.matching.engine import evidence_fingerprint, run_match, store_evidence
from app.models import Contractor, Evidence, Lead, utcnow
from app.pipeline import queue
from app.pipeline.insights import get_generator
from app.pipeline.scoring import score_contractor
from app.pipeline.sources import get_source
from app.pipeline.sources.gaf_browser import get_browser
from app.pipeline.sources.gaf_profile import parse_profile, to_evidence
from app.schemas import ContractorRecord


def _fresh(session: Session, contractor_id: int, sources: tuple[str, ...], ttl_days: float) -> bool:
    """Was any evidence from these sources fetched inside the freshness window?"""
    cutoff = utcnow() - timedelta(days=ttl_days)
    return bool(session.scalar(
        select(func.count()).select_from(Evidence).where(
            Evidence.contractor_id == contractor_id, Evidence.source.in_(sources), Evidence.fetched_at >= cutoff)
    ))


# Filled in by a later stage (the profile page), so a re-discover that does not know them must not blank them.
PROFILE_OWNED = {"legal_name", "employees", "address", "years_in_business", "website", "google_place_id"}


def discover(payload: dict, job_id: int) -> None:
    zip_code = payload["zip_code"]
    radius = payload.get("radius_mi", 25)
    refresh = bool(payload.get("refresh"))  # ignore freshness windows and re-fetch everything

    # Scrape first, outside any DB session: it is slow and must not hold a connection open.
    records = list(islice(get_source().discover(zip_code, radius), settings.discover_limit))

    with db.SessionLocal() as session:
        for rec in records:
            row = session.execute(
                select(Contractor).where(Contractor.source == rec.source, Contractor.source_id == rec.source_id)
            ).scalar_one_or_none()
            if row is None:
                row = Contractor(source=rec.source, source_id=rec.source_id)
                session.add(row)
            for field, value in rec.model_dump().items():
                if field in ("source", "source_id"):
                    continue
                if field in PROFILE_OWNED and value is None:
                    continue
                setattr(row, field, value)
            row.search_zip = zip_code
            row.last_seen_at = utcnow()
            session.flush()
            # dedupe_key includes job_id so a retried discover doesn't double-enqueue its fan-out
            queue.enqueue(session, "enrich", {"contractor_id": row.id}, dedupe_key=f"enrich:{job_id}:{row.id}")
            job = {"contractor_id": row.id, "refresh": refresh}
            needs_profile = row.source == "gaf" and row.profile_url and (
                refresh or not _fresh(session, row.id, ("gaf_profile",), settings.profile_ttl_days))
            if needs_profile:
                queue.enqueue(session, "profile", job, dedupe_key=f"profile:{job_id}:{row.id}")
            elif settings.matching_enabled:  # profile already fresh: go straight to (freshness-checked) collection
                queue.enqueue(session, "collect", job, dedupe_key=f"collect:{job_id}:{row.id}")
        session.commit()  # contractors + their fan-out jobs land atomically


def profile(payload: dict, job_id: int) -> None:
    """Fetch the contractor's GAF profile page in the shared browser, save what it reveals on the contractor
    (address, registered name, tenure, size) and as evidence (the reviews shown), then continue the pipeline."""
    contractor_id = payload["contractor_id"]
    with db.SessionLocal() as session:
        contractor = session.get(Contractor, contractor_id)
        if contractor is None or not contractor.profile_url:
            return
        url = contractor.profile_url

    parsed = parse_profile(get_browser().fetch_html(url))  # blocked / unparsable pages raise -> queue retries

    with db.SessionLocal() as session:
        contractor = session.get(Contractor, contractor_id)
        contractor.legal_name = parsed.legal_name or contractor.legal_name
        contractor.address = parsed.street or contractor.address
        contractor.employees = parsed.employees or contractor.employees
        contractor.google_place_id = parsed.google_place_id or contractor.google_place_id
        if parsed.years_in_business is not None:
            contractor.years_in_business = parsed.years_in_business
        store_evidence(session, contractor_id, to_evidence(url, parsed))
        # Re-score now that tenure is known, then gather outside evidence using the registered business name.
        queue.enqueue(session, "enrich", {"contractor_id": contractor_id}, dedupe_key=f"enrich:{job_id}:{contractor_id}:profile")
        if settings.matching_enabled:
            queue.enqueue(
                session, "collect", {"contractor_id": contractor_id, "refresh": bool(payload.get("refresh"))},
                dedupe_key=f"collect:{job_id}:{contractor_id}")
        session.commit()


def enrich(payload: dict, job_id: int) -> None:
    # Three short phases instead of one long transaction: never hold a DB connection open across
    # the (slow, possibly LLM-backed) compute step.
    with db.SessionLocal() as session:
        contractor = session.get(Contractor, payload["contractor_id"])
        if contractor is None:
            return
        record = ContractorRecord.model_validate(contractor)

    result = score_contractor(record)
    generator = get_generator()
    insights = generator.generate(record, result)

    with db.SessionLocal() as session:
        lead = session.execute(
            select(Lead).where(Lead.contractor_id == payload["contractor_id"])
        ).scalar_one_or_none()
        if lead is None:
            lead = Lead(contractor_id=payload["contractor_id"])
            session.add(lead)
        # only system-owned columns; status / assigned_to / notes belong to reps
        lead.score = result.score
        lead.priority = result.priority
        lead.score_breakdown = [f.model_dump() for f in result.breakdown]
        lead.insights = insights.model_dump()
        lead.insights_model = generator.name
        lead.generated_at = utcnow()
        session.commit()


def collect(payload: dict, job_id: int) -> None:
    """Gather outside evidence, then hand off to `match`. Collectors fail independently: one flaky source
    must not block the others, but the job still fails afterwards so the queue retries the missing one."""
    contractor_id = payload["contractor_id"]
    with db.SessionLocal() as session:
        contractor = session.get(Contractor, contractor_id)
        if contractor is None:
            return
        record = ContractorRecord.model_validate(contractor)

    collectors = get_collectors()
    skip: set[str] = set()
    if not payload.get("refresh"):
        with db.SessionLocal() as session:
            skip = {
                c.name for c in collectors
                if getattr(c, "evidence_sources", None)
                and _fresh(session, contractor_id, c.evidence_sources, settings.evidence_ttl_days)
            }

    items, errors = [], []
    for collector in collectors:
        if collector.name in skip:  # fetched recently: reuse what is stored, no network
            continue
        try:
            items.extend(collector.collect(record))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{collector.name}: {type(e).__name__}: {e}")

    manufacturer = payload.get("manufacturer", settings.default_manufacturer)
    with db.SessionLocal() as session:
        store_evidence(session, contractor_id, items)
        session.flush()
        stored = list(session.scalars(select(Evidence).where(Evidence.contractor_id == contractor_id)))
        fingerprint = evidence_fingerprint(record, stored, manufacturer)[:16]
        # Match runs even on partial evidence (its confidence reflects the gap). Keyed on the evidence, so a
        # retry that fetched something new enqueues a fresh match, and a retry that did not is a no-op.
        queue.enqueue(
            session, "match", {"contractor_id": contractor_id, "manufacturer": manufacturer},
            dedupe_key=f"match:{job_id}:{contractor_id}:{fingerprint}",
        )
        session.commit()
    if errors:
        raise RuntimeError("; ".join(errors))


def match(payload: dict, job_id: int) -> None:
    run_match(payload["contractor_id"], payload.get("manufacturer", settings.default_manufacturer))


HANDLERS = {"discover": discover, "profile": profile, "enrich": enrich, "collect": collect, "match": match}
