"""Unit tests for app/ics/parser.py — pure ICS parsing, no network or DB."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from app.ics.parser import CalendarEvent, InvalidICSError, parse_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ics"
_UTC = datetime.UTC


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _range(start: str, end: str) -> tuple[datetime.date, datetime.date]:
    fmt = "%Y-%m-%d"
    return (
        datetime.datetime.strptime(start, fmt).date(),
        datetime.datetime.strptime(end, fmt).date(),
    )


_MAY_19 = _range("2026-05-19", "2026-05-19")
_MAY_19_20 = _range("2026-05-19", "2026-05-20")
_MAY_20 = _range("2026-05-20", "2026-05-20")
_MAY_19_23 = _range("2026-05-19", "2026-05-23")


# ---------------------------------------------------------------------------
# single event
# ---------------------------------------------------------------------------


def test_single_event_returns_one_result():
    events = parse_events(_load("single_event.ics"), _MAY_19)
    assert len(events) == 1


def test_single_event_fields():
    events = parse_events(_load("single_event.ics"), _MAY_19)
    e = events[0]
    assert e.summary == "Team Standup"
    assert e.description == "Daily standup meeting"
    assert e.location == "Conf Room A"
    assert e.url == "https://meet.example.com/standup"


def test_single_event_start_is_utc_aware():
    events = parse_events(_load("single_event.ics"), _MAY_19)
    e = events[0]
    assert e.start.tzinfo is not None
    assert e.start == datetime.datetime(2026, 5, 19, 9, 0, 0, tzinfo=_UTC)


def test_single_event_end_is_utc_aware():
    events = parse_events(_load("single_event.ics"), _MAY_19)
    e = events[0]
    assert e.end.tzinfo is not None
    assert e.end == datetime.datetime(2026, 5, 19, 10, 0, 0, tzinfo=_UTC)


def test_single_event_out_of_range_returns_empty():
    events = parse_events(_load("single_event.ics"), _MAY_20)
    assert events == []


def test_single_event_result_is_calendar_event_model():
    events = parse_events(_load("single_event.ics"), _MAY_19)
    assert isinstance(events[0], CalendarEvent)


# ---------------------------------------------------------------------------
# multi-event calendar
# ---------------------------------------------------------------------------


def test_multi_event_both_returned_in_range():
    events = parse_events(_load("multi_event.ics"), _MAY_19)
    assert len(events) == 2


def test_multi_event_sorted_by_start():
    events = parse_events(_load("multi_event.ics"), _MAY_19)
    starts = [e.start for e in events]
    assert starts == sorted(starts)


def test_multi_event_cross_day_range():
    events = parse_events(_load("multi_event.ics"), _MAY_19_20)
    assert len(events) == 3


def test_multi_event_single_day_excludes_next_day():
    events = parse_events(_load("multi_event.ics"), _MAY_19)
    summaries = {e.summary for e in events}
    assert "Next Day Meeting" not in summaries


# ---------------------------------------------------------------------------
# recurring events (RRULE expansion)
# ---------------------------------------------------------------------------


def test_recurring_event_single_day_returns_one():
    events = parse_events(_load("recurring_event.ics"), _MAY_19)
    assert len(events) == 1


def test_recurring_event_five_day_range_returns_five():
    events = parse_events(_load("recurring_event.ics"), _MAY_19_23)
    assert len(events) == 5


def test_recurring_event_occurrences_correct_dates():
    events = parse_events(_load("recurring_event.ics"), _MAY_19_23)
    starts = [e.start.date() for e in events]
    expected = [
        datetime.date(2026, 5, 19),
        datetime.date(2026, 5, 20),
        datetime.date(2026, 5, 21),
        datetime.date(2026, 5, 22),
        datetime.date(2026, 5, 23),
    ]
    assert starts == expected


def test_recurring_event_preserves_duration():
    events = parse_events(_load("recurring_event.ics"), _MAY_19_23)
    for e in events:
        assert e.end - e.start == datetime.timedelta(minutes=30)


# ---------------------------------------------------------------------------
# all-day events
# ---------------------------------------------------------------------------


def test_all_day_event_returned_in_range():
    events = parse_events(_load("all_day_event.ics"), _MAY_19)
    assert len(events) == 1


def test_all_day_event_start_is_midnight_utc():
    events = parse_events(_load("all_day_event.ics"), _MAY_19)
    e = events[0]
    assert e.start.tzinfo is not None
    assert e.start.hour == 0 and e.start.minute == 0


def test_all_day_event_summary():
    events = parse_events(_load("all_day_event.ics"), _MAY_19)
    assert events[0].summary == "Company Holiday"


# ---------------------------------------------------------------------------
# malformed ICS
# ---------------------------------------------------------------------------


def test_malformed_ics_raises_invalid_ics_error():
    with pytest.raises(InvalidICSError):
        parse_events(_load("malformed.ics"), _MAY_19)


def test_malformed_ics_error_message_useful():
    with pytest.raises(InvalidICSError, match="Failed to parse ICS data"):
        parse_events(_load("malformed.ics"), _MAY_19)


# ---------------------------------------------------------------------------
# empty calendar
# ---------------------------------------------------------------------------


def test_empty_calendar_returns_empty_list():
    events = parse_events(_load("empty_calendar.ics"), _MAY_19)
    assert events == []


# ---------------------------------------------------------------------------
# mixed timezones
# ---------------------------------------------------------------------------


def test_mixed_timezone_all_events_utc_aware():
    events = parse_events(_load("mixed_timezone.ics"), _MAY_19)
    for e in events:
        assert e.start.tzinfo is not None


def test_mixed_timezone_ny_event_normalized_to_utc():
    """America/New_York is UTC-4 in summer; 09:00 NY = 13:00 UTC."""
    events = parse_events(_load("mixed_timezone.ics"), _MAY_19)
    ny_events = [e for e in events if "NY" in e.summary]
    assert len(ny_events) == 1
    assert ny_events[0].start.hour == 13  # 09:00 EDT = 13:00 UTC


def test_mixed_timezone_two_events_returned():
    events = parse_events(_load("mixed_timezone.ics"), _MAY_19)
    assert len(events) == 2


# ---------------------------------------------------------------------------
# title_contains filter
# ---------------------------------------------------------------------------


def test_title_contains_filters_matching_events():
    events = parse_events(_load("multi_event.ics"), _MAY_19, title_contains="Standup")
    assert len(events) == 1
    assert events[0].summary == "Morning Standup"


def test_title_contains_case_insensitive():
    events = parse_events(_load("multi_event.ics"), _MAY_19, title_contains="standup")
    assert len(events) == 1


def test_title_contains_no_match_returns_empty():
    events = parse_events(_load("multi_event.ics"), _MAY_19, title_contains="Nonexistent")
    assert events == []


def test_title_contains_none_returns_all():
    events = parse_events(_load("multi_event.ics"), _MAY_19, title_contains=None)
    assert len(events) == 2


# ---------------------------------------------------------------------------
# edge cases
# ---------------------------------------------------------------------------


def test_inline_ics_text_single_event():
    ics_text = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:20260519T120000Z
DTEND:20260519T130000Z
SUMMARY:Inline Test
END:VEVENT
END:VCALENDAR"""
    events = parse_events(ics_text, _MAY_19)
    assert len(events) == 1
    assert events[0].summary == "Inline Test"


