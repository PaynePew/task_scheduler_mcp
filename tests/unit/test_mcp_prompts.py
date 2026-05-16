"""Unit tests for app/mcp/prompts/ — no DB, no MCP wire protocol."""

from __future__ import annotations

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

from app.mcp.prompts import daily_review, setup_summary

# ---------------------------------------------------------------------------
# daily_review
# ---------------------------------------------------------------------------


def test_daily_review_prompt_metadata():
    prompt = daily_review.build_prompt()
    assert prompt.name == "daily_review"
    assert prompt.description
    assert prompt.arguments == [] or prompt.arguments is None


def test_daily_review_result_one_user_message():
    result = daily_review.build_result()
    assert len(result.messages) == 1
    msg = result.messages[0]
    assert msg.role == "user"
    assert msg.content.type == "text"


def test_daily_review_result_references_tasks_list():
    result = daily_review.build_result()
    text = result.messages[0].content.text
    assert "tasks://list" in text


def test_daily_review_result_mentions_failed_runs():
    result = daily_review.build_result()
    text = result.messages[0].content.text
    assert "failed" in text.lower() or "24" in text


# ---------------------------------------------------------------------------
# setup_summary — build_prompt
# ---------------------------------------------------------------------------


def test_setup_summary_prompt_metadata():
    prompt = setup_summary.build_prompt()
    assert prompt.name == "setup_summary"
    assert prompt.description
    assert prompt.arguments is not None
    arg_names = [a.name for a in prompt.arguments]
    assert "topic" in arg_names
    assert "schedule" in arg_names


def test_setup_summary_prompt_args_required():
    prompt = setup_summary.build_prompt()
    for arg in prompt.arguments:
        assert arg.required is True, f"arg {arg.name!r} should be required"


# ---------------------------------------------------------------------------
# setup_summary — build_result
# ---------------------------------------------------------------------------


def test_setup_summary_result_one_user_message():
    result = setup_summary.build_result("AI news", "every morning at 8am")
    assert len(result.messages) == 1
    msg = result.messages[0]
    assert msg.role == "user"
    assert msg.content.type == "text"


def test_setup_summary_result_substitutes_topic():
    result = setup_summary.build_result("AI news", "every morning at 8am")
    assert "AI news" in result.messages[0].content.text


def test_setup_summary_result_substitutes_schedule():
    result = setup_summary.build_result("AI news", "every morning at 8am")
    assert "every morning at 8am" in result.messages[0].content.text


def test_setup_summary_result_references_tasks_actions():
    result = setup_summary.build_result("AI news", "every morning at 8am")
    assert "tasks://actions" in result.messages[0].content.text


def test_setup_summary_empty_string_args_accepted():
    """Empty strings are valid (stringly-typed; LLM handles content)."""
    result = setup_summary.build_result("", "")
    assert len(result.messages) == 1


# ---------------------------------------------------------------------------
# setup_summary — validate_args
# ---------------------------------------------------------------------------


def test_validate_args_ok():
    topic, schedule = setup_summary.validate_args({"topic": "AI news", "schedule": "daily at 8am"})
    assert topic == "AI news"
    assert schedule == "daily at 8am"


def test_validate_args_missing_schedule_raises_invalid_params():
    with pytest.raises(McpError) as exc_info:
        setup_summary.validate_args({"topic": "x"})
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "schedule" in exc_info.value.error.message


def test_validate_args_missing_topic_raises_invalid_params():
    with pytest.raises(McpError) as exc_info:
        setup_summary.validate_args({"schedule": "daily"})
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "topic" in exc_info.value.error.message


def test_validate_args_none_raises_invalid_params():
    with pytest.raises(McpError) as exc_info:
        setup_summary.validate_args(None)
    assert exc_info.value.error.code == INVALID_PARAMS


def test_validate_args_empty_dict_raises_invalid_params():
    with pytest.raises(McpError) as exc_info:
        setup_summary.validate_args({})
    assert exc_info.value.error.code == INVALID_PARAMS


def test_validate_args_empty_string_accepted():
    """Empty string IS a present key; validation passes."""
    topic, schedule = setup_summary.validate_args({"topic": "", "schedule": ""})
    assert topic == ""
    assert schedule == ""
