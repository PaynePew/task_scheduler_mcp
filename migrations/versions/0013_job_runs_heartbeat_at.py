"""job_runs_heartbeat_at: add DB-side heartbeat lease column to job_runs

Issue #267 (PRD #266, execution-plane durability). Today's heartbeat only
extends SQS visibility; ``updated_at`` is frozen at the RUNNING claim, so a
future orphan sweep keyed on claim age would kill legitimately long-running
actions. ``heartbeat_at`` is a dedicated, worker-renewed lease: the claim sets
it at the RUNNING transition, and the executor's heartbeat loop re-bumps it
on each tick (~30s) alongside the existing visibility extension. This slice
only adds the column and the writers — no sweep consumes it yet.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("job_runs", "heartbeat_at")
