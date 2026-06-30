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

    # Cutover stamp for the renamed continuation cursor (ADR-067).
    #
    # S3 renamed the consumer's processed_by cursor key from "recurring_watcher"
    # to "continuation" and dropped the old cron_expr filter, so poll_once() now
    # selects EVERY terminal run_events row whose processed_by JSONB lacks the
    # "continuation" key. No existing event carries that key (the old key was
    # "recurring_watcher"; one-shot/chained terminal events were never stamped at
    # all), so on deploy the consumer would rescan the entire historical
    # terminal-event log and, for every historical chained upstream whose
    # downstream did not yet exist, materialise a brand-new downstream run —
    # real, retroactive Slack/email/LLM sends. The unique indexes above only stop
    # double-creation when a downstream row ALREADY exists; they do nothing for
    # these historical gaps.
    #
    # So stamp every EXISTING terminal event as already handled by the
    # "continuation" consumer. After deploy it then reacts only to NEW terminal
    # events. processed_by is a JSONB dict {consumer_name: ISO-timestamp} that
    # defaults to {}; COALESCE guards any legacy NULL before the || merge so an
    # existing "recurring_watcher" stamp is preserved.
    #
    # Known, negligible cutover edge: a recurring job whose terminal event lands
    # in the exact deploy window may get stamped before the consumer reacts; it
    # self-heals on the job's next run. The severe issue (retroactive chained
    # sends across all history) is eliminated.
    op.execute(
        "UPDATE run_events "
        "SET processed_by = COALESCE(processed_by, '{}'::jsonb) "
        "|| jsonb_build_object('continuation', (now() AT TIME ZONE 'utc')::text) "
        "WHERE event_type IN ('SUCCEEDED','FAILED','CANCELLED')"
    )


def downgrade() -> None:
    # Reverse the cutover stamp (symmetry with upgrade's backfill).
    op.execute("UPDATE run_events SET processed_by = processed_by - 'continuation'")
    op.drop_index("uq_job_runs_recurring_tick", table_name="job_runs")
    op.drop_index("uq_job_runs_trigger_cause", table_name="job_runs")
