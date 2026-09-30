import json
from collections.abc import Iterable
from importlib import resources

from app.schemas import ContractorRecord


class FixtureSource:
    """Serves synthetic sample data shaped like a GAF results page. Lets the whole stack run offline
    and gives tests something deterministic. The businesses in the JSON are made up."""

    name = "fixture"

    def discover(self, zip_code: str, radius_mi: int) -> Iterable[ContractorRecord]:
        data = json.loads(
            resources.files("app.pipeline.sources").joinpath("data/gaf_10013.json").read_text()
        )
        for row in data:
            if row.get("distance_mi") is None or row["distance_mi"] <= radius_mi:
                yield ContractorRecord(source=self.name, raw=row, **row)
