import pytest
from sqlalchemy import select

from app import db
from app.config import settings
from app.models import Contractor, Evidence, Job, Match
from app.pipeline import queue
from app.pipeline.sources.gaf_browser import GafBlocked
from app.pipeline.worker import run_once
from app.schemas import ContractorRecord
from tests.fakes import gaf_profile_html


def drain(kinds=None) -> int:
    n = 0
    while run_once(kinds):
        n += 1
    return n


def rec(i: int, dist: float) -> ContractorRecord:
    return ContractorRecord(
        source="gaf", source_id=f"90000{i}", name=f"Example Roofing {i}", city="New York", state="NY", zip_code="10013",
        phone="(212) 555-0100", distance_mi=dist, rating=4.8, review_count=120, cert_tier="Master Elite",
        profile_url=f"https://www.gaf.com/x/90000{i}", raw={"gaf_contractor_id": f"90000{i}"},
    )


class FakeSource:
    def __init__(self, n=3):
        self.records = [rec(i, float(i)) for i in range(1, n + 1)]

    def discover(self, zip_code, radius_mi):
        return iter(self.records)


class FakeBrowser:
    def __init__(self, html=None, fail_first=0):
        self.html, self.fail_first, self.urls = html or gaf_profile_html(), fail_first, []

    def fetch_html(self, url):
        self.urls.append(url)
        if len(self.urls) <= self.fail_first:
            raise GafBlocked("gaf.com refused (HTTP 403)")
        return self.html


@pytest.fixture()
def gaf_env(client, monkeypatch):
    monkeypatch.setattr(settings, "lead_source", "gaf")
    source, browser = FakeSource(), FakeBrowser()
    monkeypatch.setattr("app.pipeline.handlers.get_source", lambda: source)
    monkeypatch.setattr("app.pipeline.handlers.get_browser", lambda: browser)
    client.source, client.browser = source, browser
    return client


def run(client):
    assert client.post("/api/pipeline/runs", json={"zip_code": "10013"}).status_code == 202
    return drain()


def test_profile_enriches_the_contractor_and_feeds_matching(gaf_env):
    run(gaf_env)
    lead = gaf_env.get("/api/leads").json()["items"][0]
    c = lead["contractor"]
    assert (c["legal_name"], c["address"], c["employees"]) == ("Acme Roofing Group LLC", "1 Main St Suite 2", "6-10")
    assert c["years_in_business"] >= 10 and c["source"] == "gaf"

    evidence = gaf_env.get(f"/api/leads/{lead['id']}/evidence").json()
    kinds = sorted(e["kind"] for e in evidence if e["source"].startswith("gaf_"))
    assert kinds == ["profile", "review", "review"]
    reviews = [e for e in evidence if e["kind"] == "review"]
    assert {e["payload"]["author"] for e in reviews} == {"P", "S"} and all(e["url"] for e in reviews)
    assert all(e["facts"] for e in reviews)  # extracted

    assert lead["match"] and lead["match"]["match_pct"] is not None
    pack = next(p for p in gaf_env_llm(gaf_env).judge_inputs if p["listing"]["name"] == lead["contractor"]["name"])
    assert pack["profile"]["legal_name"] == "Acme Roofing Group LLC" and pack["profile"]["employees"] == "6-10"
    assert "Only 2 of the contractor's ~120 reviews" in pack["reviews"]["sample_note"]


def gaf_env_llm(client):
    from app import llm

    return llm.get_llm()


def test_rediscovering_never_blanks_what_the_profile_page_taught_us(gaf_env):
    run(gaf_env)
    run(gaf_env)  # directory knows nothing about legal name / address / tenure / size; it must not erase them
    with db.SessionLocal() as s:
        for c in s.scalars(select(Contractor)):
            assert (c.legal_name, c.address, c.employees) == ("Acme Roofing Group LLC", "1 Main St Suite 2", "6-10")
            assert c.years_in_business is not None
        assert s.query(Match).count() == 3


