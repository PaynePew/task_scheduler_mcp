"""ICS parser deep module — parses ICS text into structured CalendarEvent objects.

Interface: parse_events(ics_text, date_range, title_contains=None) -> list[CalendarEvent]

Pure function wrapping `icalendar`. Handles single events, recurring events
(RRULE expansion), all-day events, timezone normalization, and malformed ICS.
"""

from __future__ import annotations

import datetime
from typing import Any

import icalendar
from dateutil.rrule import rrulestr
from pydantic import BaseModel


class InvalidICSError(Exception):
    """Raised when ICS text cannot be parsed as a valid calendar."""


class CalendarEvent(BaseModel):
    """A single calendar event occurrence."""

    summary: str
    start: datetime.datetime
    end: datetime.datetime
    location: str | None = None
    description: str | None = None
    url: str | None = None


_UTC = datetime.UTC


def parse_events(
    ics_text: str,
    date_range: tuple[datetime.date, datetime.date],
    title_contains: str | None = None,
) -> list[CalendarEvent]:
    """Parse ICS text and return events within date_range.

    Args:
        ics_text: Raw ICS calendar text.
        date_range: (start_date, end_date) inclusive filter (UTC day boundaries).
        title_contains: Optional substring filter on event summary (case-insensitive).

    Returns:
        Sorted list of CalendarEvent instances within the date range.

    Raises:
        InvalidICSError: If ics_text cannot be parsed as a valid ICS calendar.
    """
    try:
        cal = icalendar.Calendar.from_ical(ics_text)
    except Exception as exc:
        raise InvalidICSError(f"Failed to parse ICS data: {exc}") from exc

    range_start, range_end = date_range
    # Convert date bounds to timezone-aware datetimes (UTC day boundaries)
    range_start_dt = datetime.datetime(
        range_start.year, range_start.month, range_start.day, tzinfo=_UTC
    )
    range_end_dt = datetime.datetime(
        range_end.year, range_end.month, range_end.day, 23, 59, 59, tzinfo=_UTC
    )

    events: list[CalendarEvent] = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        summary = _str_field(component, "SUMMARY") or ""
        location = _str_field(component, "LOCATION")
        description = _str_field(component, "DESCRIPTION")
        url = _str_field(component, "URL")

        dtstart_raw = component.get("DTSTART")
        dtend_raw = component.get("DTEND")
        if dtstart_raw is None:
            continue

        start_dt = _to_aware_datetime(dtstart_raw.dt)
        end_dt = _derive_end(component, start_dt, dtend_raw)

        duration = end_dt - start_dt

        # Build kwargs for event factory
        event_kwargs: dict[str, Any] = {
            "summary": summary,
            "location": location,
            "description": description,
            "url": url,
        }

        rrule_prop = component.get("RRULE")
        if rrule_prop is not None:
            # Expand recurring event within a padded window
            occurrences = _expand_rrule(component, start_dt, range_start_dt, range_end_dt)
            for occ_start in occurrences:
                occ_end = occ_start + duration
                events.append(CalendarEvent(start=occ_start, end=occ_end, **event_kwargs))
        else:
            events.append(CalendarEvent(start=start_dt, end=end_dt, **event_kwargs))

    # Filter by date range
    events = [e for e in events if e.start <= range_end_dt and e.end >= range_start_dt]

    # Filter by title_contains
    if title_contains is not None:
        needle = title_contains.lower()
        events = [e for e in events if needle in e.summary.lower()]

    events.sort(key=lambda e: e.start)
    return events


def _str_field(component: Any, key: str) -> str | None:
    """Extract a string field from a calendar component, or None if absent."""
    value = component.get(key)
    if value is None:
        return None
    return str(value)


def _to_aware_datetime(dt: datetime.date | datetime.datetime) -> datetime.datetime:
    """Normalise a date or datetime to a timezone-aware UTC datetime."""
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_UTC)
        return dt.astimezone(_UTC)
    # All-day date — treat as midnight UTC
    return datetime.datetime(dt.year, dt.month, dt.day, tzinfo=_UTC)


def _derive_end(
    component: Any,
    start_dt: datetime.datetime,
    dtend_raw: Any,
) -> datetime.datetime:
    """Return end datetime, falling back to DURATION or a 1-day/1-hour default."""
    if dtend_raw is not None:
        return _to_aware_datetime(dtend_raw.dt)

    duration_prop = component.get("DURATION")
    if duration_prop is not None:
        return start_dt + duration_prop.dt

    # Default: all-day events get 1 day duration; timed events get 1 hour
    original = component.get("DTSTART").dt
    if isinstance(original, datetime.datetime):
        return start_dt + datetime.timedelta(hours=1)
    return start_dt + datetime.timedelta(days=1)


def _expand_rrule(
    component: Any,
    dtstart: datetime.datetime,
    range_start: datetime.datetime,
    range_end: datetime.datetime,
) -> list[datetime.datetime]:
    """Expand RRULE within range, respecting EXDATE exclusions."""
    rrule_text = "RRULE:" + component["RRULE"].to_ical().decode()

    try:
        rule = rrulestr(
            rrule_text,
            dtstart=dtstart,
            ignoretz=False,
        )
    except Exception:
        # Unparseable RRULE — return the base event only
        return [dtstart]

    # Collect exclusion dates
    exdates: set[datetime.datetime] = set()
    exdate_prop = component.get("EXDATE")
    if exdate_prop is not None:
        exdate_list = exdate_prop if isinstance(exdate_prop, list) else [exdate_prop]
        for exd in exdate_list:
            for d in exd.dts:
                exdates.add(_to_aware_datetime(d.dt))

    return [
        _to_aware_datetime(occ)
        for occ in rule.between(range_start, range_end, inc=True)
        if _to_aware_datetime(occ) not in exdates
    ]
