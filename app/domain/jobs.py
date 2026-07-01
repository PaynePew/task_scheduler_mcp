"""Business logic for jobs — pure functions on (session, args).

Per ADR-010 this layer **knows nothing about MCP**. It accepts an
``AsyncSession``, mutates DB state, and raises domain exceptions. The MCP
handlers in ``app.mcp.handlers`` wrap each call, map exceptions to the
7-code error vocabulary, and shape the envelope.

Anything that writes a status transition also writes a matching
``RunEvent`` *in the same transaction* — the transactional outbox pattern
(CONTEXT.md §5). Downstream watchers consume the immutable event log,
never the mutable status column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.registry import ACTION_REGISTRY
from app.config.cron import next_after, validate_cron_expr
from app.config.timezone_resolver import resolve_timezone
from app.db.models import Job, JobRun, RunEvent
from app.domain.chain_validation import validate_chain, validate_run_source
from app.domain.run_materializer import (
    _acquire_job_lock,
    has_executing_run,
    materialize_initial,
)


class UnknownActionError(Exception):
    """Raised when action is not registered in ACTION_REGISTRY (maps to UNKNOWN_ACTION)."""


class OperatorOnlyActionError(Exception):
    """Raised when a non-operator caller attempts to create a requires_operator action (ADR-051).

    Carries the action name as its single arg.
    """


class JobNotFoundError(Exception):
    """Raised when job_id does not exist or belongs to another user (maps to NOT_FOUND)."""


class InvalidStateError(Exception):
    """Raised when a job cannot transition into the requested state (INVALID_STATE).

    For cancel_job, this means the job's schedule is already exhausted
    (``Job.state == 'completed'``) — there is no future work to stop — see
    ADR-068 §5 for the lifecycle and ADR-022 for the best-effort semantics.

    Carries a run-status hint as its single arg so the transport layer can map
    it to a user-facing external status (ADR-014): a succeeded one-shot maps to
    'completed', a failed one to 'failed'. The domain must not depend on the MCP
    presentation layer (ADR-010), so message formatting happens in the handler.
    """


class UnsupportedScheduleTypeError(Exception):
    """Raised when schedule_type is not in the supported set (maps to USER_INPUT).

    Supported: 'immediate', 'one-shot', 'recurring'. Passing anything else is
    rejected explicitly rather than silently degraded, which would mask
    scheduling bugs far from their cause.
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
_VALID_TRIGGER_ON_STATUS = frozenset({"SUCCEEDED", "FAILED", "ANY"})


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
    trigger_on_job_id: int | None = None,
    trigger_on_status: str | None = None,
    operator_user_id: str | None = None,
) -> Job:
    """Insert Job + JobRun + RunEvent(CREATED) in one transaction.

    When trigger_on_job_id is set, runs chain validation (V1-V5) but creates NO
    run: under continuation (ADR-067) a chained job has zero runs until its
    upstream terminates, when the continuation consumer materializes its first
    PENDING run.

    Returns the existing Job on (user_id, idempotency_key) collision.
    Raises UnknownActionError if action is not in ACTION_REGISTRY.
    Raises OperatorOnlyActionError if action requires_operator and caller is not the operator.
    Raises UnsupportedScheduleTypeError for unimplemented schedule_type values.
    Raises InvalidTimezoneError if timezone is not a valid IANA key.
    Raises InvalidScheduledAtError if scheduled_at is missing, unparseable, or past.
    Raises InvalidCronExprError if cron_expr is missing or invalid for recurring.
    Raises ChainRunSourceError if both trigger_on_job_id and cron_expr are set (V6).
    Raises ChainJobNotFoundError / ChainJobTerminatedError / ChainCycleError /
        ChainDepthError when trigger_on_job_id fails V1-V5 validation.
    """
    if schedule_type not in _SUPPORTED_SCHEDULE_TYPES:
        raise UnsupportedScheduleTypeError(schedule_type)
    if action not in ACTION_REGISTRY:
        raise UnknownActionError(action)
    handler = ACTION_REGISTRY[action]
    # Fail closed: a None operator_user_id means the operator identity is unknown,
    # so requires_operator actions must be denied (never short-circuit on None).
    if handler.requires_operator and user_id != operator_user_id:
        raise OperatorOnlyActionError(action)

    if trigger_on_status is not None and trigger_on_status not in _VALID_TRIGGER_ON_STATUS:
        raise ValueError(
            f"trigger_on_status must be one of {sorted(_VALID_TRIGGER_ON_STATUS)}, "
            f"got {trigger_on_status!r}"
        )

    # V6: reject jobs that declare both a cron_expr and a trigger_on_job_id (ADR-065).
    # Must run before cron parsing so the error is surfaced at the earliest opportunity
    # and no DB state is written.
    validate_run_source(trigger_on_job_id=trigger_on_job_id, cron_expr=cron_expr)

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

        # Chain validation (V1-V5) runs inside the same transaction so the
        # ancestor walk and the insert are atomic — no TOCTOU window. Under the
        # continuation model (ADR-067) nothing is pre-armed here; V3 rejects
        # chaining off a trigger whose Job.state is already terminal (it would
        # never fire again), while an active trigger — including a not-yet-fired
        # chained one — is allowed, which is what lets 3+-hop chains be created.
        if trigger_on_job_id is not None:
            await validate_chain(
                session,
                user_id=user_id,
                trigger_on_job_id=trigger_on_job_id,
            )

        effective_trigger_status = trigger_on_status or "SUCCEEDED"

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
            trigger_on_job_id=trigger_on_job_id,
            trigger_on_status=effective_trigger_status if trigger_on_job_id is not None else None,
        )
        session.add(job)
        await session.flush()

        # Delegate run creation to RunMaterializer (ADR-065, ADR-067). Only
        # schedule-driven jobs get an initial run; a trigger-driven (chained) job
        # has zero runs until its upstream terminates — its first run is created
        # by the continuation consumer (no pre-armed WAITING run).
        if trigger_on_job_id is None:
            await materialize_initial(session, job, run_at=run_at)

    return job


