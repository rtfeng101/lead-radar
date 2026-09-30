from sqlalchemy import select

from app import db
from app.config import settings
from app.evidence import fixture as fixture_mod
from app.matching.engine import run_match
from app.models import Evidence, Job, Match
from app.pipeline.worker import run_once


def drain() -> int:
    n = 0
    while run_once():
        n += 1
    return n


def run_zip(client) -> None:
    assert client.post("/api/pipeline/runs", json={"zip_code": "10013"}).status_code == 202
    drain()


def leads_by_name(client) -> dict:
    items = client.get("/api/leads", params={"limit": 200}).json()["items"]
    return {i["contractor"]["name"]: i for i in items}


def test_full_run_produces_cited_matches_ranked_by_match(client, fake_llm):
    run_zip(client)
    body = client.get("/api/leads", params={"limit": 200}).json()
    assert body["total"] == 12 and all(i["match"] for i in body["items"])

    pcts = [i["match"]["match_pct"] for i in body["items"]]
    assert pcts == sorted(pcts, reverse=True)  # default sort is by match

    top = body["items"][0]
    m = top["match"]
    assert m["manufacturer"] == "gaf" and 0 <= m["confidence"] <= 1 and m["summary"]
    assert [c["id"] for c in m["criteria"]] == [
        "product_fit", "volume", "reputation", "compliance", "brand_alignment", "stability"]

    # every citation resolves to real evidence for this contractor
    refs = {e["ref"] for e in client.get(f"/api/leads/{top['id']}/evidence").json()}
    cited = {ref for c in m["criteria"] for ref in c["evidence_ids"]}
    assert cited and cited <= refs


def test_reviews_are_extracted_once_and_only_sent_to_the_judge_as_facts(client, fake_llm):
    run_zip(client)
    with db.SessionLocal() as s:
        reviews = list(s.scalars(select(Evidence).where(Evidence.kind == "review")))
        assert reviews and all(r.facts and r.facts["sentiment"] for r in reviews)
    first = fake_llm.judge_inputs[0]["reviews"]
    assert first["facts_extracted_for"] == first["count"] and "excerpts" in first
    # the GAF-mentioning review was normalized Timberline -> GAF
    assert any("GAF" in p["reviews"].get("reviews_naming_manufacturer", {}) for p in fake_llm.judge_inputs)


def test_rerun_with_unchanged_evidence_spends_no_llm_calls_and_keeps_rep_edits(client, fake_llm):
    run_zip(client)
    lead_id = client.get("/api/leads").json()["items"][0]["id"]
    client.patch(f"/api/leads/{lead_id}", json={"status": "qualified"})
    calls = list(fake_llm.calls)

    run_zip(client)

    assert fake_llm.calls == calls  # nothing changed -> zero extraction and zero judge calls
    assert client.get(f"/api/leads/{lead_id}").json()["status"] == "qualified"
    with db.SessionLocal() as s:
        assert s.query(Match).count() == 12 and s.query(Evidence).count() > 12


def test_changed_evidence_rejudges_only_that_contractor_and_reextracts_only_the_edit(client, fake_llm, monkeypatch):
    run_zip(client)
    before_extract, before_judge = fake_llm.count("ReviewBatch"), fake_llm.count("Judgement")

    data = fixture_mod._load()
    data["fx-001"]["reviews"][0]["text"] += " Update: still perfect a year later."
    monkeypatch.setattr(fixture_mod, "_load", lambda: data)
    run_zip(client)

    assert fake_llm.count("ReviewBatch") == before_extract + 1  # one batch, containing just the edited review
    assert fake_llm.count("Judgement") == before_judge + 1  # only Harborline is re-judged


def test_changing_the_judge_model_or_rubric_invalidates_the_cache(client, fake_llm, monkeypatch):
    run_zip(client)
    calls = fake_llm.count("Judgement")
    monkeypatch.setattr(settings, "judge_model", "some-other-model")
    run_zip(client)
    assert fake_llm.count("Judgement") == calls + 12


