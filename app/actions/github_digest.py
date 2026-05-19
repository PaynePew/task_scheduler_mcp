"""github_digest action handler — queries GitHub Issues + PRs for a repo.

Error classification policy (deliberate trade-offs per ADR-013 commentary):
  - 401 Unauthorized          → DLQ (permanent failure, bad token)
  - 403 rate-limited          → retry (x-ratelimit-remaining == 0)
  - 403 forbidden (other)     → DLQ (permanent failure, insufficient permissions)
  - 404 Not Found             → DLQ (permanent failure, repo doesn't exist or inaccessible)
  - 422 Unprocessable Entity  → DLQ (permanent failure, bad query params)
  - 5xx Server Error          → retry
  - timeout / network error   → retry

Does NOT honour x-ratelimit-reset — relies on SQS visibility timeout + retry
count to provide backoff (deliberate trade-off: simpler, no wall-clock dependency).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from app.actions.base import ActionResult
from app.secrets.resolver import SecretResolutionError, build_effective_whitelist, resolve

GITHUB_API_BASE = "https://api.github.com"


class GitHubDigestParams(BaseModel):
    repo: str
    labels: list[str]
    pr_stale_days: int = 3


class _IssueItem(BaseModel):
    number: int
    title: str
    url: str


class _PRItem(BaseModel):
    number: int
    title: str
    url: str
    stale_days: int


class GitHubDigestResult(BaseModel):
    repo: str
    queried_at: str
    labels: dict[str, list[_IssueItem]]
    prs: dict[str, Any]


class GitHubDigestHandler:
    name: ClassVar[str] = "github_digest"
    description: ClassVar[str] = (
        "Queries GitHub Issues + PRs for a repository and returns a structured JSON payload. "
        "Filters issues by label and identifies stale open PRs. "
        "Result is stored in JobRun.result for downstream chaining (e.g., slack_post)."
    )
    params_model: ClassVar[type[BaseModel]] = GitHubDigestParams
    timeout_seconds: ClassVar[int] = 30

    async def execute(self, run: Any, params: GitHubDigestParams) -> ActionResult:
        whitelist = build_effective_whitelist()
        env = dict(os.environ)

        try:
            resolve(params.repo, env, whitelist)
        except SecretResolutionError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=False)

        token = env.get("GITHUB_TOKEN", "")
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(
                base_url=GITHUB_API_BASE,
                headers=headers,
                timeout=self.timeout_seconds,
            ) as client:
                labels_result = await self._query_labels(client, params)
                if isinstance(labels_result, ActionResult):
                    return labels_result

                prs_result = await self._query_prs(client, params)
                if isinstance(prs_result, ActionResult):
                    return prs_result

        except httpx.TimeoutException as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=True)
        except httpx.RequestError as exc:
            return ActionResult(ok=False, result=None, error=str(exc), retryable=True)

        result = {
            "repo": params.repo,
            "queried_at": datetime.now(UTC).isoformat(),
            "labels": {
                label: [i.model_dump() for i in items] for label, items in labels_result.items()
            },
            "prs": {
                "open": prs_result["open"],
                "stuck": [pr.model_dump() for pr in prs_result["stuck"]],
            },
        }

        return ActionResult(ok=True, result=result, error=None, retryable=False)

    async def _query_labels(
        self,
        client: httpx.AsyncClient,
        params: GitHubDigestParams,
    ) -> dict[str, list[_IssueItem]] | ActionResult:
        labels_result: dict[str, list[_IssueItem]] = {label: [] for label in params.labels}

        for label in params.labels:
            page = 1
            while True:
                resp = await client.get(
                    f"/repos/{params.repo}/issues",
                    params={
                        "state": "open",
                        "labels": label,
                        "per_page": 100,
                        "page": page,
                    },
                )
                err = _classify_error(resp)
                if err is not None:
                    return err

                items = resp.json()
                for item in items:
                    if "pull_request" not in item:
                        labels_result[label].append(
                            _IssueItem(
                                number=item["number"],
                                title=item["title"],
                                url=item["html_url"],
                            )
                        )
                if len(items) < 100:
                    break
                page += 1

        return labels_result

    async def _query_prs(
        self,
        client: httpx.AsyncClient,
        params: GitHubDigestParams,
    ) -> dict[str, Any] | ActionResult:
        now = datetime.now(UTC)
        stale_threshold = timedelta(days=params.pr_stale_days)
        open_count = 0
        stuck: list[_PRItem] = []

        page = 1
        while True:
            resp = await client.get(
                f"/repos/{params.repo}/pulls",
                params={"state": "open", "per_page": 100, "page": page},
            )
            err = _classify_error(resp)
            if err is not None:
                return err

            items = resp.json()
            open_count += len(items)
            for pr in items:
                updated_at = datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00"))
                age = now - updated_at
                if age >= stale_threshold:
                    stuck.append(
                        _PRItem(
                            number=pr["number"],
                            title=pr["title"],
                            url=pr["html_url"],
                            stale_days=age.days,
                        )
                    )
            if len(items) < 100:
                break
            page += 1

        return {"open": open_count, "stuck": stuck}


def _classify_error(response: httpx.Response) -> ActionResult | None:
    """Return an ActionResult for error responses, None for success."""
    if response.is_success:
        return None

    status = response.status_code

    if status == 401:
        return ActionResult(
            ok=False,
            result=None,
            error="GitHub API 401 Unauthorized — check GITHUB_TOKEN",
            retryable=False,
        )

    if status == 403:
        remaining = response.headers.get("x-ratelimit-remaining", "1")
        if remaining == "0":
            return ActionResult(
                ok=False,
                result=None,
                error="GitHub API 403 rate-limited (x-ratelimit-remaining=0)",
                retryable=True,
            )
        return ActionResult(
            ok=False,
            result=None,
            error="GitHub API 403 Forbidden — insufficient permissions",
            retryable=False,
        )

    if status == 404:
        return ActionResult(
            ok=False,
            result=None,
            error="GitHub API 404 Not Found — repo not found or inaccessible",
            retryable=False,
        )

    if status == 422:
        return ActionResult(
            ok=False,
            result=None,
            error="GitHub API 422 Unprocessable Entity",
            retryable=False,
        )

    if status >= 500:
        return ActionResult(
            ok=False,
            result=None,
            error=f"GitHub API {status} Server Error",
            retryable=True,
        )

    return ActionResult(
        ok=False,
        result=None,
        error=f"GitHub API {status}",
        retryable=False,
    )