@dataclass
class RunView:
    run_id: int
    status: str
    scheduled_at: datetime
    start_at: datetime | None
    finish_at: datetime | None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class JobView:
    job_id: int
    action: str
    job_state: str
    # None when the job has no run yet (a not-yet-triggered chained downstream,
    # or one whose create predicate never matched) — distinct from any concrete
    # internal run status. The MCP boundary derives external status from
    # (job_state, latest_run_status) via to_external_job_status (ADR-067 §9).
    latest_run_status: str | None
    runs: list[RunView] | None
    description: str | None = None
    job_type: str | None = None
    created_at: datetime | None = None
    # trigger_on_job_id, surfaced as task.status's `triggered_by` for trigger-driven jobs.
    triggered_by: int | None = None
    # Set from the latest run regardless of include_runs, for surfacing in status responses.
    error_code: str | None = None
    error_message: str | None = None


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

    latest = db_runs[0] if db_runs else None
    run_views = [
        RunView(
            run_id=r.run_id,
            status=r.status,
            scheduled_at=r.scheduled_at,
            start_at=r.start_at,
            finish_at=r.finish_at,
            error_code=r.error_code,
            error_message=r.error_message,
        )
        for r in db_runs
    ]

    return JobView(
        job_id=job.job_id,
        action=job.action,
        job_state=job.state,
        latest_run_status=latest.status if latest else None,
        runs=run_views if include_runs else None,
        description=job.description,
        job_type=job.job_type,
        created_at=job.created_at,
        triggered_by=job.trigger_on_job_id,
        error_code=latest.error_code if latest else None,
        error_message=latest.error_message if latest else None,
    )


# Statuses that are safe to flip to CANCELLED; RUNNING runs are left to complete.
_CANCELLABLE_STATUSES: frozenset[str] = frozenset({"PENDING", "QUEUED", "RETRYING"})

# Parent lifecycle states that mean the trigger will produce no further upstream
# runs, so a chained downstream with no non-terminal run can settle (ADR-068 §2).
_PARENT_TERMINAL_STATES: frozenset[str] = frozenset({"completed", "cancelled"})


