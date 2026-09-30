# Lead Radar

AI-assisted sales intelligence for a roofing distributor: find GAF-certified contractors near a ZIP
code, score how good a customer each would be, and pre-write an account-planning brief per lead.

```
 LeadSource ──► discover ──► contractors ──┬─► enrich ──► leads (rule-based fit + brief)
 (GAF / fixture)                           │
                                           └─► collect ──► evidence ──► match ──► LLM rubric score (GAF match %)
                                               reviews · permits · licenses        │
                    all stages are jobs on one Postgres queue (SKIP LOCKED)       ▼
                                                                          FastAPI ──► React UI
```

## Run it

Each block below is one terminal and contains no `#` comments, so it pastes cleanly into zsh (interactive zsh
does not treat `#` as a comment and chokes on lines like `cmd  # note (with parens)`).

**One-time setup** (from the repo root). `cp -n` never overwrites an existing `.env`, which holds your API keys. Postgres runs in Docker on host port **5433**, so it cannot collide with
a Postgres already installed on your machine at 5432:

```bash
docker compose up -d --wait db
cd backend
python3 -m venv --clear .venv
.venv/bin/pip install -e ".[dev]"
cp -n .env.example .env
.venv/bin/alembic upgrade head
```

**Terminal 1: worker.** `--seed` queues a run for ZIP 10013, then the worker keeps polling for more jobs:

```bash
cd backend
.venv/bin/python -m app.pipeline.worker --seed 10013
```

**Terminal 2: API** on :8000:

```bash
cd backend
.venv/bin/uvicorn app.main:app --reload
```

**Terminal 3: UI** on :5173:

```bash
cd frontend
pnpm install
pnpm dev
```

To turn on evidence collection and GAF matching, add `OPENAI_API_KEY=...` to `backend/.env` before starting the
worker. Without it the app still runs, but leads have no match %.

Stop the database with `docker compose down` (add `-v` to also delete its data).

**Troubleshooting**
- `password authentication failed for user "leadgen"`: the app is talking to some other Postgres. `DATABASE_URL` in
  `backend/.env` must use port **5433**, and `docker compose ps` must show `db` as healthy.
- `KeyError: 'profile'` (or another job kind) in a worker: that worker is running old code; restart it.
- After pulling new code, run `.venv/bin/alembic upgrade head` (schema and data migrations).
- `matching_enabled` is false / no match %: `OPENAI_API_KEY` is missing from `backend/.env`.
- `zsh: unknown sort specifier` / `command not found: #`: a commented command was pasted into zsh; drop the comment.

Tests (no infra needed, uses SQLite): `cd backend && .venv/bin/python -m pytest`

By default the app uses **synthetic fixture data** (`LEAD_SOURCE=fixture`, `EVIDENCE_SOURCE=fixture`) and **template
insights**, so it runs offline. Matching is **off until `OPENAI_API_KEY` is set**; the tests never call OpenAI
(they use a fake). See "Status" below.

## Status: what is real vs. stubbed

| Piece | State |
|---|---|
| Postgres schema + Alembic migration | Working, verified on Postgres 18 |
| Job queue (retries, backoff, dedupe, crash recovery) | Working; verified with 3 concurrent workers, no double-processing |
| Idempotent re-runs that preserve rep notes/status | Working, covered by tests |
| Scoring (transparent 0-100, per-factor breakdown) | Working; weights are a first guess |
| REST API + React UI (filter, sort, detail, notes, status, copy brief, j/k nav) | Working; the list shows only the configured `LEAD_SOURCE`, so demo data never mixes with real |
| **Manufacturer match** (evidence -> extract -> judge -> weighted %) | Working end to end, run against real OpenAI models on the fixtures (12 contractors, ~45 s, 3 workers). Cached by evidence hash, cited, with confidence. |
| Evidence: NYC DOB permits + DCA licenses | Working against live NYC Open Data (checked on real roofing companies). NYC only. |
| Evidence: reviews | **Real, but thin**: the 3 Google reviews GAF shows on each contractor's profile page (with rating and date). GAF also gives the full rating and review count, which the judge weighs more heavily. No source yet for a contractor's full review history; see `evidence/reviews.py`. |
| GAF rubric (`matching/rubrics/gaf.json`) | **Draft**, from a general understanding of GAF's programs, not verified against GAF's terms. |
| **GAF data** (`LEAD_SOURCE=gaf`) | **Real.** Directory: all GAF residential contractors within a radius of any ZIP (82 within 25 mi of 10013), via the same search index gaf.com's own page uses. Profile pages: address, registered business name, tenure, size, reviews. Needs a visible Chrome window (Akamai blocks headless). See "Real data" below. |
| **Claude-written insights** | Written (`ClaudeInsights`, structured output) but **not yet run against the live API** - no key was available while scaffolding. Set `INSIGHTS_BACKEND=claude` + `ANTHROPIC_API_KEY`. |
| Auth, multi-rep ownership, territories | Not started |

## How this maps to the brief

