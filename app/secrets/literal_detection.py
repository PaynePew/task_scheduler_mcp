"""Literal secret detection: identifies credential strings embedded as plain text.

Interface: detect_literal_secret(value) -> matched_prefix | None

Walks str/dict/list structures and returns the first matched prefix string if
any value appears to be a literal secret, or None if no match is found.

This is a best-effort heuristic — it is not claimed to be complete. Its purpose
is to provide an escape hatch at task.create.v1 time, prompting the caller to
use ${VAR} form instead of embedding credentials in plaintext.
"""

from __future__ import annotations

import re
from typing import Any

# Known secret prefix patterns with minimum length checks to avoid false positives.
# Each tuple: (compiled regex, prefix string for reporting)
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Anthropic (most specific first to avoid sk- catching sk-ant-)
    (re.compile(r"(?<![/\w])sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-"),
    # OpenAI / generic sk- key (after sk-ant- rule so ant keys get specific prefix)
    (re.compile(r"(?<![/\w])sk-[A-Za-z0-9_\-]{8,}"), "sk-"),
    # Slack bot token
    (re.compile(r"(?<![/\w])xoxb-[0-9A-Za-z\-]{8,}"), "xoxb-"),
    # GitHub PAT
    (re.compile(r"(?<![/\w])ghp_[A-Za-z0-9]{8,}"), "ghp_"),
    # GitLab PAT
    (re.compile(r"(?<![/\w])glpat-[A-Za-z0-9_\-]{8,}"), "glpat-"),
    # Google API key
    (re.compile(r"(?<![/\w])AIza[A-Za-z0-9_\-]{15,}"), "AIza"),
]


def detect_literal_secret(value: Any) -> str | None:
    """Return the first matched prefix if *value* contains a literal secret, else None.

    Recursively walks dict and list structures.
    """
    if isinstance(value, str):
        return _check_string(value)
    if isinstance(value, dict):
        for v in value.values():
            result = detect_literal_secret(v)
            if result is not None:
                return result
    if isinstance(value, list):
        for item in value:
            result = detect_literal_secret(item)
            if result is not None:
                return result
    return None


def _check_string(s: str) -> str | None:
    for pattern, prefix in _SECRET_PATTERNS:
        if pattern.search(s):
            return prefix
    return None
