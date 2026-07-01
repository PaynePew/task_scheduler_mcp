"""Canonical internal→external status mapping (CONTEXT.md §2).

Kept in one place so both the task.status.v1 handler and S10b's status_filter
share a single source of truth.
"""

from __future__ import annotations

_MAPPING: dict[str, str] = {
    "PENDING": "scheduled",
    "QUEUED": "scheduled",
    "RUNNING": "running",
    "RETRYING": "running",
    "SUCCEEDED": "completed",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
}

_REVERSE_MAPPING: dict[str, frozenset[str]] = {
    "scheduled": frozenset({"PENDING", "QUEUED"}),
    "running": frozenset({"RUNNING", "RETRYING"}),
    "completed": frozenset({"SUCCEEDED"}),
    "failed": frozenset({"FAILED"}),
    "cancelled": frozenset({"CANCELLED"}),
}

# Job.state (ADR-068) -> external status, used only when a job has no run yet
# to map from (see to_external_job_status).
_JOB_STATE_MAPPING: dict[str, str] = {
    "active": "scheduled",
    "completed": "completed",
    "cancelled": "cancelled",
}


def to_external(internal_status: str) -> str:
    """Map one of the 7 internal statuses to one of the 5 external statuses."""
    try:
        return _MAPPING[internal_status]
    except KeyError:
        raise ValueError(f"Unknown internal status: {internal_status!r}") from None


def to_internal_set(external_status: str) -> frozenset[str]:
    """Map one of the 5 external statuses to the set of internal statuses it covers."""
    try:
        return _REVERSE_MAPPING[external_status]
    except KeyError:
        raise ValueError(f"Unknown external status: {external_status!r}") from None


def to_job_states(external_status: str) -> frozenset[str]:
    """Map an external status to the ``Job.state`` values a **run-less** job needs to match it.

    Inverse of ``_JOB_STATE_MAPPING`` — the same table ``to_external_job_status``
    uses to render a job with no run yet. A ``status=`` filter joins on the latest
    run, but a chained downstream has zero runs until it fires (ADR-067), so the
    filter must additionally admit run-less jobs whose ``Job.state`` maps here.

    ``running`` and ``failed`` return the empty set: a job with no run is never
    running or failed, so the run-less fallback must not include one.
    """
    if external_status not in _REVERSE_MAPPING:
        raise ValueError(f"Unknown external status: {external_status!r}")
    return frozenset(
        state for state, external in _JOB_STATE_MAPPING.items() if external == external_status
    )


def to_external_job_status(*, job_state: str, latest_run_status: str | None) -> str:
    """Derive the external status from ``(Job.state, latest run)`` — ADR-067 §9.

    A job with no run yet — a not-yet-triggered chained downstream, a
    trigger-driven job whose create predicate never matched, or one cancelled
    before it ever fired — has no run status to map from, so ``Job.state``
    alone determines the external status (``active``->``scheduled``,
    ``completed``->``completed``, ``cancelled``->``cancelled``). Once a run
    exists, the run's own status is authoritative: e.g. a ``RUNNING`` run left
    to finish after a job-level cancel (ADR-022 best-effort semantics) is
    reported as ``running``, not ``cancelled``, until it actually terminates.
    """
    if latest_run_status is not None:
        return to_external(latest_run_status)
    try:
        return _JOB_STATE_MAPPING[job_state]
    except KeyError:
        raise ValueError(f"Unknown job state: {job_state!r}") from None
