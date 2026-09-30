# Backend architecture

What every file in `backend/` is for, and how data moves through them.

The backend is two processes that share one Postgres database:

* **API** (`app.main`, FastAPI). Reads leads for the UI, saves rep edits, and enqueues jobs. It never does slow work itself.
* **Worker** (`app.pipeline.worker`). Pulls jobs off a Postgres queue and runs the pipeline stages: scrape, score, collect evidence, ask an LLM to grade.

They never call each other. The `jobs` table is the only channel between them.

---

## 1. Data flow at a glance

```
                              WRITE PATH (worker process)
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │                                                                                          │
 │  POST /api/pipeline/runs ─► jobs: discover(zip) ──┐                                      │
 │  (or worker --seed ZIP)                            │ sources.get_source().discover()     │
 │                                                    ▼                                     │
 │                                            contractors (upsert)                          │
 │                                                    │ fan-out, one set per contractor     │
 │                          ┌─────────────────────────┼──────────────────────────┐          │
 │                          ▼                         ▼                          ▼          │
 │                    jobs: enrich            jobs: profile               jobs: collect     │
 │                          │                  (GAF source only,               ▲            │
 │                          │                   real Chrome window)            │            │
 │                          │                         │  parse_profile()       │            │
 │                          │                         ├─► contractors (address,│legal name, │
 │                          │                         │   tenure, size)        │place id)   │
 │                          │                         ├─► evidence (profile + reviews)     │
 │                          │                         ├─► jobs: enrich (again, tenure known)
 │                          │                         └─► jobs: collect ───────┘            │
 │                          ▼                                                   │           │
 │            score_contractor() + insights                    collectors: NYC licenses,    │
 │                          │                                  NYC permits, Google reviews  │
 │                          ▼                                                   ▼           │
 │                        leads                                       evidence (upsert)     │
 │              (score, priority, brief)                                        │           │
 │                                                                               ▼          │
 │                                                                        jobs: match       │
 │                                                            stage 1: reviews ─► facts     │
 │                                                            stage 2: evidence pack ─► LLM │
 │                                                            aggregate() ─► match %        │
 │                                                                               ▼          │
 │                                                                            matches       │
 └──────────────────────────────────────────────────────────────────────────────────────────┘
                                             │
                     Postgres: contractors · evidence · leads · matches · jobs
                                             │
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │                               READ PATH (API process)                                    │
 │  GET /api/leads          leads ⨝ contractors ⟕ matches ─► LeadOut (priority from match)   │
 │  GET /api/leads/{id}     one lead + its match                                            │
 │  GET /api/leads/{id}/evidence   every row a match can cite (ref = "E<id>")               │
 │  GET /api/leads/{id}/activity   queued / running / last failed jobs for the contractor   │
 │  PATCH /api/leads/{id}   writes status / assigned_to / notes (rep-owned columns only)    │
 │  POST /api/leads/{id}/refresh   enqueues profile|collect with refresh=true               │
 │  GET /api/pipeline/status       job counts by status                                     │
 └──────────────────────────────────────────────────────────────────────────────────────────┘
```

### The job chain

Every stage is a job kind. A handler does its work, then enqueues the next stage in the **same transaction** as the data it wrote, so a crash cannot leave data without its follow-up job.

| Job kind | Payload | Reads | Writes | Enqueues next |
|---|---|---|---|---|
| `discover` | `zip_code, radius_mi, refresh` | the lead source (GAF Coveo API or fixture JSON) | `contractors` (upsert) | `enrich`, plus `profile` (GAF, stale profile) or `collect` (matching on) |
| `profile` | `contractor_id, refresh` | gaf.com profile page via Chrome | `contractors` (address, legal name, tenure, size, place id), `evidence` (profile and reviews) | `enrich` again, and `collect` if matching is on |
| `enrich` | `contractor_id` | `contractors` | `leads` (score, priority, breakdown, insights) | nothing |
| `collect` | `contractor_id, refresh, manufacturer` | NYC Open Data, Google Places, fixture JSON | `evidence` (upsert and prune) | `match` |
| `match` | `contractor_id, manufacturer` | `contractors`, `evidence` | `evidence.facts`, `matches` | nothing |

