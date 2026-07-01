"""Chain-creation validation rules V1-V6 (ADR-020, ADR-065).

Called from create_job.  Pure domain — no MCP knowledge.  Raises typed
exceptions that map_domain_error (in app.mcp.errors) maps to the 7-code
error vocabulary.

V1  Trigger Job exists                           → ChainJobNotFoundError
V2  Trigger Job has same user_id as caller       → ChainJobNotFoundError (404, not 403)
V3  Trigger Job's lifecycle is not terminal      → ChainJobTerminatedError
    (Job.state = 'active', ADR-068)
V4  No cycle via trigger_on_job_id ancestry      → ChainCycleError
V5  Chain depth ≤ 10 from new job through ancs   → ChainDepthError
V6  trigger_on_job_id and cron_expr are mutually → ChainRunSourceError
    exclusive run sources (ADR-065)

V4 + V5 share one recursive CTE that walks ancestors of trigger_on_job_id.
V6 is a synchronous pre-check called from create_job before V1-V5.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job

MAX_CHAIN_DEPTH = 10


class ChainJobNotFoundError(Exception):
    """V1/V2: trigger_on_job_id doesn't exist or belongs to another user."""


class ChainJobTerminatedError(Exception):
    """V3: trigger job is already fully terminated; waiting on it would hang forever."""


class ChainCycleError(Exception):
    """V4: ancestor walk via trigger_on_job_id forms a cycle."""


class ChainDepthError(Exception):
    """V5: ancestor chain depth would exceed MAX_CHAIN_DEPTH including the new job."""


class ChainRunSourceError(Exception):
    """V6: trigger_on_job_id and cron_expr are mutually exclusive run sources (ADR-065).

    A chained (trigger-driven) job carries no cron of its own; it recurs
    because its trigger recurs (inherited recurrence).  Declaring both would
    create a double-firing, half-broken job.
    """


def validate_run_source(
    *,
    trigger_on_job_id: int | None,
    cron_expr: str | None,
) -> None:
    """V6: reject a job that declares both a cron_expr and a trigger_on_job_id.

    These are mutually-exclusive run sources per ADR-065 (run-source dichotomy):
    - Schedule-driven: cron_expr set, trigger_on_job_id absent.
    - Trigger-driven:  trigger_on_job_id set, cron_expr absent (inherited recurrence).

    Raises ChainRunSourceError when both are set.  No-ops otherwise.
    This is a pure synchronous guard — no DB access required.
    """
    if trigger_on_job_id is not None and cron_expr:
        raise ChainRunSourceError(
            "trigger_on_job_id and cron_expr are mutually exclusive: "
            "a chained job inherits recurrence from its trigger and must not carry its own cron."
        )


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
) -> None:
    """Enforce chain-creation rules V1-V5 for a new job's trigger_on_job_id.

    Raises ChainJobNotFoundError, ChainJobTerminatedError, ChainCycleError, or
    ChainDepthError on violation.  Caller must be inside an open transaction.

    Returns nothing: under continuation (ADR-067) a chained downstream is not
    pre-armed, so ``wait_for_run_id`` is set by the continuation consumer when it
    materialises the downstream run — never here at create time.
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

    # V3: reject only when the trigger job's lifecycle is already terminal —
    #     Job.state 'completed'/'cancelled' (ADR-068). Such a job will never emit
    #     another terminal event, so waiting on it would hang forever. An 'active'
    #     trigger is always chainable, whether it is a scheduled job about to run
    #     or a chained job still waiting for its own trigger.
    #
    #     This MUST key on Job.state, not on "does the trigger have a non-terminal
    #     run?": continuation (ADR-067) removed the pre-armed WAITING run, so a
    #     not-yet-fired chained job now has zero runs. The old run-scan therefore
    #     mis-classified an active chained job as terminated and rejected every
    #     3+-hop chain (e.g. github_digest → slack_post → email_send).
    if trigger_job.state != "active":
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
