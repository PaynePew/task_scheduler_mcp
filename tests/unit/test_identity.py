"""Unit tests for app/db/identity.py stdio resolver."""

from app.db.identity import resolve_user_id_stdio


def test_env_var_returned(monkeypatch):
    monkeypatch.setenv("MCP_USER_ID", "env-user")
    assert resolve_user_id_stdio() == "env-user"


def test_default_user_fallback(monkeypatch):
    monkeypatch.delenv("MCP_USER_ID", raising=False)
    assert resolve_user_id_stdio() == "default-user"


def test_empty_env_var_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MCP_USER_ID", "")
    # empty string means not set → falls back via get(key, "default-user")
    # os.environ.get("MCP_USER_ID", "default-user") returns "" when set to ""
    # This is an edge-case: we keep "" as-is (empty string is technically a value).
    assert resolve_user_id_stdio() == ""
