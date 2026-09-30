import pytest

from app.pipeline.scoring import score_contractor
from app.schemas import ContractorRecord


def rec(**kw) -> ContractorRecord:
    return ContractorRecord(source="t", source_id="1", name="Acme Roofing", **kw)


def test_top_contractor_is_hot_and_explained():
    r = score_contractor(rec(cert_tier="Master Elite", review_count=400, rating=4.9, distance_mi=1, years_in_business=25))
    assert r.priority == "hot" and r.score >= 90
    assert r.score == pytest.approx(sum(f.points for f in r.breakdown), abs=1)  # per-factor rounding
    assert all(f.points <= f.max_points for f in r.breakdown)


def test_sparse_profile_is_cold_and_does_not_crash():
    r = score_contractor(rec())
    assert r.priority == "cold" and r.score == 0
