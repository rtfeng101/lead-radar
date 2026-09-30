"""Evidence rows -> the compact "evidence pack" the judge reads.

Numbers the judge should not have to compute (averages, counts, complaint tallies) are computed here in code.
Every source is reported as one of: has data / searched-found-nothing / not searched / not collected, because
those are four different situations and the scorer must not confuse them.
"""

from collections import Counter
from datetime import date, timedelta
from statistics import mean

from app.models import Evidence
from app.schemas import ContractorRecord

MAX_EXCERPTS = 6
EXCERPT_CHARS = 400


def cid(e: Evidence) -> str:
    return f"E{e.id}"


def _awards(c: ContractorRecord) -> list[str]:
    designations = c.raw.get("gaf_f_contractor_designations_residential") or []
    return [d.split("^_^")[0].replace("\u2019", "'") for d in designations if "President" in d.split("^_^")[0]]


def _listing(c: ContractorRecord) -> dict:
    return {
        "id": "LISTING",
        "source": "GAF contractor directory listing",
        "name": c.name,
        "registered_name": c.legal_name,
        "employees": c.employees,
        "gaf_awards": _awards(c),
        "city": c.city,
        "state": c.state,
        "distance_mi": c.distance_mi,
        "gaf_certification": c.cert_tier,
        "directory_rating": c.rating,
        "directory_review_count": c.review_count,
        "years_in_business": c.years_in_business,
        "services_listed": c.services,
    }


def _distinct_reviews(rows: list[Evidence]) -> list[Evidence]:
    """The same Google review can arrive twice (GAF's profile page shows 3 of them, the Places API returns its own
    5). Count and cite it once: keep the first by id."""
    seen, out = set(), []
    for r in rows:
        key = ((r.payload.get("date") or "")[:10], (r.payload.get("text") or "")[:80])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _recent_activity(dates: list[str], today: date) -> dict:
    """Dated reviews are the only signal that a contractor is working *now* (permit data lags and misses most
    residential re-roofs), so surface it explicitly instead of leaving the judge to infer it from a date range."""
    parsed = []
    for d in dates:
        try:
            parsed.append(date.fromisoformat(d[:10]))
        except ValueError:
            continue
    def since(days: int) -> int:
        return sum(1 for d in parsed if d >= today - timedelta(days=days))
    return {
        "as_of": today.isoformat(),
        "newest_review": max(parsed).isoformat() if parsed else None,
        "last_90_days": since(90),
        "last_12_months": since(365),
        "note": "Counts come only from the reviews we collected, so they are lower bounds. A recent review of a "
        "finished roof is evidence of current roofing work.",
    }


def _reviews(rows: list[Evidence], directory_total: int | None, today: date) -> dict:
    if not rows:
        return {"status": "none_collected", "count": 0}
    ratings = [r.payload["rating"] for r in rows if isinstance(r.payload.get("rating"), (int, float))]
    facts = [r.facts for r in rows if r.facts]
    brands = Counter(b for f in facts for b in set(f.get("brands", [])))

    # Excerpts: the reviews a rep would want quoted - the worst, the best, and the newest.
    by_rating = sorted(rows, key=lambda r: (r.payload.get("rating") or 3, r.payload.get("date") or ""))
    newest = sorted(rows, key=lambda r: r.payload.get("date") or "", reverse=True)
    picked: list[Evidence] = []
    for r in by_rating[:3] + by_rating[-2:] + newest[:1]:
        if r not in picked:
            picked.append(r)
    dates = sorted(d for d in (r.payload.get("date") for r in rows) if d)

    sample_note = (
        f"Only {len(rows)} of the contractor's ~{directory_total} reviews were collected; treat this as a small "
        "sample. The directory rating and review count (in `listing`) cover the full set."
        if directory_total and directory_total > len(rows) else None
    )
    return {
        "status": "collected",
        "count": len(rows),
        "sample_note": sample_note,
        "all_ids": [cid(r) for r in rows],
        "avg_rating": round(mean(ratings), 2) if ratings else None,
        "date_range": [dates[0], dates[-1]] if dates else None,
        "recent_activity": _recent_activity(dates, today),
        "facts_extracted_for": len(facts),
        "sentiment": dict(Counter(f["sentiment"] for f in facts)),
        "project_mix": dict(Counter(f["project"] for f in facts)),
        "complaints": dict(Counter(x for f in facts for x in f["complaints"])),
        "praises": dict(Counter(x for f in facts for x in f["praises"])),
        "reviews_naming_manufacturer": dict(brands),
        "excerpts": [
            {"id": cid(r), "rating": r.payload.get("rating"), "date": r.payload.get("date"),
             "text": (r.payload.get("text") or "")[:EXCERPT_CHARS]}
            for r in picked[:MAX_EXCERPTS]
        ],
    }


