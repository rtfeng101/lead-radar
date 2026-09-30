"""mark already-resolved failed jobs as superseded

Data-only migration. A failed job is superseded when a later job of the same kind and about the same work
succeeded (same rule as app/pipeline/queue.py: supersede_failed). New failures are handled when jobs complete;
this cleans up rows that were left `failed` before that rule existed.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

jobs = sa.table(
    "jobs",
    sa.column("id", sa.Integer),
    sa.column("kind", sa.String),
    sa.column("status", sa.String),
    sa.column("payload", sa.JSON),
)


def _identity(payload) -> dict:
    return {k: v for k, v in (payload or {}).items() if k != "refresh"}


def upgrade() -> None:
    conn = op.get_bind()
    failed = conn.execute(sa.select(jobs.c.id, jobs.c.kind, jobs.c.payload).where(jobs.c.status == "failed")).all()
    for job_id, kind, payload in failed:
        later_done = conn.execute(
            sa.select(jobs.c.payload).where(jobs.c.kind == kind, jobs.c.status == "done", jobs.c.id > job_id)
        ).all()
        if any(_identity(p) == _identity(payload) for (p,) in later_done):
            conn.execute(jobs.update().where(jobs.c.id == job_id).values(status="superseded"))


def downgrade() -> None:
    op.get_bind().execute(jobs.update().where(jobs.c.status == "superseded").values(status="failed"))
