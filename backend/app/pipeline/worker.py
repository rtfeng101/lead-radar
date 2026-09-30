"""Worker + CLI.

    python -m app.pipeline.worker                 # run forever (start as many as you like)
    python -m app.pipeline.worker --drain         # process until the queue is empty, then exit
    python -m app.pipeline.worker --seed 10013    # enqueue a discover job for a zip first
    python -m app.pipeline.worker --kinds profile # only take some job kinds (e.g. a dedicated browser worker)
"""

import argparse
import logging
import signal
import time

from app import db
from app.config import settings
from app.pipeline import queue
from app.pipeline.handlers import HANDLERS

log = logging.getLogger("worker")
_stop = False


def run_once(kinds: set[str] | None = None) -> bool:
    """Process at most one job. Returns False if the queue had nothing runnable.

    A worker only claims kinds it has a handler for, so during a rolling deploy an older worker leaves a newly
    introduced job kind for updated workers instead of failing it with a KeyError."""
    with db.SessionLocal() as session:
        job = queue.claim(session, (kinds or set(HANDLERS)) & set(HANDLERS))
    if job is None:
        return False
    try:
        HANDLERS[job.kind](job.payload, job.id)
    except Exception as e:  # noqa: BLE001 - any failure goes back through the retry policy
        log.exception("job %s (%s) failed on attempt %s", job.id, job.kind, job.attempts)
        with db.SessionLocal() as session:
            queue.fail(session, job.id, e)
    else:
        with db.SessionLocal() as session:
            queue.complete(session, job.id)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--seed", metavar="ZIP")
    parser.add_argument("--refresh", action="store_true", help="with --seed: re-fetch everything, ignoring freshness")
    parser.add_argument("--radius", type=int, default=settings.default_radius_mi)
    parser.add_argument("--kinds", metavar="K1,K2", help="only take these job kinds: " + ", ".join(sorted(HANDLERS)))
    args = parser.parse_args()
    kinds = {k.strip() for k in args.kinds.split(",")} if args.kinds else None
    if kinds and (unknown := kinds - set(HANDLERS)):
        parser.error(f"unknown job kind(s): {', '.join(sorted(unknown))}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if args.seed:
        with db.SessionLocal() as session:
            queue.enqueue(session, "discover", {"zip_code": args.seed, "radius_mi": args.radius, "refresh": args.refresh})
            session.commit()
        log.info("enqueued discover for %s", args.seed)

    def _handle(signum, frame):  # finish the current job, then exit
        global _stop
        _stop = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    while not _stop:
        if run_once(kinds):
            continue
        if args.drain:
            break
        time.sleep(settings.worker_poll_interval_s)


if __name__ == "__main__":
    main()
