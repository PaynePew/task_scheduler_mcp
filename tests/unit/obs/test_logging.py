"""Unit tests for obs/logging — ADR-056.

Covers:
- JSON shape correctness (required fields on every record)
- Correlation ids (user_id, job_id, run_id) injected from context vars
- Redaction: token-shaped fields are replaced with "[REDACTED]"
- configure_logging is idempotent (no duplicate handlers on second call)
"""

from __future__ import annotations

import json
import logging

from app.obs.logging import (
    _SECRET_KEY_RE,
    _CorrelationFilter,
    _job_id_var,
    _redact_value,
    _run_id_var,
    _SchemaFormatter,
    _user_id_var,
    bind_job_id,
    bind_run_id,
    bind_user_id,
    configure_logging,
    current_job_id,
    current_run_id,
    current_user_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def _format_as_json(record: logging.LogRecord, service: str = "test-svc") -> dict:
    """Apply the correlation filter + JSON formatter and parse the result."""
    f = _CorrelationFilter(service=service, git_sha="abc1234")
    fmt = _SchemaFormatter()
    f.filter(record)
    raw = fmt.format(record)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Shape tests
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def test_ts_present(self):
        rec = _make_record()
        data = _format_as_json(rec)
        assert "ts" in data

    def test_level_present(self):
        rec = _make_record()
        data = _format_as_json(rec)
        assert "level" in data

    def test_level_value(self):
        rec = _make_record()
        data = _format_as_json(rec)
        assert data["level"] == "INFO"

    def test_event_present(self):
        rec = _make_record("something happened")
        data = _format_as_json(rec)
        assert data["event"] == "something happened"

    def test_service_present(self):
        rec = _make_record()
        data = _format_as_json(rec, service="mcp-http")
        assert data["service"] == "mcp-http"

    def test_git_sha_present(self):
        rec = _make_record()
        data = _format_as_json(rec)
        assert data["git_sha"] == "abc1234"

    def test_output_is_valid_json(self):
        rec = _make_record("a log line")
        f = _CorrelationFilter(service="svc", git_sha="sha")
        fmt = _SchemaFormatter()
        f.filter(rec)
        raw = fmt.format(rec)
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Correlation id tests
# ---------------------------------------------------------------------------


class TestCorrelationIds:
    def test_user_id_injected_when_set(self):
        tok = bind_user_id("user-123")
        try:
            rec = _make_record()
            data = _format_as_json(rec)
            assert data.get("user_id") == "user-123"
        finally:
            _user_id_var.reset(tok)

    def test_user_id_absent_when_not_set(self):
        tok = bind_user_id(None)
        try:
            rec = _make_record()
            data = _format_as_json(rec)
            assert "user_id" not in data
        finally:
            _user_id_var.reset(tok)

    def test_job_id_injected_when_set(self):
        tok = bind_job_id("job-456")
        try:
            rec = _make_record()
            data = _format_as_json(rec)
            assert data.get("job_id") == "job-456"
        finally:
            _job_id_var.reset(tok)

    def test_run_id_injected_when_set(self):
        tok = bind_run_id("run-789")
        try:
            rec = _make_record()
            data = _format_as_json(rec)
            assert data.get("run_id") == "run-789"
        finally:
            _run_id_var.reset(tok)

    def test_all_correlation_ids_together(self):
        tu = bind_user_id("u1")
        tj = bind_job_id("j1")
        tr = bind_run_id("r1")
        try:
            rec = _make_record()
            data = _format_as_json(rec)
            assert data["user_id"] == "u1"
            assert data["job_id"] == "j1"
            assert data["run_id"] == "r1"
        finally:
            _run_id_var.reset(tr)
            _job_id_var.reset(tj)
            _user_id_var.reset(tu)

    def test_current_user_id_accessor(self):
        tok = bind_user_id("u-xyz")
        try:
            assert current_user_id() == "u-xyz"
        finally:
            _user_id_var.reset(tok)

    def test_current_job_id_accessor(self):
        tok = bind_job_id("j-xyz")
        try:
            assert current_job_id() == "j-xyz"
        finally:
            _job_id_var.reset(tok)

    def test_current_run_id_accessor(self):
        tok = bind_run_id("r-xyz")
        try:
            assert current_run_id() == "r-xyz"
        finally:
            _run_id_var.reset(tok)


# ---------------------------------------------------------------------------
# Redaction tests
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_token_field_redacted(self):
        rec = _make_record()
        rec.token = "supersecretvalue12345"  # type: ignore[attr-defined]
        data = _format_as_json(rec)
        assert data.get("token") == "[REDACTED]"

    def test_access_token_field_redacted(self):
        rec = _make_record()
        rec.access_token = "tok_abc123xyz789efgh"  # type: ignore[attr-defined]
        data = _format_as_json(rec)
        assert data.get("access_token") == "[REDACTED]"

    def test_secret_field_redacted(self):
        rec = _make_record()
        rec.client_secret = "mysecretvalue12345678"  # type: ignore[attr-defined]
        data = _format_as_json(rec)
        assert data.get("client_secret") == "[REDACTED]"

    def test_password_field_redacted(self):
        rec = _make_record()
        rec.password = "hunter2"  # type: ignore[attr-defined]
        data = _format_as_json(rec)
        assert data.get("password") == "[REDACTED]"

    def test_authorization_field_redacted(self):
        rec = _make_record()
        rec.authorization = "Bearer eyJhbGciOiJSUzI1NiJ9.payload"  # type: ignore[attr-defined]
        data = _format_as_json(rec)
        assert data.get("authorization") == "[REDACTED]"

    def test_bearer_field_redacted(self):
        rec = _make_record()
        rec.bearer_token = "eyJhbGciOiJSUzI1NiJ9.somevalue"  # type: ignore[attr-defined]
        data = _format_as_json(rec)
        assert data.get("bearer_token") == "[REDACTED]"

    def test_api_key_field_redacted(self):
        rec = _make_record()
        rec.api_key = "sk-abc123def456ghi789"  # type: ignore[attr-defined]
        data = _format_as_json(rec)
        assert data.get("api_key") == "[REDACTED]"

    def test_safe_field_not_redacted(self):
        rec = _make_record()
        rec.connection_id = "conn-abc123"  # type: ignore[attr-defined]
        data = _format_as_json(rec)
        assert data.get("connection_id") == "conn-abc123"

    def test_user_id_not_redacted(self):
        tok = bind_user_id("user-safe")
        try:
            rec = _make_record()
            data = _format_as_json(rec)
            assert data.get("user_id") == "user-safe"
        finally:
            _user_id_var.reset(tok)

    def test_redact_value_token_key(self):
        assert _redact_value("token", "anyvalue") == "[REDACTED]"

    def test_redact_value_safe_key(self):
        assert _redact_value("connection_id", "conn-123") == "conn-123"

    def test_secret_key_regex_matches_variants(self):
        assert _SECRET_KEY_RE.search("access_token")
        assert _SECRET_KEY_RE.search("client_secret")
        assert _SECRET_KEY_RE.search("password")
        assert _SECRET_KEY_RE.search("api_key")
        assert _SECRET_KEY_RE.search("authorization")
        assert _SECRET_KEY_RE.search("bearer")
        assert not _SECRET_KEY_RE.search("user_id")
        assert not _SECRET_KEY_RE.search("connection_id")
        assert not _SECRET_KEY_RE.search("job_id")


# ---------------------------------------------------------------------------
# configure_logging idempotency / handler count
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def test_configure_logging_installs_handler(self):
        configure_logging("test-svc")
        assert len(logging.getLogger().handlers) >= 1

    def test_configure_logging_idempotent(self):
        configure_logging("test-svc")
        configure_logging("test-svc")
        assert len(logging.getLogger().handlers) == 1

    def test_log_output_is_json(self, capfd):
        configure_logging("shape-test")
        log = logging.getLogger("test.shape")
        log.info("shape check")
        out, _ = capfd.readouterr()
        line = out.strip().splitlines()[-1]
        parsed = json.loads(line)
        assert "ts" in parsed
        assert "level" in parsed
        assert "event" in parsed
        assert parsed["service"] == "shape-test"
