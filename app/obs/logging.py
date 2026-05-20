"""Central JSON logging configuration (ADR-056).

Replaces per-entrypoint ``logging.basicConfig`` calls.  Every log record
gets: ``ts``, ``level``, ``service``, ``event``, ``git_sha`` plus the
correlation ids ``user_id`` / ``job_id`` / ``run_id`` when present in the
current async context.

Usage::

    from app.obs.logging import bind_user_id, configure_logging, unbind_user_id

    configure_logging("mcp-http")

    token = bind_user_id("user-abc")
    try:
        logger.info("request received", extra={"event": "request.start"})
    finally:
        unbind_user_id(token)

Redaction: any extra field whose name contains a known secret keyword
(token, secret, password, key, credential, authorization, bearer) has its
value replaced with ``"[REDACTED]"`` before the record is serialised.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import sys
import urllib.request
from contextvars import ContextVar, Token
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from app.config.settings import settings

# ---------------------------------------------------------------------------
# Correlation context vars (async-safe — each task inherits a copy)
# ---------------------------------------------------------------------------

_user_id_var: ContextVar[str | None] = ContextVar("log_user_id", default=None)
_job_id_var: ContextVar[str | None] = ContextVar("log_job_id", default=None)
_run_id_var: ContextVar[str | None] = ContextVar("log_run_id", default=None)


def bind_user_id(value: str | None) -> Token[str | None]:
    return _user_id_var.set(value)


def unbind_user_id(token: Token[str | None]) -> None:
    _user_id_var.reset(token)


def bind_job_id(value: str | None) -> Token[str | None]:
    return _job_id_var.set(value)


def unbind_job_id(token: Token[str | None]) -> None:
    _job_id_var.reset(token)


def bind_run_id(value: str | None) -> Token[str | None]:
    return _run_id_var.set(value)


def unbind_run_id(token: Token[str | None]) -> None:
    _run_id_var.reset(token)


def current_user_id() -> str | None:
    return _user_id_var.get()


def current_job_id() -> str | None:
    return _job_id_var.get()


def current_run_id() -> str | None:
    return _run_id_var.get()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|credential|authorization|bearer|api[_-]?key)",
    re.IGNORECASE,
)


def _redact_value(key: str, value: Any) -> Any:
    """Return ``"[REDACTED]"`` if *key* looks like a secret name.

    Only string values are touched; redaction is keyed off the field name to
    avoid accidentally masking neutral-named values like UUIDs or git SHAs.
    """
    if isinstance(value, str) and _SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    return value


# Built-in LogRecord attributes — never candidates for redaction.
_BUILTIN_RECORD_FIELDS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
)


def _redact_record(record: logging.LogRecord) -> None:
    """Redact secret-shaped fields on the record's ``__dict__`` in place."""
    for key in list(record.__dict__):
        if key in _BUILTIN_RECORD_FIELDS:
            continue
        record.__dict__[key] = _redact_value(key, record.__dict__[key])


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class _CorrelationFilter(logging.Filter):
    """Inject per-call correlation ids + static metadata into every record."""

    def __init__(self, service: str, git_sha: str) -> None:
        super().__init__()
        self._service = service
        self._git_sha = git_sha

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self._service  # type: ignore[attr-defined]
        record.git_sha = self._git_sha  # type: ignore[attr-defined]
        user_id = _user_id_var.get()
        if user_id is not None:
            record.user_id = user_id  # type: ignore[attr-defined]
        job_id = _job_id_var.get()
        if job_id is not None:
            record.job_id = job_id  # type: ignore[attr-defined]
        run_id = _run_id_var.get()
        if run_id is not None:
            record.run_id = run_id  # type: ignore[attr-defined]
        _redact_record(record)
        return True


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

_JSON_FORMAT = "%(asctime)s %(levelname)s %(message)s"

# Rename built-in LogRecord fields to the canonical names required by ADR-056.
_RENAME_FIELDS = {
    "asctime": "ts",
    "levelname": "level",
    "message": "event",
}


class _SchemaFormatter(JsonFormatter):
    """JSON formatter that enforces the ADR-056 field schema."""

    def __init__(self) -> None:
        super().__init__(
            fmt=_JSON_FORMAT,
            rename_fields=_RENAME_FIELDS,
            timestamp=True,
        )

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        # Promote correlation fields from the record if present.
        for field in ("service", "git_sha", "user_id", "job_id", "run_id"):
            val = record.__dict__.get(field)
            if val is not None:
                log_record[field] = val


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure_logging(service: str, level: str | None = None) -> None:
    """Configure the root logger with structured JSON output.

    Call once at process startup, before any loggers are used.

    Args:
        service: Short role name written to every record (e.g. ``"mcp-http"``,
            ``"worker"``, ``"watcher"``).
        level:   Override log level string (e.g. ``"DEBUG"``).  Defaults to
            ``settings.log_level``.
    """
    effective_level = (level or settings.log_level).upper()
    root = logging.getLogger()
    root.setLevel(effective_level)

    # Remove any handlers added by earlier basicConfig / uvicorn / other calls.
    root.handlers.clear()

    correlation_filter = _CorrelationFilter(service=service, git_sha=settings.git_sha)
    formatter = _SchemaFormatter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(correlation_filter)
    root.addHandler(stdout_handler)

    # Optional: ship to Better Stack when the source token is configured.
    token = settings.better_stack_source_token
    if token:
        _add_better_stack_handler(root, token, correlation_filter, formatter)


def _add_better_stack_handler(
    root: logging.Logger,
    source_token: str,
    correlation_filter: logging.Filter,
    formatter: logging.Formatter,
) -> None:
    """Attach an HTTPS log-shipping handler targeting Better Stack's ingest."""

    class _BetterStackHandler(logging.Handler):
        """Push each log record as JSON to the Better Stack ingest endpoint."""

        def _record_as_dict(self, record: logging.LogRecord) -> dict[str, Any]:
            # Re-use the JSON formatter to get the canonical dict.
            msg = formatter.format(record)
            try:
                return json.loads(msg)
            except Exception:
                return {"event": msg, "level": record.levelname}

        def emit(self, record: logging.LogRecord) -> None:
            try:
                payload = json.dumps(self._record_as_dict(record)).encode()
                req = urllib.request.Request(
                    "https://in.logs.betterstack.com/",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {source_token}",
                    },
                    method="POST",
                )
                ctx = ssl.create_default_context()
                urllib.request.urlopen(req, context=ctx, timeout=3)
            except Exception:
                self.handleError(record)

    handler = _BetterStackHandler()
    handler.addFilter(correlation_filter)
    root.addHandler(handler)
