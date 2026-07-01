"""Unit tests for app/mcp/status_mapping — all 7 internal inputs."""

from __future__ import annotations

import pytest

from app.mcp.status_mapping import to_external


@pytest.mark.parametrize(
    "internal,expected",
    [
        ("PENDING", "scheduled"),
        ("QUEUED", "scheduled"),
        ("RUNNING", "running"),
        ("RETRYING", "running"),
        ("SUCCEEDED", "completed"),
        ("FAILED", "failed"),
        ("CANCELLED", "cancelled"),
    ],
)
def test_to_external_all_7_inputs(internal: str, expected: str) -> None:
    assert to_external(internal) == expected


def test_to_external_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown internal status"):
        to_external("BOGUS")


def test_to_external_waiting_is_removed() -> None:
    """WAITING was dropped from the 7-state machine (ADR-067) — it is now unknown."""
    with pytest.raises(ValueError, match="Unknown internal status"):
        to_external("WAITING")