def test_event_missing_optional_fields_ok():
    ics_text = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:20260519T120000Z
DTEND:20260519T130000Z
SUMMARY:Minimal Event
END:VEVENT
END:VCALENDAR"""
    events = parse_events(ics_text, _MAY_19)
    assert len(events) == 1
    e = events[0]
    assert e.location is None
    assert e.description is None
    assert e.url is None


def test_event_without_dtstart_is_skipped():
    """A VEVENT missing DTSTART is silently skipped (not an error)."""
    ics_text = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
SUMMARY:No Start Event
END:VEVENT
BEGIN:VEVENT
DTSTART:20260519T120000Z
DTEND:20260519T130000Z
SUMMARY:Normal Event
END:VEVENT
END:VCALENDAR"""
    events = parse_events(ics_text, _MAY_19)
    assert len(events) == 1
    assert events[0].summary == "Normal Event"


def test_event_with_duration_uses_duration():
    """An event with DURATION but no DTEND should compute end correctly."""
    events = parse_events(_load("event_with_duration.ics"), _MAY_19)
    assert len(events) == 1
    e = events[0]
    assert e.end - e.start == datetime.timedelta(hours=2)


def test_event_no_dtend_no_duration_timed_gets_one_hour():
    """A timed event with no DTEND and no DURATION defaults to 1 hour."""
    ics_text = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:20260519T090000Z
SUMMARY:No End Event
END:VEVENT
END:VCALENDAR"""
    events = parse_events(ics_text, _MAY_19)
    assert len(events) == 1
    assert events[0].end - events[0].start == datetime.timedelta(hours=1)


def test_event_no_dtend_no_duration_allday_gets_one_day():
    """An all-day event with no DTEND defaults to 1-day duration."""
    ics_text = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260519
SUMMARY:All Day No End
END:VEVENT
END:VCALENDAR"""
    events = parse_events(ics_text, _MAY_19)
    assert len(events) == 1
    assert events[0].end - events[0].start == datetime.timedelta(days=1)


def test_naive_datetime_treated_as_utc():
    """Naive datetimes (no tzinfo) are assumed to be UTC."""
    ics_text = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:20260519T090000
DTEND:20260519T100000
SUMMARY:Naive Event
END:VEVENT
END:VCALENDAR"""
    events = parse_events(ics_text, _MAY_19)
    assert len(events) == 1
    assert events[0].start.tzinfo is not None
    assert events[0].start == datetime.datetime(2026, 5, 19, 9, 0, 0, tzinfo=_UTC)


def test_recurring_with_exdate_skips_excluded_occurrence():
    """EXDATEs are excluded from recurring event expansion."""
    events = parse_events(_load("recurring_with_exdate.ics"), _MAY_19_23)
    # 5 daily occurrences minus 1 EXDATE on May 21 = 4
    assert len(events) == 4
    dates = {e.start.date() for e in events}
    assert datetime.date(2026, 5, 21) not in dates
