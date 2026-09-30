from datetime import date

import httpx
import pytest

from app.config import settings
from app.evidence import get_collectors
from app.evidence.google_places import GooglePlacesCollector
from app.matching.features import build_pack
from app.models import Evidence
from app.schemas import ContractorRecord


def record(place_id="ChIJabc123456789") -> ContractorRecord:
    return ContractorRecord(source="gaf", source_id="1", name="KNA Roofing", google_place_id=place_id)


def reply(status: int, body: dict | None = None):
    def fake_get(url, headers=None, timeout=None):
        fake_get.calls.append((url, headers))
        return httpx.Response(status, json=body or {}, request=httpx.Request("GET", url))
    fake_get.calls = []
    return fake_get


REVIEWS = {"reviews": [
    {"rating": 5, "publishTime": "2026-08-02T10:00:00.123Z", "text": {"text": "Full replacement, perfect."},
     "authorAttribution": {"displayName": "Nick Example"}},
    {"rating": 4, "publishTime": "2026-05-01T10:00:00Z", "text": {"text": ""}},  # no text: nothing to cite
]}


def test_reviews_become_evidence_with_only_the_reviewer_initial(monkeypatch):
    monkeypatch.setattr(settings, "google_places_api_key", "k")
    get = reply(200, REVIEWS)
    monkeypatch.setattr(httpx, "get", get)
    items = GooglePlacesCollector().collect(record())
    assert [(i.kind, i.source, i.payload["date"], i.payload["author"]) for i in items] == [
        ("review", "google_places", "2026-08-02", "N")]
    url, headers = get.calls[0]
    assert url.endswith("/places/ChIJabc123456789") and headers["X-Goog-Api-Key"] == "k"
    assert "reviews" in headers["X-Goog-FieldMask"]


def test_no_place_id_means_no_request(monkeypatch):
    get = reply(200, REVIEWS)
    monkeypatch.setattr(httpx, "get", get)
    assert GooglePlacesCollector().collect(record(place_id=None)) == [] and not get.calls


def test_a_stale_place_id_is_not_an_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", reply(404))
    assert GooglePlacesCollector().collect(record()) == []


@pytest.mark.parametrize("status", [403, 429, 500])
def test_bad_key_quota_and_outages_raise_so_the_queue_retries(monkeypatch, status):
    monkeypatch.setattr(httpx, "get", reply(status))
    with pytest.raises(httpx.HTTPStatusError):
        GooglePlacesCollector().collect(record())


def test_collector_is_only_registered_with_a_key_on_live_evidence(monkeypatch):
    monkeypatch.setattr(settings, "evidence_source", "live")
    monkeypatch.setattr(settings, "google_places_api_key", None)
    assert "google_places" not in [c.name for c in get_collectors()]
    monkeypatch.setattr(settings, "google_places_api_key", "k")
    assert "google_places" in [c.name for c in get_collectors()]


def test_a_review_seen_on_both_gaf_and_google_is_counted_once():
    def row(id_, source):
        return Evidence(id=id_, kind="review", source=source, external_id="x",
                        payload={"rating": 5, "date": "2026-08-02", "text": "Full replacement, perfect."}, facts=None)
    pack, valid = build_pack(record(), [row(1, "gaf_reviews"), row(2, "google_places")], today=date(2026, 9, 21))
    assert pack["reviews"]["count"] == 1 and "E2" not in valid
