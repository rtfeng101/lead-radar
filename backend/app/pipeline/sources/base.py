from collections.abc import Iterable
from typing import Protocol

from app.schemas import ContractorRecord


class LeadSource(Protocol):
    """One implementation per external data source. Adding LinkedIn / Google Maps / state license
    boards later means adding a class here - nothing downstream changes."""

    name: str

    def discover(self, zip_code: str, radius_mi: int) -> Iterable[ContractorRecord]:
        """Yield every contractor the source lists within radius_mi of zip_code."""
        ...
