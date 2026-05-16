"""Unit tests for app/config/cron.py.

Covers: next_after inclusive semantics, DST spring-forward, DST fall-back,
accepted/rejected cron expressions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config.cron import next_after, validate_cron_expr

UTC = UTC
LA = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# next_after — basic behaviour
# ---------------------------------------------------------------------------


def test_next_after_inclusive_of_ref():
    """If ref falls exactly on a cron boundary the same moment is returned."""
    ref = datetime(2026, 5, 16, 8, 0, 0, tzinfo=UTC)
    nxt = next_after("0 8 * * *", ZoneInfo("UTC"), ref)
    assert nxt == ref


def test_next_after_advances_one_period():
    """ref one second past boundary → next occurrence is the following day."""
    ref = datetime(2026, 5, 16, 8, 0, 1, tzinfo=UTC)
    nxt = next_after("0 8 * * *", ZoneInfo("UTC"), ref)
    assert nxt == datetime(2026, 5, 17, 8, 0, 0, tzinfo=UTC)


def test_next_after_returns_aware_datetime():
    """next_after always returns a timezone-aware datetime."""
    ref = datetime(2026, 5, 16, 7, 59, 0, tzinfo=UTC)
    nxt = next_after("0 8 * * *", ZoneInfo("UTC"), ref)
    assert nxt.tzinfo is not None


# ---------------------------------------------------------------------------
# next_after — DST spring-forward (America/Los_Angeles, 2026-03-08)
# Clocks jump from 2:00 AM to 3:00 AM, so 2:00 AM never exists.
# ---------------------------------------------------------------------------


def test_dst_spring_forward_skips_to_next_valid_wall_clock():
    """0 2 * * * on spring-forward day → croniter skips to 3:00 AM wall-clock."""
    # 2026 US spring-forward: 2026-03-08 at 02:00 → 03:00
    ref = datetime(2026, 3, 8, 1, 59, 0, tzinfo=LA)
    nxt = next_after("0 2 * * *", LA, ref)
    nxt_la = nxt.astimezone(LA)
    assert nxt_la.date() == date(2026, 3, 8)
    # 2 AM does not exist; croniter advances to 3:00 AM (-07:00 PDT)
    assert nxt_la.hour == 3
    assert nxt_la.minute == 0
    assert nxt_la.utcoffset() == timedelta(hours=-7)


# ---------------------------------------------------------------------------
# next_after — DST fall-back (America/Los_Angeles, 2026-11-01)
# Clocks fall from 2:00 AM back to 1:00 AM, so 1:30 AM occurs twice.
# ---------------------------------------------------------------------------


def test_dst_fall_back_first_occurrence_correct():
    """30 1 * * * on fall-back day: ref before the fold → next is first 1:30 PDT."""
    ref = datetime(2026, 11, 1, 1, 0, 0, tzinfo=LA)  # 1:00 AM PDT, before the fold
    nxt = next_after("30 1 * * *", LA, ref)
    nxt_la = nxt.astimezone(LA)
    assert nxt_la.date() == date(2026, 11, 1)
    assert nxt_la.hour == 1
    assert nxt_la.minute == 30
    assert nxt_la.utcoffset() == timedelta(hours=-7)  # PDT


def test_dst_fall_back_fires_once_not_twice():
    """30 1 * * * on fall-back day fires once per occurrence, not stuck in a loop.

    After the SECOND (PST, fold=1) occurrence the next firing is the following
    day — confirming that two firings happen on the transition day (one PDT, one
    PST) and then the schedule advances normally.
    """
    # Just after the second (PST fold) 1:30 AM occurrence
    ref_pst = datetime(2026, 11, 1, 1, 35, 0, tzinfo=LA, fold=1)
    assert ref_pst.utcoffset() == timedelta(hours=-8), "sanity-check: fold=1 gives PST"

    nxt = next_after("30 1 * * *", LA, ref_pst)
    nxt_la = nxt.astimezone(LA)

    # Must advance to the NEXT day, not fire again on 2026-11-01
    assert nxt_la.date() == date(2026, 11, 2), (
        "After the PST fold occurrence next_after must return the following day"
    )
    assert nxt_la.hour == 1
    assert nxt_la.minute == 30


# ---------------------------------------------------------------------------
# validate_cron_expr — accepted expressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "0 8 * * *",
        "*/15 * * * *",
        "0 0 1 * *",
        "30 6 * * 1-5",
        "@daily",
        "@hourly",
        "@weekly",
        "@monthly",
        "@yearly",
        # shortcuts are case-insensitive per our strip+lower
        "@Daily",
        "@HOURLY",
    ],
)
def test_validate_cron_expr_accepted(expr):
    ok, hint = validate_cron_expr(expr)
    assert ok is True, f"Expected {expr!r} to be accepted; got hint={hint!r}"
    assert hint is None


# ---------------------------------------------------------------------------
# validate_cron_expr — rejected expressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "0 0 8 * * *",  # 6-field (seconds)
        "* * * * * *",  # 6-field
        "@reboot",
        "@REBOOT",
        "@every 5m",
        "@every 1h",
        "not_a_cron",
        "61 25 * * *",  # out-of-range fields
    ],
)
def test_validate_cron_expr_rejected(expr):
    ok, hint = validate_cron_expr(expr)
    assert ok is False, f"Expected {expr!r} to be rejected"
    assert hint is not None and len(hint) > 0, "Rejected expression must include a hint"


def test_validate_cron_expr_rejected_hint_is_helpful():
    """Rejected hint should mention 5-field or POSIX so an LLM can self-correct."""
    _, hint = validate_cron_expr("0 0 8 * * *")
    assert hint is not None
    assert "5-field" in hint or "POSIX" in hint or "minute" in hint
