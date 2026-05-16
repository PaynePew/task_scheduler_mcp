"""Business logic for jobs — pure functions on (session, args).

Per ADR-010 this layer **knows nothing about MCP**. It accepts an
``AsyncSession``, mutates DB state, and raises domain exceptions. The MCP
handlers in ``app.mcp.handlers`` wrap each call, map exceptions to the
6-code error vocabulary, and shape the envelope.

Anything that writes a status transition also writes a matching
``RunEvent`` *in the same transaction* — the transactional outbox pattern
(CONTEXT.md §5). Downstream watchers consume the immutable event log,
never the mutable status column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.registry import ACTION_REGISTRY
from app.config.cron import next_after, validate_cron_expr
from app.config.timezone_resolver import resolve_timezone
from app.db.models import Job, JobRun, RunEvent


class UnknownActionError(Exception):
    """Raised when action is not registered in ACTION_REGISTRY (maps to UNKNOWN_ACTION)."""


class JobNotFoundError(Exception):
    """Raised when job_id does not exist or belongs to another user (maps to NOT_FOUND)."""


class InvalidStateError(Exception):
    """Raised when a job cannot transition into the requested state (INVALID_STATE).

    For cancel_job, this means every run has completed naturally
    (SUCCEEDED/FAILED) and there is no pending or in-flight work to cancel —
    see ADR-022 for the best-effort semantics.

    Carries the current internal status as its single arg so the transport
    layer can map it to a user-facing external status (ADR-014). The domain
    must not depend on the MCP presentation layer (ADR-010), so message
    formatting happens in the handler, not here.
    """


class UnsupportedScheduleTypeError(Exception):
    """Raised when schedule_type is not yet implemented (maps to USER_INPUT).

    W1 implements 'immediate' and 'one-shot'. Recurring/chain schedules land
    in later slices (S13). Passing anything else is rejected explicitly rather
    than silently degraded, which would mask scheduling bugs far from their cause.
    """


class InvalidScheduledAtError(Exception):
    """Raised when scheduled_at is missing, unparseable, or not in the future (USER_INPUT)."""


class InvalidTimezoneError(Exception):
    """Raised when timezone is not a valid IANA tz key (USER_INPUT).

    Offset strings (UTC+8, +08:00) and Windows IDs (Taipei Standard Time) are
    rejected here; only IANA keys accepted by zoneinfo.ZoneInfo are valid.
    """


class InvalidCronExprError(Exception):
    """Raised when cron_expr is missing or malformed for a recurring job (USER_INPUT).

    The single arg is the human-readable hint returned by validate_cron_expr so
    the MCP handler can forward it as the ``expected`` field for LLM self-correction.
    """


_SUPPORTED_SCHEDULE_TYPES = frozenset({"immediate", "one-shot", "recurring"})


def _validate_iana_timezone(tz_str: str) -> ZoneInfo:
    """Return ZoneInfo for tz_str; raise InvalidTimezoneError if not a valid IANA key."""
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        raise InvalidTimezoneError(tz_str) from exc


def _parse_and_normalise_scheduled_at(scheduled_at_str: str | None, tz: ZoneInfo) -> datetime:
    """Parse scheduled_at string, apply tz if naive, convert to UTC, validate future.

    Raises InvalidScheduledAtError for None, unparseable, or past values.
    """
    if scheduled_at_str is None:
        raise InvalidScheduledAtError("scheduled_at is required for one-shot scheduling")

    try:
        dt = datetime.fromisoformat(scheduled_at_str)
    except (ValueError, TypeError) as exc:
        raise InvalidScheduledAtError(
            f"Cannot parse scheduled_at '{scheduled_at_str}' as ISO 8601"
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)

    dt_utc = dt.astimezone(UTC)

    if dt_utc <= datetime.now(tz=UTC):
        raise InvalidScheduledAtError(f"scheduled_at '{scheduled_at_str}' must be in the future")

    return dt_utc


async def create_job(
    session: AsyncSession,
    *,
    user_id: str,
    action: str,
    action_params: dict,
    schedule_type: str,
    idempotency_key: str | None = None,
    scheduled_at: str | None = None,
    timezone: str | None = None,
    cron_expr: str | None = None,
    tz_header: str | None = None,
    tz_env: str | None = None,
) -> Job:
    """Insert Job + JobRun(PENDING) + RunEvent(CREATED) in one transaction.

    Returns the existing Job on (user_id, idempotency_key) collision.
    Raises UnknownActionError if action is not in ACTION_REGISTRY.
    Raises UnsupportedScheduleTypeError for unimplemented schedule_type values.
    Raises InvalidTimezoneError if timezone is not a valid IANA key.
    Raises InvalidScheduledAtError if scheduled_at is missing, unparseable, or past.
    Raises InvalidCronExprError if cron_expr is missing or invalid for recurring.
    """
    if schedule_type not in _SUPPORTED_SCHEDULE_TYPES:
        raise UnsupportedScheduleTypeError(schedule_type)
    if action not in ACTION_REGISTRY:
        raise UnknownActionError(action)

    if schedule_type == "recurring":
        if not cron_expr:
            raise InvalidCronExprError(
                "cron_expr is required for recurring jobs; "
                "use a 5-field POSIX expression like '0 8 * * *'"
            )
        ok, hint = validate_cron_expr(cron_expr)
        if not ok:
            raise InvalidCronExprError(hint)
        resolved_tz = resolve_timezone(timezone, tz_header, tz_env)
        tz = _validate_iana_timezone(resolved_tz)
        now = datetime.now(tz=UTC)
        run_at = next_after(cron_expr, tz, now)
    elif schedule_type == "one-shot":
        tz = _validate_iana_timezone(timezone or "UTC")
        run_at = _parse_and_normalise_scheduled_at(scheduled_at, tz)
        resolved_tz = timezone or "UTC"
        cron_expr = None
    else:
        run_at = datetime.now(tz=UTC)
        resolved_tz = timezone or "UTC"
        cron_expr = None

    async with session.begin():
        if idempotency_key is not None:
            # Idempotency short-circuit: return the existing Job verbatim instead
            # of creating a duplicate. Caller-side retry safety — a flaky network
            # making the caller retry create_job twice won't spawn two jobs. The
            # uniqueness constraint (user_id, idempotency_key) backs this up at
            # the DB level for the concurrent case.
            result = await session.execute(
                select(Job).where(
                    Job.idempotency_key == idempotency_key,
                    Job.user_id == user_id,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                return existing

        # Hour-truncated partition key. See JobRun.time_bucket in db/models.py
        # for the full rationale — it lets the watcher's hot query scan one
        # bucket instead of the whole table.
        time_bucket = run_at.replace(minute=0, second=0, microsecond=0).isoformat()

        is_recurring = schedule_type == "recurring"
        job = Job(
            user_id=user_id,
            description=action,
            action=action,
            action_params=action_params,
            job_type="recurring" if is_recurring else "one_shot",
            scheduled_at=None if is_recurring else run_at,
            cron_expr=cron_expr,
            timezone=resolved_tz,
            idempotency_key=idempotency_key,
        )
        session.add(job)
        await session.flush()

        run = JobRun(
            time_bucket=time_bucket,
            job_id=job.job_id,
            scheduled_at=run_at,
            status="PENDING",
        )
        session.add(run)
        await session.flush()

        event = RunEvent(
            run_id=run.run_id,
            job_id=job.job_id,
            event_type="CREATED",
        )
        session.add(event)

    return job


@dataclass
class RunView:
    run_id: int
    status: str
    scheduled_at: datetime
    start_at: datetime | None
    finish_at: datetime | None


@dataclass
class JobView:
    job_id: int
    action: str
    internal_status: str
    runs: list[RunView] | None
    description: str | None = None
    job_type: str | None = None
    created_at: datetime | None = None


async def get_job_with_runs(
    session: AsyncSession,
    *,
    user_id: str,
    job_id: int,
    include_runs: bool,
    run_limit: int = 10,
) -> JobView:
    """Return the job's current status and optionally its most recent runs.

    Raises JobNotFoundError if job_id does not exist or belongs to another user.
    Runs are ordered newest-first by start_at, falling back to scheduled_at when
    start_at is NULL. run_limit controls how many runs to return (default 10).
    """
    job_result = await session.execute(
        select(Job).where(Job.job_id == job_id, Job.user_id == user_id)
    )
    job = job_result.scalar_one_or_none()
    if job is None:
        raise JobNotFoundError(job_id)

    sort_key = func.coalesce(JobRun.start_at, JobRun.scheduled_at).desc()
    limit = run_limit if include_runs else 1
    runs_result = await session.execute(
        select(JobRun).where(JobRun.job_id == job_id).order_by(sort_key).limit(limit)
    )
    db_runs = runs_result.scalars().all()

    internal_status = db_runs[0].status if db_runs else "PENDING"
    run_views = [
        RunView(
            run_id=r.run_id,
            status=r.status,
            scheduled_at=r.scheduled_at,
            start_at=r.start_at,
            finish_at=r.finish_at,
        )
        for r in db_runs
    ]

    return JobView(
        job_id=job.job_id,
        action=job.action,
        internal_status=internal_status,
        runs=run_views if include_runs else None,
        description=job.description,
        job_type=job.job_type,
        created_at=job.created_at,
    )


# Statuses that represent natural job completion — not caused by a cancel request.
_NATURALLY_TERMINAL: frozenset[str] = frozenset({"SUCCEEDED", "FAILED"})
# Statuses that are safe to flip to CANCELLED; RUNNING runs are left to complete.
_CANCELLABLE_STATUSES: frozenset[str] = frozenset({"PENDING", "QUEUED", "WAITING", "RETRYING"})


async def cancel_job(
    session: AsyncSession,
    *,
    user_id: str,
    job_id: int,
) -> JobView:
    """Job-level cancel: set cancelled_at, flip non-RUNNING pending runs to CANCELLED.

    Best-effort semantics (ADR-022):
    - RUNNING runs are left untouched so they complete naturally.
    - Re-cancel on an already-cancelled job is idempotent (returns success).
    - INVALID_STATE only when all runs finished naturally (SUCCEEDED/FAILED).

    Raises JobNotFoundError if job_id does not exist or belongs to another user.
    Raises InvalidStateError(internal_status) if job fully terminated naturally.
    Returns a JobView reflecting post-cancel state (internal_status='CANCELLED').
    """
    async with session.begin():
        job_result = await session.execute(
            select(Job).where(Job.job_id == job_id, Job.user_id == user_id)
        )
        job = job_result.scalar_one_or_none()
        if job is None:
            raise JobNotFoundError(job_id)

        # Idempotent: cancelled_at is the single source of truth for "was this job cancelled".
        if job.cancelled_at is not None:
            return JobView(
                job_id=job.job_id,
                action=job.action,
                internal_status="CANCELLED",
                runs=None,
            )

        runs_result = await session.execute(select(JobRun).where(JobRun.job_id == job_id))
        all_runs = runs_result.scalars().all()

        # INVALID_STATE only when every run completed naturally (no in-flight or pending work).
        if all_runs and all(r.status in _NATURALLY_TERMINAL for r in all_runs):
            # Either SUCCEEDED or FAILED — both naturally-terminal, so any run's status
            # is a valid hint for the external error message.
            current_status = all_runs[0].status
            raise InvalidStateError(current_status)

        # Mark job as cancelled.
        job.cancelled_at = datetime.now(tz=UTC)

        # Flip only the pending/queued/waiting/retrying runs; leave RUNNING alone.
        to_cancel = [r for r in all_runs if r.status in _CANCELLABLE_STATUSES]
        for run in to_cancel:
            # Capture pre-update status before SQLAlchemy's synchronize_session mutates it.
            original_status = run.status
            await session.execute(
                update(JobRun)
                .where(JobRun.run_id == run.run_id, JobRun.time_bucket == run.time_bucket)
                .values(status="CANCELLED")
            )
            event = RunEvent(
                run_id=run.run_id,
                job_id=job_id,
                event_type="CANCELLED",
                status_from=original_status,
                status_to="CANCELLED",
            )
            session.add(event)

    return JobView(
        job_id=job.job_id,
        action=job.action,
        internal_status="CANCELLED",
        runs=None,
    )


@dataclass
class JobListItem:
    job_id: int
    action: str
    created_at: datetime
    internal_status: str


@dataclass
class PagedJobs:
    items: list[JobListItem] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


async def list_jobs(
    session: AsyncSession,
    *,
    user_id: str,
    status_filter: frozenset[str] | None = None,
    created_at_from: datetime | None = None,
    created_at_to: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PagedJobs:
    """Return paged jobs for user_id sorted newest-first via idx_jobs_user_created.

    status_filter: frozenset of *internal* statuses to include (caller maps external→internal).
    created_at_from / created_at_to: inclusive bounds on Job.created_at.
    page / page_size: 1-based offset pagination.
    """
    # Subquery: latest run status per job using DISTINCT ON (Postgres-specific).
    # Ordered by (job_id, newest run first) so DISTINCT ON picks the most recent run.
    latest_run_sq = (
        select(JobRun.job_id.label("job_id"), JobRun.status.label("status"))
        .distinct(JobRun.job_id)
        .order_by(JobRun.job_id, func.coalesce(JobRun.start_at, JobRun.scheduled_at).desc())
        .subquery("latest_run")
    )

    def _base_where(q):
        q = q.where(Job.user_id == user_id)
        if created_at_from is not None:
            q = q.where(Job.created_at >= created_at_from)
        if created_at_to is not None:
            q = q.where(Job.created_at <= created_at_to)
        if status_filter is not None:
            q = q.where(latest_run_sq.c.status.in_(list(status_filter)))
        return q

    # Count query — join to latest_run for status filtering
    count_q = _base_where(
        select(func.count())
        .select_from(Job)
        .outerjoin(latest_run_sq, Job.job_id == latest_run_sq.c.job_id)
    )
    total: int = (await session.execute(count_q)).scalar_one()

    # Data query — same join, ordered newest-first, with pagination
    data_q = (
        _base_where(
            select(Job, latest_run_sq.c.status.label("run_status")).outerjoin(
                latest_run_sq, Job.job_id == latest_run_sq.c.job_id
            )
        )
        .order_by(Job.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    rows = (await session.execute(data_q)).all()

    items = [
        JobListItem(
            job_id=row.Job.job_id,
            action=row.Job.action,
            created_at=row.Job.created_at,
            internal_status=row.run_status if row.run_status is not None else "PENDING",
        )
        for row in rows
    ]

    return PagedJobs(items=items, total=total, page=page, page_size=page_size)


@dataclass
class JobResourceItem:
    job_id: int
    description: str
    job_type: str
    created_at: datetime
    internal_status: str
    cancelled_at: datetime | None


async def list_jobs_resource(
    session: AsyncSession,
    *,
    user_id: str,
    limit: int = 20,
) -> tuple[list[JobResourceItem], int]:
    """Return the top *limit* jobs newest-first for the MCP R1 resource.

    Returns (items, total_count). Includes description, job_type, and
    cancelled_at derived from the most recent CANCELLED RunEvent per job.
    """
    # Subquery: latest run status per job (DISTINCT ON, Postgres-specific)
    latest_run_sq = (
        select(JobRun.job_id.label("job_id"), JobRun.status.label("status"))
        .distinct(JobRun.job_id)
        .order_by(JobRun.job_id, func.coalesce(JobRun.start_at, JobRun.scheduled_at).desc())
        .subquery("latest_run_r")
    )

    # Subquery: most recent CANCELLED event per job
    cancelled_sq = (
        select(
            RunEvent.job_id.label("job_id"),
            func.max(RunEvent.occurred_at).label("cancelled_at"),
        )
        .where(RunEvent.event_type == "CANCELLED")
        .group_by(RunEvent.job_id)
        .subquery("cancelled_r")
    )

    count_q = select(func.count()).select_from(Job).where(Job.user_id == user_id)
    total: int = (await session.execute(count_q)).scalar_one()

    data_q = (
        select(
            Job,
            latest_run_sq.c.status.label("run_status"),
            cancelled_sq.c.cancelled_at.label("cancelled_at"),
        )
        .where(Job.user_id == user_id)
        .outerjoin(latest_run_sq, Job.job_id == latest_run_sq.c.job_id)
        .outerjoin(cancelled_sq, Job.job_id == cancelled_sq.c.job_id)
        .order_by(Job.created_at.desc())
        .limit(limit)
    )

    rows = (await session.execute(data_q)).all()

    items = [
        JobResourceItem(
            job_id=row.Job.job_id,
            description=row.Job.description,
            job_type=row.Job.job_type,
            created_at=row.Job.created_at,
            internal_status=row.run_status if row.run_status is not None else "PENDING",
            cancelled_at=row.cancelled_at,
        )
        for row in rows
    ]

    return items, total
