"""Unit tests for app/config/timezone_resolver.py.

Verifies the 4-step fallback priority: call > header > env > "UTC".
"""

from __future__ import annotations

import pytest

from app.config.timezone_resolver import resolve_timezone

# ---------------------------------------------------------------------------
# Step 1: call_value wins when valid
# ---------------------------------------------------------------------------


def test_call_value_takes_priority_over_all():
    result = resolve_timezone("America/New_York", "Europe/London", "Asia/Tokyo")
    assert result == "America/New_York"


def test_call_value_wins_over_header_and_env():
    result = resolve_timezone("Asia/Taipei", "UTC", "America/Chicago")
    assert result == "Asia/Taipei"


# ---------------------------------------------------------------------------
# Step 2: header_value used when call_value absent/invalid
# ---------------------------------------------------------------------------


def test_header_value_used_when_call_absent():
    result = resolve_timezone(None, "Europe/Berlin", "America/Denver")
    assert result == "Europe/Berlin"


def test_header_value_used_when_call_invalid():
    result = resolve_timezone("Not/ATimezone", "Europe/Paris", None)
    assert result == "Europe/Paris"


def test_header_value_used_when_call_empty():
    result = resolve_timezone("", "Australia/Sydney", None)
    assert result == "Australia/Sydney"


# ---------------------------------------------------------------------------
# Step 3: env_value used when call and header absent/invalid
# ---------------------------------------------------------------------------


def test_env_value_used_when_call_and_header_absent():
    result = resolve_timezone(None, None, "America/Los_Angeles")
    assert result == "America/Los_Angeles"


def test_env_value_used_when_call_invalid_header_invalid():
    result = resolve_timezone("bad", "also-bad", "Pacific/Auckland")
    assert result == "Pacific/Auckland"


# ---------------------------------------------------------------------------
# Step 4: UTC fallback when everything is absent/invalid
# ---------------------------------------------------------------------------


def test_utc_fallback_all_none():
    result = resolve_timezone(None, None, None)
    assert result == "UTC"


def test_utc_fallback_all_invalid():
    result = resolve_timezone("Taipei Standard Time", "UTC+8", "+08:00")
    assert result == "UTC"


def test_utc_fallback_all_empty():
    result = resolve_timezone("", "", "")
    assert result == "UTC"


# ---------------------------------------------------------------------------
# IANA validity — offset strings and Windows IDs are rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_tz",
    [
        "UTC+8",
        "+08:00",
        "UTC+05:30",
        "Taipei Standard Time",
        "Eastern Standard Time",
        "not_a_tz",
        " ",
    ],
)
def test_invalid_tz_strings_fall_through(bad_tz):
    """Non-IANA values are silently skipped; fallback continues."""
    result = resolve_timezone(bad_tz, None, None)
    assert result == "UTC"


@pytest.mark.parametrize(
    "good_tz",
    [
        "UTC",
        "Asia/Taipei",
        "America/New_York",
        "Europe/London",
        "Pacific/Auckland",
    ],
)
def test_valid_iana_keys_accepted(good_tz):
    result = resolve_timezone(good_tz, None, None)
    assert result == good_tz
