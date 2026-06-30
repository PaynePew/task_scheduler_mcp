"""job_state_lifecycle: replace never-cleared active boolean with Job.state

ADR-068. Adds ``jobs.state`` ('active' | 'completed' | 'cancelled'), backfills
it from existing data, drops the ``active`` boolean, and rebuilds the partial
index to key on ``state`` instead of the boolean.

Fixes the quota-lockout: ``active`` defaulted true and was never written again,
so every job ever created counted against the per-user active-total cap forever.
After this migration the containment quota counts ``state='active'``, so terminal
jobs (completed / cancelled) leave the active set. The backfill reclassifies the
live demo/operator account's terminal jobs as ``completed`` without manual DB
surgery, so the new quota query is correct from the first request after deploy.

Backfill (ADR-068 §6):
  - ``cancelled_at IS NOT NULL``                                  -> 'cancelled'
  - recurring (``cron_expr IS NOT NULL``), not cancelled          -> 'active'
  - one-shot/immediate/chained with no non-terminal run remaining -> 'completed'
  - one-shot/immediate/chained still pending/running              -> 'active'

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Run statuses that still consume the box — a job with any such run keeps
# resident load and stays 'active'. Mirrors run_materializer._EXECUTING plus
# WAITING (a chained downstream not yet flipped is still pending work).
_NON_TERMINAL = "('PENDING','QUEUED','WAITING','RUNNING','RETRYING')"


def upgrade() -> None:
    # Add the column defaulting to 'active' so existing rows start active; the
    # backfill below refines terminal rows. New jobs rely on this server_default.
    op.add_column(
        "jobs",
        sa.Column("state", sa.Text(), nullable=False, server_default="active"),
    )

    # Backfill. Cancelled wins over everything (a user explicitly stopped it).
    op.execute("UPDATE jobs SET state = 'cancelled' WHERE cancelled_at IS NOT NULL")
    # Exhausted non-recurring schedules: no non-terminal run remains. Recurring
    # roots (cron_expr IS NOT NULL) are left 'active' — they only leave the
    # active set on cancel (ADR-068 §2).
    op.execute(
        "UPDATE jobs SET state = 'completed' "
        "WHERE cancelled_at IS NULL "
        "AND cron_expr IS NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM job_runs r "
        f"  WHERE r.job_id = jobs.job_id AND r.status IN {_NON_TERMINAL}"
        ")"
    )
    # Recurring-not-cancelled and still-pending/running one-shots keep the
    # 'active' default — no UPDATE needed.

    # Drop the boolean and rebuild the partial index to key on state. The index
    # references `active` in its predicate, so it must be dropped before the
    # column; the rebuilt index references `state`, which now exists.
    op.drop_index("idx_jobs_active_recurring", table_name="jobs")
    op.drop_column("jobs", "active")
    op.create_index(
        "idx_jobs_active_recurring",
        "jobs",
        ["job_type"],
        postgresql_where=sa.text("state = 'active' AND job_type = 'recurring'"),
    )


def downgrade() -> None:
    # Re-add the boolean, rebuild its predicate index, then derive active from
    # state ('active' -> true, terminal -> false) before dropping state.
    op.add_column(
        "jobs",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.drop_index("idx_jobs_active_recurring", table_name="jobs")
    op.execute("UPDATE jobs SET active = (state = 'active')")
    op.create_index(
        "idx_jobs_active_recurring",
        "jobs",
        ["job_type"],
        postgresql_where=sa.text("active AND job_type = 'recurring'"),
    )
    op.drop_column("jobs", "state")
