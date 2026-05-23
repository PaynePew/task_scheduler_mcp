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

from pydantic import BaseModel

from app.actions.base import CredentialMode


class _FakeParams(BaseModel):
    pass


class _FakeHandler:
    """Minimal ActionHandler stand-in used to build deterministic fixtures."""

    name: ClassVar[str] = "fake"
    description: ClassVar[str] = "fake"
    summary_line: ClassVar[str] = "Fake handler used in tests."
    params_model: ClassVar[type[BaseModel]] = _FakeParams
    timeout_seconds: ClassVar[int] = 10
    requires_operator: ClassVar[bool] = False
    credential_mode: ClassVar[CredentialMode] = CredentialMode.none
    required_provider: ClassVar[str | None] = None


def _make_handler(
    name: str,
    summary_line: str = "stub summary",
    credential_mode: CredentialMode = CredentialMode.none,
    required_provider: str | None = None,
) -> Any:
    """Build a fresh fake handler class with the given identity."""

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


# ---------------------------------------------------------------------------
# T1 — tracer bullet: every action name from the registry appears in the
# composed instructions string.
# ---------------------------------------------------------------------------


def test_build_system_instruction_includes_every_action_name() -> None:
    from app.mcp.server import build_system_instruction

    registry = {
        "alpha": _make_handler("alpha"),
        "beta": _make_handler("beta"),
        "gamma": _make_handler("gamma"),
    }
    template = "PREAMBLE\n{ACTIONS_BLOCK}\nFOOTER"

    output = build_system_instruction(registry, template)

    assert "alpha" in output
    assert "beta" in output
    assert "gamma" in output


# ---------------------------------------------------------------------------
# T2 — each action's summary_line appears on its line of the block.
# ---------------------------------------------------------------------------


def test_action_line_includes_summary_line() -> None:
    from app.mcp.server import build_system_instruction

    registry = {
        "alpha": _make_handler("alpha", summary_line="alpha does the alpha thing"),
        "beta": _make_handler("beta", summary_line="beta does something else"),
    }
    template = "{ACTIONS_BLOCK}"

    output = build_system_instruction(registry, template)

    assert "alpha does the alpha thing" in output
    assert "beta does something else" in output


# ---------------------------------------------------------------------------
# T3 — OAuth-gated handlers (credential_mode=oauth_connection) surface their
# required_provider so the LLM can guide the user to /connections#<service>.
# ---------------------------------------------------------------------------


def test_oauth_gated_action_mentions_required_provider() -> None:
    from app.mcp.server import build_system_instruction

    registry = {
        "post_chat": _make_handler(
            "post_chat",
            summary_line="post a chat message",
            credential_mode=CredentialMode.oauth_connection,
            required_provider="slack",
        ),
        "free_action": _make_handler(
            "free_action",
            summary_line="needs nothing",
            credential_mode=CredentialMode.none,
            required_provider=None,
        ),
    }
    template = "{ACTIONS_BLOCK}"

    output = build_system_instruction(registry, template)

    # The slack provider must appear somewhere on the post_chat line.
    post_chat_line = next(line for line in output.splitlines() if "post_chat" in line)
    assert "slack" in post_chat_line.lower()

    # Non-OAuth actions should NOT carry a provider tag (no false positives).
    free_line = next(line for line in output.splitlines() if "free_action" in line)
    assert "slack" not in free_line.lower()
    assert "oauth" not in free_line.lower()


# ---------------------------------------------------------------------------
# T4 — the /connections URL must appear in the composed output. This is the
# onboarding entry point; without it the LLM has no way to guide the user
# from "missing OAuth" to "click here". The real template carries the URL;
# the test fixture below stands in for the real template file.
# ---------------------------------------------------------------------------


def test_connections_url_appears_in_real_template() -> None:
    from app.mcp.server import SYSTEM_INSTRUCTION

    assert "/connections" in SYSTEM_INSTRUCTION


# ---------------------------------------------------------------------------
# T5 — the opening anti-substitution directive must appear via a STABLE REGEX.
#
# This is load-bearing wording (the 2026-05-23 incident root cause: LLM
# proposed external webhooks instead of using built-in actions). The regex is
# narrow enough to fail if the directive is removed entirely, but loose enough
# that the operator can re-word the surrounding prose at PR review time
# without breaking the test. See the review-hook comment on issue #197.
# ---------------------------------------------------------------------------


# Matches the imperative "check (the|this) (action )?list" pattern that the
# directive must keep, regardless of surrounding prose.
_ANTI_SUBSTITUTION_REGEX = re.compile(
    r"check\s+(?:the|this)\s+(?:action\s+)?list",
    re.IGNORECASE,
)


def test_anti_substitution_directive_present() -> None:
    from app.mcp.server import SYSTEM_INSTRUCTION

    assert _ANTI_SUBSTITUTION_REGEX.search(SYSTEM_INSTRUCTION), (
        "system_instruction.md must keep an opening directive telling the LLM to "
        "check the built-in action list before proposing external webhooks or "
        "third-party services. The 2026-05-23 incident root cause was the absence "
        "of this directive. Adjust wording freely but keep the imperative "
        "'check the list' shape (or update the regex if rewording demands it)."
    )


# ---------------------------------------------------------------------------
# T6 — real registry + real template integration. Verifies that the
# module-load composition wires everything together correctly. If a future
# handler is added to the registry without `summary_line` or a Protocol drift
# breaks `required_provider`, this test (and module import itself) will fail.
# ---------------------------------------------------------------------------


def test_real_system_instruction_lists_every_registered_action() -> None:
    from app.actions.registry import ACTION_REGISTRY
    from app.mcp.server import SYSTEM_INSTRUCTION

    for name in ACTION_REGISTRY:
        assert name in SYSTEM_INSTRUCTION, f"action {name!r} missing from SYSTEM_INSTRUCTION"


def test_real_system_instruction_surfaces_oauth_providers() -> None:
    from app.actions.base import CredentialMode
    from app.actions.registry import ACTION_REGISTRY
    from app.mcp.server import SYSTEM_INSTRUCTION

    for handler in ACTION_REGISTRY.values():
        if handler.credential_mode != CredentialMode.oauth_connection:
            continue
        provider = handler.required_provider
        assert provider is not None, (
            f"OAuth-gated action {handler.name!r} must declare required_provider"
        )
        # Find the line that lists this action and assert the provider is on it.
        action_line = next(
            (line for line in SYSTEM_INSTRUCTION.splitlines() if handler.name in line),
            None,
        )
        assert action_line is not None, f"{handler.name!r} not found in any line"
        assert provider in action_line.lower(), (
            f"OAuth-gated action {handler.name!r} must surface "
            f"required_provider={provider!r} on its instructions line"
        )
