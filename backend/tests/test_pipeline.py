from app import db
from app.models import Job
from app.pipeline import queue
from app.pipeline.worker import run_once


def drain() -> int:
    n = 0
    while run_once():
        n += 1
    return n


def test_run_pipeline_then_browse_and_edit_leads(client):
    r = client.post("/api/pipeline/runs", json={"zip_code": "10013", "radius_mi": 25})
    assert r.status_code == 202

    assert drain() == 1 + 12 * 3  # discover, then enrich + collect + match per contractor
    assert client.get("/api/pipeline/status").json() == {"queued": 0, "running": 0, "done": 37, "failed": 0}

    body = client.get("/api/leads", params={"sort": "score"}).json()
    assert body["total"] == 12
    scores = [i["score"] for i in body["items"]]
    assert scores == sorted(scores, reverse=True)
    assert client.get("/api/leads", params={"priority": "hot"}).json()["total"] < 12
    assert client.get("/api/leads", params={"q": "nassau"}).json()["total"] == 1

    lead = body["items"][0]
    assert lead["insights"]["talking_points"] and lead["score_breakdown"]
    patched = client.patch(f"/api/leads/{lead['id']}", json={"status": "contacted", "notes": "left vm"}).json()
    assert (patched["status"], patched["notes"]) == ("contacted", "left vm")


def test_rerun_is_idempotent_and_keeps_rep_edits(client):
    client.post("/api/pipeline/runs", json={"zip_code": "10013"})
    drain()
    lead_id = client.get("/api/leads").json()["items"][0]["id"]
    client.patch(f"/api/leads/{lead_id}", json={"status": "qualified"})

    client.post("/api/pipeline/runs", json={"zip_code": "10013"})
    drain()

    body = client.get("/api/leads", params={"limit": 200}).json()
    assert body["total"] == 12  # no duplicates
    assert client.get(f"/api/leads/{lead_id}").json()["status"] == "qualified"  # not clobbered


def test_failed_job_retries_with_backoff_then_parks(client, monkeypatch):
    from app.config import settings
    from app.pipeline import handlers

    monkeypatch.setattr(settings, "job_max_attempts", 2)
    monkeypatch.setitem(handlers.HANDLERS, "discover", lambda payload, job_id: 1 / 0)
    with db.SessionLocal() as s:
        queue.enqueue(s, "discover", {"zip_code": "10013"})
        s.commit()

    assert run_once()  # attempt 1 fails -> requeued with a future run_after
    assert not run_once()  # backoff: not runnable yet
    with db.SessionLocal() as s:
        job = s.query(Job).one()
        assert (job.status, job.attempts) == ("queued", 1)
        job.run_after = job.created_at  # skip the wait
        s.commit()
    assert run_once()  # attempt 2 fails -> parked
    with db.SessionLocal() as s:
        job = s.query(Job).one()
        assert job.status == "failed" and "ZeroDivisionError" in job.last_error


def test_dedupe_key_prevents_double_enqueue(client):
    with db.SessionLocal() as s:
        assert queue.enqueue(s, "enrich", {"contractor_id": 1}, dedupe_key="k") is not None
        assert queue.enqueue(s, "enrich", {"contractor_id": 1}, dedupe_key="k") is None
        s.commit()
        assert s.query(Job).count() == 1
