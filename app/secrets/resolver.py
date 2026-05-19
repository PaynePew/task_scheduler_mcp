"""Secrets resolver: replaces ${VAR} placeholders with env values.

Interface: resolve(value, env, whitelist) -> resolved_value | raise SecretResolutionError

Only vars listed in *whitelist* may be substituted. Vars in the whitelist but
absent from *env* also raise SecretResolutionError (not a server retry — the
operator must set the env var).
"""

from __future__ import annotations

import os
import re
from typing import Any

_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

DEFAULT_WHITELIST: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "SLACK_WEBHOOK_URL",
        "GITHUB_TOKEN",
        "GCAL_ICS_URL",
    }
)


class SecretResolutionError(Exception):
    """Raised when a ${VAR} reference cannot be resolved.

    retryable=False: this is a configuration problem; retrying will not help.
    """

    retryable: bool = False


def build_effective_whitelist() -> frozenset[str]:
    """Merge DEFAULT_WHITELIST with ALLOWED_TEMPLATE_VARS env var."""
    extra_raw = os.environ.get("ALLOWED_TEMPLATE_VARS", "")
    extras = {v.strip() for v in extra_raw.split(",") if v.strip()}
    return DEFAULT_WHITELIST | extras


def resolve(value: Any, env: dict[str, str], whitelist: frozenset[str]) -> Any:
    """Recursively replace ${VAR} tokens in *value* using *env*.

    - str → substituted string
    - dict → recursively resolved dict (keys left as-is)
    - list → recursively resolved list
    - anything else → returned unchanged

    Raises SecretResolutionError if a reference names a var not in *whitelist*
    or names a whitelisted var that is absent from *env*.
    """
    if isinstance(value, str):
        return _resolve_string(value, env, whitelist)
    if isinstance(value, dict):
        return {k: resolve(v, env, whitelist) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(item, env, whitelist) for item in value]
    return value


def _resolve_string(s: str, env: dict[str, str], whitelist: frozenset[str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        if not var:
            return match.group(0)
        if var not in whitelist:
            raise SecretResolutionError(
                f"Template variable '${{{var}}}' is not in the allowed whitelist. "
                f"Add it to ALLOWED_TEMPLATE_VARS or use a whitelisted variable."
            )
        if var not in env:
            raise SecretResolutionError(
                f"Template variable '${{{var}}}' is whitelisted but the environment "
                f"variable {var!r} is not set."
            )
        return env[var]

    return _VAR_PATTERN.sub(_replace, s)
