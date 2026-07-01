"""Unit tests for app/mcp/status_mapping — all 7 internal inputs."""

from __future__ import annotations

import pytest

from app.mcp.status_mapping import to_external, to_external_job_status, to_job_states


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


# ---------------------------------------------------------------------------
# to_external_job_status — derived from (Job.state, latest run), ADR-067 §9.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_state,expected",
    [
        ("active", "scheduled"),
        ("completed", "completed"),
        ("cancelled", "cancelled"),
    ],
)
def test_no_run_falls_back_to_job_state(job_state: str, expected: str) -> None:
    """A job with no run yet (e.g. a not-yet-triggered chained downstream, or one
    that predicate-missed / was cancelled before ever firing) has nothing to map
    from a run status, so Job.state alone determines the external status."""
    assert to_external_job_status(job_state=job_state, latest_run_status=None) == expected


@pytest.mark.parametrize(
    "job_state,latest_run_status,expected",
    [
        # A settled one-shot whose only run failed is still 'completed' at the Job
        # level (ADR-068), but the run's own status is authoritative externally.
        ("completed", "FAILED", "failed"),
        ("completed", "SUCCEEDED", "completed"),
        # A RUNNING run left to finish after a job-level cancel (ADR-022
        # best-effort semantics) reports its true progress, not 'cancelled'.
        ("cancelled", "RUNNING", "running"),
        ("cancelled", "SUCCEEDED", "completed"),
    ],
)
def test_run_status_wins_when_a_run_exists(
    job_state: str, latest_run_status: str, expected: str
) -> None:
    assert (
        to_external_job_status(job_state=job_state, latest_run_status=latest_run_status) == expected
    )


def test_unknown_job_state_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown job state"):
        to_external_job_status(job_state="bogus", latest_run_status=None)


# ---------------------------------------------------------------------------
# to_job_states — the run-less filter fallback (inverse of the Job.state map).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "external,expected",
    [
        ("scheduled", frozenset({"active"})),
        ("completed", frozenset({"completed"})),
        ("cancelled", frozenset({"cancelled"})),
        # A run-less job is never running or failed, so nothing to fall back on.
        ("running", frozenset()),
        ("failed", frozenset()),
    ],
)
def test_to_job_states(external: str, expected: frozenset[str]) -> None:
    assert to_job_states(external) == expected


def test_to_job_states_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown external status"):
        to_job_states("bogus")


@pytest.mark.parametrize("external", ["scheduled", "completed", "cancelled"])
def test_to_job_states_roundtrips_with_to_external_job_status(external: str) -> None:
    """Each Job.state to_job_states returns must render back to the same external status
    for a run-less job — the filter and the display path stay in lockstep."""
    for job_state in to_job_states(external):
        assert to_external_job_status(job_state=job_state, latest_run_status=None) == external
