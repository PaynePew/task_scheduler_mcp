"""Unit tests for app/secrets/literal_detection.py — pure function, no I/O."""

from __future__ import annotations

import pytest

from app.secrets.literal_detection import detect_literal_secret

# ---------------------------------------------------------------------------
# Known prefixes should be detected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected_prefix",
    [
        ("sk-ant-api03-xxxxx", "sk-ant-"),
        ("sk-proj-something", "sk-"),
        ("sk-somethingelse", "sk-"),
        ("xoxb-123-456-abc", "xoxb-"),
        ("ghp_abcdefghijklmnopqrst", "ghp_"),
        ("glpat-abcdefghijklmno", "glpat-"),
        ("AIzaSyABCDEFGHIJKLMNO", "AIza"),
    ],
)
def test_known_prefix_detected(value: str, expected_prefix: str):
    result = detect_literal_secret(value)
    assert result == expected_prefix, f"Expected prefix {expected_prefix!r} for {value!r}"


# ---------------------------------------------------------------------------
# False-positive avoidance — plain strings that look similar but aren't secrets
# ---------------------------------------------------------------------------


def test_plain_text_returns_none():
    assert detect_literal_secret("hello world") is None


def test_url_with_sk_path_returns_none():
    """A URL path like /sk-font/... should not trigger."""
    assert detect_literal_secret("https://example.com/sk-something") is None


def test_empty_string_returns_none():
    assert detect_literal_secret("") is None


def test_short_string_no_match():
    assert detect_literal_secret("sk-ab") is None


def test_template_var_returns_none():
    assert detect_literal_secret("${ANTHROPIC_API_KEY}") is None


# ---------------------------------------------------------------------------
# Nested structure walking
# ---------------------------------------------------------------------------


def test_dict_value_with_literal_secret_detected():
    params = {"Authorization": "Bearer sk-ant-api03-real-key"}
    result = detect_literal_secret(params)
    assert result == "sk-ant-"


def test_nested_dict_value_detected():
    params = {"headers": {"X-Api-Key": "ghp_realkeyhere1234567890"}}
    result = detect_literal_secret(params)
    assert result == "ghp_"


def test_list_with_literal_secret_detected():
    params = ["safe-value", "xoxb-123456789-abcdefg"]
    result = detect_literal_secret(params)
    assert result == "xoxb-"


def test_nested_list_in_dict_detected():
    params = {"keys": ["AIzaSyABCDEFGHIJKLMNOPQ"]}
    result = detect_literal_secret(params)
    assert result == "AIza"


def test_dict_with_no_secrets_returns_none():
    params = {"method": "GET", "url": "https://example.com"}
    result = detect_literal_secret(params)
    assert result is None


def test_non_string_scalar_returns_none():
    assert detect_literal_secret(42) is None
    assert detect_literal_secret(None) is None
    assert detect_literal_secret(True) is None
