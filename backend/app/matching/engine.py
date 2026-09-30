import hashlib
import json
import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import db
from app.config import settings
from app.matching.aggregate import aggregate
from app.matching.extract import extract_review_facts
from app.matching.features import build_pack
from app.matching.judge import PROMPT_VERSION, judge
from app.matching.rubric import load_rubric
from app.matching.types import EvidenceItem
from app.models import Contractor, Evidence, Match, utcnow
from app.schemas import ContractorRecord

log = logging.getLogger(__name__)


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def store_evidence(session: Session, contractor_id: int, items: list[EvidenceItem]) -> int:
    """Idempotent upsert keyed on (source, external_id). Returns how many rows changed.

    Changed payload -> facts reset (re-extract). Rows a source no longer returns are pruned, but only for
    sources that reported in this run, so a failed collector never deletes what it could not re-fetch."""
    existing = {
        (e.source, e.external_id): e
        for e in session.scalars(select(Evidence).where(Evidence.contractor_id == contractor_id))
    }
    changed, seen = 0, set()
    for item in items:
        key = (item.source, item.external_id)
        seen.add(key)
        h = _hash(item.payload)
        row = existing.get(key)
        if row is None:
            session.add(Evidence(
                contractor_id=contractor_id, kind=item.kind, source=item.source, external_id=item.external_id,
                url=item.url, occurred_at=item.occurred_at, payload=item.payload, content_hash=h,
            ))
            changed += 1
        else:
            row.fetched_at = utcnow()
            if row.content_hash != h:
                row.payload, row.content_hash, row.facts = item.payload, h, None
                row.url, row.occurred_at, row.kind = item.url, item.occurred_at, item.kind
                changed += 1
    reported_sources = {s for s, _ in seen}
    stale = [e.id for (s, x), e in existing.items() if s in reported_sources and (s, x) not in seen]
    if stale:
        session.execute(delete(Evidence).where(Evidence.id.in_(stale)))
        changed += len(stale)
    return changed


def evidence_fingerprint(record: ContractorRecord, evidence: list[Evidence], manufacturer: str) -> str:
    rubric = load_rubric(manufacturer)
    return _hash({
        "rubric": rubric.model_dump(),
        "judge_model": settings.judge_model,
        "prompt_version": PROMPT_VERSION,
        "listing": record.model_dump(exclude={"raw", "google_place_id"}),  # the id is a lookup key, not grading input
        "evidence": sorted((e.kind, e.source, e.external_id, e.content_hash) for e in evidence),
    })


def run_match(contractor_id: int, manufacturer: str) -> str:
    """Score one contractor against one manufacturer's rubric. Returns "matched" or "skipped" (inputs
    unchanged since the last match, so no LLM spend).

    Three short DB phases around the slow LLM calls, so no connection is held open while waiting."""
    rubric = load_rubric(manufacturer)

    # 1. read + decide
    with db.SessionLocal() as s:
        contractor = s.get(Contractor, contractor_id)
        if contractor is None:
            return "skipped"
        record = ContractorRecord.model_validate(contractor)
        evidence = list(s.scalars(select(Evidence).where(Evidence.contractor_id == contractor_id)))
        fingerprint = evidence_fingerprint(record, evidence, manufacturer)
        existing = s.scalar(select(Match).where(Match.contractor_id == contractor_id, Match.manufacturer == manufacturer))
        if existing is not None and existing.input_hash == fingerprint:
            return "skipped"
        pending = [
            (str(e.id), e.payload.get("rating"), e.payload.get("text") or "")
            for e in evidence if e.kind == "review" and e.facts is None and e.payload.get("text")
        ]

    # 2. stage 1: extract facts for reviews we have not read yet, and persist them
    if pending:
        facts = extract_review_facts(pending, settings.extraction_model)
        with db.SessionLocal() as s:
            for eid, f in facts.items():
                s.get(Evidence, int(eid)).facts = f.model_dump()
            s.commit()
        missing = len(pending) - len(facts)
        if missing:
            log.warning("contractor %s: %d review(s) not extracted; judging without them", contractor_id, missing)

    # 3. stage 2: judge, aggregate, store
    with db.SessionLocal() as s:
        evidence = list(s.scalars(select(Evidence).where(Evidence.contractor_id == contractor_id)))
    pack, valid_ids = build_pack(record, evidence)
    result = aggregate(rubric, judge(rubric, pack, settings.judge_model), valid_ids)

    with db.SessionLocal() as s:
        row = s.scalar(select(Match).where(Match.contractor_id == contractor_id, Match.manufacturer == manufacturer))
        if row is None:
            row = Match(contractor_id=contractor_id, manufacturer=manufacturer)
            s.add(row)
        row.match_pct, row.confidence = result.match_pct, result.confidence
        row.criteria = [c.model_dump() for c in result.criteria]
        row.summary, row.gaps, row.next_action = result.summary, result.gaps, result.next_best_action
        row.input_hash, row.model, row.generated_at = fingerprint, settings.judge_model, utcnow()
        s.commit()
    return "matched"
