"""Unit tests for derived MCP system instructions (#197, Layer 1).

The MCP `Implementation.instructions` string is a load-bearing onboarding
surface — stale content caused the 2026-05-23 incident where Claude Desktop
proposed Slack incoming webhooks instead of using the built-in `slack_post`
action. See parent PRD #196 and ADR-061.

These tests verify that the composed instructions string is derived from the
action registry rather than hand-maintained, so adding a new handler
automatically updates the discovery surface.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

import pytest

from app.actions.base import CredentialMode


class _FakeHandler:
    """Minimal ActionHandler stand-in. Only the attributes `build_system_instruction`
    actually reads are declared; the rest are not relevant to these tests."""

    name: ClassVar[str] = "fake"
    summary_line: ClassVar[str] = "Fake handler used in tests."
    credential_mode: ClassVar[CredentialMode] = CredentialMode.none
    required_provider: ClassVar[str | None] = None


def _make_handler(
    name: str,
    summary_line: str = "stub summary",
    credential_mode: CredentialMode = CredentialMode.none,
    required_provider: str | None = None,
) -> Any:
    return type(
        f"H_{name}",
        (_FakeHandler,),
        {
            "name": name,
            "summary_line": summary_line,
            "credential_mode": credential_mode,
            "required_provider": required_provider,
        },
    )()


def test_build_system_instruction_includes_every_action_name() -> None:
    from app.mcp.server import build_system_instruction

    registry = {
        "alpha": _make_handler("alpha"),
        "beta": _make_handler("beta"),
        "gamma": _make_handler("gamma"),
    }
    output = build_system_instruction(registry, "PREAMBLE\n{ACTIONS_BLOCK}\nFOOTER")

    assert "alpha" in output
    assert "beta" in output
    assert "gamma" in output


def test_action_line_includes_summary_line() -> None:
    from app.mcp.server import build_system_instruction

    registry = {
        "alpha": _make_handler("alpha", summary_line="alpha does the alpha thing"),
        "beta": _make_handler("beta", summary_line="beta does something else"),
    }
    output = build_system_instruction(registry, "{ACTIONS_BLOCK}")

    assert "alpha does the alpha thing" in output
    assert "beta does something else" in output


def test_oauth_gated_action_mentions_required_provider() -> None:
    from app.mcp.server import build_system_instruction

    registry = {
        "post_chat": _make_handler(
            "post_chat",
            credential_mode=CredentialMode.oauth_connection,
            required_provider="slack",
        ),
        "free_action": _make_handler("free_action"),
    }
    output = build_system_instruction(registry, "{ACTIONS_BLOCK}")

    post_chat_line = next(line for line in output.splitlines() if "post_chat" in line)
    assert "slack" in post_chat_line.lower()

    free_line = next(line for line in output.splitlines() if "free_action" in line)
    assert "slack" not in free_line.lower()
    assert "oauth" not in free_line.lower()


def test_missing_placeholder_raises_at_compose_time() -> None:
    # A template that forgets {ACTIONS_BLOCK} would otherwise silently produce
    # an instructions string with zero actions — the 2026-05-23 failure mode
    # all over again. Fail loud at import time instead.
    from app.mcp.server import build_system_instruction

    with pytest.raises(ValueError, match="ACTIONS_BLOCK"):
        build_system_instruction({"alpha": _make_handler("alpha")}, "no placeholder here")


def test_connections_url_appears_in_real_template() -> None:
    from app.mcp.server import SYSTEM_INSTRUCTION

    assert "/connections" in SYSTEM_INSTRUCTION


# Matches the imperative "check (the|this) (action )?list" shape that the
# anti-substitution directive must keep. Narrow enough to fail if the directive
# is removed; loose enough that the operator can re-word surrounding prose
# without breaking the test (see #197 review-hook comment).
_ANTI_SUBSTITUTION_REGEX = re.compile(
    r"check\s+(?:the|this)\s+(?:action\s+)?list",
    re.IGNORECASE,
)


def test_anti_substitution_directive_present() -> None:
    """The 2026-05-23 incident root cause was the absence of an opening
    directive telling the LLM to check the built-in action list before
    proposing external webhooks. This test pins the directive's imperative
    shape; if rewording demands changing the shape, update the regex too."""
    from app.mcp.server import SYSTEM_INSTRUCTION

    assert _ANTI_SUBSTITUTION_REGEX.search(SYSTEM_INSTRUCTION)


def test_real_system_instruction_lists_every_non_operator_action() -> None:
    """Every public action appears in the cold-start instructions; operator-only
    actions (ADR-051) are deliberately excluded so they are not advertised to
    delegated users (ADR-066). They stay discoverable via task.list_actions.v1."""
    from app.actions.registry import ACTION_REGISTRY
    from app.mcp.server import SYSTEM_INSTRUCTION

    for name, handler in ACTION_REGISTRY.items():
        if getattr(handler, "requires_operator", False):
            assert name not in SYSTEM_INSTRUCTION, (
                f"operator-only action {name!r} must NOT appear in SYSTEM_INSTRUCTION"
            )
        else:
            assert name in SYSTEM_INSTRUCTION, (
                f"public action {name!r} missing from SYSTEM_INSTRUCTION"
            )


def test_real_system_instruction_surfaces_oauth_providers() -> None:
    from app.actions.registry import ACTION_REGISTRY
    from app.mcp.server import SYSTEM_INSTRUCTION

    for handler in ACTION_REGISTRY.values():
        if handler.credential_mode != CredentialMode.oauth_connection:
            continue
        provider = handler.required_provider
        assert provider is not None, (
            f"OAuth-gated action {handler.name!r} must declare required_provider"
        )
        action_line = next(
            (line for line in SYSTEM_INSTRUCTION.splitlines() if handler.name in line),
            None,
        )
        assert action_line is not None, f"{handler.name!r} not found in any line"
        assert provider in action_line.lower(), (
            f"OAuth-gated action {handler.name!r} must surface "
            f"required_provider={provider!r} on its instructions line"
        )
