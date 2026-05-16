"""Cron expression parsing and next-occurrence computation (ADR-016).

Accepts 5-field POSIX expressions and the 5 standard shortcuts:
  @daily  @hourly  @weekly  @monthly  @yearly

Rejects 6-field (second-level precision), @reboot, and @every.
DST transitions follow croniter defaults:
  spring-forward: skipped hours are advanced to the next valid wall-clock minute
  fall-back:      both wall-clock occurrences are yielded (one PDT, one PST)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

_ACCEPTED_SHORTCUTS: frozenset[str] = frozenset(
    {"@daily", "@hourly", "@weekly", "@monthly", "@yearly"}
)
_REJECTED_PREFIXES: tuple[str, ...] = ("@reboot", "@every")

_FIVE_FIELD_HINT = (
    "use a 5-field POSIX expression (minute hour dom month dow), "
    "e.g. '0 8 * * *', or one of: @daily @hourly @weekly @monthly @yearly"
)


def validate_cron_expr(cron_expr: str) -> tuple[bool, str | None]:
    """Check that *cron_expr* is an accepted cron string.

    Returns ``(True, None)`` on success or ``(False, hint)`` where *hint* is a
    short string suitable for returning as the ``expected`` field in a
    USER_INPUT error so LLM clients can self-correct.
    """
    expr = cron_expr.strip()
    lower = expr.lower()

    for prefix in _REJECTED_PREFIXES:
        if lower.startswith(prefix):
            return (
                False,
                f"'{prefix}' is not supported; {_FIVE_FIELD_HINT}",
            )

    if lower in _ACCEPTED_SHORTCUTS:
        return True, None

    parts = expr.split()
    if len(parts) == 6:
        return (
            False,
            f"6-field cron (with seconds field) is not supported; {_FIVE_FIELD_HINT}",
        )

    if not croniter.is_valid(expr):
        return (
            False,
            f"invalid cron expression '{expr}'; {_FIVE_FIELD_HINT}",
        )

    return True, None


def next_after(cron_expr: str, tz: ZoneInfo, ref: datetime) -> datetime:
    """Return the earliest cron fire time that is >= *ref* in timezone *tz*.

    Uses ``start_time = ref - 1µs`` so that a *ref* that falls exactly on a
    cron boundary is considered the "current" occurrence (inclusive semantics).

    The returned datetime is timezone-aware (same offset as the computed
    occurrence in *tz*).
    """
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=tz)
    # Convert to UTC before subtracting so Python preserves the correct wall-clock
    # fold on DST-ambiguous datetimes (fold attribute is dropped by arithmetic).
    start = (ref.astimezone(UTC) - timedelta(microseconds=1)).astimezone(tz)

    c = croniter(cron_expr, start_time=start)
    return c.get_next(datetime)
