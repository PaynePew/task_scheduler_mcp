"""run_cause_unique_constraints: exactly-once downstream/successor creation

ADR-067 §4. Adds two partial unique indexes on ``job_runs`` keyed on the run's
*cause*, so a redelivered terminal event can never double-create the run it
materialises — exactly-once becomes a data-layer guarantee, not an operational
hope on the ``processed_by`` cursor (which stays only as an efficiency layer):

  - ``uq_job_runs_trigger_cause`` — UNIQUE (job_id, wait_for_run_id) WHERE
    wait_for_run_id IS NOT NULL. A trigger-driven (chained) run is caused by one
    upstream terminal run; ``wait_for_run_id`` carries that run_id, so this index
    makes "one downstream run per (downstream job, upstream run)" a hard rule.
  - ``uq_job_runs_recurring_tick`` — UNIQUE (job_id, scheduled_at) WHERE
    wait_for_run_id IS NULL. A schedule-driven run (recurring successor, one-shot,
    immediate) is caused by a clock tick; ``scheduled_at`` is that tick, so this
    index makes "one run per (job, scheduled tick)" a hard rule. Successive cron
    ticks always carry a strictly-later scheduled_at (the anchor clamp in
    ``materialize_successor``), so legitimate rows never collide.

Both are partial so they stay small and never overlap (a row has exactly one
run source: trigger-driven ⇒ wait_for_run_id set; schedule-driven ⇒ NULL).

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_job_runs_trigger_cause",
        "job_runs",
        ["job_id", "wait_for_run_id"],
        unique=True,
        postgresql_where=sa.text("wait_for_run_id IS NOT NULL"),
    )
    op.create_index(
        "uq_job_runs_recurring_tick",
        "job_runs",
        ["job_id", "scheduled_at"],
        unique=True,
        postgresql_where=sa.text("wait_for_run_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_job_runs_recurring_tick", table_name="job_runs")
    op.drop_index("uq_job_runs_trigger_cause", table_name="job_runs")
