from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Use DateTime(timezone=True) which renders as TZ on PostgreSQL
TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    action_params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    job_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="one_shot")
    scheduled_at: Mapped[str | None] = mapped_column(TZ, nullable=True)
    # W2 bonus columns landed in W1 schema
    cron_expr: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    raw_user_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsing_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    trigger_on_job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True
    )
    trigger_on_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[str] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[str] = mapped_column(TZ, nullable=False, server_default=func.now())

    runs: Mapped[list["JobRun"]] = relationship("JobRun", back_populates="job")

    __table_args__ = (
        CheckConstraint(
            "(job_type = 'one_shot' AND scheduled_at IS NOT NULL AND cron_expr IS NULL)"
            " OR (job_type = 'recurring' AND cron_expr IS NOT NULL)",
            name="ck_jobs_schedule_consistency",
        ),
        Index("idx_jobs_user_created", "user_id", "created_at"),
        Index(
            "idx_jobs_active_recurring",
            "job_type",
            postgresql_where="active AND job_type = 'recurring'",
        ),
        Index("idx_jobs_action", "action"),
    )


class JobRun(Base):
    __tablename__ = "job_runs"

    # Composite primary key (time_bucket, run_id) per ADR-009
    time_bucket: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.job_id"), nullable=False)
    scheduled_at: Mapped[str] = mapped_column(TZ, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    # W2 bonus column landed in W1 schema
    wait_for_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    start_at: Mapped[str | None] = mapped_column(TZ, nullable=True)
    finish_at: Mapped[str | None] = mapped_column(TZ, nullable=True)
    created_at: Mapped[str] = mapped_column(TZ, nullable=False, server_default=func.now())
    updated_at: Mapped[str] = mapped_column(TZ, nullable=False, server_default=func.now())

    job: Mapped["Job"] = relationship("Job", back_populates="runs")

    __table_args__ = (
        Index(
            "idx_job_runs_due",
            "time_bucket",
            "scheduled_at",
            postgresql_where="status IN ('PENDING', 'WAITING')",
        ),
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
    # run_id and job_id are plain columns — job_runs has a composite PK so no FK here
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    job_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    status_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[str] = mapped_column(TZ, nullable=False, server_default=func.now())
    # JSONB cursor for RecurringJobWatcher / ChainWatcher per ADR-009
    processed_by: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        Index(
            "idx_run_events_recent_terminal",
            "occurred_at",
            "event_type",
            postgresql_where="event_type IN ('SUCCEEDED','FAILED','CANCELLED')",
        ),
    )
