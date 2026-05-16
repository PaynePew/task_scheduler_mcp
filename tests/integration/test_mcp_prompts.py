"""Integration tests for MCP prompts/list and prompts/get over in-memory transport.

Run with:
    uv run pytest -m integration tests/integration/test_mcp_prompts.py

No DB or queue required — prompts are stateless template generators.
"""

from __future__ import annotations

import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import INVALID_PARAMS

from app.mcp.server import create_server


def _collect_mcp_errors(exc: BaseException) -> list[McpError]:
    """Recursively collect all McpError instances from nested exception groups."""
    if isinstance(exc, McpError):
        return [exc]
    if isinstance(exc, BaseExceptionGroup):
        result: list[McpError] = []
        for sub in exc.exceptions:
            result.extend(_collect_mcp_errors(sub))
        return result
    return []


@pytest.fixture
def mcp_server():
    return create_server(user_id="prompt-test-user")


# ---------------------------------------------------------------------------
# prompts/list
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_prompts_list_returns_two_entries(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.list_prompts()

    names = [p.name for p in result.prompts]
    assert len(result.prompts) == 2
    assert "daily_review" in names
    assert "setup_summary" in names


@pytest.mark.integration
async def test_prompts_list_daily_review_has_no_required_args(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.list_prompts()

    daily = next(p for p in result.prompts if p.name == "daily_review")
    # no required arguments
    assert daily.arguments is None or daily.arguments == []


@pytest.mark.integration
async def test_prompts_list_setup_summary_has_topic_and_schedule_args(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.list_prompts()

    setup = next(p for p in result.prompts if p.name == "setup_summary")
    assert setup.arguments is not None
    arg_names = [a.name for a in setup.arguments]
    assert "topic" in arg_names
    assert "schedule" in arg_names


@pytest.mark.integration
async def test_prompts_list_setup_summary_args_are_required(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.list_prompts()

    setup = next(p for p in result.prompts if p.name == "setup_summary")
    for arg in setup.arguments:
        assert arg.required is True, f"arg {arg.name!r} should be required"


@pytest.mark.integration
async def test_prompts_list_setup_summary_has_descriptions(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.list_prompts()

    setup = next(p for p in result.prompts if p.name == "setup_summary")
    for arg in setup.arguments:
        assert arg.description, f"arg {arg.name!r} missing description"


# ---------------------------------------------------------------------------
# prompts/get daily_review
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_daily_review_returns_one_user_message(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.get_prompt("daily_review")

    assert len(result.messages) == 1
    assert result.messages[0].role == "user"


@pytest.mark.integration
async def test_get_daily_review_template_contains_tasks_list(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.get_prompt("daily_review")

    text = result.messages[0].content.text
    assert "tasks://list" in text


# ---------------------------------------------------------------------------
# prompts/get setup_summary — happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_setup_summary_returns_one_user_message(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.get_prompt(
            "setup_summary", {"topic": "AI news", "schedule": "every morning at 8am"}
        )

    assert len(result.messages) == 1
    assert result.messages[0].role == "user"


@pytest.mark.integration
async def test_get_setup_summary_contains_topic_substitution(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.get_prompt(
            "setup_summary", {"topic": "AI news", "schedule": "every morning at 8am"}
        )

    assert "AI news" in result.messages[0].content.text


@pytest.mark.integration
async def test_get_setup_summary_contains_schedule_substitution(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.get_prompt(
            "setup_summary", {"topic": "AI news", "schedule": "every morning at 8am"}
        )

    assert "every morning at 8am" in result.messages[0].content.text


@pytest.mark.integration
async def test_get_setup_summary_contains_tasks_actions(mcp_server):
    async with create_connected_server_and_client_session(mcp_server) as client:
        result = await client.get_prompt(
            "setup_summary", {"topic": "AI news", "schedule": "every morning at 8am"}
        )

    assert "tasks://actions" in result.messages[0].content.text


# ---------------------------------------------------------------------------
# prompts/get setup_summary — missing required arg → INVALID_PARAMS
# ---------------------------------------------------------------------------
# anyio wraps exceptions from task groups in ExceptionGroup; use except* to
# unwrap on Python 3.11+.


@pytest.mark.integration
async def test_get_setup_summary_missing_schedule_raises_invalid_params(mcp_server):
    errors: list[McpError] = []
    try:
        async with create_connected_server_and_client_session(mcp_server) as client:
            await client.get_prompt("setup_summary", {"topic": "x"})
    except BaseException as exc:
        errors = _collect_mcp_errors(exc)

    assert errors, "Expected McpError was not raised"
    assert errors[0].error.code == INVALID_PARAMS


@pytest.mark.integration
async def test_get_setup_summary_missing_both_args_raises_invalid_params(mcp_server):
    errors: list[McpError] = []
    try:
        async with create_connected_server_and_client_session(mcp_server) as client:
            await client.get_prompt("setup_summary", {})
    except BaseException as exc:
        errors = _collect_mcp_errors(exc)

    assert errors, "Expected McpError was not raised"
    assert errors[0].error.code == INVALID_PARAMS
