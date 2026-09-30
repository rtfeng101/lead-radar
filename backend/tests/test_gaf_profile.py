from datetime import date

import pytest

from app.pipeline.sources.gaf_profile import ProfileParseError, parse_profile, to_evidence
from tests.fakes import gaf_profile_html


def test_parses_business_facts_from_json_ld_and_details():
    p = parse_profile(gaf_profile_html(since=2015, employees="6-10"))
    assert p.legal_name == "Acme Roofing Group LLC"
    assert (p.street, p.city, p.state, p.zip_code) == ("1 Main St Suite 2", "Springfield", "NY", "10001")
    assert p.address == "1 Main St Suite 2, Springfield NY 10001"
    assert p.phone == "(212) 555-0100" and p.employees == "6-10"
    assert p.in_business_since == 2015 and p.years_in_business == date.today().year - 2015


def test_reviews_keep_rating_text_date_and_only_the_authors_initial():
    p = parse_profile(gaf_profile_html())
    assert [(r.rating, r.published, r.platform) for r in p.reviews] == [
        (5.0, "2026-06-09", "Google"), (2.0, "2026-04-15", "Google")]
    assert [r.author_initial for r in p.reviews] == ["P", "S"]  # reviewers are private individuals
    assert "Pat" not in repr(p) and "Example" not in repr(p)


def test_a_business_founded_this_year_has_zero_years_not_none():
    assert parse_profile(gaf_profile_html(since=date.today().year)).years_in_business == 0


def test_missing_optional_sections_degrade_gracefully():
    html = gaf_profile_html(reviews=[]).replace("In business since 2015", "Not listed")
    p = parse_profile(html)
    assert p.reviews == [] and p.in_business_since is None and p.years_in_business is None


def test_an_error_page_raises_instead_of_recording_an_empty_profile():
    for page in ("<html><title>Access Denied</title></html>", "", "<html><body>Something went wrong</body></html>"):
        with pytest.raises(ProfileParseError):
            parse_profile(page)


def test_evidence_items_are_stable_and_carry_the_page_url():
    url = "https://www.gaf.com/en-us/roofing-contractors/residential/usa/ny/x-1"
    items = to_evidence(url, parse_profile(gaf_profile_html()))
    assert [i.kind for i in items] == ["profile", "review", "review"]
    assert all(i.url == url for i in items)
    assert items[1].source == "gaf_reviews" and items[1].occurred_at.year == 2026
    assert items[1].payload["author"] == "P" and "Pat Example" not in str(items[1].payload)
    again = to_evidence(url, parse_profile(gaf_profile_html()))
    assert [i.external_id for i in items] == [i.external_id for i in again]  # idempotent upserts
    assert len({i.external_id for i in items[1:]}) == 2


def test_a_description_with_raw_line_breaks_does_not_hide_the_registered_name_and_address():
    """Regression: ~15 of 82 real profiles have multi-line descriptions. Strict JSON rejected the whole block, so
    the legal name and address were silently lost."""
    html = gaf_profile_html(legal_name="Jersey Roofing LLC", street="411 River Dr", city="Garfield", state="NJ",
                            description="Family owned since 1984.\n6 Gold Award Winner&#39;s in a row.\nOffice open Mon-Fri.")
    import json, re

    block = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1)
    with pytest.raises(json.JSONDecodeError):
        json.loads(block)  # the fixture really is invalid strict JSON, like the real pages
    p = parse_profile(html)
    assert (p.legal_name, p.street, p.city, p.state) == ("Jersey Roofing LLC", "411 River Dr", "Garfield", "NJ")


def test_html_entities_in_the_registered_name_are_decoded():
    assert parse_profile(gaf_profile_html(legal_name="B&amp;B Siding &amp; Roofing LLC")).legal_name == "B&B Siding & Roofing LLC"


def test_google_place_id_is_read_from_the_see_all_reviews_link():
    assert parse_profile(gaf_profile_html(place_id="ChIJYb7e6zZm44kRxVEw6gNPUow")).google_place_id == "ChIJYb7e6zZm44kRxVEw6gNPUow"
    assert parse_profile(gaf_profile_html(place_id=None)).google_place_id is None
