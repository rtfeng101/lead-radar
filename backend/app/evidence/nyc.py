"""Live evidence from NYC Open Data (Socrata): DCA home-improvement licenses and DOB NOW permits.

Entity resolution (contractor on GAF <-> record in a city dataset) is the hard part, so every match
records *how* it was made and how strong it is; the scorer sees that and can discount weak links:
  * licenses:  phone number (strong) -> business-name similarity (medium)
  * permits:   business-name similarity only. DOB NOW truncates names to 25 chars and has no phone,
               so this link is the weakest and the summary says so.
Only NYC is covered. Contractors elsewhere get an explicit "not searched" marker rather than silence.
"""

from collections import Counter
from datetime import date, datetime, timedelta, timezone

import httpx

from app.config import settings
from app.evidence.util import (
    is_nyc_zip,
    is_roofing,
    name_similarity,
    phone_digits,
    phone_variants,
    search_prefix,
)
from app.matching.types import EvidenceItem
from app.schemas import ContractorRecord

BASE = "https://data.cityofnewyork.us/resource"
DCA_LICENSES = "w7w3-xahh"  # Issued Licenses
DOB_NOW_PERMITS = "rbx6-tga4"  # DOB NOW: Build - Approved Permits

WINDOW_MONTHS = 24
NAME_MATCH_MIN = 0.90
MAX_PERMIT_ROWS = 5000
MAX_PERMIT_SAMPLES = 8


def _soql(dataset: str, **params: str) -> list[dict]:
    headers = {"X-App-Token": settings.nyc_open_data_app_token} if settings.nyc_open_data_app_token else {}
    r = httpx.get(f"{BASE}/{dataset}.json", params=params, headers=headers, timeout=45.0)
    r.raise_for_status()  # 429 / 5xx propagate so the job queue retries with backoff
    return r.json()


def _q(value: str) -> str:
    return value.replace("'", "''")


def _names(c: ContractorRecord) -> list[str]:
    """The trade name and the registered name; permits and licenses are filed under the registered one."""
    return list(dict.fromkeys(n for n in (c.name, c.legal_name) if n))


def _similarity(candidate: str, names: list[str], **kw) -> float:
    return max(name_similarity(candidate, n, **kw) for n in names)


def _not_searched(reason: str) -> dict:
    return {"searched": False, "reason": reason}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class NycLicenseCollector:
    name = "nyc_dca"
    evidence_sources = ("nyc_dca",)

    def collect(self, c: ContractorRecord) -> list[EvidenceItem]:
        if not is_nyc_zip(c.zip_code):
            return [self._summary(_not_searched("NYC DCA license data only covers businesses located in NYC."))]

        names = _names(c)
        rows: list[tuple[dict, str, float]] = []  # (row, method, similarity)
        digits = phone_digits(c.phone)
        if digits:
            in_list = ",".join(f"'{v}'" for v in phone_variants(digits))
            for row in _soql(
                DCA_LICENSES,
                **{"$where": f"business_category='Home Improvement Contractor' AND contact_phone in ({in_list})"},
            ):
                rows.append((row, "phone", _similarity(row["business_name"], names)))
        if not rows:
            seen: set[str] = set()
            for prefix in dict.fromkeys(p for p in (search_prefix(n) for n in names) if p):
                for row in _soql(
                    DCA_LICENSES,
                    **{
                        "$where": "business_category='Home Improvement Contractor' "
                        f"AND upper(business_name) like '{_q(prefix)}%'",
                        "$limit": "200",
                    },
                ):
                    sim = _similarity(row["business_name"], names)
                    if sim >= NAME_MATCH_MIN and row["license_nbr"] not in seen:
                        seen.add(row["license_nbr"])
                        rows.append((row, "name", sim))

        items = [self._license(row, method, sim) for row, method, sim in rows]
        # Licenses are re-issued under new numbers (the agency was renamed DCA -> DCWP), so an old expired record
        # often sits beside its active successor. Flag it so it is not mistaken for a compliance problem.
        if any(i.payload["status"] == "Active" for i in items):
            for i in items:
                if i.payload["status"] != "Active":
                    i.payload["superseded_by_active"] = True
        return [self._summary({"searched": True, "found": len(items), "matched_by": sorted({m for _, m, _ in rows})})] + items

    def _summary(self, payload: dict) -> EvidenceItem:
        payload = {"dataset": "NYC DCA Issued Licenses (Home Improvement Contractor)", **payload}
        return EvidenceItem(kind="license_search", source=self.name, external_id="search", payload=payload)

    def _license(self, row: dict, method: str, sim: float) -> EvidenceItem:
        return EvidenceItem(
            kind="license",
            source=self.name,
            external_id=row["license_nbr"],
            url=None,
            occurred_at=_parse_ts(row.get("license_creation_date")),
            payload={
                "license_nbr": row["license_nbr"],
                "license_type": "NYC Home Improvement Contractor",
                "status": row.get("license_status"),
                "business_name": row.get("business_name"),
                "first_issued": (row.get("license_creation_date") or "")[:10] or None,
                "expires": (row.get("lic_expir_dd") or "")[:10] or None,
                "match": {"method": method, "similarity": round(sim, 2)},
            },
        )


