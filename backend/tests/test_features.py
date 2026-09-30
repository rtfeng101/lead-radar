from datetime import date

from app.matching.features import build_pack
from app.models import Evidence
from app.schemas import ContractorRecord

TODAY = date(2026, 9, 21)


def record() -> ContractorRecord:
    return ContractorRecord(source="gaf", source_id="1", name="KNA Roofing", review_count=79, rating=4.9)


def review(id_: int, when: str) -> Evidence:
    return Evidence(id=id_, kind="review", source="gaf_reviews", external_id=str(id_),
                    payload={"rating": 5, "date": when, "text": "new roof"}, facts=None)


def permit_summary(id_: int, **payload) -> Evidence:
    base = {"searched": True, "window_months": 24, "total_jobs": 0, "roofing_jobs": 0, "last_issued": None}
    return Evidence(id=id_, kind="permit_summary", source="nyc_dob_now", external_id="summary",
                    payload={**base, **payload}, facts=None)


def test_recent_reviews_are_reported_as_current_activity():
    rows = [review(1, "2026-08-02"), review(2, "2026-06-09"), review(3, "2026-04-15")]
    pack, _ = build_pack(record(), rows, today=TODAY)
    recent = pack["reviews"]["recent_activity"]
    assert recent["newest_review"] == "2026-08-02"
    assert recent["last_90_days"] == 1 and recent["last_12_months"] == 3


def test_an_empty_permit_search_is_labelled_a_lower_bound_not_proof_of_no_work():
    """KNA Roofing: three reviews of finished roofs this year, zero DOB filings. The judge must be told the
    permit count does not contradict the reviews."""
    rows = [review(1, "2026-08-02"), permit_summary(2)]
    pack, _ = build_pack(record(), rows, today=TODAY)
    assert pack["permits"]["status"] == "searched" and pack["permits"]["roofing_jobs"] == 0
    assert "lower bound" in pack["permits"]["coverage_note"]
    assert pack["reviews"]["recent_activity"]["last_90_days"] == 1


def test_undated_or_malformed_review_dates_do_not_break_the_pack():
    pack, _ = build_pack(record(), [review(1, "not-a-date"), review(2, "")], today=TODAY)
    recent = pack["reviews"]["recent_activity"]
    assert recent["newest_review"] is None and recent["last_12_months"] == 0
