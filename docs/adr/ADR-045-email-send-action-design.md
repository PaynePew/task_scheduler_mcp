# ADR-045: `email_send` Action — SMTP vs API-Service Design

- **Status**: Superseded in part (2026-06-11) — `email_send` is now OAuth/Gmail-only; the SMTP path below has been removed. See Amendment.
- **Date**: 2026-05-19
- **Deciders**: PaynePew
- **Related**: ADR-032 (secrets convention), ADR-033 (inter-handler data flow), ADR-050 (dual-credential model), #103

## Amendment (2026-06-11): SMTP removed — Gmail-only

This deployment only ever sends via the user's connected Google account, and a
per-user SMTP-configuration surface was never built (there is no web flow for a
user to enter SMTP credentials). The SMTP path (`_send_via_smtp`, `aiosmtplib`,
`SMTP_*` / `EMAIL_FROM` env vars) has been **removed**. `email_send` now sends
exclusively via the Gmail API using the caller's Google OAuth connection
(`provider="google"`, scope `gmail.send`); a user with no Google connection
receives `MISSING_CONNECTION` (connect at `/connections`). The original SMTP
decision below is retained for historical context.

## Context

The `email_send` handler sends transactional email from workflows (e.g.
`github_digest → email_send` for daily digest delivery, or standalone
alerts). Two integration paths exist:

1. **SMTP** — direct connection to an SMTP server using the open standard
   (RFC 5321). Credentials: host, port, username, password.
2. **API service** (SendGrid, Amazon SES, Mailgun, etc.) — vendor-specific
   HTTP APIs with per-provider SDKs, API keys, and domain verification flows.

The question is which path to implement for the initial handler.

## Decision

**Use SMTP with STARTTLS (port 587 default) via `aiosmtplib`.**

All SMTP credentials (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
`SMTP_PASSWORD`, `EMAIL_FROM`) are passed via environment variables using
the ADR-032 secrets convention. No credentials appear in `action_params` or
in the database.

## Rationale

### SMTP vs API service

| Dimension | SMTP | API service (SendGrid/SES) |
|-----------|------|---------------------------|
| Setup time | ~2 min (copy credentials from provider dashboard) | ~30 min (account, domain verify, DKIM, API key) |
| Vendor lock-in | None — any SMTP server works | Tight SDK and account coupling |
| Protocol | Open standard (RFC 5321) | Proprietary HTTP REST |
| Auth | Username/password via env | API key via env |
| Self-hosting | Yes (Postfix, Exim, Mailhog) | No |
| Cost at low volume | Free (SMTP relay or Gmail) | Free tier then per-message |
| Code complexity | `aiosmtplib.send()` — one call | Vendor SDK import + auth wrap |

The capability delta for transactional send is zero. The effort and coupling
deltas strongly favour SMTP for a scheduler that serves individual operators
with varied infrastructure.

### Vendor neutrality

SMTP is the universal mail transfer protocol. The same handler works
unchanged with:
- Self-hosted relays (Postfix, Exim, Haraka)
- Managed SMTP relays (Gmail, Outlook, Fastmail, Zoho)
- Transactional SMTP services in SMTP relay mode (SendGrid, SES, Mailgun —
  all support SMTP as well as HTTP API)
- Local test servers (`aiosmtpd`, Mailhog, Mailtrap)

API-service SDKs would bind the handler to a single vendor's authentication
and payload schema.

### Secrets model fit

The ADR-032 env-var pattern fits naturally: the operator sets five env vars in
the deployment environment; `action_params` stores only addressing and content
(`to`, `subject`, `body`, `from_run_id`, `template`). The secrets resolver
may expand `${VAR}` tokens in subject/body if needed; SMTP credentials are
never referenced from params.

## Transport: STARTTLS on port 587

Port 587 (SMTP submission) with STARTTLS is the modern recommended path for
mail clients and relay submission (RFC 6409). Port 465 (SMTPS / implicit TLS)
is a historical alternative; both work with `aiosmtplib` but 587+STARTTLS is
the interoperability default.