Whether `collect`/`match` run at all depends on `OPENAI_API_KEY` (`settings.matching_enabled`). Without it the app still produces rule-based leads.

### Queue mechanics

```
enqueue() ─► jobs(status=queued, run_after=now) ─► claim()  FOR UPDATE SKIP LOCKED ─► status=running, attempts+1
                                                        │
                        handler succeeds ◄──────────────┴──────────────► handler raises
                              │                                               │
                     complete(): status=done                     fail(): attempts < max?
                     supersede_failed(): earlier failed                yes → status=queued, run_after = now + min(300, 2^attempts)s
                     rows for the same work → superseded               no  → status=failed (last_error kept)
```

* **At-least-once delivery.** A job stuck in `running` longer than `job_visibility_timeout_s` (300s) is re-claimed. This is safe because every handler is idempotent.
* **Dedupe keys** make retries and fan-out safe. `enqueue` returns `None` if the key already exists:
  * `enrich:{job_id}:{contractor_id}` (and `…:profile` for the second enrich)
  * `profile:{job_id}:{contractor_id}`, `collect:{job_id}:{contractor_id}`
  * `match:{job_id}:{contractor_id}:{evidence_fingerprint}`: a `collect` retry that fetched new evidence gets a new fingerprint and so a new match job. One that found nothing new is a no-op.
  * `refresh:{contractor_id}:{timestamp}`: unique on purpose, so refresh always enqueues.
* **Worker filtering.** A worker only claims job kinds it has a handler for (`HANDLERS`). During a rolling deploy an old worker leaves a new kind queued instead of failing it. `--kinds profile` runs a dedicated browser worker.

---

## 2. Who owns which data

The schema separates what the outside world says, what the system derived, and what reps typed, so that re-running the pipeline can never overwrite rep work.

| Table | Owner | Overwritten on re-run? | Notes |
|---|---|---|---|
| `contractors` | pipeline | Yes. `discover` refreshes it and `profile` fills more in | Unique on `(source, source_id)`. `PROFILE_OWNED` fields are never blanked by a re-discover that doesn't know them. `raw` keeps the untouched source payload |
| `evidence` | pipeline | Upserted on `(contractor_id, source, external_id)` | `content_hash` detects change. A changed payload resets `facts` so the LLM re-reads it |
| `leads` (`score`, `priority`, `score_breakdown`, `insights`, `insights_model`, `generated_at`) | pipeline (`enrich`) | Yes | Rule-based fit score and brief |
| `leads` (`status`, `assigned_to`, `notes`) | **reps** (`PATCH`) | **Never touched by the pipeline** | Same row as the score, different columns |
| `matches` | pipeline (`match`) | Only if `input_hash` changed | One row per `(contractor, manufacturer)`. `input_hash` is the LLM cache key |
| `jobs` | queue | n/a | Statuses: `queued`, `running`, `done`, `failed`, `superseded` |

### Two scores, one priority

* `leads.score` and `leads.priority` come from `pipeline/scoring.py` (rule-based, 0-100, hot at 80+, warm at 55+).
* `matches.match_pct` comes from the LLM rubric (0-100).
* When a match exists, the API's `priority` comes from the **match**, banded by `matching/bands.py` (hot at 75+, warm at 50+). The rule-based priority is only the fallback. `api/leads.py` applies the same rule in SQL (`EFFECTIVE_PRIORITY`, for filtering) and in Python (`_out`, for the response).

---

## 3. The matching pipeline in detail

`handlers.collect` gathers evidence, then `match` → `matching/engine.run_match` does the LLM work in three short DB phases (no DB connection is held during LLM calls):

