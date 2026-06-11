"""Chain-creation validation rules V1-V5 (ADR-020).

Called from create_job when trigger_on_job_id is set.  Pure domain — no MCP
knowledge.  Raises typed exceptions that map_domain_error (in app.mcp.errors)
maps to the 6-code error vocabulary.

V1  Trigger Job exists                           → ChainJobNotFoundError
V2  Trigger Job has same user_id as caller       → ChainJobNotFoundError (404, not 403)
V3  Trigger Job is not already fully terminated  → ChainJobTerminatedError
V4  No cycle via trigger_on_job_id ancestry      → ChainCycleError
V5  Chain depth ≤ 10 from new job through ancs   → ChainDepthError

V4 + V5 share one recursive CTE that walks ancestors of trigger_on_job_id.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, JobRun

# Terminal statuses — a job whose only runs are in this set is fully done.
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

MAX_CHAIN_DEPTH = 10


class ChainJobNotFoundError(Exception):
    """V1/V2: trigger_on_job_id doesn't exist or belongs to another user."""


class ChainJobTerminatedError(Exception):
    """V3: trigger job is already fully terminated; waiting on it would hang forever."""


class ChainCycleError(Exception):
    """V4: ancestor walk via trigger_on_job_id forms a cycle."""


class ChainDepthError(Exception):
    """V5: ancestor chain depth would exceed MAX_CHAIN_DEPTH including the new job."""


# Recursive CTE: walk ancestors of :start_id via trigger_on_job_id.
# Returns (max_depth, has_cycle) where depth counts the trigger job as 1.
# The new job being created adds 1 more, so we reject if max_depth >= MAX_CHAIN_DEPTH.
_ANCESTORS_CTE = text(
    """
    WITH RECURSIVE ancestors(cur_id, next_id, depth, path, is_cycle) AS (
        SELECT
            j.job_id            AS cur_id,
            j.trigger_on_job_id AS next_id,
            1                   AS depth,
            ARRAY[j.job_id]     AS path,
            false               AS is_cycle
        FROM jobs j
        WHERE j.job_id = :start_id

        UNION ALL

        SELECT
            j.job_id            AS cur_id,
            j.trigger_on_job_id AS next_id,
            a.depth + 1,
            a.path || j.job_id,
            j.job_id = ANY(a.path) AS is_cycle
        FROM ancestors a
        JOIN jobs j ON j.job_id = a.next_id
        WHERE a.next_id IS NOT NULL
          AND NOT a.is_cycle
          AND a.depth < :max_depth + 2
    )
    SELECT MAX(depth) AS max_depth, bool_or(is_cycle) AS has_cycle
    FROM ancestors
    """
)


async def validate_chain(
    session: AsyncSession,
    *,
    user_id: str,
    trigger_on_job_id: int,
) -> JobRun:
    """Enforce V1-V5 and return the upstream run that the new job should wait on.

    The returned JobRun is the upstream job's most-recent non-terminal run.
    Raises ChainJobNotFoundError, ChainJobTerminatedError, ChainCycleError, or
    ChainDepthError on violation.  Caller must be inside an open transaction.
    """
    # V1 + V2: trigger job must exist AND belong to the same user.
    trigger_job = (
        await session.execute(select(Job).where(Job.job_id == trigger_on_job_id))
    ).scalar_one_or_none()

    if trigger_job is None:
        raise ChainJobNotFoundError(trigger_on_job_id)

    # V2: intentional 404, not 403, to prevent job-id enumeration.
    if trigger_job.user_id != user_id:
        raise ChainJobNotFoundError(trigger_on_job_id)

    # V3: find the most-recent non-terminal run of the trigger job.
    #     If none exists, the trigger job is fully terminated — reject.
    runs_result = await session.execute(
        select(JobRun).where(JobRun.job_id == trigger_on_job_id).order_by(JobRun.run_id.desc())
    )
    all_runs = runs_result.scalars().all()

    wait_run: JobRun | None = None
    for run in all_runs:
        if run.status not in _TERMINAL:
            wait_run = run
            break

    if wait_run is None:
        raise ChainJobTerminatedError(trigger_on_job_id)

    # V4 + V5: recursive CTE walks ancestors of trigger_on_job_id.
    row = (
        await session.execute(
            _ANCESTORS_CTE,
            {"start_id": trigger_on_job_id, "max_depth": MAX_CHAIN_DEPTH},
        )
    ).one()

    if row.has_cycle:
        raise ChainCycleError(trigger_on_job_id)

    # depth counts ancestors including the trigger job itself (depth=1).
    # The new job adds 1 more → total chain length = max_depth + 1.
    # Reject if that would exceed MAX_CHAIN_DEPTH.
    if (row.max_depth or 0) >= MAX_CHAIN_DEPTH:
        raise ChainDepthError(trigger_on_job_id)

    return wait_run
