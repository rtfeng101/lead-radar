from app.config import settings
from app.evidence.base import Collector


def get_collectors() -> list[Collector]:
    if settings.evidence_source == "live":
        from app.evidence.nyc import NycLicenseCollector, NycPermitCollector

        collectors: list[Collector] = [NycLicenseCollector(), NycPermitCollector()]
        if settings.google_places_api_key:  # otherwise the only reviews are the 3 GAF shows (see evidence/reviews.py)
            from app.evidence.google_places import GooglePlacesCollector

            collectors.append(GooglePlacesCollector())
        return collectors
    from app.evidence.fixture import FixtureCollector

    return [FixtureCollector()]
