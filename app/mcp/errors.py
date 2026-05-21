"""Maps domain exceptions to the 7-code MCP error vocabulary per ADR-014 / CONTEXT.md §6."""

from __future__ import annotations

from typing import Any

from app.domain.chain_validation import (
    ChainCycleError,
    ChainDepthError,
    ChainJobNotFoundError,
    ChainJobTerminatedError,
)
from app.domain.jobs import (
    InvalidCronExprError,
    InvalidScheduledAtError,
    InvalidStateError,
    InvalidTimezoneError,
    JobNotFoundError,
    OperatorOnlyActionError,
    UnknownActionError,
    UnsupportedScheduleTypeError,
)
from app.mcp.envelope import error


def map_domain_error(exc: Exception) -> dict[str, Any]:
    """Return an error envelope for a domain exception.

    Codes: USER_INPUT | NOT_FOUND | INVALID_STATE | UNKNOWN_ACTION
           | DUPLICATE | INTERNAL | MISSING_CONNECTION
    """
    if isinstance(exc, UnknownActionError):
        return error(
            "UNKNOWN_ACTION",
            f"Unknown action: {exc}",
            field="action",
            expected="echo",
        )
    if isinstance(exc, OperatorOnlyActionError):
        return error(
            "INVALID_STATE",
            f"Action '{exc}' is restricted to the operator.",
            field="action",
        )
    if isinstance(exc, UnsupportedScheduleTypeError):
        return error(
            "USER_INPUT",
            f"Unsupported schedule_type '{exc}'.",
            field="schedule_type",
            expected="immediate, one-shot, or recurring",
        )
    if isinstance(exc, InvalidCronExprError):
        return error(
            "USER_INPUT",
            str(exc),
            field="cron_expr",
            expected=(
                "5-field POSIX expression (minute hour dom month dow), "
                "e.g. '0 8 * * *', or @daily/@hourly/@weekly/@monthly/@yearly"
            ),
        )
    if isinstance(exc, InvalidScheduledAtError):
        return error(
            "USER_INPUT",
            str(exc),
            field="scheduled_at",
            expected="ISO 8601 datetime in the future",
        )
    if isinstance(exc, InvalidTimezoneError):
        return error(
            "USER_INPUT",
            f"Invalid timezone '{exc}'.",
            field="timezone",
            expected='IANA timezone key (e.g. "Asia/Taipei", "UTC")',
        )
    if isinstance(exc, JobNotFoundError):
        return error("NOT_FOUND", f"Job not found: {exc}")
    if isinstance(exc, InvalidStateError):
        return error("INVALID_STATE", str(exc))
    # Chain validation errors (V1-V5, ADR-020)
    if isinstance(exc, ChainJobNotFoundError):
        return error(
            "NOT_FOUND",
            f"Trigger job not found: {exc}",
            field="trigger_on_job_id",
        )
    if isinstance(exc, ChainJobTerminatedError):
        return error(
            "INVALID_STATE",
            f"Trigger job {exc} is already fully terminated; chaining on it would wait forever.",
            field="trigger_on_job_id",
        )
    if isinstance(exc, ChainCycleError):
        return error(
            "USER_INPUT",
            f"Trigger job {exc} forms a cycle in the chain ancestry.",
            field="trigger_on_job_id",
            expected="non-circular chain",
        )
    if isinstance(exc, ChainDepthError):
        return error(
            "USER_INPUT",
            f"Chain depth would exceed the maximum of 10 (trigger_on_job_id={exc}).",
            field="trigger_on_job_id",
            expected="chain depth ≤ 10",
        )
    if isinstance(exc, ValueError):
        return error("USER_INPUT", str(exc))
    return error("INTERNAL", "An unexpected error occurred.")