```
evidence rows ──► evidence_fingerprint() ──► equals matches.input_hash? ──yes──► "skipped" (no LLM spend)
                  (rubric + judge model +           │ no
                   prompt version + listing +        ▼
                   evidence content hashes)   ① STAGE 1  extract.extract_review_facts()
                                                 reviews with facts IS NULL ─► cheap model, batches of 15
                                                 ─► ReviewFacts (closed enums: sentiment, project kind,
                                                    complaints, praises, brands) saved to evidence.facts
                                                    │
                                              ② features.build_pack()
                                                 evidence + listing ─► compact "evidence pack"
                                                 (averages, counts, tallies computed in code; each source
                                                  marked collected / searched-none / not_searched / not_collected)
                                                 also returns the set of ids the judge may cite ("E12", "LISTING")
                                                    │
                                              ③ STAGE 2  judge.judge()
                                                 rubric prompt + pack ─► strong model ─► per-criterion score,
                                                 confidence, rationale, cited ids, plus summary / gaps / next action
                                                    │
                                              ④ aggregate.aggregate()
                                                 drop hallucinated citations, cap uncited confidence at 0.3,
                                                 clamp ranges, weighted mean of scored criteria ─► match_pct
                                                    │
                                                    ▼
                                              matches row (input_hash = fingerprint)
```

Design choices that show up across these files:

* **The LLM never produces the headline percentage.** `aggregate.py` computes it from criterion scores and the rubric weights, so the percentage is reproducible from the stored criteria. Note that `evidence_fingerprint` hashes the whole rubric including weights, so editing a weight currently invalidates the match cache and re-runs stage 2 (stage 1 facts stay cached).
* **Unknown ≠ zero.** A criterion the evidence cannot score is left out of `match_pct`, but counts as zero confidence.
* **Reviews are untrusted.** Stage 1 outputs only closed enums, so a review cannot pass instructions on to stage 2.
* **Vendor isolation.** `matching/*` calls `llm.get_llm()` and never imports the OpenAI SDK. Tests patch `get_llm` with a fake.

---

## 4. Caching and freshness

| What | Where | Rule |
|---|---|---|
| GAF profile page | `evidence` rows with source `gaf_profile` | Reused for `profile_ttl_days` (14). `discover` skips enqueuing `profile` while fresh |
| NYC permits, licenses, Google reviews | `evidence.fetched_at`, per collector's `evidence_sources` | `collect` skips a collector for `evidence_ttl_days` (7) |
| Review facts (stage 1) | `evidence.facts` | Extracted once per distinct review text. Reset only when the payload hash changes |
| Match (stage 2) | `matches.input_hash` | Re-judged only if the rubric, judge model, prompt version, listing, or any evidence changed |
| GAF search token | `.cache/gaf_coveo_token.json` | Reused until GAF rejects it (401/403), then re-captured once via the browser |
| ZIP geocode | in-memory `lru_cache` | Per process |
| Insights brief | not cached | `enrich` regenerates it every time it runs. With `INSIGHTS_BACKEND=claude`, each `enrich` is one Claude call |

`refresh=true` (from `POST /runs` with `refresh`, `worker --seed --refresh`, or `POST /leads/{id}/refresh`) bypasses the two freshness windows. It does **not** bypass the match cache.

---

## 5. File reference

### Top level

| File | Purpose |
|---|---|
| `pyproject.toml` | Package `leadgen-backend`: dependencies (FastAPI, SQLAlchemy, psycopg, Alembic, pydantic-settings, openai, anthropic, httpx, playwright), dev tools (pytest, ruff), and `package-data` so the bundled JSON files ship with the package |
| `alembic.ini` | Alembic config. The database URL is not here; `migrations/env.py` injects it from `settings` |
| `.env.example` | Documented template for `.env`, with the switches in section 6 |
| `.env` | Real local config and API keys. **Gitignored** |
| `.cache/gaf_coveo_token.json` | Cached GAF search token (mode 0600). **Gitignored** |
| `leadgen_backend.egg-info/`, `.pytest_cache/` | Generated by `pip install -e` and pytest. Not source. **Gitignored** |