**Intuitive UI** - `frontend/`. Ranked list with a colour-coded fit score; the detail panel leads with
the summary and *next best action*, then talking points, watch-outs, and a "why this score" breakdown so
reps can trust (or challenge) the ranking. Notes and status write straight back.

**Robust data management** - `backend/app/models.py`. Source data (`contractors`), derived output
(`leads`) and rep-owned state are separated so re-scoring never clobbers a rep's work. `(source, source_id)`
is unique, so re-scrapes upsert. The raw source payload is kept in `contractors.raw` so parsing bugs are
fixed by reprocessing, not re-scraping. Schema changes go through Alembic.

**Scalable pipeline** - `backend/app/pipeline/`. Work is split into small idempotent jobs
(`discover` fans out to one `enrich` per contractor) on a Postgres queue using `FOR UPDATE SKIP LOCKED`,
so throughput scales by starting more workers. Delivery is at-least-once with exponential backoff and
crash recovery (orphaned `running` jobs are re-claimed after a visibility timeout). Enrichment reads,
computes, then writes in separate short transactions so slow LLM calls never hold a DB connection.

## Real data

Set these in `backend/.env` (they are documented in `.env.example`):

```
LEAD_SOURCE=gaf
EVIDENCE_SOURCE=live
DISCOVER_LIMIT=5
```

`DISCOVER_LIMIT` takes only the nearest N contractors per run; remove it for the full ~80. Then run the worker and
API as above. **A Chrome window opens while the worker runs**; leave it alone (it closes when the worker exits).

What happens on a run for a ZIP:

1. **Directory** (`sources/gaf.py`): the ZIP is geocoded, then GAF's search index is queried for every contractor
   within the radius. The search token GAF ships to every visitor's browser is captured once by the browser, cached
   in `backend/.cache/` (git-ignored, mode 0600), and re-captured automatically if rejected. Note gaf.com centers its
   own search on your IP location, not the ZIP typed in; we search from the ZIP.
2. **Profile pages** (`sources/gaf_profile.py`, one `profile` job per contractor, throttled to ~1 page per 1.5 s):
   address, **registered business name**, years in business, size band, and the reviews shown. The registered name
   matters: permits and licenses are filed under it (e.g. "KNA Roofing" files as "KNA Construction Group LLC").
3. **NYC evidence** (`evidence/nyc.py`): DCA licenses (matched by phone, then name) and DOB permits (by name), for
   contractors located in NYC. Everyone else is recorded as *not searched*, which lowers confidence rather than the score.
4. **Google reviews** (optional, `evidence/google_places.py`): set `GOOGLE_PLACES_API_KEY` (Google Cloud, Places API
   (New) enabled) and each contractor's Google Place ID, read from the profile page's "See All Reviews" link, is used
   to fetch up to **5** reviews. That is Google's cap per place, chosen by its own ranking rather than newest-first; more
   needs a review-data vendor. Without a key nothing changes: only the 3 reviews GAF shows are used.
5. **Match**, as described below.

**Read this before relying on it**
- **GAF's terms.** Akamai on gaf.com rejects plain HTTP clients and headless browsers, so we use a real, visible
  Chrome; we do not spoof fingerprints or rotate proxies. Whether automated access to gaf.com and reuse of the
  reviews it displays is permitted has not been checked (its robots.txt and terms were unreachable to us). For a
  product, get a data agreement with GAF; this code is a prototype of what that feed would carry.
- **Needs a display.** Fine on a laptop; on a server it needs Xvfb or, better, the agreement above. The browser is
  used only by `profile` jobs, so it can be its own small worker pool: `python -m app.pipeline.worker --kinds profile`.
- **Reviews are a small sample** (3 per contractor, up to 8 distinct with Google Places). Reviewer names are not stored, only an initial.
- **Permit counts are filings, not jobs done.** They only see work filed under that name, and many residential
  re-roofs are never filed at all, so a working contractor can show zero (KNA Roofing does). The judge treats a permit
  count as a lower bound and reads recent review dates as the signal of current activity.
- **After pulling new code, restart your workers.** A worker started before an update does not know newer job
  kinds; workers now leave kinds they cannot handle for up-to-date ones instead of failing them.

## What is saved, and what a re-run redoes

Everything is written to Postgres as each contractor's job finishes (not in a batch at the end), and the UI only
ever reads from the database. Two layers keep a re-run from redoing work:

| Layer | Stored in | Re-run behavior |
|---|---|---|
| Grades (`matches`: %, confidence, per-criterion scores, cited evidence, next action, model, `generated_at`) | `matches`, with an `input_hash` of rubric + judge model + prompt version + all evidence | Re-graded **only if the hash changes**. Same inputs: zero LLM calls. |
| Review facts extracted by the cheap model | `evidence.facts` | Extracted once per distinct review text. |
| Fetched data (GAF profile, NYC permits/licenses) | `evidence` (`fetched_at`, `content_hash`) | Reused inside a **freshness window** (`PROFILE_TTL_DAYS`=14, `EVIDENCE_TTL_DAYS`=7); no page load or API call. Past the window it is re-fetched, and if the content is unchanged the grade is still reused. |

