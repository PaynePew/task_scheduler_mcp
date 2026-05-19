# ADR-048: `calendar_digest_ics` Action — ICS URL vs OAuth Design

- **Status**: Accepted
- **Date**: 2026-05-19
- **Deciders**: PaynePew
- **Related**: ADR-032 (secrets convention), ADR-033 (inter-handler data flow), #105

## Context

The `calendar_digest_ics` handler fetches calendar events for daily digest workflows
(e.g., `calendar_digest_ics → slack_post` for interview briefings). Two integration
paths exist for Google Calendar (and compatible providers):

1. **OAuth 2.0** — Google Cloud Console project, consent screen configuration, refresh
   token management, per-user credential storage.
2. **Signed ICS URL** — A single URL (e.g., `https://calendar.google.com/calendar/ical/...`)
   that acts as a bearer token; any holder of the URL can read the calendar.

The question is which path to implement.

## Decision

**Use the signed ICS URL approach.**

The ICS URL is passed via environment variable (`${GCAL_ICS_URL}`) using the ADR-032
secrets convention. No OAuth infrastructure is built.

## Rationale

### Effort vs capability

| Dimension | OAuth | ICS URL |
|-----------|-------|---------|
| Setup time | ~12h (Cloud Console, consent screen, refresh tokens) | ~2 min (copy URL from calendar settings) |
| Code complexity | OAuth library, token refresh loop, per-user credential store | `httpx.get(url)` |
| Read capability | Same: read events | Same: read events |
| Write capability | Yes (not needed) | No |
| Vendor lock-in | Google-specific | Vendor-neutral (RFC 5545) |

The capability delta is zero for read-only workflows. The effort delta is 4× in favor of ICS.

### Vendor neutrality

ICS is an open standard (RFC 5545). The same handler works unchanged with:
- Google Calendar
- Microsoft Outlook / Office 365
- Apple Calendar (iCloud)
- Any CalDAV server that exports ICS feeds

OAuth would bind the handler to Google's auth flow specifically.

### Secrets model fit

The ADR-032 env-var substitution pattern (`${VAR}` in `action_params`, resolved at
execution time from `os.environ`) fits naturally: the operator sets `GCAL_ICS_URL` in the
deployment environment; job definitions reference `${GCAL_ICS_URL}` symbolically. The
URL never appears in the database.

## Security model

**URL-as-bearer-token**: the ICS URL contains a unique token embedded by the calendar
provider. Possession of the URL equals read access to the calendar.

Consequences:

- **Leak == compromise**: if the URL leaks (log line, error message, git history), the
  calendar is readable by the attacker until the URL is regenerated.
- **Mitigation**: ADR-032 whitelist prevents `${GCAL_ICS_URL}` from appearing in logs.
  The URL is resolved at execution time and is not stored in the DB.
- **Revocation**: regenerate the ICS URL in the calendar provider's settings, then update
  the `GCAL_ICS_URL` env var in the deployment. No code changes needed.
- **Scope**: read-only by design; the ICS URL provides no write access.

Operators should treat `GCAL_ICS_URL` with the same care as an API key.

## Read-only by design

Write access to calendar events (create, update, delete) is explicitly out of scope.
Write operations require OAuth or CalDAV with credentials — neither is supported by this
handler. Any future write requirement must use a separate handler and a different auth
mechanism.

## Error classification

| HTTP status | Classification | Rationale |
|-------------|---------------|-----------|
| 401, 403 | DLQ (non-retryable) | URL invalidated or revoked; operator action needed |
| 404 | DLQ (non-retryable) | Feed URL no longer exists |
| 5xx | Retry | Transient calendar server error |
| Timeout | Retry | Transient network issue |
| Malformed ICS | DLQ (non-retryable) | Feed is broken; operator must investigate |

## RRULE expansion

Recurring events are expanded client-side using `dateutil.rrule`. EXDATE exclusions are
honoured. The expansion window is bounded by `date_range_days` to avoid unbounded memory
growth. All-day events are normalized to midnight UTC.

## Alternatives considered

### OAuth 2.0

Rejected: ~12 hours of auth plumbing (Google Cloud Console, consent screen, refresh token
rotation, credential storage) for zero additional read capability. Revisit if write access
is ever required.

### CalDAV

Rejected: CalDAV requires per-provider authentication and a CalDAV client library.
No read capability advantage over ICS. Revisit if fine-grained event filtering or write
access is needed.

## Consequences

**Positive:**
- Minimal setup: copy URL from calendar settings → set env var → done.
- Vendor-neutral: same handler works with Google, Outlook, Apple, and any ICS source.
- No OAuth token refresh loop to maintain.
- Fits ADR-032 secrets model exactly.

**Negative:**
- URL revocation is manual (regenerate + redeploy env var).
- No write access to calendar events.
- URL-as-bearer-token security model requires operator discipline (treat like an API key).
