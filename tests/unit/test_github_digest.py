"""Unit tests for the github_digest action handler (no real network)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.actions.github_digest import GitHubDigestHandler, GitHubDigestParams, GitHubDigestResult
from app.actions.registry import ACTION_REGISTRY

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_issue_as_pr(number: int, title: str = "PR in issues") -> dict:
    """GitHub issues endpoint returns PRs too — these have a pull_request key."""
    item = _make_issue(number, title)
    item["pull_request"] = {"url": f"https://api.github.com/repos/owner/repo/pulls/{number}"}
    return item


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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_structured_result():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"], pr_stale_days=3)

    issues_resp = httpx.Response(200, json=[_make_issue(1, "A bug")])
    prs_resp = httpx.Response(200, json=[])

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([issues_resp, prs_resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.error is None
    assert result.result is not None
    assert result.result["repo"] == "owner/repo"
    assert "queried_at" in result.result
    assert result.result["labels"]["bug"][0]["number"] == 1
    assert result.result["labels"]["bug"][0]["title"] == "A bug"
    assert result.result["prs"]["open"] == 0
    assert result.result["prs"]["stuck"] == []


@pytest.mark.asyncio
async def test_result_validates_against_pydantic_model():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["enhancement"], pr_stale_days=5)

    issues_resp = httpx.Response(200, json=[_make_issue(42, "Feature request")])
    prs_resp = httpx.Response(200, json=[])

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([issues_resp, prs_resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    validated = GitHubDigestResult.model_validate(result.result)
    assert validated.repo == "owner/repo"
    assert len(validated.labels["enhancement"]) == 1
    assert validated.prs["open"] == 0


# ---------------------------------------------------------------------------
# Empty result set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_result_emits_all_requested_labels():
    """Empty result set still emits all requested labels with empty lists."""
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug", "enhancement", "question"])

    responses = [
        httpx.Response(200, json=[]),  # bug
        httpx.Response(200, json=[]),  # enhancement
        httpx.Response(200, json=[]),  # question
        httpx.Response(200, json=[]),  # PRs
    ]

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client(responses),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert result.result is not None
    assert "bug" in result.result["labels"]
    assert "enhancement" in result.result["labels"]
    assert "question" in result.result["labels"]
    assert result.result["labels"]["bug"] == []
    assert result.result["labels"]["enhancement"] == []
    assert result.result["labels"]["question"] == []


# ---------------------------------------------------------------------------
# Stale PR detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_stale_days_filter_identifies_stale_prs():
    """PRs updated more than pr_stale_days ago appear in stuck list."""
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=[], pr_stale_days=3)

    stale_pr = _make_pr(10, "Old PR", updated_at="2020-01-01T00:00:00Z")
    fresh_pr = _make_pr(11, "New PR", updated_at="2099-12-31T00:00:00Z")

    # No labels → no issues calls; PRs response is first in the list
    prs_resp = httpx.Response(200, json=[stale_pr, fresh_pr])

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([prs_resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    stuck = result.result["prs"]["stuck"]  # type: ignore[index]
    stuck_numbers = [p["number"] for p in stuck]
    assert 10 in stuck_numbers
    assert 11 not in stuck_numbers
    assert result.result["prs"]["open"] == 2


@pytest.mark.asyncio
async def test_pr_stale_days_boundary_zero_means_all_stuck():
    """pr_stale_days=0 marks every PR as stale (age >= 0 days)."""
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=[], pr_stale_days=0)

    pr = _make_pr(5, "Any PR", updated_at="2020-01-01T00:00:00Z")
    # No labels → no issues calls; PRs response is first
    prs_resp = httpx.Response(200, json=[pr])

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([prs_resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    assert any(p["number"] == 5 for p in result.result["prs"]["stuck"])  # type: ignore[index]


# ---------------------------------------------------------------------------
# PR items in issues list are filtered out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_items_in_issues_endpoint_are_filtered_out():
    """GitHub /issues endpoint returns PRs too; those must be excluded from label lists."""
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"])

    responses = [
        httpx.Response(
            200,
            json=[_make_issue(1, "Real issue"), _make_issue_as_pr(2, "PR disguised as issue")],
        ),
        httpx.Response(200, json=[]),  # PRs
    ]

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client(responses),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is True
    label_numbers = [i["number"] for i in result.result["labels"]["bug"]]  # type: ignore[index]
    assert 1 in label_numbers
    assert 2 not in label_numbers


# ---------------------------------------------------------------------------
# Error classifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_401_is_dlq():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"])

    resp = httpx.Response(401, json={"message": "Unauthorized"})

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "401" in (result.error or "")


@pytest.mark.asyncio
async def test_error_403_rate_limited_is_retryable():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"])

    resp = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0"},
    )

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True
    assert "rate-limited" in (result.error or "").lower() or "rate" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_error_403_forbidden_not_rate_limited_is_dlq():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"])

    resp = httpx.Response(
        403,
        json={"message": "Forbidden"},
        headers={"x-ratelimit-remaining": "5000"},
    )

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "403" in (result.error or "")


@pytest.mark.asyncio
async def test_error_404_is_dlq():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/nonexistent", labels=["bug"])

    resp = httpx.Response(404, json={"message": "Not Found"})

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "404" in (result.error or "")


@pytest.mark.asyncio
async def test_error_422_is_dlq():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"])

    resp = httpx.Response(422, json={"message": "Validation Failed"})

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is False
    assert "422" in (result.error or "")


@pytest.mark.asyncio
async def test_error_500_is_retryable():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"])

    resp = httpx.Response(500, json={"message": "Internal Server Error"})

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True
    assert "500" in (result.error or "")


@pytest.mark.asyncio
async def test_error_503_is_retryable():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"])

    resp = httpx.Response(503, json={"message": "Service Unavailable"})

    with patch(
        "app.actions.github_digest.httpx.AsyncClient",
        return_value=_build_mock_client([resp]),
    ):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_timeout_is_retryable():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"])

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.github_digest.httpx.AsyncClient", return_value=ctx):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


@pytest.mark.asyncio
async def test_network_error_is_retryable():
    handler = GitHubDigestHandler()
    params = GitHubDigestParams(repo="owner/repo", labels=["bug"])

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch("app.actions.github_digest.httpx.AsyncClient", return_value=ctx):
        result = await handler.execute(run=None, params=params)

    assert result.ok is False
    assert result.retryable is True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_github_digest_registered_in_registry():
    assert "github_digest" in ACTION_REGISTRY
    assert isinstance(ACTION_REGISTRY["github_digest"], GitHubDigestHandler)


def test_registry_now_has_three_actions():
    assert len(ACTION_REGISTRY) >= 3
    assert "echo" in ACTION_REGISTRY
    assert "http_call" in ACTION_REGISTRY
    assert "github_digest" in ACTION_REGISTRY
