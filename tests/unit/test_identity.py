"""Unit tests for app/db/identity.py resolver."""

from app.db.identity import resolve_user_id


def test_header_takes_precedence(monkeypatch):
    monkeypatch.setenv("MCP_USER_ID", "env-user")
    assert resolve_user_id("header-user") == "header-user"


def test_env_var_used_when_no_header(monkeypatch):
    monkeypatch.setenv("MCP_USER_ID", "env-user")
    assert resolve_user_id(None) == "env-user"


def test_default_user_fallback(monkeypatch):
    monkeypatch.delenv("MCP_USER_ID", raising=False)
    assert resolve_user_id(None) == "default-user"


def test_empty_header_falls_through_to_env(monkeypatch):
    monkeypatch.setenv("MCP_USER_ID", "env-user")
    assert resolve_user_id("") == "env-user"
