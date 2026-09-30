from typing import Protocol

from app.matching.types import EvidenceItem
from app.schemas import ContractorRecord


class Collector(Protocol):
    """One implementation per outside source of facts about a contractor (reviews, permits, licenses...).

    Contract: return every item you can find, plus a *_summary / *_search item that says whether the
    source was searched at all. "Searched, found nothing" and "cannot search this jurisdiction" mean
    very different things to the scorer. Raise on transient failures so the job queue retries."""

    name: str
    # Optional: the `Evidence.source` values this collector writes. If set, a re-run within
    # `settings.evidence_ttl_days` skips the collector (no network) and reuses what is stored.
    evidence_sources: tuple[str, ...]

    def collect(self, contractor: ContractorRecord) -> list[EvidenceItem]: ...