def test_profile_reruns_rescore_the_lead_with_tenure_and_the_match_is_cached(gaf_env):
    run(gaf_env)
    llm = gaf_env_llm(gaf_env)
    judged = llm.count("Judgement")
    run(gaf_env)
    assert llm.count("Judgement") == judged  # same page -> same evidence -> no new LLM spend


def test_an_akamai_block_is_retried_with_backoff_and_then_succeeds(gaf_env):
    gaf_env.browser.fail_first = 1
    run(gaf_env)
    with db.SessionLocal() as s:
        blocked = s.scalars(select(Job).where(Job.kind == "profile", Job.status == "queued")).all()
        assert len(blocked) == 1 and "GafBlocked" in blocked[0].last_error and blocked[0].attempts == 1
        blocked[0].run_after = blocked[0].created_at  # skip the backoff wait
        s.commit()
    drain()
    with db.SessionLocal() as s:
        assert s.query(Job).filter(Job.status == "failed").count() == 0
        assert s.query(Match).count() == 3  # nothing lost: the blocked contractor got its match on retry


def test_a_page_that_is_not_a_profile_fails_the_job_instead_of_saving_an_empty_profile(gaf_env, monkeypatch):
    monkeypatch.setattr(settings, "job_max_attempts", 1)
    gaf_env.browser.html = "<html><body>maintenance</body></html>"
    run(gaf_env)
    with db.SessionLocal() as s:
        failed = s.scalars(select(Job).where(Job.status == "failed")).all()
        assert len(failed) == 3 and all(j.kind == "profile" and "ProfileParseError" in j.last_error for j in failed)
        assert s.query(Evidence).filter(Evidence.kind == "profile").count() == 0


