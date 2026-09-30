import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app import llm as llm_mod
from app.config import settings
from app.models import Base
from tests.fakes import FakeLLM


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """backend/.env is the developer's real config (live GAF, real API keys). Tests must never inherit it: force
    the offline sources and make any attempt to open a browser a loud failure."""
    monkeypatch.setattr(settings, "lead_source", "fixture")
    monkeypatch.setattr(settings, "evidence_source", "fixture")
    monkeypatch.setattr(settings, "discover_limit", None)
    monkeypatch.setattr(settings, "insights_backend", "heuristic")
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "gaf_coveo_token", None)

    def no_browser():
        raise AssertionError("a test tried to launch the GAF browser; inject a fake instead")

    for target in ("app.pipeline.sources.gaf_browser.get_browser", "app.pipeline.sources.gaf.get_browser",
                   "app.pipeline.handlers.get_browser"):
        monkeypatch.setattr(target, no_browser)


@pytest.fixture()
def fake_llm(monkeypatch) -> FakeLLM:
    """Every test gets the fake: nothing in the suite can reach OpenAI or spend money."""
    fake = FakeLLM()
    monkeypatch.setattr(llm_mod, "get_llm", lambda: fake)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")  # turns on collect/match fan-out
    monkeypatch.setattr(settings, "evidence_source", "fixture")
    return fake


@pytest.fixture()
def client(tmp_path, monkeypatch, fake_llm):
    """Fresh SQLite DB per test, wired in wherever the app looks up SessionLocal."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(engine, expire_on_commit=False))
    from app.main import app

    return TestClient(app)
