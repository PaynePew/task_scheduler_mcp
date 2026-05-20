"""Unit tests for app/db/identity.py stdio resolver."""

from app.db.identity import resolve_user_id_stdio


def test_env_var_returned(monkeypatch):
    monkeypatch.setenv("MCP_USER_ID", "env-user")
    assert resolve_user_id_stdio() == "env-user"


def test_default_user_fallback(monkeypatch):
    monkeypatch.delenv("MCP_USER_ID", raising=False)
    assert resolve_user_id_stdio() == "default-user"


def test_empty_env_var_is_returned_as_is(monkeypatch):
    # os.environ.get only applies its default when the key is absent, not when
    # it is set to "". An empty MCP_USER_ID therefore propagates through.
    monkeypatch.setenv("MCP_USER_ID", "")
    assert resolve_user_id_stdio() == ""