STARTTLS can be disabled via `SMTP_USE_STARTTLS=false` for plain-text
test servers (e.g., `aiosmtpd` in integration tests) or port-25 relay
connections inside a trusted network.

## Bounce handling philosophy

SMTP error codes map directly to `retryable`:

| SMTP code range | Classification | Rationale |
|----------------|---------------|-----------|
| 4xx (temporary) | `retryable=True` | Mailbox temporarily full, greylisting, server overload — worth retrying |
| 5xx (permanent) | `retryable=False → DLQ` | No such user, domain rejected, policy block — retrying will not help |
| 535 / 5.7.0 (auth) | `retryable=False → DLQ` | Operator credentials invalid — requires operator action, not retry |
| TLS / connect / timeout | `retryable=True` | Transient network issue — retry is appropriate |

**Do not auto-retry 5xx.** A 550 "No such user" is a permanent address
error; repeated delivery attempts waste queue capacity and may cause the
sending IP to be blocklisted. Operators should inspect the DLQ and correct
the recipient address.

**Auth failures route to DLQ.** A 535 error means the SMTP credentials are
wrong or revoked. Retrying would generate repeated auth failures and
potentially trigger account lockout. Operator must rotate credentials and
redeploy the env var.

## DKIM / SPF

The handler does not configure or enforce DKIM signing or SPF alignment.
Deliverability depends on the operator's SMTP server configuration:
- **Self-hosted relay**: operator must configure DKIM signing and SPF records.
- **Managed relay (Gmail, SES in SMTP mode)**: the relay handles DKIM/SPF
  automatically.

This is intentional: DKIM/SPF is infrastructure-layer configuration, not
application-layer code. The handler's responsibility ends at delivering the
message to the relay; what the relay does with it is the operator's concern.

## Chain-fed mode

Following ADR-033, `EmailSendParams` includes `from_run_id: int | None`.
When set, the handler reads the upstream `JobRun.result` via
`chain.upstream_reader.read_upstream` and dispatches on the `UpstreamPayload`
variant:

- `Ok(data)` → upstream JSON formatted via template → email body
- `UpstreamError(error_msg)` → error alert in email body (self-healing chain)
- `NoResult` / `InvalidJson` → placeholder body; email is still sent

Templates (`raw`, `digest_v1`) mirror the slack_post pattern so
`github_digest → email_send` and `github_digest → slack_post` workflows
share the same upstream data shape.

## Alternatives considered

### SendGrid HTTP API

Rejected: requires SendGrid account, domain verification, API key rotation,
and the `sendgrid-python` SDK dependency. Zero capability advantage over SMTP
for relay-mode delivery. Revisit if bulk send (>10k/day), click tracking, or
unsubscribe list management is ever required.

### Amazon SES

Rejected: requires AWS account, domain verification (DKIM + DMARC), IAM
credentials, and `boto3` email calls. Boto3 is already a dependency of this
project, but SES setup overhead is comparable to SendGrid. Revisit for
high-volume production deployments where SES cost-per-message matters.

### Mailgun

Rejected: same argument as SendGrid. No incremental capability over SMTP at
this project's current scale.

## Consequences

**Positive:**
- Works with any SMTP provider — zero vendor lock-in.
- Minimal setup: set five env vars → done.
- Fits ADR-032 secrets model exactly.
- `aiosmtplib` is a thin async wrapper over the standard SMTP protocol;
  no heavyweight SDK.
- Integration-testable with `aiosmtpd` in-process mock server (no external
  service needed in CI).

**Negative:**
- Deliverability depends on the operator's SMTP server reputation
  (SPF/DKIM/DMARC); the handler provides no deliverability tooling.
- No built-in unsubscribe / bounce list management; those are relay concerns.
- STARTTLS is required for security on untrusted networks; operators must
  not disable it in production (only appropriate for internal/test relays).
