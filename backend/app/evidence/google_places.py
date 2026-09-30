"""Google reviews via the Places API (New) "Place Details" call.

The Place ID comes from the GAF profile page (its "See All Reviews" button), so this collector always looks up the
exact business GAF lists, with no name matching. Registered only when `GOOGLE_PLACES_API_KEY` is set.

Limits worth knowing:
  * Google returns at most 5 reviews per place, chosen by its own "relevance" ranking, not the newest 10. More
    than that needs a review-data vendor (see evidence/reviews.py).
  * Google's Maps Platform terms restrict how long Places content may be stored. Reviews are re-fetched on the
    evidence freshness window, but this is a compliance question to settle before production.
  * Only the reviewer's first initial is kept: reviewers are private individuals.
"""

import hashlib
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.matching.types import EvidenceItem
from app.schemas import ContractorRecord

URL = "https://places.googleapis.com/v1/places/{place_id}"
FIELDS = "id,rating,userRatingCount,reviews"


class GooglePlacesCollector:
    name = "google_places"
    evidence_sources = ("google_places",)

    def collect(self, c: ContractorRecord) -> list[EvidenceItem]:
        if not c.google_place_id:
            return []  # GAF profile not fetched yet, or it has no Google link: nothing to look up
        r = httpx.get(
            URL.format(place_id=c.google_place_id),
            headers={"X-Goog-Api-Key": settings.google_places_api_key or "", "X-Goog-FieldMask": FIELDS},
            timeout=30.0,
        )
        if r.status_code == 404:
            return []  # the place id has gone stale (business merged or closed); keep whatever else we know
        r.raise_for_status()  # bad key / API not enabled (403), quota (429), 5xx: fail loudly so the queue retries
        return [item for review in r.json().get("reviews", []) if (item := self._review(review))]

    @staticmethod
    def _review(raw: dict) -> EvidenceItem | None:
        text = ((raw.get("text") or {}).get("text") or (raw.get("originalText") or {}).get("text") or "").strip()
        if not text:
            return None
        published = (raw.get("publishTime") or "")[:10] or None
        author = ((raw.get("authorAttribution") or {}).get("displayName") or "").strip()
        return EvidenceItem(
            kind="review",
            source="google_places",
            # Same scheme as the GAF profile parser, so a review shown in both places is recognised as one.
            external_id=hashlib.sha1(f"{published}|{text[:80]}".encode()).hexdigest()[:12],
            occurred_at=None if not published else _midnight(published),
            payload={"rating": raw.get("rating"), "text": text, "date": published, "platform": "Google",
                     "author": author[:1].upper() or None},
        )


def _midnight(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
