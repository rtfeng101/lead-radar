from app.config import settings
from app.pipeline.sources.base import LeadSource


def get_source() -> LeadSource:
    if settings.lead_source == "gaf":
        from app.pipeline.sources.gaf import GafSource

        return GafSource()
    from app.pipeline.sources.fixture import FixtureSource

    return FixtureSource()
