"""Unit tests for the calendar_digest_ics action handler (no real network or DB)."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.actions.calendar_digest_ics import CalendarDigestICSHandler, CalendarDigestICSParams
from app.actions.registry import ACTION_REGISTRY

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ics"
_UTC = datetime.UTC


def _load_ics(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _mock_client(response: httpx.Response) -> MagicMock:
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _patch_client(response: httpx.Response):
    ctx = _mock_client(response)
    return patch("app.actions.calendar_digest_ics.httpx.AsyncClient", return_value=ctx)


def _today_iso() -> str:
    return datetime.datetime.now(_UTC).date().isoformat()


# ---------------------------------------------------------------------------
# Happy path — single event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_single_event_returns_ok():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(
        ics_url="https://calendar.example.com/feed.ics?token=secret",
        date_range_days=7,
    )
    ics_bytes = _load_ics("single_event.ics")
    response = httpx.Response(200, content=ics_bytes)

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.error is None
    assert result.result is not None
    assert "events" in result.result
    assert "count" in result.result
    assert "date_range" in result.result


@pytest.mark.asyncio
async def test_happy_result_has_correct_schema():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(
        ics_url="https://calendar.example.com/feed.ics",
        date_range_days=7,
    )
    ics_bytes = _load_ics("single_event.ics")
    response = httpx.Response(200, content=ics_bytes)

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    events = result.result["events"]
    # single_event.ics has a 2026-05-19 event; only shown if today is in range
    # The date_range is from today, so the event may or may not appear depending on date
    # Just verify the structure is correct (could be 0 if run much later)
    for event in events:
        assert "summary" in event
        assert "start" in event
        assert "end" in event


@pytest.mark.asyncio
async def test_happy_events_validated_as_pydantic():
    """Events in the result can be re-validated against the CalendarEvent model."""
    from app.ics.parser import CalendarEvent

    handler = CalendarDigestICSHandler()
    # Use inline ICS with today's date so events are always in range
    today = datetime.datetime.now(_UTC).date()
    ics_text = f"""\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:{today.strftime("%Y%m%d")}T090000Z
DTEND:{today.strftime("%Y%m%d")}T100000Z
SUMMARY:Today's Interview
DESCRIPTION:Candidate screen
END:VEVENT
END:VCALENDAR"""

    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")
    response = httpx.Response(200, content=ics_text.encode())

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.result["count"] == 1
    events = result.result["events"]
    # Validate each event dict against the Pydantic model
    validated = [CalendarEvent.model_validate(e) for e in events]
    assert validated[0].summary == "Today's Interview"


@pytest.mark.asyncio
async def test_multi_day_range_default_1_day():
    """Default date_range_days=1 returns only today's events."""
    handler = CalendarDigestICSHandler()
    today = datetime.datetime.now(_UTC).date()
    ics_text = f"""\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:{today.strftime("%Y%m%d")}T090000Z
DTEND:{today.strftime("%Y%m%d")}T100000Z
SUMMARY:Today Meeting
END:VEVENT
BEGIN:VEVENT
DTSTART:{(today + datetime.timedelta(days=1)).strftime("%Y%m%d")}T090000Z
DTEND:{(today + datetime.timedelta(days=1)).strftime("%Y%m%d")}T100000Z
SUMMARY:Tomorrow Meeting
END:VEVENT
END:VCALENDAR"""

    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics", date_range_days=1)
    response = httpx.Response(200, content=ics_text.encode())

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    summaries = [e["summary"] for e in result.result["events"]]
    assert "Today Meeting" in summaries
    assert "Tomorrow Meeting" not in summaries


@pytest.mark.asyncio
async def test_multi_day_range_spans_multiple_days():
    """date_range_days=2 returns today and tomorrow's events."""
    handler = CalendarDigestICSHandler()
    today = datetime.datetime.now(_UTC).date()
    ics_text = f"""\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:{today.strftime("%Y%m%d")}T090000Z
DTEND:{today.strftime("%Y%m%d")}T100000Z
SUMMARY:Today Meeting
END:VEVENT
BEGIN:VEVENT
DTSTART:{(today + datetime.timedelta(days=1)).strftime("%Y%m%d")}T090000Z
DTEND:{(today + datetime.timedelta(days=1)).strftime("%Y%m%d")}T100000Z
SUMMARY:Tomorrow Meeting
END:VEVENT
END:VCALENDAR"""

    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics", date_range_days=2)
    response = httpx.Response(200, content=ics_text.encode())

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    summaries = [e["summary"] for e in result.result["events"]]
    assert "Today Meeting" in summaries
    assert "Tomorrow Meeting" in summaries


