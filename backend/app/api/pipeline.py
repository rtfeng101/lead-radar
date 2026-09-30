from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Job
from app.pipeline import queue
from app.schemas import PipelineStatus, RunRequest, RunResponse

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.post("/runs", response_model=RunResponse, status_code=202)
def start_run(req: RunRequest, session: Session = Depends(get_session)) -> RunResponse:
    """Enqueue a discover job and return immediately; workers do the rest."""
    job = queue.enqueue(
        session, "discover", {"zip_code": req.zip_code, "radius_mi": req.radius_mi, "refresh": req.refresh})
    session.commit()
    return RunResponse(job_id=job.id)


@router.get("/status", response_model=PipelineStatus)
def status(session: Session = Depends(get_session)) -> PipelineStatus:
    counts = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    return PipelineStatus(**{k: counts.get(k, 0) for k in ("queued", "running", "done", "failed")})