To ignore the windows and re-fetch data, there are three ways:
- **Re-fetch all** (top bar): every contractor near the ZIP. Asks for confirmation; it reloads every GAF page, so it
  takes a few minutes. Also available as `"refresh": true` on `POST /api/pipeline/runs`, or `--seed 10013 --refresh`.
- **Refresh this contractor** (bar pinned to the bottom of a contractor's panel): re-fetches just that one and shows
  progress ("Queued" if no worker is running, "Refreshing...", then "Updated just now"). API: `POST /api/leads/{id}/refresh`
  and `GET /api/leads/{id}/activity`. Re-fetching still does not re-grade
anything whose data did not change.

**Failed jobs.** A job that failed is marked `superseded` once a later job for the same work succeeds (history and
`last_error` are kept), so the status pill and the per-contractor panel only show failures that still matter.

## Manufacturer match: how the % is computed

The LLM judges evidence against a written rubric; **code** turns its judgements into the percentage.

1. **Collect** (`app/evidence/`): one collector per source writes `evidence` rows (review, permit, license...).
   Each source also reports whether it was *searched at all*, because "searched, found nothing" and "cannot
   search this jurisdiction" mean different things and must not both look like a bad score.
2. **Extract** (`matching/extract.py`, cheap model): each new review becomes closed-vocabulary facts (sentiment,
   project type, complaint categories, brands named). Paid once per distinct review.
3. **Judge** (`matching/judge.py`, stronger model): the rubric (`matching/rubrics/gaf.json`) plus a compact
   evidence pack go in; per-criterion `score`, `confidence`, rationale and **cited evidence ids** come out.
4. **Aggregate** (`matching/aggregate.py`): `match % = weighted mean of the criteria that could be scored`
   (unknown is left out, never counted as 0); `confidence = weight-averaged confidence over all criteria`
   (unknown counts as 0); citations to ids that do not exist are dropped and an uncited score has its
   confidence capped. Numbers are clamped; the model never writes the headline number.

`match` also sets the lead's priority band (>= 75 hot, >= 50 warm) and next best action. The older rule-based
fit score remains as a fallback until a match exists and is shown under "Rule-based fit score".

Re-runs cost nothing when nothing changed: a match is skipped unless the rubric, judge model, prompt version, or
evidence hash changed, and only edited reviews are re-extracted.

Entity resolution is the fragile part: licenses match on phone (strong) then name; permits match on name
similarity only (DOB NOW truncates names to 25 chars), and the evidence says so, so the judge lowers confidence.

## Evolving toward production

- **Queue**: the queue interface is four functions (`enqueue/claim/complete/fail`). Move to SQS / Cloud
  Tasks / Temporal when job volume outgrows Postgres polling or you need scheduled runs and workflows.
- **Skip redundant work**: done for the expensive parts (see "What is saved, and what a re-run redoes"). Still cheap-but-repeated: the directory query and the rule-based `enrich` on every run; add change detection there if it ever matters.
- **Scraping**: run browsers as a separate worker pool behind per-domain rate limits and a proxy policy;
  store raw HTML/JSON in object storage; alert on parser breakage (zero results, schema drift). Check the
  site's ToS/robots, and consider a data partnership with GAF.
- **More sources / entity resolution**: add `LeadSource`s (Google Maps, state license boards, LinkedIn for
  decision-makers), then dedupe contractors across sources. That is where "engage decision-makers" gets real.
- **Data model**: per-rep lead state (`rep_leads`) and territories (PostGIS) so hundreds of reps share one
  contractor table; `pg_trgm` / full-text search; keyset pagination past a few thousand rows; JSONB + GIN
  indexes if we filter on insights/services.
- **Deal-breakers**: a weighted mean lets strong criteria outvote a fatal one (a contractor locked to a competitor
  can still score ~77%). Add rubric-level caps (e.g. expired license or competitor exclusivity caps the match at 50).
- **Reviews**: pick a licensed source (Places API, a review-data vendor, or the contractor's own site) and add
  a `Collector`; add more jurisdictions than NYC (state license boards, city permit portals, or an aggregator).
- **Match quality**: 30-50 contractors hand-rated by reps as an eval set; track LLM-vs-rep agreement per rubric
  version; re-tune weights from won/lost outcomes.
- **Scoring**: replace hand-set weights with a model trained on the distributor's CRM outcomes
  (won/lost/spend); feed status changes back as labels.
- **LLM cost/quality**: batch API for bulk re-scoring; a cheaper model tier for bulk runs (`INSIGHTS_MODEL`);
  prompt caching for the shared system prompt; an eval set of contractors with rep ratings of the briefs.
- **Ops**: containerize API/worker/web; autoscale workers on queue depth; structured logs + metrics
  (queue lag, failure rate, LLM tokens/cost per lead); Sentry; auth (SSO) and audit logging on rep edits.
