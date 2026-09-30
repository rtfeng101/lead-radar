from sqlalchemy import select

from app import db
from app.config import settings
from app.models import Job
from app.pipeline import queue
from app.pipeline.worker import run_once
from tests.fakes import gaf_profile_html
from tests.test_gaf_pipeline import drain, gaf_env, gaf_env_llm, run  # noqa: F401  (fixtures + helpers)


def status(client):
    return client.get("/api/pipeline/status").json()


def make(session, kind, payload, st, **kw):
    j = Job(kind=kind, payload=payload, status=st, **kw)
    session.add(j)
    session.flush()
    return j


# ---------- failures that were later fixed stop counting ----------


def test_a_later_success_supersedes_earlier_failures_of_the_same_work(client):
    with db.SessionLocal() as s:
        old_a = make(s, "profile", {"contractor_id": 1}, "failed")
        old_b = make(s, "profile", {"contractor_id": 2}, "failed")
        other_kind = make(s, "collect", {"contractor_id": 1}, "failed")
        newer = make(s, "profile", {"contractor_id": 1, "refresh": True}, "running")
        s.commit()
        ids = (old_a.id, old_b.id, other_kind.id, newer.id)
    with db.SessionLocal() as s:
        queue.complete(s, ids[3])
    with db.SessionLocal() as s:
        st = {j.id: j.status for j in s.scalars(select(Job))}
    assert st[ids[0]] == "superseded"  # same kind + same contractor ('refresh' does not change what the work is)
    assert st[ids[1]] == "failed"  # a different contractor's failure is untouched
    assert st[ids[2]] == "failed"  # a different kind is untouched
    assert status(client)["failed"] == 2


def test_a_failure_that_happened_after_the_success_stays_failed(client):
    with db.SessionLocal() as s:
        ok = make(s, "profile", {"contractor_id": 1}, "running")
        s.commit()
        ok_id = ok.id
    with db.SessionLocal() as s:
        queue.complete(s, ok_id)
        later = make(s, "profile", {"contractor_id": 1}, "failed")  # a newer failure, id > the success
        s.commit()
        later_id = later.id
    with db.SessionLocal() as s:
        queue.complete(s, make(s, "profile", {"contractor_id": 9}, "running").id)  # unrelated success
        assert s.get(Job, later_id).status == "failed"


def test_discover_failures_are_matched_on_zip_and_radius(client):
    with db.SessionLocal() as s:
        f1 = make(s, "discover", {"zip_code": "10013", "radius_mi": 25}, "failed")
        f2 = make(s, "discover", {"zip_code": "07030", "radius_mi": 25}, "failed")
        ok = make(s, "discover", {"zip_code": "10013", "radius_mi": 25, "refresh": True}, "running")
        s.commit()
        f1_id, f2_id, ok_id = f1.id, f2.id, ok.id
    with db.SessionLocal() as s:
        queue.complete(s, ok_id)
        assert s.get(Job, f1_id).status == "superseded" and s.get(Job, f2_id).status == "failed"


def test_end_to_end_a_run_that_failed_and_was_rerun_shows_no_failures(gaf_env, monkeypatch):
    monkeypatch.setattr(settings, "job_max_attempts", 1)
    good = gaf_env.browser.html
    gaf_env.browser.html = "<html>maintenance</html>"
    run(gaf_env)
    assert status(gaf_env)["failed"] == 3  # what the user saw: 4 failed jobs in the status pill
    gaf_env.browser.html = good
    run(gaf_env)  # the same work, now succeeding
    assert status(gaf_env)["failed"] == 0
    with db.SessionLocal() as s:
        assert s.query(Job).filter(Job.status == "superseded").count() == 3  # history is kept


# ---------- refresh one contractor ----------


def first_lead(client):
    return client.get("/api/leads").json()["items"][0]


def test_refreshing_one_card_refetches_only_that_contractor_and_skips_regrading_if_unchanged(gaf_env):
    llm = gaf_env_llm(gaf_env)
    run(gaf_env)
    loads, judged = len(gaf_env.browser.urls), llm.count("Judgement")
    lead = first_lead(gaf_env)

    r = gaf_env.post(f"/api/leads/{lead['id']}/refresh")
    assert r.status_code == 202
    drain()

    assert len(gaf_env.browser.urls) == loads + 1  # exactly one page load, despite fresh data (window bypassed)
    assert gaf_env.browser.urls[-1] == lead["contractor"]["profile_url"]
    assert llm.count("Judgement") == judged  # nothing changed -> the stored grade is reused


def test_refreshing_a_card_whose_page_changed_regrades_only_that_card(gaf_env):
    llm = gaf_env_llm(gaf_env)
    run(gaf_env)
    judged = llm.count("Judgement")
    gaf_env.browser.html = gaf_profile_html(employees="26-50")
    gaf_env.post(f"/api/leads/{first_lead(gaf_env)['id']}/refresh")
    drain()
    assert llm.count("Judgement") == judged + 1


def test_a_second_refresh_while_one_is_in_flight_is_rejected(gaf_env):
    run(gaf_env)
    lead_id = first_lead(gaf_env)["id"]
    assert gaf_env.post(f"/api/leads/{lead_id}/refresh").status_code == 202
    assert gaf_env.post(f"/api/leads/{lead_id}/refresh").status_code == 409
    drain()
    assert gaf_env.post(f"/api/leads/{lead_id}/refresh").status_code == 202  # fine again once it finished


def test_activity_reports_progress_and_the_last_unresolved_error(gaf_env, monkeypatch):
    run(gaf_env)
    lead_id = first_lead(gaf_env)["id"]
    assert gaf_env.get(f"/api/leads/{lead_id}/activity").json() == {"queued": 0, "running": 0, "last_error": None}

    gaf_env.post(f"/api/leads/{lead_id}/refresh")
    assert gaf_env.get(f"/api/leads/{lead_id}/activity").json()["queued"] == 1  # waiting for a worker
    assert gaf_env.get("/api/leads/999999/activity").status_code == 404

    monkeypatch.setattr(settings, "job_max_attempts", 1)
    gaf_env.browser.html = "<html>maintenance</html>"
    drain()
    act = gaf_env.get(f"/api/leads/{lead_id}/activity").json()
    assert (act["queued"], act["running"]) == (0, 0) and "ProfileParseError" in act["last_error"]

    gaf_env.browser.html = gaf_profile_html()
    gaf_env.post(f"/api/leads/{lead_id}/refresh")
    drain()
    assert gaf_env.get(f"/api/leads/{lead_id}/activity").json()["last_error"] is None  # cleared by the success


def test_a_fixture_card_refreshes_via_collection_and_a_keyless_one_says_there_is_nothing_to_do(client, fake_llm, monkeypatch):
    assert client.post("/api/pipeline/runs", json={"zip_code": "10013"}).status_code == 202
    drain()
    lead_id = client.get("/api/leads").json()["items"][0]["id"]
    assert client.post(f"/api/leads/{lead_id}/refresh").status_code == 202
    with db.SessionLocal() as s:
        job = s.scalars(select(Job).where(Job.dedupe_key.like("refresh:%"))).one()
        assert job.kind == "collect" and job.payload["refresh"] is True
    drain()
    monkeypatch.setattr(settings, "openai_api_key", None)
    assert client.post(f"/api/leads/{lead_id}/refresh").status_code == 422