async def settle_job(session: AsyncSession, *, job_id: int) -> bool:
    """CAS ``Job.state`` active → completed for a finished one-shot/immediate job.

    Called by the executor in the SAME transaction that writes a one-shot run's
    terminal status (ADR-068 decision A). ``completed`` means the schedule is
    *exhausted* — NOT that the run succeeded; run success/failure stays on
    ``JobRun.status``, so a one-shot whose only run failed still settles to
    ``completed``.

    The transition is a compare-and-set from ``active`` (``UPDATE ... WHERE
    state='active'``) so it races safely against a concurrent ``task.cancel``:
    the first commit wins and the loser matches zero rows; either way the job
    leaves the active count. Recurring roots (``job_type='recurring'``) stay
    ``active`` until cancelled, and trigger-driven downstreams
    (``trigger_on_job_id`` set) are excluded here — their settle needs the
    trigger-parent check and downstream cascade, which the continuation consumer
    drives via ``settle_check`` (ADR-068 §2). This non-cascading self-settle is
    the executor's prompt one-shot/immediate path; the exclusions also make it a
    safe no-op when the executor settles a recurring/chained run.

    Returns True if this call performed the transition, False on a no-op (already
    terminal, recurring, or chained). Caller must be inside an open transaction.
    """
    result = await session.execute(
        update(Job)
        .where(
            Job.job_id == job_id,
            Job.state == "active",
            Job.job_type != "recurring",
            Job.trigger_on_job_id.is_(None),
        )
        .values(state="completed", updated_at=datetime.now(tz=UTC))
        .returning(Job.job_id)
        # No session sync: the executor never holds the Job ORM instance here, and
        # we read only RETURNING — avoid the evaluate strategy mutating in-memory
        # state on a zero-row CAS (anti-pattern #1).
        .execution_options(synchronize_session=False)
    )
    return result.first() is not None


async def _settle_downstreams(session: AsyncSession, *, parent_job_id: int) -> None:
    """``settle_check`` every job triggered by *parent_job_id* — the one-hop cascade.

    A just-settled or just-cancelled upstream may release its not-yet-run
    downstreams (ADR-068 §2). Shared by ``settle_check`` (post-settle) and
    ``cancel_job`` (post-cancel). Caller must be inside an open transaction.
    """
    downstream_ids = (
        (await session.execute(select(Job.job_id).where(Job.trigger_on_job_id == parent_job_id)))
        .scalars()
        .all()
    )
    for downstream_id in downstream_ids:
        await settle_check(session, job_id=downstream_id)


async def settle_check(session: AsyncSession, *, job_id: int) -> bool:
    """CAS ``Job.state`` active → completed if the schedule is exhausted; cascade.

    Generalises ``settle_job`` to **trigger-driven (chained)** jobs and drives the
    one-hop parent propagation of ADR-068 §2. A job is exhausted — settled to
    ``completed`` — when ALL of:

    - it is currently ``active`` (else no-op — already terminal/cancelled);
    - it is not a recurring root (``cron_expr`` set) — those stay ``active`` until
      cancelled and keep producing runs;
    - it has **no non-terminal run** (``has_executing_run`` — an in-flight run is
      resident load and must finish first);
    - if it is trigger-driven (``trigger_on_job_id`` set), its trigger parent is
      terminal (``completed``/``cancelled``): an ``active`` parent may still fire
      another upstream run, so the downstream may still get a run.

    On settle, cascade ``settle_check`` to each downstream job (``trigger_on_job_id
    == job_id``): a settled/cancelled upstream lets its not-yet-run downstreams
    settle in turn. Chains are acyclic (validate_chain V4/V5), so the recursion
    terminates.

    Idempotent: a no-op (returns False) when the job is already terminal,
    recurring, still has a live run, or is waiting on an ``active`` parent — so it
    is safe to call redundantly (e.g. the executor already settled a one-shot).
    Caller must be inside an open transaction.
    """
    job = (await session.execute(select(Job).where(Job.job_id == job_id))).scalar_one_or_none()
    if job is None or job.state != "active":
        return False
    if job.cron_expr is not None:
        return False  # recurring root — only cancel ends it (ADR-068 §2)

    # Serialize per job before the non-terminal-run check so a concurrent
    # materialize_downstream cannot insert a run between the check and the CAS
    # (the has_executing_run contract, issue #237).
    await _acquire_job_lock(session, job_id)
    if await has_executing_run(session, job_id):
        return False  # in-flight run is resident load — leave it to finish

    if job.trigger_on_job_id is not None:
        parent_state = (
            await session.execute(select(Job.state).where(Job.job_id == job.trigger_on_job_id))
        ).scalar_one_or_none()
        # A missing parent (FK SET NULL on delete) can never fire again → settle.
        if parent_state == "active":
            return False  # parent still produces upstream runs

    settled = await session.execute(
        update(Job)
        .where(Job.job_id == job_id, Job.state == "active")
        .values(state="completed", updated_at=datetime.now(tz=UTC))
        .returning(Job.job_id)
        .execution_options(synchronize_session=False)
    )
    if settled.first() is None:
        return False  # lost the CAS to a concurrent cancel/settle

    # One-hop parent propagation: a just-settled upstream releases its downstreams.
    await _settle_downstreams(session, parent_job_id=job_id)
    return True


