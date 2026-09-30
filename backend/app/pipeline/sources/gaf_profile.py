"""Parse a gaf.com contractor profile page into structured facts. Pure function over HTML: no network, so it is
unit-testable and re-runnable from stored HTML.

The page is server-rendered and embeds machine-readable data, which is far more stable than CSS scraping:
  * JSON-LD `LocalBusiness`: registered business name, street address, phone
  * `<section class="contractor-reviews" data-reviews="[...]">`: review objects (Google reviews, 3 shown)
  * `contractor-details__info` blocks: "In business since YYYY", "Number of Employees"
"""

import hashlib
import html as html_lib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from app.matching.types import EvidenceItem


@dataclass
class ProfileReview:
    external_id: str
    rating: float | None
    text: str
    published: str | None  # ISO date
    platform: str
    author_initial: str | None  # deliberately not the full name: reviewers are private individuals


@dataclass
class GafProfile:
    legal_name: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    phone: str | None = None
    in_business_since: int | None = None
    employees: str | None = None
    google_place_id: str | None = None
    reviews: list[ProfileReview] = field(default_factory=list)

    @property
    def address(self) -> str | None:
        locality = " ".join(p for p in (self.city, self.state, self.zip_code) if p)
        return ", ".join(p for p in (self.street, locality) if p) or None

    @property
    def years_in_business(self) -> int | None:
        return max(0, date.today().year - self.in_business_since) if self.in_business_since else None


class ProfileParseError(ValueError):
    pass


def _json_ld_business(html: str) -> dict:
    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            # strict=False: contractors' free-text descriptions contain raw line breaks, which strict JSON rejects.
            # Parsing strictly silently dropped the registered name and address for ~1 in 5 profiles.
            data = json.loads(block, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "LocalBusiness":
            return data
    return {}


def _details(html: str) -> dict[str, str]:
    out = {}
    for title, desc in re.findall(
        r'<h3 class="contractor-details__title">\s*(.*?)\s*</h3>\s*<p class="contractor-details__description">\s*(.*?)\s*</p>',
        html,
        re.S,
    ):
        out[html_lib.unescape(re.sub(r"<[^>]+>", "", title)).strip()] = html_lib.unescape(re.sub(r"<[^>]+>", "", desc)).strip()
    return out


def _reviews(html: str) -> list[ProfileReview]:
    m = re.search(r'<section class="contractor-reviews"[^>]*?\sdata-reviews="([^"]*)"', html)
    if not m:
        return []
    try:
        raw = json.loads(html_lib.unescape(m.group(1)))
    except json.JSONDecodeError:
        return []
    reviews = []
    for r in raw:
        text = (r.get("Text") or "").strip()
        if not text:
            continue
        published = (r.get("PublishTime") or "")[:10] or None
        author = (r.get("AuthorName") or "").strip()
        reviews.append(ProfileReview(
            external_id=hashlib.sha1(f"{published}|{text[:80]}".encode()).hexdigest()[:12],
            rating=float(r["Rating"]) if r.get("Rating") is not None else None,
            text=text,
            published=published,
            platform=r.get("ReviewSource") or "unknown",
            author_initial=author[:1].upper() or None,
        ))
    return reviews


def _place_id(html: str) -> str | None:
    """The \"See All Reviews\" button links to https://search.google.com/local/reviews?placeid=ChIJ...; that id is
    what lets us ask Google for the same business's reviews directly."""
    m = re.search(r"search\.google\.com/local/reviews\?placeid=([A-Za-z0-9_-]{10,})", html_lib.unescape(html))
    return m.group(1) if m else None


def parse_profile(html: str) -> GafProfile:
    """Raises ProfileParseError if this does not look like a contractor profile (e.g. an error page), so the job
    is retried rather than silently recording an empty profile."""
    ld = _json_ld_business(html)
    details = _details(html)
    if not ld and not details:
        raise ProfileParseError("no LocalBusiness JSON-LD or contractor details found; not a profile page?")

    ld = {k: html_lib.unescape(v) if isinstance(v, str) else v for k, v in ld.items()}
    addr = {k: html_lib.unescape(v) if isinstance(v, str) else v for k, v in (ld.get("address") or {}).items()}
    since = re.search(r"\b(19|20)\d{2}\b", details.get("Years in Business", ""))
    return GafProfile(
        legal_name=(ld.get("name") or None),
        street=addr.get("streetAddress") or None,
        city=addr.get("addressLocality") or None,
        state=addr.get("addressRegion") or None,
        zip_code=addr.get("Postalcode") or addr.get("postalCode") or None,
        phone=ld.get("telephone") or None,
        in_business_since=int(since.group(0)) if since else None,
        employees=details.get("Number of Employees") or None,
        google_place_id=_place_id(html),
        reviews=_reviews(html),
    )


def to_evidence(url: str, profile: GafProfile) -> list[EvidenceItem]:
    """One `profile` item (facts about the business) plus one `review` item per review shown."""
    items = [EvidenceItem(
        kind="profile", source="gaf_profile", external_id="profile", url=url,
        payload={
            "legal_name": profile.legal_name,
            "address": profile.address,
            "phone": profile.phone,
            "in_business_since": profile.in_business_since,
            "years_in_business": profile.years_in_business,
            "employees": profile.employees,
        },
    )]
    for r in profile.reviews:
        items.append(EvidenceItem(
            kind="review", source="gaf_reviews", external_id=r.external_id, url=url,
            occurred_at=datetime.fromisoformat(r.published).replace(tzinfo=timezone.utc) if r.published else None,
            payload={"rating": r.rating, "text": r.text, "date": r.published, "platform": r.platform,
                     "author": r.author_initial},
        ))
    return items
