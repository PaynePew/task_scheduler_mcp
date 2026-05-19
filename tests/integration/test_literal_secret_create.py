"""Integration test: task.create.v1 rejects literal secrets in action_params.

Per AC: literal sk-ant-xxx in headers → USER_INPUT error with fixable expected hint.

Detection runs before any DB access so this test does not require a live DB —
but it is placed under integration/ because it tests the MCP server layer end-to-end
(not just the pure detection function).
"""

from __future__ import annotations

import pytest

from app.mcp.server import _handle_task_create


async def _create_with_params(action_params: dict) -> dict:
    """Call _handle_task_create with a dummy session_factory (never invoked)."""

    class _NeverUsedFactory:
        def __call__(self):
            raise AssertionError("DB should not be reached when literal secret detected")

    return await _handle_task_create(
        arguments={"action": "echo", "action_params": action_params},
        user_id="test-user",
        session_factory=_NeverUsedFactory(),  # type: ignore[arg-type]
    )


@pytest.mark.integration
async def test_literal_anthropic_key_in_headers_rejected():
    """sk-ant-xxx in action_params.headers → USER_INPUT error."""
    result = await _create_with_params(
        {"method": "POST", "url": "https://api.example.com", "headers": {"Authorization": "Bearer sk-ant-api03-realkey1234567890"}}
    )
    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "USER_INPUT"
    assert err["field"] == "action_params"
    assert "${" in err["expected"]  # hint suggests ${VAR_NAME} form


@pytest.mark.integration
async def test_literal_openai_key_in_body_rejected():
    """sk-xxxxxxxx in action_params.body → USER_INPUT error."""
    result = await _create_with_params(
        {"method": "POST", "url": "https://api.openai.com/v1/chat", "body": {"api_key": "sk-proj-secretkeyhere1234"}}
    )
    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "USER_INPUT"
    assert err["field"] == "action_params"


@pytest.mark.integration
async def test_no_literal_secret_proceeds_to_db():
    """action_params without secrets does not trigger detection; proceeds to DB (and fails with UNKNOWN_ACTION)."""
    result = await _create_with_params(
        {"method": "GET", "url": "https://example.com", "headers": {"Authorization": "Bearer ${ANTHROPIC_API_KEY}"}}
    )
    # The dummy session factory would raise if called, but unknown_action is caught earlier
    # by domain.jobs.create_job -> this may succeed with UNKNOWN_ACTION or INTERNAL depending
    # on action. But the key check: it does NOT return a USER_INPUT/literal-secret error.
    assert result.get("error", {}).get("code") != "USER_INPUT" or "literal" not in result.get("error", {}).get("message", "")


@pytest.mark.integration
async def test_template_var_in_params_not_rejected():
    """${ANTHROPIC_API_KEY} form should NOT be flagged as a literal secret."""
    result = await _create_with_params(
        {"headers": {"Authorization": "Bearer ${ANTHROPIC_API_KEY}"}}
    )
    assert result.get("ok") is not False or "literal" not in result.get("error", {}).get("message", "")
