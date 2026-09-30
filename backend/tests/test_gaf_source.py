import pytest

from app.pipeline.sources import gaf
from app.pipeline.sources.gaf import GafDirectoryError, GafSource, build_query, cert_tier, to_record


def result(cid="900001", name="Example Roofing Co", dist=3.26, **raw):
    base = {
        "gaf_contractor_id": cid, "gaf_navigation_title": name, "gaf_phone": "(212) 555-0100", "gaf_f_city": "New York",
        "gaf_f_state_code": "NY", "gaf_postal_code": "10013", "gaf_rating": 4.8, "gaf_number_of_reviews": 120,
        "distanceinmiles": dist,
        "gaf_f_contractor_certifications_and_awards_residential": ["President's Club - Award", "GAF Master Elite® - Certification"],
        "gaf_f_contractor_designations_residential": ["President’s Club 1 Star^_^RESIDENTIAL^_^A", "MASTER_ELITE^_^RESIDENTIAL^_^C", "SOLAR^_^RESIDENTIAL^_^S"],
        "gaf_f_contractor_raq_specialties_residential": ["FORTIFIED"],
    }
    return {"title": name, "clickUri": f"https://www.gaf.com/x/{cid}", "raw": {**base, **raw}}


class FakeResp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body or {}, str(body)[:100]

    def json(self):
        return self._body


def test_to_record_maps_the_directory_fields():
    r = to_record(result(dist=3.26))
    assert (r.source, r.source_id, r.name) == ("gaf", "900001", "Example Roofing Co")
    assert (r.city, r.state, r.zip_code, r.phone) == ("New York", "NY", "10013", "(212) 555-0100")
    assert (r.rating, r.review_count, r.distance_mi) == (4.8, 120, 3.3)
    assert r.cert_tier == "Master Elite" and r.services == ["Fortified", "Solar"]
    assert r.profile_url == "https://www.gaf.com/x/900001" and r.raw["gaf_contractor_id"] == "900001"


def test_zip_plus_four_is_trimmed_and_incomplete_rows_are_skipped():
    assert to_record(result(gaf_postal_code="10013-1234")).zip_code == "10013"
    assert to_record({"raw": {"gaf_navigation_title": "No id"}}) is None
    assert to_record({"raw": {"gaf_contractor_id": "1"}, "title": ""}) is None


def test_cert_tier_reads_the_certification_list():
    assert cert_tier({"gaf_f_contractor_certifications_and_awards_residential": ["GAF Master Elite® - Certification"]}) == "Master Elite"
    assert cert_tier({"gaf_f_contractor_certifications_and_awards_residential": ["GAF Certified Plus - Certification"]}) == "Certified Plus"
    assert cert_tier({}) is None


def test_query_searches_around_the_given_point_not_the_visitors_ip():
    q = build_query(40.7185, -74.0025, 25, first=100)
    fn = q["queryFunctions"][0]["function"]
    assert "40.7185, -74.0025" in fn and "@distanceinmiles <= 25" in q["aq"]
    assert q["firstResult"] == 100 and q["numberOfResults"] == gaf.PAGE_SIZE and "gaf_contractor_id" in q["fieldsToInclude"]


@pytest.fixture()
def wired(monkeypatch):
    monkeypatch.setattr(gaf, "geocode_zip", lambda z: (40.7185, -74.0025))
    monkeypatch.setattr(gaf, "get_token", lambda refresh=False: "fresh" if refresh else "stale")


def test_discover_pages_through_everything_dedupes_and_sorts_nearest_first(monkeypatch, wired):
    pages = {0: [result("1", "Far", 20), result("2", "Near", 1)], 2: [result("3", "Mid", 9), result("2", "Near", 1)]}
    seen_first = []

    def post(self, body, token):
        seen_first.append(body["firstResult"])
        rows = pages[body["firstResult"]]
        return FakeResp(200, {"totalCount": 4, "results": rows})

    monkeypatch.setattr(GafSource, "_post", post)
    recs = list(GafSource().discover("10013", 25))
    assert seen_first == [0, 2]
    assert [r.name for r in recs] == ["Near", "Mid", "Far"]  # duplicate id 2 collapsed, nearest first


def test_a_rejected_token_is_refreshed_once_then_the_query_succeeds(monkeypatch, wired):
    tokens = []

    def post(self, body, token):
        tokens.append(token)
        return FakeResp(401) if token == "stale" else FakeResp(200, {"totalCount": 1, "results": [result()]})

    monkeypatch.setattr(GafSource, "_post", post)
    assert len(list(GafSource().discover("10013", 25))) == 1
    assert tokens == ["stale", "fresh"]


def test_a_token_that_is_still_rejected_after_refresh_fails_loudly(monkeypatch, wired):
    monkeypatch.setattr(GafSource, "_post", lambda self, body, token: FakeResp(401))
    with pytest.raises(GafDirectoryError, match="401"):
        list(GafSource().discover("10013", 25))


def test_server_errors_raise_so_the_queue_retries(monkeypatch, wired):
    monkeypatch.setattr(GafSource, "_post", lambda self, body, token: FakeResp(503))
    with pytest.raises(GafDirectoryError, match="503"):
        list(GafSource().discover("10013", 25))


def test_token_comes_from_env_then_cache_then_browser(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "gaf_token_cache", str(tmp_path / "sub" / "token.json"))
    launched = []

    class Browser:
        def capture_coveo_token(self):
            launched.append(1)
            return f"browser-{len(launched)}"

    monkeypatch.setattr(gaf, "get_browser", lambda: Browser())

    assert gaf.get_token() == "browser-1" and len(launched) == 1  # cache empty -> browser, then cached
    assert gaf.get_token() == "browser-1" and len(launched) == 1  # served from cache: no browser
    assert (tmp_path / "sub" / "token.json").stat().st_mode & 0o777 == 0o600  # not world-readable
    assert gaf.get_token(refresh=True) == "browser-2"  # forced refresh replaces the cache
    assert gaf.get_token() == "browser-2"
    monkeypatch.setattr(settings, "gaf_coveo_token", "from-env")
    assert gaf.get_token() == "from-env"
    assert gaf.get_token(refresh=True) == "browser-3"  # an env token that was rejected is not reused
