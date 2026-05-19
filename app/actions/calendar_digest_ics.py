"""calendar_digest_ics action handler — fetches a signed ICS URL and returns today's events."""

from __future__ import annotations

import datetime
import os
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from app.actions.base import ActionResult
from app.ics.parser import InvalidICSError, parse_events
from app.secrets.resolver import SecretResolutionError, build_effective_whitelist, resolve


class CalendarDigestICSParams(BaseModel):
    ics_url: str
    date_range_days: int = 1
    title_contains: str | None = None


class CalendarDigestICSHandler:
    name: ClassVar[str] = "calendar_digest_ics"
    description: ClassVar[str] = (
        "Fetches a read-only Google Calendar (or any iCal-compatible calendar) via a signed "
        "ICS URL and returns structured event data for the requested date window. "
        "Use ics_url=${GCAL_ICS_URL} to pass the URL via environment variable. "
        "date_range_days controls how many days forward from today to include "
        "(default: 1 = today only). "
        "title_contains filters events by a case-insensitive substring of the event title. "
        "401/403/404 responses are permanent failures (DLQ); 5xx and timeouts are retried."
    )
    params_model: ClassVar[type[BaseModel]] = CalendarDigestICSParams
    timeout_seconds: ClassVar[int] = 30

    async def execute(self, run: Any, params: CalendarDigestICSParams) -> ActionResult:
        # Resolve ${VAR} tokens in the URL before making any network calls
        whitelist = build_effective_whitelist()
        env = dict(os.environ)
        try:
            resolved_url = resolve(params.ics_url, env, whitelist)
        except SecretResolutionError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=False)

        # Fetch the ICS file
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(resolved_url)
        except httpx.TimeoutException as exc:
            return ActionResult(
                ok=False, result=None, error=f"Request timed out: {exc}", retryable=True
            )
        except httpx.RequestError as exc:
            return ActionResult(
                ok=False, result=None, error=f"Network error: {exc}", retryable=True
            )

        # Classify HTTP errors
        if response.status_code in (401, 403):
            return ActionResult(
                ok=False,
                result=None,
                error=(
                    f"HTTP {response.status_code}: ICS URL may be invalidated; "
                    "operator action needed"
                ),
                retryable=False,
            )
        if response.status_code == 404:
            return ActionResult(
                ok=False,
                result=None,
                error="HTTP 404: ICS URL not found",
                retryable=False,
            )
        if response.status_code >= 500:
            return ActionResult(
                ok=False,
                result=None,
                error=f"HTTP {response.status_code}: calendar server error",
                retryable=True,
            )
        if not response.is_success:
            return ActionResult(
                ok=False,
                result=None,
                error=f"HTTP {response.status_code}",
                retryable=False,
            )

        # Parse events
        today = datetime.datetime.now(datetime.UTC).date()
        end_date = today + datetime.timedelta(days=params.date_range_days - 1)
        date_range = (today, end_date)

        try:
            events = parse_events(
                response.text,
                date_range=date_range,
                title_contains=params.title_contains,
            )
        except InvalidICSError as exc:
            return ActionResult(
                ok=False, result=None, error=f"Malformed ICS: {exc}", retryable=False
            )

        return ActionResult(
            ok=True,
            result={
                "events": [e.model_dump(mode="json") for e in events],
                "count": len(events),
                "date_range": {
                    "start": today.isoformat(),
                    "end": end_date.isoformat(),
                },
            },
            error=None,
            retryable=False,
        )
