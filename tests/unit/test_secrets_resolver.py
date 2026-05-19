"""Unit tests for app/secrets/resolver.py — pure function, no I/O."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.secrets.resolver import (
    DEFAULT_WHITELIST,
    SecretResolutionError,
    _build_effective_whitelist,
    resolve,
)

ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-real-key",
    "OPENAI_API_KEY": "sk-openai-key",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/real",
    "MY_CUSTOM_VAR": "custom-value",
}


# ---------------------------------------------------------------------------
# String substitution
# ---------------------------------------------------------------------------


def test_plain_string_no_op():
    result = resolve("hello world", ENV, DEFAULT_WHITELIST)
    assert result == "hello world"


def test_single_var_substitution():
    result = resolve("Bearer ${ANTHROPIC_API_KEY}", ENV, DEFAULT_WHITELIST)
    assert result == "Bearer sk-ant-real-key"


def test_multiple_vars_in_same_string():
    result = resolve("${OPENAI_API_KEY} and ${ANTHROPIC_API_KEY}", ENV, DEFAULT_WHITELIST)
    assert result == "sk-openai-key and sk-ant-real-key"


def test_string_with_no_template_left_intact():
    result = resolve("no vars here", ENV, DEFAULT_WHITELIST)
    assert result == "no vars here"


# ---------------------------------------------------------------------------
# Nested dict / list recursion
# ---------------------------------------------------------------------------


def test_dict_values_resolved():
    inp = {"Authorization": "Bearer ${ANTHROPIC_API_KEY}", "Content-Type": "application/json"}
    result = resolve(inp, ENV, DEFAULT_WHITELIST)
    assert result == {"Authorization": "Bearer sk-ant-real-key", "Content-Type": "application/json"}


def test_nested_dict_resolved():
    inp = {"outer": {"inner": "${OPENAI_API_KEY}"}}
    result = resolve(inp, ENV, DEFAULT_WHITELIST)
    assert result == {"outer": {"inner": "sk-openai-key"}}


def test_list_values_resolved():
    inp = ["${ANTHROPIC_API_KEY}", "literal"]
    result = resolve(inp, ENV, DEFAULT_WHITELIST)
    assert result == ["sk-ant-real-key", "literal"]


def test_mixed_nested_structure():
    inp = {
        "headers": {"Authorization": "Bearer ${ANTHROPIC_API_KEY}"},
        "tags": ["${OPENAI_API_KEY}"],
    }
    result = resolve(inp, ENV, DEFAULT_WHITELIST)
    assert result == {
        "headers": {"Authorization": "Bearer sk-ant-real-key"},
        "tags": ["sk-openai-key"],
    }


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_unknown_var_raises_not_in_whitelist():
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve("${UNKNOWN_VAR}", ENV, DEFAULT_WHITELIST)
    assert exc_info.value.retryable is False
    assert "UNKNOWN_VAR" in str(exc_info.value)


def test_whitelisted_var_missing_from_env_raises():
    limited_env: dict[str, str] = {}
    with pytest.raises(SecretResolutionError) as exc_info:
        resolve("${ANTHROPIC_API_KEY}", limited_env, DEFAULT_WHITELIST)
    assert exc_info.value.retryable is False
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_malformed_template_no_closing_brace_is_left_as_is():
    """${VAR without closing brace should not be replaced (regex won't match)."""
    result = resolve("${ANTHROPIC_API_KEY", ENV, DEFAULT_WHITELIST)
    assert result == "${ANTHROPIC_API_KEY"


def test_empty_var_name_not_substituted():
    """${} should not be substituted — treat as plain text."""
    result = resolve("${}", ENV, DEFAULT_WHITELIST)
    assert result == "${}"


# ---------------------------------------------------------------------------
# Whitelist extension via custom whitelist
# ---------------------------------------------------------------------------


def test_custom_whitelist_allows_custom_var():
    custom_whitelist = DEFAULT_WHITELIST | {"MY_CUSTOM_VAR"}
    result = resolve("${MY_CUSTOM_VAR}", ENV, custom_whitelist)
    assert result == "custom-value"


def test_var_not_in_custom_whitelist_rejected():
    custom_whitelist = DEFAULT_WHITELIST  # no MY_CUSTOM_VAR
    with pytest.raises(SecretResolutionError):
        resolve("${MY_CUSTOM_VAR}", ENV, custom_whitelist)


# ---------------------------------------------------------------------------
# Non-string scalars pass through unchanged
# ---------------------------------------------------------------------------


def test_integer_value_passes_through():
    result = resolve(42, ENV, DEFAULT_WHITELIST)
    assert result == 42


def test_none_value_passes_through():
    result = resolve(None, ENV, DEFAULT_WHITELIST)
    assert result is None


def test_bool_value_passes_through():
    result = resolve(True, ENV, DEFAULT_WHITELIST)
    assert result is True


# ---------------------------------------------------------------------------
# ALLOWED_TEMPLATE_VARS env var extends whitelist
# ---------------------------------------------------------------------------


def test_allowed_template_vars_extends_whitelist():
    """ALLOWED_TEMPLATE_VARS env var adds vars to the effective whitelist."""
    with patch.dict(os.environ, {"ALLOWED_TEMPLATE_VARS": "MY_CUSTOM_VAR,ANOTHER_VAR"}):
        whitelist = _build_effective_whitelist()
    assert "MY_CUSTOM_VAR" in whitelist
    assert "ANOTHER_VAR" in whitelist
    # base vars still present
    assert "ANTHROPIC_API_KEY" in whitelist


def test_allowed_template_vars_empty_no_change():
    """Empty ALLOWED_TEMPLATE_VARS leaves default whitelist unchanged."""
    with patch.dict(os.environ, {"ALLOWED_TEMPLATE_VARS": ""}):
        whitelist = _build_effective_whitelist()
    assert whitelist == DEFAULT_WHITELIST


def test_resolve_uses_env_extended_whitelist():
    """When ALLOWED_TEMPLATE_VARS=MY_CUSTOM_VAR, custom var resolves at runtime."""
    env_with_custom = {**ENV, "MY_CUSTOM_VAR": "custom-value"}
    with patch.dict(os.environ, {"ALLOWED_TEMPLATE_VARS": "MY_CUSTOM_VAR"}):
        whitelist = _build_effective_whitelist()
    result = resolve("${MY_CUSTOM_VAR}", env_with_custom, whitelist)
    assert result == "custom-value"