@pytest.mark.asyncio
async def test_title_contains_filter():
    """title_contains parameter filters events by summary substring."""
    handler = CalendarDigestICSHandler()
    today = datetime.datetime.now(_UTC).date()
    ics_text = f"""\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:{today.strftime("%Y%m%d")}T090000Z
DTEND:{today.strftime("%Y%m%d")}T100000Z
SUMMARY:Interview: Jane Doe
END:VEVENT
BEGIN:VEVENT
DTSTART:{today.strftime("%Y%m%d")}T110000Z
DTEND:{today.strftime("%Y%m%d")}T120000Z
SUMMARY:Team Standup
END:VEVENT
END:VCALENDAR"""

    params = CalendarDigestICSParams(
        ics_url="https://example.com/cal.ics",
        date_range_days=1,
        title_contains="Interview",
    )
    response = httpx.Response(200, content=ics_text.encode())

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    summaries = [e["summary"] for e in result.result["events"]]
    assert "Interview: Jane Doe" in summaries
    assert "Team Standup" not in summaries


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_is_not_retryable():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")
    response = httpx.Response(401, content=b"Unauthorized")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_403_is_not_retryable():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")
    response = httpx.Response(403, content=b"Forbidden")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "403" in (result.error or "")


@pytest.mark.asyncio
async def test_404_is_not_retryable():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")
    response = httpx.Response(404, content=b"Not Found")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "404" in (result.error or "")


@pytest.mark.asyncio
async def test_500_is_retryable():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")
    response = httpx.Response(500, content=b"Server Error")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True
    assert "500" in (result.error or "")


@pytest.mark.asyncio
async def test_503_is_retryable():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")
    response = httpx.Response(503, content=b"Service Unavailable")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_timeout_is_retryable():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.calendar_digest_ics.httpx.AsyncClient", return_value=ctx):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True
    assert "timed out" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_network_error_is_retryable():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.calendar_digest_ics.httpx.AsyncClient", return_value=ctx):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_other_4xx_is_not_retryable():
    """Other 4xx codes (e.g. 429) are permanent non-retryable failures."""
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")
    response = httpx.Response(429, content=b"Too Many Requests")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "429" in (result.error or "")


@pytest.mark.asyncio
async def test_malformed_ics_response_is_not_retryable():
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="https://example.com/cal.ics")
    response = httpx.Response(200, content=b"this is not valid ICS")

    with _patch_client(response):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "ICS" in (result.error or "") or "malformed" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# ${VAR} resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ics_url_var_resolved_from_env():
    """${GCAL_ICS_URL} in ics_url is resolved to the env value before the request."""
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="${GCAL_ICS_URL}")

    captured_url: dict = {}

    async def _fake_get(url: str, **kwargs):
        captured_url["url"] = url
        return httpx.Response(200, content=b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR")

    mock_client = AsyncMock()
    mock_client.get = _fake_get
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.actions.calendar_digest_ics.httpx.AsyncClient", return_value=ctx),
        patch.dict("os.environ", {"GCAL_ICS_URL": "https://calendar.google.com/real-url"}),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert captured_url["url"] == "https://calendar.google.com/real-url"


@pytest.mark.asyncio
async def test_non_whitelisted_var_rejected():
    """A ${VAR} not in the whitelist causes a permanent failure (retryable=False)."""
    handler = CalendarDigestICSHandler()
    params = CalendarDigestICSParams(ics_url="${DATABASE_URL}")

    with patch.dict("os.environ", {"DATABASE_URL": "postgres://..."}):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "DATABASE_URL" in (result.error or "")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_calendar_digest_ics_registered():
    assert "calendar_digest_ics" in ACTION_REGISTRY
    assert isinstance(ACTION_REGISTRY["calendar_digest_ics"], CalendarDigestICSHandler)
