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

Pre-flight de-duplication: production already holds rows that violate these
indexes — historical duplicate runs created before this exactly-once guarantee
existed (e.g. a recurring tick that double-fired). CREATE UNIQUE INDEX aborts on
such a row, so ``upgrade`` first collapses every cause group to a single run.
See ``upgrade`` for the keep-rule and the destructive-but-safe rationale.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-01

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("alembic.runtime.migration")

# Run statuses that still consume the box — mirrors 0010 / run_materializer._EXECUTING
# (+ WAITING). The dedup keep-rule sorts these FIRST so a still-live run is never
# dropped in favour of a terminal duplicate (that would strand in-flight work).
_NON_TERMINAL = "('PENDING','QUEUED','WAITING','RUNNING','RETRYING')"

# Ranks every job_run within its *cause group*; rn = 1 is the row to KEEP, rn > 1
# are redundant duplicates to delete. Two disjoint cause groups (a row belongs to
# exactly one): schedule-driven (wait_for_run_id NULL, keyed on scheduled_at) and
# trigger-driven (wait_for_run_id NOT NULL, keyed on wait_for_run_id) — mirroring
# the two partial unique indexes. Keep-rule: live (non-terminal) rows first so a
# dedup never strands in-flight work, then earliest run_id (the original fire) so
# the later double is the one dropped. run_id is a BIGSERIAL sequence value → each
# row's run_id is globally unique, so deletes may key on run_id alone.
_RANKED_DUPLICATE_RUN_IDS = (
    "SELECT run_id FROM ("
    "  SELECT run_id, ROW_NUMBER() OVER ("
    "    PARTITION BY job_id, scheduled_at"
    f"    ORDER BY (status IN {_NON_TERMINAL}) DESC, run_id"
    "  ) AS rn"
    "  FROM job_runs WHERE wait_for_run_id IS NULL"
    "  UNION ALL"
    "  SELECT run_id, ROW_NUMBER() OVER ("
    "    PARTITION BY job_id, wait_for_run_id"
    f"    ORDER BY (status IN {_NON_TERMINAL}) DESC, run_id"
    "  ) AS rn"
    "  FROM job_runs WHERE wait_for_run_id IS NOT NULL"
    ") ranked WHERE rn > 1"
)


def upgrade() -> None:
    # ---------------------------------------------------------------- de-dup
    # The two partial unique indexes below encode exactly-once run creation, but
    # production already holds pre-existing violations: historical duplicate runs
    # from before this guarantee existed (e.g. a recurring tick that double-fired
    # → two job_runs sharing (job_id, scheduled_at) with wait_for_run_id NULL).
    # CREATE UNIQUE INDEX aborts on such a row — this is exactly what blocked the
    # S3 deploy (a real duplicate at (job_id, scheduled_at) = (1, 2026-05-18
    # 14:01)); CI stayed green only because the test DB had no such row. So
    # collapse every cause group to a single run FIRST, deleting the redundant
    # duplicate(s) and their run_events. run_events has no FK to job_runs (ADR-009:
    # the outbox only carries run_id, not job_runs' composite PK), so the child
    # events must be deleted explicitly or they orphan.
    #
    # This is destructive and irreversible (downgrade cannot resurrect the deleted
    # rows), but it only ever removes a redundant duplicate of a run that already
    # has a surviving twin in the same cause group — never a unique run. The
    # keep-rule (see _RANKED_DUPLICATE_RUN_IDS) preserves any still-live run.
    bind = op.get_bind()
    # Delete orphan events first: it reads job_runs' pre-delete state, so it must
    # run before the runs themselves are removed. Both statements recompute the
    # same rn>1 set from the (still-unchanged) job_runs snapshot.
    bind.execute(sa.text(f"DELETE FROM run_events WHERE run_id IN ({_RANKED_DUPLICATE_RUN_IDS})"))
    deleted = bind.execute(
        sa.text(f"DELETE FROM job_runs WHERE run_id IN ({_RANKED_DUPLICATE_RUN_IDS})")
    )
    _log.warning(
        "0011: removed %s duplicate job_runs (and their run_events) before "
        "creating the cause-unique indexes",
        deleted.rowcount,
    )

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
    # Cutover edge (near-zero, but NOT self-healing — do not assume otherwise):
    # if a recurring root's terminal event is stamped here before its successor
    # was created, the new consumer skips it and that job silently STOPS recurring
    # (no successor -> no future run -> no future event to react to; it cannot
    # self-heal). The real safety net during the S3 deploy is the still-running
    # OLD recurring-watcher (distinct cursor key "recurring_watcher"), which drains
    # its pending recurring events and creates their successors before this
    # migration runs; uq_job_runs_recurring_tick then makes any double-create a
    # no-op. The old watcher polls every <=5s (far faster than a deploy), so the
    # window is tiny — but to be safe, let the old recurring-watcher drain before
    # `migrate` runs. The severe issue (retroactive chained sends across all
    # history) is eliminated regardless.
    op.execute(
        "UPDATE run_events "
        "SET processed_by = COALESCE(processed_by, '{}'::jsonb) "
        "|| jsonb_build_object('continuation', (now() AT TIME ZONE 'utc')::text) "
        "WHERE event_type IN ('SUCCEEDED','FAILED','CANCELLED')"
    )


def downgrade() -> None:
    # Reverse the cutover stamp (symmetry with upgrade's backfill). The pre-flight
    # dedup is intentionally NOT reversed — the deleted duplicate runs are gone for
    # good; downgrade only removes the indexes and un-stamps the cursor.
    op.execute("UPDATE run_events SET processed_by = processed_by - 'continuation'")
    op.drop_index("uq_job_runs_recurring_tick", table_name="job_runs")
    op.drop_index("uq_job_runs_trigger_cause", table_name="job_runs")
