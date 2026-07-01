"""Unit tests for per-action idempotency posture (ADR-013, issue #268).

`ActionHandler.idempotent` lets the reconciler's `RUNNING`-orphan recovery
(PRD #266) decide retry-in-place (idempotent) vs fail-and-alert (not) without
hardcoding an action name. This module locks each handler's declared posture
so a future action cannot silently default to the wrong one.
"""

from __future__ import annotations

from app.actions.registry import ACTION_REGISTRY

# Every action name in ACTION_REGISTRY must have exactly one entry here.
# Adding a new handler without extending this map fails
# test_every_registered_action_has_a_declared_posture below, forcing the
# idempotent/not choice to be made explicitly instead of assumed.
EXPECTED_IDEMPOTENT_POSTURE: dict[str, bool] = {
    "echo": True,
    "llm_summarize": True,
    "llm_polish": True,
    "calendar_digest_ics": True,
    "http_call": False,
    "email_send": False,
    "slack_post": False,
    "github_digest": False,
}


def test_every_registered_action_has_a_declared_posture():
    assert set(ACTION_REGISTRY) == set(EXPECTED_IDEMPOTENT_POSTURE)


def test_registered_handlers_match_expected_idempotent_posture():
    for name, expected in EXPECTED_IDEMPOTENT_POSTURE.items():
        handler = ACTION_REGISTRY[name]
        assert isinstance(handler.idempotent, bool), f"{name}.idempotent must be a bool"
        assert handler.idempotent is expected, f"{name}.idempotent posture mismatch"


def test_pure_output_only_actions_are_idempotent():
    for name in ("echo", "llm_summarize", "llm_polish", "calendar_digest_ics"):
        assert ACTION_REGISTRY[name].idempotent is True


def test_external_side_effect_actions_are_not_idempotent():
    for name in ("email_send", "slack_post", "github_digest", "http_call"):
        assert ACTION_REGISTRY[name].idempotent is False