def _profile(rows: list[Evidence]) -> dict:
    row = next((r for r in rows if r.kind == "profile"), None)
    if row is None:
        return {"status": "not_collected"}
    p = row.payload
    return {"id": cid(row), "status": "collected", **{k: p.get(k) for k in (
        "legal_name", "address", "in_business_since", "years_in_business", "employees")}}


PERMIT_COVERAGE_NOTE = (
    "DOB NOW only lists jobs filed with the city by a licensed filer, and it is matched by business name. Many "
    "residential re-roofs by home-improvement contractors are never filed there, so a low or zero count is a "
    "lower bound, NOT evidence of little or no work. Cross-check against `reviews.recent_activity`."
)


def _permits(rows: list[Evidence]) -> dict:
    summary = next((r for r in rows if r.kind == "permit_summary"), None)
    if summary is None:
        return {"status": "not_collected"}
    p = summary.payload
    if not p.get("searched"):
        return {"id": cid(summary), "status": "not_searched", "reason": p.get("reason")}
    return {
        "id": cid(summary),
        "status": "searched",
        "coverage_note": PERMIT_COVERAGE_NOTE,
        **{k: p[k] for k in (
            "window_months", "total_jobs", "roofing_jobs", "last_issued", "by_work_type", "unit_note", "match_note",
        ) if k in p},
        "sample_roofing_jobs": [
            {"id": cid(r), "issued": r.payload.get("issued"), "work_type": r.payload.get("work_type"),
             "description": r.payload.get("description")}
            for r in rows if r.kind == "permit"
        ],
    }


def _licenses(rows: list[Evidence]) -> dict:
    search = next((r for r in rows if r.kind == "license_search"), None)
    if search is None:
        return {"status": "not_collected"}
    p = search.payload
    if not p.get("searched"):
        return {"id": cid(search), "status": "not_searched", "reason": p.get("reason")}
    return {
        "id": cid(search),
        "status": "searched",
        "found": p.get("found", 0),
        "matched_by": p.get("matched_by", []),
        "licenses": [
            {"id": cid(r), **{k: r.payload.get(k) for k in ("license_type", "status", "first_issued", "expires", "match", "superseded_by_active")}}
            for r in rows if r.kind == "license"
        ],
    }


def build_pack(record: ContractorRecord, evidence: list[Evidence], today: date | None = None) -> tuple[dict, set[str]]:
    """Returns (pack for the prompt, set of ids the judge is allowed to cite)."""
    today = today or date.today()
    by_kind: dict[str, list[Evidence]] = {}
    for e in sorted(evidence, key=lambda e: e.id):
        by_kind.setdefault(e.kind, []).append(e)
    by_kind["review"] = _distinct_reviews(by_kind.get("review", []))
    flat = [e for rows in by_kind.values() for e in rows]
    pack = {
        "listing": _listing(record),
        "profile": _profile(by_kind.get("profile", [])),
        "reviews": _reviews(by_kind.get("review", []), record.review_count, today),
        "permits": _permits(by_kind.get("permit_summary", []) + by_kind.get("permit", [])),
        "licenses": _licenses(by_kind.get("license_search", []) + by_kind.get("license", [])),
    }
    return pack, {"LISTING"} | {cid(e) for e in flat}
