"""Unit tests for app/mcp/status_mapping — all 8 internal inputs."""

from __future__ import annotations

import pytest

from app.mcp.status_mapping import to_external


@pytest.mark.parametrize(
    "internal,expected",
    [
        ("PENDING", "scheduled"),
        ("QUEUED", "scheduled"),
        ("WAITING", "scheduled"),
        ("RUNNING", "running"),
        ("RETRYING", "running"),
        ("SUCCEEDED", "completed"),
        ("FAILED", "failed"),
        ("CANCELLED", "cancelled"),
    ],
)
def test_to_external_all_8_inputs(internal: str, expected: str) -> None:
    assert to_external(internal) == expected


def test_to_external_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown internal status"):
        to_external("BOGUS")