### `app/`: shared foundation

| File | Purpose | Depends on |
|---|---|---|
| `main.py` | Creates the FastAPI app, mounts the two routers, and serves `GET /api/health` | `api.*` |
| `config.py` | `Settings` (pydantic-settings, reads `.env`) and the `settings` singleton. Holds every switch and tuning knob: lead/evidence sources, TTLs, model names, queue limits. `matching_enabled` is `bool(openai_api_key)` | none |
| `db.py` | SQLAlchemy `engine`, `SessionLocal`, and the `get_session` FastAPI dependency. `SessionLocal` is looked up at call time so tests can swap it | `config` |
| `models.py` | The five tables: `Contractor`, `Evidence`, `Match`, `Lead`, `Job`. The module docstring explains the ownership split | none |
| `schemas.py` | Pydantic shapes. `ContractorRecord` is the source-agnostic contractor that every `LeadSource` yields and that scoring, insights, and collectors consume. The rest are API request/response models (`LeadOut`, `MatchOut`, `EvidenceOut`, `LeadPatch`, `RunRequest`, `PipelineStatus`, `LeadActivity`…) | `matching.types` |
| `llm.py` | Structured-output wrapper: `LLM` protocol, `OpenAILLM.parse()` (Responses API), `get_llm()`, and the errors `LLMNotConfigured`/`LLMRefused`. The only place the OpenAI SDK is imported | `config` |

### `app/api/`: HTTP layer (API process)

| File | Purpose | Depends on |
|---|---|---|
| `leads.py` | `/api/leads` router. List with filters, sort, and pagination (joins `Lead`, `Contractor`, and `Match` for the requested manufacturer). Get one, list its evidence, report activity, enqueue a per-lead refresh, and `PATCH` rep fields. Contains `EFFECTIVE_PRIORITY`, which lets the match override the rule-based priority. Only shows contractors from the configured `LEAD_SOURCE` | `db`, `models`, `schemas`, `pipeline.queue`, `matching.bands` |
| `pipeline.py` | `/api/pipeline` router. `POST /runs` enqueues a `discover` job and returns 202. `GET /status` counts jobs by status | `db`, `models`, `schemas`, `pipeline.queue` |

### `app/pipeline/`: job system and rule-based stages (worker process)

| File | Purpose | Depends on |
|---|---|---|
| `queue.py` | The Postgres queue: `enqueue` (savepoint-protected dedupe), `claim` (`SKIP LOCKED`, orphan recovery, max-attempts guard), `complete`, `fail` (exponential backoff), `supersede_failed`, `contractor_jobs`. Used by both the worker and the API | `config`, `models` |
| `worker.py` | Entry point and CLI (`--seed`, `--drain`, `--kinds`, `--refresh`, `--radius`). `run_once` claims a job, dispatches to `HANDLERS[kind]`, then calls `complete` or `fail`. Finishes the current job on SIGTERM/SIGINT | `db`, `queue`, `handlers` |
| `handlers.py` | The five stage functions (`discover`, `profile`, `enrich`, `collect`, `match`) and the `HANDLERS` registry. This is the orchestration hub: it decides what each stage enqueues next, applies the freshness checks, and keeps DB sessions short | almost everything below |
| `scoring.py` | `score_contractor()`: transparent 0-100 fit score. Factors: certification tier (35), review volume on a log scale (25), rating (15), proximity (15), tenure (10). Returns `ScoreResult` with a per-factor breakdown | `schemas` |
| `insights.py` | Account-planning brief generators behind one protocol: `HeuristicInsights` (templates, free, default) and `ClaudeInsights` (Anthropic structured output, falls back to heuristic on non-retryable errors). `get_generator()` picks one from `INSIGHTS_BACKEND` | `config`, `scoring`, `schemas` |

