"""Integration test for github_digest handler — uses httpx mock (no real GitHub API).

The handler now uses CredentialMode.oauth_connection; tests inject a fake
session_factory (with mocked ConnectionStore) so no real Postgres or KMS calls
are made even though these are marked as integration tests.

Run with:
    uv run pytest -m integration tests/integration/test_github_digest.py
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.actions.github_digest import (
    GitHubDigestHandler,
    GitHubDigestParams,
    GitHubDigestResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeRun:
    user_id: str = "user-integration"


def _make_issue(number: int, title: str = "Issue title") -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
    }


def _make_pr(
    number: int, title: str = "PR title", updated_at: str = "2020-01-01T00:00:00Z"
) -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "updated_at": updated_at,
    }


def _build_mock_client(responses: list[httpx.Response]) -> MagicMock:
    """Build a mock AsyncClient that returns responses in order for each GET call."""
    call_count = {"n": 0}

    async def _fake_get(path: str, **kwargs) -> httpx.Response:
        idx = call_count["n"]
        call_count["n"] += 1
        if idx < len(responses):
            return responses[idx]
        return httpx.Response(200, json=[])

    mock_client = MagicMock()
    mock_client.get = _fake_get
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _make_handler(github_token: str = "fake-token") -> tuple[GitHubDigestHandler, AsyncMock]:
    """Build a handler with a fake session_factory and mocked ConnectionStore."""
    fake_session = MagicMock()

    @asynccontextmanager
    async def _factory():
        yield fake_session

    mock_envelope = MagicMock()
    handler = GitHubDigestHandler(session_factory=_factory, kms_envelope=mock_envelope)

    mock_store = AsyncMock()
    mock_store.get_fresh_token = AsyncMock(return_value=github_token)
    return handler, mock_store


# ---------------------------------------------------------------------------
# Integration: full query + JSON parse + schema validation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_query_json_parse_schema_validation():
    """Full query → JSON parse → result schema validation via Pydantic."""
    handler, mock_store = _make_handler()
    params = GitHubDigestParams(
        repo="owner/repo",
        labels=["bug", "enhancement"],
        pr_stale_days=5,
    )
    run = FakeRun()

    issues_bug = httpx.Response(
        200,
        json=[
            _make_issue(1, "Bug report one"),
            _make_issue(2, "Bug report two"),
        ],
    )
    issues_enhancement = httpx.Response(
        200,
        json=[
            _make_issue(3, "Feature request"),
        ],
    )
    prs_resp = httpx.Response(
        200,
        json=[
            _make_pr(10, "A stale PR", updated_at="2020-06-01T00:00:00Z"),
            _make_pr(11, "A fresh PR", updated_at="2099-12-31T00:00:00Z"),
        ],
    )

    with (
        patch("app.actions.github_digest.ConnectionStore", return_value=mock_store),
        patch(
            "app.actions.github_digest.httpx.AsyncClient",
            return_value=_build_mock_client([issues_bug, issues_enhancement, prs_resp]),
        ),
    ):
        result = await handler.execute(run=run, params=params)

    assert result.ok is True
    assert result.error is None
    assert result.result is not None

    validated = GitHubDigestResult.model_validate(result.result)

    assert validated.repo == "owner/repo"
    assert "queried_at" in result.result
    assert isinstance(result.result["queried_at"], str)

    assert "bug" in validated.labels
    assert "enhancement" in validated.labels
    assert len(validated.labels["bug"]) == 2
    assert len(validated.labels["enhancement"]) == 1
    assert validated.labels["bug"][0].number == 1
    assert validated.labels["bug"][1].number == 2
    assert validated.labels["enhancement"][0].number == 3

    assert validated.prs["open"] == 2
    stuck_numbers = [p["number"] for p in validated.prs["stuck"]]
    assert 10 in stuck_numbers
    assert 11 not in stuck_numbers

    stale = next(p for p in validated.prs["stuck"] if p["number"] == 10)
    assert stale["stale_days"] > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_result_is_json_serializable():
    """Result dict must be JSON-serializable (no datetime objects etc.)."""
    handler, mock_store = _make_handler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"], pr_stale_days=3)
    run = FakeRun()

    responses = [
        httpx.Response(200, json=[_make_issue(1, "A bug")]),
        httpx.Response(200, json=[]),
    ]

    with (
        patch("app.actions.github_digest.ConnectionStore", return_value=mock_store),
        patch(
            "app.actions.github_digest.httpx.AsyncClient",
            return_value=_build_mock_client(responses),
        ),
    ):
        result = await handler.execute(run=run, params=params)

    assert result.ok is True
    assert result.result is not None
    serialized = json.dumps(result.result)
    parsed = json.loads(serialized)
    assert parsed["repo"] == "owner/repo"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_labels_list_no_issues_call():
    """When labels=[] no issues calls are made; only PRs are queried."""
    handler, mock_store = _make_handler()
    params = GitHubDigestParams(repo="owner/repo", labels=[], pr_stale_days=7)
    run = FakeRun()

    call_count = {"n": 0}
    prs_resp = httpx.Response(
        200, json=[_make_pr(99, "Only PR", updated_at="2020-01-01T00:00:00Z")]
    )

    async def _fake_get(path: str, **kwargs) -> httpx.Response:
        call_count["n"] += 1
        return prs_resp

    mock_client = MagicMock()
    mock_client.get = _fake_get
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.actions.github_digest.ConnectionStore", return_value=mock_store),
        patch("app.actions.github_digest.httpx.AsyncClient", return_value=ctx),
    ):
        result = await handler.execute(run=run, params=params)

    assert result.ok is True
    assert result.result is not None
    assert result.result["labels"] == {}
    assert result.result["prs"]["open"] == 1
    assert call_count["n"] == 1  # Only PRs call