def test_without_an_api_key_profiles_still_enrich_but_nothing_is_matched(gaf_env, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    run(gaf_env)
    with db.SessionLocal() as s:
        assert s.query(Match).count() == 0 and s.query(Job).filter(Job.kind == "collect").count() == 0
        assert s.scalar(select(Contractor).limit(1)).legal_name == "Acme Roofing Group LLC"


def test_discover_limit_takes_the_nearest_n(gaf_env, monkeypatch):
    monkeypatch.setattr(settings, "discover_limit", 2)
    run(gaf_env)
    with db.SessionLocal() as s:
        assert sorted(c.name for c in s.scalars(select(Contractor))) == ["Example Roofing 1", "Example Roofing 2"]


def test_a_worker_limited_to_some_kinds_leaves_the_rest_queued(gaf_env):
    gaf_env.post("/api/pipeline/runs", json={"zip_code": "10013"})
    assert run_once({"profile"}) is False  # only a discover job exists so far
    assert run_once({"discover"}) is True
    assert drain({"profile"}) == 3 and gaf_env.browser.urls  # the "browser worker" only ever touched profiles
    with db.SessionLocal() as s:
        assert s.query(Job).filter(Job.kind == "discover", Job.status == "done").count() == 1
        assert s.query(Job).filter(Job.kind == "collect", Job.status == "queued").count() == 3  # profile queued them
        assert s.query(Job).filter(Job.kind.in_(["collect", "match"]), Job.status == "done").count() == 0  # not run
        assert s.query(Job).filter(Job.kind == "enrich", Job.status == "queued").count() > 0  # not taken by it


def test_claim_filters_by_kind_and_takes_anything_when_no_kinds_are_given(gaf_env):
    with db.SessionLocal() as s:
        queue.enqueue(s, "enrich", {"contractor_id": 1}, dedupe_key="a")
        s.commit()
        assert queue.claim(s, {"profile"}) is None
        assert queue.claim(s, None).kind == "enrich"


def test_the_lead_list_only_shows_the_configured_source(gaf_env, monkeypatch):
    run(gaf_env)
    assert gaf_env.get("/api/leads").json()["total"] == 3
    monkeypatch.setattr(settings, "lead_source", "fixture")  # demo data must not mix into real leads
    assert gaf_env.get("/api/leads").json()["total"] == 0


def test_a_worker_leaves_job_kinds_it_has_no_handler_for(gaf_env, monkeypatch):
    from app.pipeline import handlers

    with db.SessionLocal() as s:
        queue.enqueue(s, "from_the_future", {}, dedupe_key="x")
        s.commit()
    assert run_once() is False  # not claimed, so not failed with a KeyError
    with db.SessionLocal() as s:
        job = s.scalar(select(Job).where(Job.kind == "from_the_future"))
        assert (job.status, job.attempts) == ("queued", 0)
    monkeypatch.setitem(handlers.HANDLERS, "from_the_future", lambda payload, job_id: None)  # an updated worker
    assert run_once() is True


# ---------- freshness windows: re-runs reuse stored data ----------


def age(sources, days):
    """Backdate stored evidence, as if it had been fetched `days` ago."""
    from datetime import timedelta

    from app.models import utcnow

    with db.SessionLocal() as s:
        for e in s.scalars(select(Evidence).where(Evidence.source.in_(sources))):
            e.fetched_at = utcnow() - timedelta(days=days)
        s.commit()


def test_a_rerun_reuses_fresh_profiles_instead_of_reloading_pages(gaf_env):
    llm = gaf_env_llm(gaf_env)
    run(gaf_env)
    assert len(gaf_env.browser.urls) == 3
    calls = list(llm.calls)
    run(gaf_env)
    assert len(gaf_env.browser.urls) == 3  # no page loads at all on the second run
    assert llm.calls == calls  # and no LLM calls either


def test_stale_profiles_are_refetched_but_an_unchanged_page_does_not_trigger_a_regrade(gaf_env):
    llm = gaf_env_llm(gaf_env)
    run(gaf_env)
    judged = llm.count("Judgement")
    age(["gaf_profile"], settings.profile_ttl_days + 1)
    run(gaf_env)
    assert len(gaf_env.browser.urls) == 6  # past the window: fetched again
    assert llm.count("Judgement") == judged  # same content -> same fingerprint -> the stored grade is reused


def test_a_changed_profile_page_does_regrade(gaf_env):
    llm = gaf_env_llm(gaf_env)
    run(gaf_env)
    judged = llm.count("Judgement")
    age(["gaf_profile"], settings.profile_ttl_days + 1)
    gaf_env.browser.html = gaf_profile_html(employees="11-25")  # the contractor grew
    run(gaf_env)
    assert llm.count("Judgement") == judged + 3


def test_refresh_refetches_everything_even_when_data_is_fresh(gaf_env):
    run(gaf_env)
    assert gaf_env.post("/api/pipeline/runs", json={"zip_code": "10013", "refresh": True}).status_code == 202
    drain()
    assert len(gaf_env.browser.urls) == 6


def test_fresh_collectors_are_skipped_and_stale_or_forced_ones_run(gaf_env, monkeypatch):
    from app.matching.types import EvidenceItem

    class Counting:
        name, evidence_sources, calls = "counting", ("counting_src",), 0

        def collect(self, c):
            Counting.calls += 1
            return [EvidenceItem(kind="license_search", source="counting_src", external_id="s",
                                 payload={"searched": False, "reason": "test"})]

    monkeypatch.setattr("app.pipeline.handlers.get_collectors", lambda: [Counting()])
    run(gaf_env)
    assert Counting.calls == 3
    run(gaf_env)
    assert Counting.calls == 3  # fresh: skipped, evidence reused
    age(["counting_src"], settings.evidence_ttl_days + 1)
    run(gaf_env)
    assert Counting.calls == 6  # stale: collected again
    gaf_env.post("/api/pipeline/runs", json={"zip_code": "10013", "refresh": True})
    drain()
    assert Counting.calls == 9  # forced


def test_profile_saves_the_google_place_id_and_a_later_discover_does_not_blank_it(gaf_env):
    run(gaf_env)
    with db.SessionLocal() as s:
        assert {c.google_place_id for c in s.scalars(select(Contractor))} == {"ChIJexamplePlaceId123"}
    gaf_env.browser.html = gaf_profile_html(place_id=None)  # a later page without the link must not erase it
    run(gaf_env)
    with db.SessionLocal() as s:
        assert {c.google_place_id for c in s.scalars(select(Contractor))} == {"ChIJexamplePlaceId123"}