class NycPermitCollector:
    name = "nyc_dob_now"
    evidence_sources = ("nyc_dob_now",)

    def collect(self, c: ContractorRecord) -> list[EvidenceItem]:
        if not is_nyc_zip(c.zip_code):
            return [self._summary(_not_searched("NYC DOB permit data only covers work in NYC; contractor is based outside NYC."))]
        names = _names(c)
        prefixes = list(dict.fromkeys(p for p in (search_prefix(n) for n in names) if p))
        if not prefixes:
            return [self._summary(_not_searched("Business name too short/ambiguous to search permit filers safely."))]

        cutoff = (date.today() - timedelta(days=round(WINDOW_MONTHS * 30.4))).isoformat()
        rows, seen = [], set()
        for prefix in prefixes:
            for r in _soql(
                DOB_NOW_PERMITS,
                **{
                    "$select": "job_filing_number,work_permit,permit_status,issued_date,work_type,job_description,"
                    "estimated_job_costs,applicant_business_name,zip_code",
                    "$where": f"issued_date >= '{cutoff}' AND upper(applicant_business_name) like '{_q(prefix)}%'",
                    "$order": "issued_date DESC",
                    "$limit": str(MAX_PERMIT_ROWS),
                },
            ):
                key = (r["job_filing_number"], r.get("work_permit"))
                if key not in seen:
                    seen.add(key)
                    rows.append(r)
        rows.sort(key=lambda r: r["issued_date"], reverse=True)
        # Name similarity is checked per distinct filer name (there are few), not per row.
        accepted = {
            n
            for n in {r.get("applicant_business_name") or "" for r in rows}
            if n and _similarity(n, names, truncated_a=len(n) >= 24) >= NAME_MATCH_MIN
        }
        mine = [r for r in rows if r.get("applicant_business_name") in accepted]
        roofing = [r for r in mine if is_roofing(r.get("job_description"))]
        # One job files several permits (its sidewalk shed, fence, scaffold, each trade...) and repeats the
        # same description on all of them, so volume is counted in distinct job filings, not permit rows.
        roofing_jobs: dict[str, dict] = {}
        for r in roofing:  # rows are newest-first, so the first row per job is its latest permit
            roofing_jobs.setdefault(r["job_filing_number"], r)

        summary = {
            "searched": True,
            "window_months": WINDOW_MONTHS,
            "searched_names": names,
            "matched_filer_names": sorted(accepted),
            "match_note": "Matched by business-name similarity only (source truncates names to 25 chars); "
            "could include a different business with a near-identical name.",
            "total_jobs": len({r["job_filing_number"] for r in mine}),
            "roofing_jobs": len(roofing_jobs),
            "total_permit_rows": len(mine),
            "unit_note": "a 'job' is one DOB job filing; ancillary permits (sheds, fences, scaffolds) share it",
            "roofing_classifier": "keyword match on job description (re-roof / roofing / roof replacement|repair...)",
            "last_issued": max((r["issued_date"][:10] for r in mine), default=None),
            "by_work_type": dict(Counter(r.get("work_type") for r in mine).most_common(6)),
            "truncated": len(rows) >= MAX_PERMIT_ROWS,
        }
        items = [self._summary(summary)]
        for r in list(roofing_jobs.values())[:MAX_PERMIT_SAMPLES]:  # newest first
            items.append(
                EvidenceItem(
                    kind="permit",
                    source=self.name,
                    external_id=f"{r['job_filing_number']}:{r.get('work_permit', '')}",
                    occurred_at=_parse_ts(r["issued_date"]),
                    payload={
                        "issued": r["issued_date"][:10],
                        "work_type": r.get("work_type"),
                        "description": (r.get("job_description") or "")[:400],
                        "estimated_cost": r.get("estimated_job_costs"),
                        "zip": r.get("zip_code"),
                    },
                )
            )
        return items

    def _summary(self, payload: dict) -> EvidenceItem:
        payload = {"dataset": "NYC DOB NOW: Build - Approved Permits", **payload}
        return EvidenceItem(kind="permit_summary", source=self.name, external_id="summary", payload=payload)
