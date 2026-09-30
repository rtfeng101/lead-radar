"""Judgement -> headline match %, with the guardrails that make an LLM's opinion safe to rank on.

  match_pct  = weighted mean of the criteria the evidence could score (unknown criteria are left out, not
               counted as zero - "we do not know" must not look like "bad")
  confidence = weight-averaged confidence over ALL criteria (unknown = 0), so a score built on two
               criteria cannot look as trustworthy as one built on six
  guardrails = a score that cites no verifiable evidence has its confidence capped; citations of ids that
               are not in the pack are dropped
"""

from app.matching.rubric import Rubric
from app.matching.types import CriterionResult, Judgement, MatchResult

UNCITED_CONFIDENCE_CAP = 0.3


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def aggregate(rubric: Rubric, judgement: Judgement, valid_ids: set[str]) -> MatchResult:
    by_id = {c.id: c for c in judgement.criteria}
    results: list[CriterionResult] = []

    for crit in rubric.criteria:
        j = by_id.get(crit.id)
        if j is None or j.score is None:
            results.append(CriterionResult(
                id=crit.id, name=crit.name, weight=crit.weight, score=None, confidence=0.0,
                rationale=j.rationale if j else "Not scored.", evidence_ids=[],
            ))
            continue
        cites = [e for e in dict.fromkeys(j.evidence_ids) if e in valid_ids]
        confidence = _clamp(j.confidence, 0.0, 1.0)
        uncited = not cites
        if uncited:
            confidence = min(confidence, UNCITED_CONFIDENCE_CAP)
        results.append(CriterionResult(
            id=crit.id, name=crit.name, weight=crit.weight, score=round(_clamp(j.score, 0, 100)),
            confidence=round(confidence, 2), rationale=j.rationale, evidence_ids=cites, uncited=uncited,
        ))

    total_w = sum(r.weight for r in results)
    known = [r for r in results if r.score is not None]
    known_w = sum(r.weight for r in known)
    match_pct = round(sum(r.weight * r.score for r in known) / known_w) if known_w else None
    confidence = round(sum(r.weight * r.confidence for r in results) / total_w, 2)

    return MatchResult(
        manufacturer=rubric.manufacturer, match_pct=match_pct, confidence=confidence,
        criteria=results, summary=judgement.summary, gaps=judgement.gaps[:6],
        next_best_action=judgement.next_best_action,
    )
