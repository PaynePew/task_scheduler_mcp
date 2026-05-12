"""create_jobs_job_runs_run_events

Revision ID: 0001
Revises:
Create Date: 2026-05-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------ jobs
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "action_params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("job_type", sa.Text(), server_default="one_shot", nullable=False),
        sa.Column("scheduled_at", postgresql.TIMESTAMPTZ(), nullable=True),
        # W2 bonus columns landed in W1 schema
        sa.Column("cron_expr", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), server_default="UTC", nullable=False),
        sa.Column("raw_user_input", sa.Text(), nullable=True),
        sa.Column(
            "parsing_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("trigger_on_job_id", sa.BigInteger(), nullable=True),
        sa.Column("trigger_on_status", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMPTZ(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMPTZ(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(job_type = 'one_shot' AND scheduled_at IS NOT NULL AND cron_expr IS NULL)"
            " OR (job_type = 'recurring' AND cron_expr IS NOT NULL)",
            name="ck_jobs_schedule_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_on_job_id"],
            ["jobs.job_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    )
    op.create_index("idx_jobs_user_created", "jobs", ["user_id", sa.text("created_at DESC")])
    op.create_index(
        "idx_jobs_active_recurring",
        "jobs",
        ["job_type"],
        postgresql_where=sa.text("active AND job_type = 'recurring'"),
    )
    op.create_index("idx_jobs_action", "jobs", ["action"])

    # --------------------------------------------------------------- job_runs
    op.create_table(
        "job_runs",
        sa.Column("time_bucket", sa.Text(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("scheduled_at", postgresql.TIMESTAMPTZ(), nullable=False),
        sa.Column("status", sa.Text(), server_default="PENDING", nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default="3", nullable=False),
        # W2 bonus column landed in W1 schema
        sa.Column("wait_for_run_id", sa.BigInteger(), nullable=True),
        sa.Column("start_at", postgresql.TIMESTAMPTZ(), nullable=True),
        sa.Column("finish_at", postgresql.TIMESTAMPTZ(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMPTZ(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMPTZ(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"]),
        # Composite primary key (time_bucket, run_id) per ADR-009
        sa.PrimaryKeyConstraint("time_bucket", "run_id"),
    )
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
    op.create_index("idx_job_runs_by_job", "job_runs", ["job_id"])

    # --------------------------------------------------------------- run_events
    op.create_table(
        "run_events",
        sa.Column("event_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("status_from", sa.Text(), nullable=True),
        sa.Column("status_to", sa.Text(), nullable=True),
        sa.Column(
            "event_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "occurred_at",
            postgresql.TIMESTAMPTZ(),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        # JSONB cursor per ADR-009 for RecurringJobWatcher / ChainWatcher
        sa.Column(
            "processed_by",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "idx_run_events_recent_terminal",
        "run_events",
        ["occurred_at", "event_type"],
        postgresql_where=sa.text("event_type IN ('SUCCEEDED','FAILED','CANCELLED')"),
    )


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_table("job_runs")
    op.drop_table("jobs")
