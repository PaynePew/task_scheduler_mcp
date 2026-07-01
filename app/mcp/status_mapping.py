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
