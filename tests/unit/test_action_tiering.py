"""Unit tests for action-surface tiering and the operator gate (ADR-051, issue #132)."""

from __future__ import annotations

import pytest

from app.actions.base import CredentialMode
from app.actions.registry import ACTION_REGISTRY
from app.domain.jobs import OperatorOnlyActionError, UnknownActionError, create_job

# ---------------------------------------------------------------------------
# Handler attribute tests
# ---------------------------------------------------------------------------


def test_echo_is_public_no_secret():
    handler = ACTION_REGISTRY["echo"]
    assert handler.requires_operator is False
    assert handler.credential_mode == CredentialMode.none


def test_http_call_is_operator_only():
    handler = ACTION_REGISTRY["http_call"]
    assert handler.requires_operator is True
    assert handler.credential_mode == CredentialMode.operator_env


def test_email_send_smtp_is_operator_only():
    handler = ACTION_REGISTRY["email_send"]
    assert handler.requires_operator is True
    assert handler.credential_mode == CredentialMode.operator_env


def test_r2_upload_removed_from_registry():
    assert "r2_upload" not in ACTION_REGISTRY


def test_slack_post_has_tiering_attributes():
    handler = ACTION_REGISTRY["slack_post"]
    assert hasattr(handler, "requires_operator")
    assert hasattr(handler, "credential_mode")
    assert handler.requires_operator is False


def test_github_digest_has_tiering_attributes():
    handler = ACTION_REGISTRY["github_digest"]
    assert hasattr(handler, "requires_operator")
    assert hasattr(handler, "credential_mode")
    assert handler.requires_operator is False


def test_calendar_digest_ics_is_operator_only():
    handler = ACTION_REGISTRY["calendar_digest_ics"]
    assert handler.requires_operator is True
    assert handler.credential_mode == CredentialMode.operator_env


# ---------------------------------------------------------------------------
# create_job operator gate tests (pure domain — no DB needed)
# ---------------------------------------------------------------------------

OPERATOR_ID = "operator-uid"
PUBLIC_ID = "public-user-123"


@pytest.mark.asyncio
async def test_public_caller_rejected_for_operator_only_action():
    """A non-operator caller must get OperatorOnlyActionError for http_call."""
    with pytest.raises(OperatorOnlyActionError) as exc_info:
        await create_job(
            None,  # type: ignore[arg-type]  # gate fires before DB touch
            user_id=PUBLIC_ID,
            action="http_call",
            action_params={"method": "GET", "url": "https://example.com"},
            schedule_type="immediate",
            operator_user_id=OPERATOR_ID,
        )
    assert "http_call" in str(exc_info.value)


@pytest.mark.asyncio
async def test_operator_caller_passes_gate_for_operator_only_action():
    """Operator must NOT get OperatorOnlyActionError (gate passes; DB error expected instead)."""
    # Gate passes, but session=None triggers an error from DB code — that's fine;
    # it proves the gate did not fire for the operator.
    try:
        await create_job(
            None,  # type: ignore[arg-type]  # will error at DB, not at gate
            user_id=OPERATOR_ID,
            action="http_call",
            action_params={"method": "GET", "url": "https://example.com"},
            schedule_type="immediate",
            operator_user_id=OPERATOR_ID,
        )
    except OperatorOnlyActionError:
        pytest.fail("Operator should not be blocked by requires_operator gate")
    except Exception:
        pass  # DB/session errors expected; gate did not fire — test passes


@pytest.mark.asyncio
async def test_public_caller_allowed_for_public_action():
    """A public caller must NOT be rejected for echo (requires_operator=False)."""
    try:
        await create_job(
            None,  # type: ignore[arg-type]
            user_id=PUBLIC_ID,
            action="echo",
            action_params={"message": "hello"},
            schedule_type="immediate",
            operator_user_id=OPERATOR_ID,
        )
    except OperatorOnlyActionError:
        pytest.fail("Public action echo must not be blocked")
    except Exception:
        pass  # DB errors expected after gate passes


@pytest.mark.asyncio
async def test_unknown_action_raises_unknown_action_error():
    """Unknown actions still raise UnknownActionError regardless of operator gate."""
    with pytest.raises(UnknownActionError):
        await create_job(
            None,  # type: ignore[arg-type]
            user_id=PUBLIC_ID,
            action="nonexistent_action",
            action_params={},
            schedule_type="immediate",
            operator_user_id=OPERATOR_ID,
        )


@pytest.mark.asyncio
async def test_gate_skipped_when_operator_user_id_not_set():
    """When operator_user_id=None the gate is skipped (backward compat)."""
    try:
        await create_job(
            None,  # type: ignore[arg-type]
            user_id=PUBLIC_ID,
            action="http_call",
            action_params={"method": "GET", "url": "https://example.com"},
            schedule_type="immediate",
            operator_user_id=None,
        )
    except OperatorOnlyActionError:
        pytest.fail("Gate must not fire when operator_user_id is None")
    except Exception:
        pass  # DB errors expected


@pytest.mark.asyncio
async def test_email_send_blocked_for_public_caller():
    """email_send (SMTP) must be blocked for public callers."""
    with pytest.raises(OperatorOnlyActionError):
        await create_job(
            None,  # type: ignore[arg-type]
            user_id=PUBLIC_ID,
            action="email_send",
            action_params={"to": ["test@example.com"], "subject": "hi", "body": "body"},
            schedule_type="immediate",
            operator_user_id=OPERATOR_ID,
        )
