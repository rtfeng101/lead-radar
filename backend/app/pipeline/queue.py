"""Tiny Postgres-backed job queue.

Why not Celery/Redis on day one: the jobs table is already transactional with the data it produces
(a discover job upserts contractors AND enqueues their enrich jobs in one commit), there is no
extra infra to run, and FOR UPDATE SKIP LOCKED lets any number of workers pull concurrently.
Because handlers are idempotent and the interface is 4 functions, swapping in SQS / Cloud Tasks /
Temporal later is a contained change.

Delivery is at-least-once: a worker that dies mid-job leaves it `running`; after the visibility
timeout another worker re-claims it.
"""

from collections.abc import Collection
from datetime import timedelta

from sqlalchemy import and_, or_, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Job, utcnow


def enqueue(
    session: Session,
    kind: str,
    payload: dict,
    *,
    dedupe_key: str | None = None,
    delay_s: float = 0,
) -> Job | None:
    """Add a job to the caller's transaction. Returns None if dedupe_key already exists."""
    job = Job(kind=kind, payload=payload, dedupe_key=dedupe_key, run_after=utcnow() + timedelta(seconds=delay_s))
    try:
        with session.begin_nested():  # savepoint, so a duplicate doesn't poison the outer transaction
            session.add(job)
            session.flush()
    except IntegrityError:
        return None
    return job


def claim(session: Session, kinds: Collection[str] | None = None) -> Job | None:
    """Atomically take the next runnable job, or None. Commits immediately to keep the row lock short.

    `kinds` restricts which job kinds this worker will take (e.g. only `profile`, for the browser worker)."""
    now = utcnow()
    orphaned_before = now - timedelta(seconds=settings.job_visibility_timeout_s)
    stmt = (
        select(Job)
        .where(
            or_(
                and_(Job.status == "queued", Job.run_after <= now),
                and_(Job.status == "running", Job.locked_at < orphaned_before),
            )
        )
        .where(Job.kind.in_(list(kinds)) if kinds else true())
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.execute(stmt).scalar_one_or_none()
    if job is None:
        session.rollback()
        return None
    if job.attempts >= settings.job_max_attempts:  # keeps crashing its worker (e.g. OOM) - stop re-claiming
        job.status, job.finished_at = "failed", now
        job.last_error = job.last_error or "exceeded max attempts (worker likely died)"
        session.commit()
        return claim(session, kinds)
    job.status, job.locked_at = "running", now
    job.attempts += 1
    session.commit()
    return job


def identity(payload: dict | None) -> dict:
    """What a job is *about*. `refresh` only changes how it runs, so it is not part of the identity."""
    return {k: v for k, v in (payload or {}).items() if k != "refresh"}


def complete(session: Session, job_id: int) -> None:
    job = session.get(Job, job_id)
    job.status, job.finished_at, job.locked_at = "done", utcnow(), None
    supersede_failed(session, job)
    session.commit()


def supersede_failed(session: Session, done: Job) -> int:
    """A later success for the same work clears earlier failures of it.

    Failed rows are kept (history, last_error) but marked `superseded`, so status counts and the UI only show
    failures that still matter: a contractor that failed on Monday and was fetched fine on Tuesday is not a problem.
    A failure that happened *after* the success is newer and stays failed."""
    ident, n = identity(done.payload), 0
    earlier = session.scalars(select(Job).where(Job.kind == done.kind, Job.status == "failed", Job.id < done.id))
    for failed in earlier:
        if identity(failed.payload) == ident:
            failed.status = "superseded"
            n += 1
    return n


CONTRACTOR_KINDS = ("profile", "enrich", "collect", "match")


def contractor_jobs(session: Session, contractor_id: int, statuses: Collection[str]) -> list[Job]:
    """Jobs about one contractor. Filtered in Python (payload is generic JSON) over a status-limited scan: the
    active set is small, and failed rows are rare."""
    rows = session.scalars(select(Job).where(Job.status.in_(list(statuses)), Job.kind.in_(CONTRACTOR_KINDS)))
    return [j for j in rows if (j.payload or {}).get("contractor_id") == contractor_id]


def fail(session: Session, job_id: int, error: Exception) -> None:
    """Retry with exponential backoff until max attempts, then park as `failed` for inspection."""
    job = session.get(Job, job_id)
    job.last_error = f"{type(error).__name__}: {error}"[:2000]
    job.locked_at = None
    if job.attempts >= settings.job_max_attempts:
        job.status, job.finished_at = "failed", utcnow()
    else:
        job.status = "queued"
        job.run_after = utcnow() + timedelta(seconds=min(300, 2 ** job.attempts))
    session.commit()