### `app/pipeline/sources/`: where contractors come from

| File | Purpose |
|---|---|
| `base.py` | `LeadSource` protocol: `discover(zip, radius) -> Iterable[ContractorRecord]`. A new source (LinkedIn, Google Maps…) only needs to implement this |
| `__init__.py` | `get_source()` factory: `GafSource` if `LEAD_SOURCE=gaf`, else `FixtureSource` |
| `fixture.py` | Offline source that serves `data/gaf_10013.json` (synthetic contractors), filtered by radius |
| `data/gaf_10013.json` | The synthetic contractor list (`fx-001`…) |
| `gaf.py` | Live GAF directory. Geocodes the ZIP (zippopotam.us), gets the Coveo search token (env, then disk cache, then browser), pages through the Coveo API 100 at a time, refreshes a rejected token once, maps each result to a `ContractorRecord` (`to_record`, `cert_tier`), dedupes, and sorts nearest-first |
| `gaf_browser.py` | One shared, visible Chrome window (Playwright) for gaf.com, which blocks plain HTTP and headless clients. `fetch_html(url)` loads a profile page. `capture_coveo_token()` sniffs the search token from the directory page's own requests. Throttled, thread-locked, and raises `GafBlocked` on Akamai 403s so the queue retries |
| `gaf_profile.py` | Pure HTML → facts parser (no network). `parse_profile()` reads JSON-LD (legal name, address, phone), the details block (years in business, employees), the `data-reviews` attribute (reviews, keeping only the author's initial), and the Google Place id. `to_evidence()` turns a profile into `EvidenceItem`s. Raises `ProfileParseError` on non-profile pages so the job retries |

### `app/evidence/`: outside facts about a contractor

| File | Purpose |
|---|---|
| `base.py` | `Collector` protocol: `collect(ContractorRecord) -> list[EvidenceItem]`. Must emit a `*_summary`/`*_search` item saying whether the source was searched, and must raise on transient errors. Optional `evidence_sources` opts into the TTL skip |
| `__init__.py` | `get_collectors()`: `[FixtureCollector]` for `EVIDENCE_SOURCE=fixture`. For `live`, the NYC license and permit collectors, plus `GooglePlacesCollector` if a key is set |
| `fixture.py` | Synthetic reviews, permits, and licenses keyed by fixture `source_id`. Same shape as the live collectors, so the whole pipeline runs offline |
| `data/fixture_evidence.json` | The synthetic evidence data (all invented) |
| `nyc.py` | Live NYC Open Data (Socrata). `NycLicenseCollector` (DCA home-improvement licenses: match by phone, then by name similarity) and `NycPermitCollector` (DOB NOW permits from the last 24 months: name match only, counts distinct job filings, classifies roofing work by keyword). Contractors outside NYC get an explicit "not searched" item |
| `google_places.py` | `GooglePlacesCollector`: up to 5 Google reviews per business via Places API (New), using the Place id scraped from the GAF profile. Only registered when `GOOGLE_PLACES_API_KEY` is set |
| `reviews.py` | Docstring only: notes on why a bulk review source is not implemented and what it would need to emit |
| `util.py` | Helpers for entity resolution: NYC ZIP check, phone normalization and variants, business-name normalization and similarity, `search_prefix` for cheap `LIKE` pre-filters, and the roofing-work regex `is_roofing` |

### `app/matching/`: LLM grading of contractor ↔ manufacturer fit

| File | Purpose | Depends on |
|---|---|---|
| `types.py` | Shared shapes. `EvidenceItem` (what collectors emit). Stage 1 output (`ReviewFacts`, `ReviewBatch`). Stage 2 output (`Judgement`, `CriterionJudgement`). Final result (`MatchResult`, `CriterionResult`). LLM-facing models avoid defaults and numeric bounds because OpenAI strict mode rejects them | none |
| `rubric.py` | `Rubric` model with validation (weights > 0, unique ids) and `load_rubric(manufacturer)`, which reads and caches `rubrics/<name>.json` | none |
| `rubrics/gaf.json` | The GAF rubric (marked DRAFT): `product_fit` 25, `volume` 20, `reputation` 20, `compliance` 20, `brand_alignment` 10, `stability` 5. Each has a high/mid/low guide. Adding a manufacturer means adding a JSON file here | n/a |
| `extract.py` | Stage 1. Batches reviews to the cheap model, returns `ReviewFacts` by evidence id. Normalizes and clamps brand names (Timberline → GAF). System prompt tells the model to treat review text as untrusted | `llm` |
| `features.py` | Builds the evidence pack the judge reads. De-duplicates reviews seen on both GAF and Google, computes rating averages, sentiment/complaint tallies, recency counts, permit and license summaries with explicit coverage caveats, and picks excerpts. Also returns the set of citable ids | `models`, `schemas` |
| `judge.py` | Stage 2. Builds the system prompt from the rubric and calls the strong model with the pack. `PROMPT_VERSION` is part of the cache fingerprint, so bump it when the prompt or schema changes | `llm`, `rubric` |
| `aggregate.py` | Turns a `Judgement` into a `MatchResult`: citation validation, confidence cap for uncited scores (0.3), clamping, weighted mean over scored criteria, weight-averaged confidence over all criteria | `rubric`, `types` |
| `bands.py` | `HOT_MIN=75`, `WARM_MIN=50`, `band()`. The frontend mirrors these numbers in `LeadTable.tsx` | none |
| `engine.py` | Orchestrates matching. `store_evidence()` is the idempotent evidence upsert (also prunes rows a reporting source no longer returns). `evidence_fingerprint()` is the cache key. `run_match()` runs the three phases and writes the `Match` row | `extract`, `features`, `judge`, `aggregate`, `rubric`, `models`, `db` |

### `migrations/`: Alembic

| File | Purpose |
|---|---|
| `env.py` | Wires Alembic to `settings.database_url` and `Base.metadata` |
| `script.py.mako` | Template for new revisions |
| `versions/0001_initial_schema.py` | `contractors`, `leads`, `jobs` |
| `versions/0002_evidence_and_matches.py` | `evidence` and `matches` tables |
| `versions/0003_match_next_action.py` | `matches.next_action` |
| `versions/0004_contractor_legal_name_and_employees.py` | `contractors.legal_name`, `contractors.employees` |
| `versions/0005_supersede_resolved_failures.py` | Data-only: marks already-resolved failed jobs as `superseded` (same rule as `queue.supersede_failed`) |
| `versions/0006_contractor_google_place_id.py` | `contractors.google_place_id` |

### `tests/`

Tests run on SQLite with no network and no real LLM.

| File | Covers |
|---|---|
| `conftest.py` | Forces offline sources and clears API keys for every test, makes launching the GAF browser an error, and provides `fake_llm` and a `client` fixture (fresh SQLite DB, swapped into `db.SessionLocal`) |
| `fakes.py` | `FakeLLM` (deterministic, scores by average rating, counts calls per schema) and `gaf_profile_html()` (synthetic profile page) |
| `test_pipeline.py` | End-to-end run, idempotent re-run that keeps rep edits, retry/backoff, dedupe keys |
| `test_scoring.py` | Rule-based score and priority |
| `test_matching.py` | Full match run, extract-once, cache hits and invalidation, gaps versus bad scores, priority from match |
| `test_aggregate.py` | Weighted mean, unknown criteria, confidence cap, hallucinated citations, clamping |
| `test_features.py` | Recency reporting and the permit lower-bound label in the evidence pack |
| `test_evidence.py` | Roofing classifier, NYC ZIPs, phone and name matching, NYC collectors (mocked HTTP), `store_evidence` idempotency and pruning |
| `test_google_places.py` | Review mapping, missing or stale place id, error propagation, registration, cross-source dedupe |
| `test_gaf_source.py` | Coveo record mapping, paging, token refresh order, error handling |
| `test_gaf_profile.py` | Profile HTML parsing and evidence conversion |
| `test_gaf_pipeline.py` | The `discover → profile → enrich/collect → match` chain with a fake browser, re-run freshness, worker `--kinds` filtering, Akamai retry |
| `test_refresh.py` | Failure superseding, per-lead refresh, in-flight guard, activity endpoint |

---

## 6. Config switches that change the data flow

| Setting (`.env`) | Effect |
|---|---|
| `LEAD_SOURCE=fixture` (default) / `gaf` | Where `discover` gets contractors. `gaf` also turns on the `profile` stage and the Chrome window. The API only lists contractors from this source |
| `EVIDENCE_SOURCE=fixture` (default) / `live` | Which collectors `collect` runs. `live` means NYC licenses and permits, plus Google reviews if `GOOGLE_PLACES_API_KEY` is set |
| `OPENAI_API_KEY` | **Turns on `collect` and `match`.** Unset means no evidence, no match %, and leads sorted by rule-based score |
| `INSIGHTS_BACKEND=heuristic` (default) / `claude` | Template brief versus a Claude-written one. `claude` needs `ANTHROPIC_API_KEY` |
| `DISCOVER_LIMIT` | Keep only the nearest N contractors per `discover`, for cost control while developing |
| `PROFILE_TTL_DAYS` / `EVIDENCE_TTL_DAYS` | Freshness windows before re-fetching profile pages or collector data |
| `EXTRACTION_MODEL` / `JUDGE_MODEL` | Stage 1 and stage 2 models. Changing the judge model invalidates the match cache |
| `JOB_MAX_ATTEMPTS`, `JOB_VISIBILITY_TIMEOUT_S`, `WORKER_POLL_INTERVAL_S` | Queue retry limit, orphan timeout, and idle poll interval |

---

## 7. Worked example: one run, start to finish

Setup: `LEAD_SOURCE=gaf`, `EVIDENCE_SOURCE=live`, `OPENAI_API_KEY` set. A rep starts a run for ZIP 10013.

1. `POST /api/pipeline/runs {"zip_code":"10013"}` → `pipeline.start_run` → `queue.enqueue("discover")` → **202** with a `job_id`. The API is done.
2. The worker's `run_once` claims the job (`queue.claim`) and calls `handlers.discover`.
3. `GafSource.discover` geocodes the ZIP, obtains the Coveo token, pages the directory, and returns `ContractorRecord`s. This happens outside any DB session.
4. For each record, `discover` upserts a `contractors` row and enqueues `enrich` and `profile` (or `collect` if the profile is still fresh). All of it commits in one transaction.
5. `profile` jobs are picked up (ideally by a `--kinds profile` worker). Chrome loads the page, `parse_profile` extracts the facts, and the handler updates `contractors` and stores evidence. It enqueues `enrich` (now with tenure) and `collect`.
6. `enrich` calls `score_contractor` and the insight generator, then upserts the `leads` row without touching `status`, `assigned_to`, or `notes`.
7. `collect` runs the NYC license, NYC permit, and Google collectors (skipping any inside the 7-day window), upserts `evidence`, computes the fingerprint, and enqueues `match`. If a collector failed, the job still enqueues `match` and then raises, so the queue retries only the missing source.
8. `match` → `run_match`: skip if the fingerprint is unchanged. Otherwise extract facts for new reviews, build the pack, judge, aggregate, and upsert `matches`.
9. Meanwhile the UI polls `GET /api/leads`. Cards appear as `leads` rows land, and a match % and priority appear once `matches` rows land. `GET /api/leads/{id}/activity` shows what is still queued or running for a contractor.
10. A rep calls `PATCH /api/leads/{id}` to set status or notes. A later re-run of the whole pipeline leaves those columns alone.