async def _terminal_status_hint(session: AsyncSession, job_id: int) -> str:
    """Latest run status for a terminal job — the hint InvalidStateError carries.

    The external 'cannot cancel' message distinguishes a succeeded one-shot
    ('completed') from a failed one ('failed'); Job.state='completed' alone does
    not carry that, so the hint comes from the run outcome. Defaults to SUCCEEDED
    (external 'completed') when the job has no runs.
    """
    status = (
        await session.execute(
            select(JobRun.status)
            .where(JobRun.job_id == job_id)
            .order_by(func.coalesce(JobRun.start_at, JobRun.scheduled_at).desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return status or "SUCCEEDED"


def _cancelled_view(job: Job) -> JobView:
    """A JobView reporting 'cancelled' regardless of a still-finishing run (ADR-022) —
    task.cancel.v1 always confirms the cancel intent, never a run's own outcome."""
    return JobView(
        job_id=job.job_id,
        action=job.action,
        job_state="cancelled",
        latest_run_status=None,
        runs=None,
    )


async def cancel_job(
    session: AsyncSession,
    *,
    user_id: str,
    job_id: int,
) -> JobView:
    """Job-level cancel: CAS ``Job.state`` active → cancelled, flip pending runs.

    Best-effort semantics (ADR-022, ADR-068):
    - The job's lifecycle is the source of truth: cancel is a compare-and-set
      from ``state='active'``, which races safely with a one-shot's settle (first
      commit wins). ``cancelled_at`` is retained as an audit timestamp only.
    - Pending/queued/retrying runs flip to CANCELLED so they never dispatch; a
      RUNNING run is left to finish naturally.
    - Cascade: a cancelled upstream stops its not-yet-run downstreams — each
      downstream settles to ``completed`` via ``settle_check`` (ADR-068 §2), so a
      cancelled pipeline stops end to end and frees quota.
    - Re-cancel on an already-cancelled job is idempotent (returns success).
    - INVALID_STATE when the schedule is already exhausted (``state='completed'``).

    Raises JobNotFoundError if job_id does not exist or belongs to another user.
    Raises InvalidStateError(hint) if the job is already completed.
    Returns a JobView reflecting post-cancel state (job_state='cancelled'); the
    caller is told 'cancelled' regardless of a still-finishing run (ADR-022).
    """
    async with session.begin():
        job = (
            await session.execute(select(Job).where(Job.job_id == job_id, Job.user_id == user_id))
        ).scalar_one_or_none()
        if job is None:
            raise JobNotFoundError(job_id)

        # Already cancelled → idempotent success.
        if job.state == "cancelled":
            return _cancelled_view(job)

        # Schedule already exhausted → nothing to cancel (ADR-068 §5).
        if job.state == "completed":
            raise InvalidStateError(await _terminal_status_hint(session, job_id))

        # CAS active → cancelled. The WHERE state='active' guard makes this atomic
        # against a concurrent settle (one-shot completion); the race loser
        # matches zero rows and is handled below.
        now = datetime.now(tz=UTC)
        cas = await session.execute(
            update(Job)
            .where(Job.job_id == job_id, Job.state == "active")
            .values(state="cancelled", cancelled_at=now, updated_at=now)
            .returning(Job.job_id)
            # No session sync: we keep reading the loaded `job` (its action/job_id)
            # and re-read state from the DB on a lost CAS — never the in-memory
            # value the evaluate strategy would mutate (anti-pattern #1).
            .execution_options(synchronize_session=False)
        )
        if cas.first() is None:
            # Lost the race: a concurrent settle/cancel moved us out of 'active'.
            fresh_state = (
                await session.execute(select(Job.state).where(Job.job_id == job_id))
            ).scalar_one()
            if fresh_state == "cancelled":
                return _cancelled_view(job)
            raise InvalidStateError(await _terminal_status_hint(session, job_id))

        # Flip only the pending/queued/retrying runs; leave RUNNING alone.
        runs = (
            (await session.execute(select(JobRun).where(JobRun.job_id == job_id))).scalars().all()
        )
        for run in runs:
            if run.status not in _CANCELLABLE_STATUSES:
                continue
            # Capture pre-update status before SQLAlchemy's synchronize_session mutates it.
            original_status = run.status
            await session.execute(
                update(JobRun)
                .where(JobRun.run_id == run.run_id, JobRun.time_bucket == run.time_bucket)
                .values(status="CANCELLED")
            )
            session.add(
                RunEvent(
                    run_id=run.run_id,
                    job_id=job_id,
                    event_type="CANCELLED",
                    status_from=original_status,
                    status_to="CANCELLED",
                )
            )

        # Cascade: a cancelled upstream will produce no more upstream runs, so its
        # not-yet-run downstreams settle to completed (ADR-068 §2). A downstream
        # with an in-flight run is left active by settle_check and settles when
        # that run terminates. Same transaction as the cancel, so the continuation
        # consumer (which skips a cancelled upstream) never re-creates a downstream
        # run for this cancelled pipeline.
        await _settle_downstreams(session, parent_job_id=job_id)

    return _cancelled_view(job)


@dataclass
class JobListItem:
    job_id: int
    action: str
    created_at: datetime
    job_state: str
    # None when the job has no run yet — see JobView.latest_run_status.
    latest_run_status: str | None


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
    job_state_filter: frozenset[str] | None = None,
    created_at_from: datetime | None = None,
    created_at_to: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PagedJobs:
    """Return paged jobs for user_id sorted newest-first via idx_jobs_user_created.

    status_filter: frozenset of *internal* run statuses to include (caller maps external→internal).
    job_state_filter: the ``Job.state`` values whose run-less jobs also match the requested
        external status (ADR-067). A chained downstream has no run until it fires, so its status
        is derived from ``Job.state`` alone (the same derivation the read path renders) — without
        this such jobs are shown in the unfiltered list yet silently dropped from any ``status=``
        filter. Only consulted alongside ``status_filter``; empty for ``running``/``failed`` (a
        run-less job is never either).
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
            match_run = latest_run_sq.c.status.in_(list(status_filter))
            if job_state_filter:
                # A run-less job (chained downstream not yet fired) has a NULL latest
                # run, so `NULL IN (...)` never matches — fall back to Job.state, the
                # same source the read path uses to render its status (ADR-067).
                match_run = or_(
                    match_run,
                    and_(
                        latest_run_sq.c.status.is_(None),
                        Job.state.in_(list(job_state_filter)),
                    ),
                )
            q = q.where(match_run)
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
            job_state=row.Job.state,
            latest_run_status=row.run_status,
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
    job_state: str
    # None when the job has no run yet — see JobView.latest_run_status.
    latest_run_status: str | None
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
            job_state=row.Job.state,
            latest_run_status=row.run_status,
            cancelled_at=row.cancelled_at,
        )
        for row in rows
    ]

    return items, total
