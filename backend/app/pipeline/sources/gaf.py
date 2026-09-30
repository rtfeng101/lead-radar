"""Live GAF "Find a Roofing Contractor" directory.

How the page works (found by watching its network calls): the results list is not in the HTML. The page's own
JavaScript POSTs to a Coveo search index (`*.org.coveo.com/rest/search/v2`) with a bearer token that GAF ships to
every visitor, filtered by distance from a lat/long. That index is a separate host from gaf.com, so once we hold
the token, the whole directory (all pages) is a handful of plain HTTP calls. Only obtaining the token needs the
browser. This matches what the site itself does, and it is much gentler on GAF than paging through the UI.

Note the page centers its search on the visitor's IP location, not the ZIP typed into it, so we geocode the ZIP
ourselves and pass the coordinates.
"""

import json
import logging
import time
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import httpx

from app.config import settings
from app.pipeline.sources.gaf_browser import get_browser
from app.schemas import ContractorRecord

log = logging.getLogger(__name__)

COVEO_URL = (
    "https://gafmaterialscorporationproduction3yalqk12.org.coveo.com/rest/search/v2"
    "?organizationId=gafmaterialscorporationproduction3yalqk12"
)
PIPELINE = "prod-gaf-recommended-residential-contractors"
PAGE_SIZE = 100
METRES_TO_MILES = 0.000621371  # metres -> miles, as used by GAF's own distance function

FIELDS = [
    "gaf_contractor_id", "gaf_contractor_type", "gaf_contractor_dba", "gaf_navigation_title", "gaf_rating",
    "gaf_number_of_reviews", "gaf_f_city", "gaf_f_state_code", "gaf_postal_code", "gaf_phone", "gaf_latitude",
    "gaf_longitude", "gaf_f_country_code", "gaf_f_contractor_designations_residential",
    "gaf_f_contractor_certifications_and_awards_residential", "gaf_f_contractor_raq_specialties_residential",
    "gaf_enrolled_in_gaf_leads", "distanceinmiles", "uri",
]


class GafDirectoryError(RuntimeError):
    pass


# ---------- geocoding ----------


@lru_cache(maxsize=256)
def geocode_zip(zip_code: str) -> tuple[float, float]:
    """(lat, lng) of a US ZIP via zippopotam.us (free, no key)."""
    r = httpx.get(f"https://api.zippopotam.us/us/{zip_code}", timeout=15.0)
    if r.status_code == 404:
        raise GafDirectoryError(f"unknown ZIP code {zip_code!r}")
    r.raise_for_status()
    place = r.json()["places"][0]
    return float(place["latitude"]), float(place["longitude"])


# ---------- token ----------


def _read_cached_token() -> str | None:
    try:
        return json.loads(Path(settings.gaf_token_cache).read_text())["token"]
    except (OSError, ValueError, KeyError):
        return None


def _write_cached_token(token: str) -> None:
    path = Path(settings.gaf_token_cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": token, "fetched_at": time.time()}))
    path.chmod(0o600)


def get_token(*, refresh: bool = False) -> str:
    """env override -> on-disk cache -> browser. The browser is only needed when the cache is empty or rejected."""
    if settings.gaf_coveo_token and not refresh:
        return settings.gaf_coveo_token
    if not refresh and (cached := _read_cached_token()):
        return cached
    token = get_browser().capture_coveo_token()
    _write_cached_token(token)
    return token


# ---------- query + mapping ----------


def build_query(lat: float, lng: float, radius_mi: int, first: int) -> dict:
    return {
        "locale": "en-US",
        "tab": "defaultTab",
        "aq": f"@distanceinmiles <= {radius_mi} AND @gaf_f_country_code = USA",
        "context": {"sortingStrategy": "gafrecommended-initial"},
        "fieldsToInclude": FIELDS,
        "pipeline": PIPELINE,
        "searchHub": PIPELINE,
        "q": "",
        "enableQuerySyntax": False,
        "sortCriteria": "relevancy",
        "cq": 'NOT @gaf_content_type=="NO CONTENT TYPE FILTER"',
        "numberOfResults": PAGE_SIZE,
        "firstResult": first,
        "queryFunctions": [
            {"fieldName": "@distanceinmiles",
             "function": f"dist(@gaf_latitude, @gaf_longitude, {lat}, {lng})*{METRES_TO_MILES}"}
        ],
    }


def cert_tier(raw: dict) -> str | None:
    certs = " ".join(raw.get("gaf_f_contractor_certifications_and_awards_residential") or [])
    for needle, tier in (("Master Elite", "Master Elite"), ("Certified Plus", "Certified Plus"), ("Certified", "Certified")):
        if needle.lower() in certs.lower():
            return tier
    return None


def awards(raw: dict) -> list[str]:
    """e.g. "President's Club 1 Star". Designations look like 'NAME^_^RESIDENTIAL^_^A'."""
    names = [d.split("^_^")[0].replace("’", "'") for d in raw.get("gaf_f_contractor_designations_residential") or []]
    return [n for n in names if "President" in n]


def to_record(result: dict) -> ContractorRecord | None:
    raw = result.get("raw") or {}
    cid = raw.get("gaf_contractor_id")
    name = raw.get("gaf_navigation_title") or result.get("title")
    if not cid or not name:
        return None
    specialties = [s.title() for s in raw.get("gaf_f_contractor_raq_specialties_residential") or []]
    designation_tags = [
        d.split("^_^")[0].title()
        for d in raw.get("gaf_f_contractor_designations_residential") or []
        if d.split("^_^")[0] in ("SOLAR", "FORTIFIED", "METAL")
    ]
    return ContractorRecord(
        source="gaf",
        source_id=str(cid),
        name=name,
        phone=raw.get("gaf_phone"),
        city=raw.get("gaf_f_city"),
        state=raw.get("gaf_f_state_code"),
        zip_code=(raw.get("gaf_postal_code") or "")[:5] or None,
        distance_mi=round(raw["distanceinmiles"], 1) if raw.get("distanceinmiles") is not None else None,
        rating=raw.get("gaf_rating"),
        review_count=raw.get("gaf_number_of_reviews"),
        cert_tier=cert_tier(raw),
        services=sorted(set(specialties + designation_tags)),
        profile_url=result.get("clickUri") or result.get("uri"),
        raw=raw,
    )


class GafSource:
    """Every GAF-listed residential contractor within `radius_mi` of a ZIP, nearest first."""

    name = "gaf"

    def _post(self, body: dict, token: str) -> httpx.Response:
        return httpx.post(COVEO_URL, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=30.0)

    def discover(self, zip_code: str, radius_mi: int) -> Iterable[ContractorRecord]:
        lat, lng = geocode_zip(zip_code)
        token = get_token()
        refreshed = False
        records: dict[str, ContractorRecord] = {}
        first, total = 0, None
        while total is None or first < total:
            resp = self._post(build_query(lat, lng, radius_mi, first), token)
            if resp.status_code in (401, 403) and not refreshed:
                log.info("GAF search token rejected (HTTP %s); fetching a fresh one", resp.status_code)
                token, refreshed = get_token(refresh=True), True
                continue
            if resp.status_code != 200:
                raise GafDirectoryError(f"GAF directory search failed: HTTP {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            total = data["totalCount"]
            results = data.get("results") or []
            if not results:
                break
            for result in results:
                rec = to_record(result)
                if rec:
                    records[rec.source_id] = rec
            first += len(results)
        log.info("GAF directory: %s contractors within %s mi of %s", len(records), radius_mi, zip_code)
        return sorted(records.values(), key=lambda r: (r.distance_mi is None, r.distance_mi or 0))

