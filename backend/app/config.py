from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://leadgen:leadgen@localhost:5432/leadgen"

    lead_source: Literal["fixture", "gaf"] = "fixture"
    default_zip: str = "10013"
    default_radius_mi: int = 25
    discover_limit: int | None = None  # cap contractors taken per run (nearest first); cost control while developing

    # Freshness windows: data fetched more recently than this is reused instead of re-fetched on later runs.
    # Graded matches are separately cached on a hash of their inputs, so they never re-run unless inputs change.
    profile_ttl_days: float = 14  # GAF profile page: address, tenure, reviews
    evidence_ttl_days: float = 7  # NYC permits / licenses / Google reviews

    # Google Places API (New). When set, live runs also collect Google reviews (max 5 per place; see evidence/google_places.py).
    google_places_api_key: str | None = None

    # Live GAF scraping. GAF's site sits behind Akamai bot protection, which rejects headless browsers and plain
    # HTTP clients, so pages are fetched with a real, visible Chrome window (see pipeline/sources/gaf_browser.py).
    gaf_browser_channel: str = "chrome"
    gaf_page_delay_s: float = 1.5  # minimum pause between page loads in one browser
    gaf_coveo_token: str | None = None  # optional override of the search token the GAF site ships to browsers
    gaf_token_cache: str = ".cache/gaf_coveo_token.json"

    # Explicit opt-in so nobody gets surprise LLM spend from a default run.
    insights_backend: Literal["heuristic", "claude"] = "heuristic"
    insights_model: str = "claude-opus-5"
    anthropic_api_key: str | None = None  # falls back to ANTHROPIC_API_KEY / `ant auth login` profile

    # Manufacturer <-> contractor matching (OpenAI). Matching jobs are only enqueued when a key is set.
    openai_api_key: str | None = None
    extraction_model: str = "gpt-5.4-mini"  # stage 1: reviews -> structured facts (bulk, cheap)
    judge_model: str = "gpt-5.4"  # stage 2: rubric scoring with cited evidence
    default_manufacturer: str = "gaf"
    evidence_source: Literal["fixture", "live"] = "fixture"
    nyc_open_data_app_token: str | None = None  # optional; raises Socrata's rate limit

    # Job queue
    job_max_attempts: int = 5
    job_visibility_timeout_s: int = 300  # a "running" job older than this is presumed orphaned
    worker_poll_interval_s: float = 1.0

    @property
    def matching_enabled(self) -> bool:
        return bool(self.openai_api_key)


settings = Settings()
