import pytest

from app.matching.aggregate import UNCITED_CONFIDENCE_CAP, aggregate
from app.matching.rubric import Criterion, CriterionGuide, Rubric
from app.matching.types import CriterionJudgement, Judgement

G = CriterionGuide(high="h", mid="m", low="l")
RUBRIC = Rubric(
    manufacturer="t", display_name="T", version="1", status="s", context="c",
    criteria=[
        Criterion(id="a", name="A", weight=60, measures="", guide=G),
        Criterion(id="b", name="B", weight=30, measures="", guide=G),
        Criterion(id="c", name="C", weight=10, measures="", guide=G),
    ],
)
VALID = {"LISTING", "E1", "E2"}


def cj(id, score, conf=0.9, ids=("E1",)):
    return CriterionJudgement(id=id, score=score, confidence=conf, rationale="r", evidence_ids=list(ids))


def judgement(*criteria):
    return Judgement(criteria=list(criteria), summary="s", gaps=[], next_best_action="n")


def test_weighted_mean_excludes_unknown_criteria_rather_than_scoring_them_zero():
    r = aggregate(RUBRIC, judgement(cj("a", 80), cj("b", 40), cj("c", None)), VALID)
    assert r.match_pct == round((60 * 80 + 30 * 40) / 90)  # 67, not dragged down by the unknown 10%
    assert [c.score for c in r.criteria] == [80, 40, None]


def test_confidence_counts_unknown_criteria_as_zero():
    full = aggregate(RUBRIC, judgement(cj("a", 80), cj("b", 80), cj("c", 80)), VALID)
    partial = aggregate(RUBRIC, judgement(cj("a", 80), cj("b", None), cj("c", None)), VALID)
    assert full.confidence == pytest.approx(0.9)
    assert partial.confidence == pytest.approx(0.9 * 60 / 100)  # same scores, far less trustworthy
    assert partial.confidence < full.confidence


def test_uncited_score_has_its_confidence_capped():
    r = aggregate(RUBRIC, judgement(cj("a", 90, ids=()), cj("b", 90), cj("c", 90)), VALID)
    a = r.criteria[0]
    assert a.uncited and a.confidence == UNCITED_CONFIDENCE_CAP and a.score == 90


def test_hallucinated_citations_are_dropped_and_count_as_uncited():
    r = aggregate(RUBRIC, judgement(cj("a", 90, ids=("E999", "E1", "E1")), cj("b", 90, ids=("E404",)), cj("c", 50)), VALID)
    assert r.criteria[0].evidence_ids == ["E1"] and not r.criteria[0].uncited  # deduped, bad id removed
    assert r.criteria[1].evidence_ids == [] and r.criteria[1].uncited


def test_out_of_range_values_are_clamped():
    r = aggregate(RUBRIC, judgement(cj("a", 250, conf=7), cj("b", -30, conf=-1), cj("c", 50)), VALID)
    assert (r.criteria[0].score, r.criteria[0].confidence) == (100, 1.0)
    assert (r.criteria[1].score, r.criteria[1].confidence) == (0, 0.0)


def test_criterion_the_model_forgot_is_unknown_and_extras_are_ignored():
    r = aggregate(RUBRIC, judgement(cj("a", 70), cj("zzz", 5)), VALID)
    assert [c.id for c in r.criteria] == ["a", "b", "c"]
    assert r.criteria[1].score is None and r.match_pct == 70


def test_nothing_scoreable_gives_no_percentage_not_zero():
    r = aggregate(RUBRIC, judgement(cj("a", None), cj("b", None), cj("c", None)), VALID)
    assert r.match_pct is None and r.confidence == 0.0
