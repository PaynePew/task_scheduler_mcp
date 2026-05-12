"""Canonical internal→external status mapping (CONTEXT.md §2).

Kept in one place so both the task.status@v1 handler and S10b's status_filter
share a single source of truth.
"""

from __future__ import annotations

_MAPPING: dict[str, str] = {
    "PENDING": "scheduled",
    "QUEUED": "scheduled",
    "WAITING": "scheduled",
    "RUNNING": "running",
    "RETRYING": "running",
    "SUCCEEDED": "completed",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
}


def to_external(internal_status: str) -> str:
    """Map one of the 8 internal statuses to one of the 5 external statuses."""
    try:
        return _MAPPING[internal_status]
    except KeyError:
        raise ValueError(f"Unknown internal status: {internal_status!r}") from None
