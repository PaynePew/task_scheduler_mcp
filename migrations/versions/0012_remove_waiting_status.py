"""remove_waiting_status: drop the WAITING run status and its pre-arm indexes

ADR-067. The pre-arm control plane (a downstream run pre-created ``WAITING`` and
flipped by ``ChainWatcher``) is replaced by continuation: a downstream run is
created ``PENDING`` when its upstream terminates. No ``WAITING`` run is produced
any more, so this migration removes the status from the schema:

  - Converts any lingering ``WAITING`` ``job_runs`` → ``CANCELLED`` (ADR-067
    "the migration must drain or convert any live WAITING runs at cutover"). By
    the S3 deploy the continuation consumer already produces none, so this is a
    defensive no-op on a clean prod, but it guarantees no row is stranded once
    ``ChainWatcher`` is gone.
  - Drops ``idx_job_runs_wait_for`` — it existed solely so ``ChainWatcher`` could
    find ``WAITING`` runs to flip; there are none, and the watcher is deleted.
  - Rebuilds ``idx_job_runs_due`` (the Watcher's due-scan) to ``status = 'PENDING'``
    — ``WAITING`` runs were never dispatchable anyway.
  - Rebuilds the forbid-concurrency partial unique index
    ``uq_job_runs_job_scheduled_nonterminal`` without ``WAITING`` in its predicate.

Deliberately does NOT touch the S3 exactly-once indexes ``uq_job_runs_trigger_cause``
/ ``uq_job_runs_recurring_tick`` or the ``wait_for_run_id`` column — under
continuation that column is repurposed as the trigger-cause carrier (the upstream
terminal run_id), keying ``uq_job_runs_trigger_cause`` and the ``from_run_id`` data
plane. Only the pre-arm *use* of ``WAITING`` is removed here, not the column.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-01

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    # ---- Cutover: convert any lingering WAITING runs (ADR-067) ----------------
    # A WAITING run is a pre-armed downstream that never flipped. With ChainWatcher
    # deleted nothing would ever advance it, so drain it to CANCELLED. Converting
    # WAITING first also clears the status out of the partial-index predicates
    # rebuilt below. (No RunEvent is emitted: this is a one-off schema cutover on a
    # status the running system no longer produces, not a domain transition.)
    bind = op.get_bind()
    converted = bind.execute(
        sa.text(
            "UPDATE job_runs SET status = 'CANCELLED', updated_at = now() WHERE status = 'WAITING'"
        )
    )
    if converted.rowcount:
        _log.warning(
            "0012: converted %s lingering WAITING job_runs to CANCELLED at cutover",
            converted.rowcount,
        )

    # ---- Drop the ChainWatcher wake index (WAITING-only) ----------------------
    op.drop_index("idx_job_runs_wait_for", table_name="job_runs")

    # ---- Rebuild the Watcher due-scan index without WAITING --------------------
    op.drop_index("idx_job_runs_due", table_name="job_runs")
    op.create_index(
        "idx_job_runs_due",
        "job_runs",
        ["time_bucket", "scheduled_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # ---- Rebuild forbid-concurrency partial unique index without WAITING -------
    # Narrowing the predicate (removing a status) can only shrink the covered set,
    # so no new uniqueness violation is possible.
    op.drop_index("uq_job_runs_job_scheduled_nonterminal", table_name="job_runs")
    op.create_index(
        "uq_job_runs_job_scheduled_nonterminal",
        "job_runs",
        ["job_id", "scheduled_at"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING','QUEUED','RUNNING','RETRYING')"),
    )


def downgrade() -> None:
    # Restore the WAITING-aware index predicates (symmetry with upgrade). The
    # WAITING→CANCELLED conversion is intentionally NOT reversed — the original
    # WAITING rows are gone for good, exactly as 0011's dedup is irreversible.
    op.drop_index("uq_job_runs_job_scheduled_nonterminal", table_name="job_runs")
    op.create_index(
        "uq_job_runs_job_scheduled_nonterminal",
        "job_runs",
        ["job_id", "scheduled_at"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING','QUEUED','WAITING','RUNNING','RETRYING')"),
    )

    op.drop_index("idx_job_runs_due", table_name="job_runs")
    op.create_index(
        "idx_job_runs_due",
        "job_runs",
        ["time_bucket", "scheduled_at"],
        postgresql_where=sa.text("status IN ('PENDING', 'WAITING')"),
    )

    op.create_index(
        "idx_job_runs_wait_for",
        "job_runs",
        ["wait_for_run_id"],
        postgresql_where=sa.text("wait_for_run_id IS NOT NULL AND status = 'WAITING'"),
    )