def test_sources_that_could_not_be_searched_show_up_as_gaps_not_as_bad_scores(client, fake_llm):
    run_zip(client)
    by_source = {p["listing"]["name"]: p for p in fake_llm.judge_inputs}
    nj = by_source["Palisade Peak Roofing"]  # New Jersey
    assert nj["permits"]["status"] == "not_searched" and nj["licenses"]["status"] == "not_searched"
    nyc = by_source["Harborline Roofing & Exteriors"]
    assert nyc["permits"]["status"] == "searched" and nyc["permits"]["roofing_jobs"] == 34
    assert nyc["licenses"]["licenses"][0]["status"] == "Active"


def test_a_failing_collector_does_not_block_the_others_but_the_job_is_retried(client, fake_llm, monkeypatch):
    from app.evidence import get_collectors as real

    class Broken:
        name = "broken"

        def collect(self, c):
            raise TimeoutError("socrata down")

    monkeypatch.setattr("app.pipeline.handlers.get_collectors", lambda: [*real(), Broken()])
    monkeypatch.setattr(settings, "job_max_attempts", 1)
    run_zip(client)

    with db.SessionLocal() as s:
        failed = s.query(Job).filter(Job.status == "failed").all()
        assert len(failed) == 12 and all(j.kind == "collect" and "socrata down" in j.last_error for j in failed)
        assert s.query(Match).count() == 12  # the healthy collector's evidence was still scored
        assert s.query(Evidence).count() > 0


def test_matching_is_off_without_an_api_key(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    assert client.post("/api/pipeline/runs", json={"zip_code": "10013"}).status_code == 202
    assert drain() == 1 + 12  # discover + enrich only: no collect / match jobs
    body = client.get("/api/leads").json()
    assert body["total"] == 12 and all(i["match"] is None for i in body["items"])


def test_unmatched_leads_sort_last_and_min_match_filters(client, fake_llm):
    run_zip(client)
    with db.SessionLocal() as s:
        s.query(Match).filter(Match.contractor_id == 1).delete()
        s.commit()
    items = client.get("/api/leads", params={"limit": 200}).json()["items"]
    assert items[-1]["match"] is None
    assert all(i["match"] for i in client.get("/api/leads", params={"min_match": 1}).json()["items"])


def test_contractor_with_no_evidence_gets_no_percentage_rather_than_a_made_up_one(client, fake_llm):
    assert client.post("/api/pipeline/runs", json={"zip_code": "10013"}).status_code == 202
    run_once()  # discover only: contractors exist, nothing collected yet
    with db.SessionLocal() as s:
        assert s.query(Evidence).count() == 0
    assert run_match(1, "gaf") == "matched"
    with db.SessionLocal() as s:
        m = s.scalars(select(Match)).one()
        assert m.match_pct is None and m.confidence == 0.0
    assert fake_llm.judge_inputs[0]["reviews"]["status"] == "none_collected"


def test_match_decides_priority_and_next_action_and_the_priority_filter_agrees(client, fake_llm):
    run_zip(client)
    body = client.get("/api/leads", params={"limit": 200}).json()
    for item in body["items"]:
        pct = item["match"]["match_pct"]
        assert item["priority"] == ("hot" if pct >= 75 else "warm" if pct >= 50 else "cold")
        assert item["match"]["next_action"] == "fake next action"
    for p in ("hot", "warm", "cold"):
        filtered = client.get("/api/leads", params={"priority": p, "limit": 200}).json()["items"]
        assert {i["priority"] for i in filtered} <= {p}
        assert len(filtered) == sum(1 for i in body["items"] if i["priority"] == p)
    assert {i["priority"] for i in body["items"]} >= {"hot", "warm"}  # the filter is exercised, not vacuous


def test_a_new_prompt_version_rejudges_instead_of_serving_stale_matches(client, fake_llm, monkeypatch):
    from app.matching import engine

    run_zip(client)
    calls = fake_llm.count("Judgement")
    monkeypatch.setattr(engine, "PROMPT_VERSION", "next")
    run_zip(client)
    assert fake_llm.count("Judgement") == calls + 12
