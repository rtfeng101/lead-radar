import json
from datetime import datetime, timezone
from importlib import resources

from app.matching.types import EvidenceItem
from app.schemas import ContractorRecord

_NOT_NYC = "Contractor is based outside NYC; this source only covers NYC."


def _load() -> dict:
    return json.loads(resources.files("app.evidence").joinpath("data/fixture_evidence.json").read_text())


def _ts(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


class FixtureCollector:
    """Synthetic reviews / permits / licenses for the fixture contractors, shaped exactly like what the
    live collectors emit. Lets the full matching pipeline run offline and gives tests stable input."""

    name = "fixture"

    def collect(self, c: ContractorRecord) -> list[EvidenceItem]:
        entry = _load().get(c.source_id)
        if c.source != "fixture" or entry is None:
            return []
        items: list[EvidenceItem] = []

        lic = entry["license"]
        if lic["nyc"]:
            items.append(EvidenceItem(kind="license_search", source="fixture_dca", external_id="search", payload={
                "synthetic": True, "searched": True, "found": len(lic["items"]), "matched_by": ["phone"]}))
            for row in lic["items"]:
                items.append(EvidenceItem(kind="license", source="fixture_dca", external_id=row["license_nbr"],
                                          occurred_at=_ts(row["first_issued"]), payload={
                    "synthetic": True, "license_nbr": row["license_nbr"], "license_type": "NYC Home Improvement Contractor",
                    "status": row["status"], "first_issued": row["first_issued"], "expires": row["expires"],
                    "business_name": c.name, "match": {"method": "phone", "similarity": 1.0}}))
        else:
            items.append(EvidenceItem(kind="license_search", source="fixture_dca", external_id="search", payload={
                "synthetic": True, "searched": False, "reason": _NOT_NYC}))

        permits = entry["permits"]
        if permits["nyc"]:
            items.append(EvidenceItem(kind="permit_summary", source="fixture_dob", external_id="summary", payload={
                "synthetic": True, "searched": True, "window_months": 24, "total_jobs": permits["total_jobs"],
                "roofing_jobs": permits["roofing_jobs"], "last_issued": permits["last_issued"],
                "by_work_type": permits["by_work_type"],
                "unit_note": "a 'job' is one DOB job filing; ancillary permits share it"}))
            for i, row in enumerate(permits["samples"]):
                items.append(EvidenceItem(kind="permit", source="fixture_dob", external_id=f"job-{i}",
                                          occurred_at=_ts(row["issued"]), payload={
                    "synthetic": True, "issued": row["issued"], "work_type": row["work_type"],
                    "description": row["description"], "estimated_cost": row["cost"]}))
        else:
            items.append(EvidenceItem(kind="permit_summary", source="fixture_dob", external_id="summary", payload={
                "synthetic": True, "searched": False, "reason": _NOT_NYC}))

        for i, r in enumerate(entry["reviews"]):
            items.append(EvidenceItem(kind="review", source="fixture_reviews", external_id=f"rev-{i}",
                                      occurred_at=_ts(r["date"]), payload={
                "synthetic": True, "rating": r["rating"], "text": r["text"], "date": r["date"], "platform": r["platform"]}))
        return items
