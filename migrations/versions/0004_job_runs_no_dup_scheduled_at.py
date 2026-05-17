"""job_runs_no_dup_scheduled_at: partial unique index to enforce Forbid-concurrency

Prevents RecurringJobWatcher from spawning two JobRuns at the same
(job_id, scheduled_at) when they are both in non-terminal states.
This is the DB-layer invariant for the spawn-time uniqueness guarantee
described in ADR-016 (addendum).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-17

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_job_runs_job_scheduled_nonterminal"
_NON_TERMINAL = "status IN ('PENDING','QUEUED','WAITING','RUNNING','RETRYING')"


def upgrade() -> None:
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX_NAME}"
        " ON job_runs (job_id, scheduled_at)"
        f" WHERE {_NON_TERMINAL}"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
