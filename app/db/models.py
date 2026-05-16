"""ORM models for the three core tables (jobs, job_runs, run_events).

See ``CONTEXT.md`` §1 (entity definitions) and ADR-009 (schema rationale).
Three distinct tables; mixing them up is the #1 source of bugs:

  Job        mutable definition       — "what should run, when, for whom"
  JobRun     one row per attempt      — claim state, retries, audit
  RunEvent   append-only outbox       — every transition, immutable
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# All timestamps are tz-aware (UTC at rest). Never use naive datetimes here —
# Postgres stores ``timestamp with time zone`` only when the column is declared
# this way; mixing naive and aware values silently drops the offset.
TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # user_id is plain TEXT (not a FK) so the source can swap from MCP_USER_ID
    # (W1 trust-only) → ALB OIDC sub claim (W3) without a migration. See ADR-015.
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Must match a key in ACTION_REGISTRY. Validated at the MCP boundary, not by the DB.
    action: Mapped[str] = mapped_column(Text, nullable=False)
    action_params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # 'one_shot' | 'recurring'. The CHECK constraint below enforces which scheduling
    # fields must be set for each type.
    job_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="one_shot")
    scheduled_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    # cron expansion (RecurringJobWatcher) and chain logic (ChainWatcher) land in W2.
    cron_expr: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    # Set by cancel_job() the first time a cancel is requested (ADR-022).
    # Once non-NULL, re-cancel is a no-op (idempotent best-effort semantics).
    cancelled_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    trigger_on_job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True
    )
    trigger_on_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    runs: Mapped[list["JobRun"]] = relationship("JobRun", back_populates="job")

    __table_args__ = (
        # one_shot ⇒ must have scheduled_at and no cron;  recurring ⇒ must have cron.
        # Catches "set both" / "set neither" misuse at the DB layer.
        CheckConstraint(
            "(job_type = 'one_shot' AND scheduled_at IS NOT NULL AND cron_expr IS NULL)"
            " OR (job_type = 'recurring' AND cron_expr IS NOT NULL)",
            name="ck_jobs_schedule_consistency",
        ),
        # Idempotency is scoped *per user* — two different users using the same key
        # don't collide. NULL keys never collide (Postgres NULL semantics).
        UniqueConstraint("user_id", "idempotency_key", name="uq_jobs_user_idempotency"),
        # Hot index for task.list.v1 ("show me my recent jobs"). Newest-first.
        # In DynamoDB this would be the GSI with PK=user_id, SK=created_at.
        Index("idx_jobs_user_created", "user_id", text("created_at DESC")),
        # Partial index: only indexes rows the RecurringJobWatcher cares about,
        # keeping the index small even as terminated recurring jobs accumulate.
        Index(
            "idx_jobs_active_recurring",
            "job_type",
            postgresql_where="active AND job_type = 'recurring'",
        ),
        Index("idx_jobs_action", "action"),
    )


class JobRun(Base):
    __tablename__ = "job_runs"

    # Composite PK (time_bucket, run_id) per ADR-009.
    # time_bucket is ``date_trunc('hour', scheduled_at)`` as an ISO string. It is
    # the partition key: in W1 it lives as a column + composite PK + partial
    # index; in W2 the table is upgraded to native PARTITION BY RANGE on it
    # without changing app code. Purpose: the watcher's "what's due in the next
    # 5 minutes?" query touches at most one or two hour-buckets instead of the
    # whole table.
    time_bucket: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.job_id"), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    # W2 bonus column landed in W1 schema
    wait_for_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    start_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    finish_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())

    job: Mapped["Job"] = relationship("Job", back_populates="runs")

    __table_args__ = (
        # *The* watcher hot query. Partial index keeps it tiny — only un-dispatched
        # rows are present. The watcher's claim:
        #   SELECT ... WHERE status='PENDING' AND scheduled_at < now()+'5 min'
        #             FOR UPDATE SKIP LOCKED
        # is satisfied by this single index.
        Index(
            "idx_job_runs_due",
            "time_bucket",
            "scheduled_at",
            postgresql_where="status IN ('PENDING', 'WAITING')",
        ),
        # ChainWatcher (W2) uses this to wake WAITING runs when their blocker terminates.
        Index(
            "idx_job_runs_wait_for",
            "wait_for_run_id",
            postgresql_where="wait_for_run_id IS NOT NULL AND status = 'WAITING'",
        ),
        Index("idx_job_runs_by_job", "job_id"),
    )


class RunEvent(Base):
    __tablename__ = "run_events"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # No FK to job_runs: job_runs has a composite PK (time_bucket, run_id) and
    # an event only knows the run_id half. The owning JobRun is found by
    # (run_id, derived time_bucket) on read. Trade-off accepted in ADR-009 —
    # the outbox would otherwise need to duplicate time_bucket on every row.
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    job_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # CREATED | QUEUED | STARTED | SUCCEEDED | FAILED | RETRY | CANCELLED
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    status_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    # Tracks which downstream watcher has consumed this event, e.g.
    #   {"recurring": true} once RecurringJobWatcher has spawned the next run.
    # Acts as an idempotency cursor so each downstream reactor processes an
    # event at most once — the outbox stays append-only, so this column is the
    # only mutable bit on RunEvent.
    processed_by: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        # Hot index for RecurringJobWatcher / ChainWatcher: "what terminal events
        # happened recently that I haven't reacted to?"
        Index(
            "idx_run_events_recent_terminal",
            "occurred_at",
            "event_type",
            postgresql_where="event_type IN ('SUCCEEDED','FAILED','CANCELLED')",
        ),
    )
