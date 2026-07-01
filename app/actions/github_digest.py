"""github_digest action handler — queries GitHub Issues + PRs for a repo.

Credential resolution (ADR-050, ADR-051):
  credential_mode = oauth_connection — resolves the GitHub token via
  the get_token facade keyed by (job.user_id, "github").

If the user has no GitHub connection:
  - execute() returns ok=False, retryable=False with a descriptive error.
  - task.create pre-flight also checks and returns an error with connect_url
    (handled in app.mcp.server, not here).

Error classification policy (deliberate trade-offs per ADR-013 commentary):
  - 401 Unauthorized          → DLQ (permanent failure, bad token)
  - 403 rate-limited          → retry (x-ratelimit-remaining == 0)
  - 403 forbidden (other)     → DLQ (permanent failure, insufficient permissions)
  - 404 Not Found             → DLQ (permanent failure, repo doesn't exist or inaccessible)
  - 422 Unprocessable Entity  → DLQ (permanent failure, bad query params)
  - 5xx Server Error          → retry
  - timeout / network error   → retry
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

from app.actions._oauth import check_oauth_for_execute, missing_connection_result
from app.actions.base import ActionResult, CredentialMode
from app.connections.store import ConnectionMiss, get_token

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
        "Result is stored in JobRun.result for downstream chaining (e.g., slack_post). "
        "Requires a GitHub OAuth connection — connect at /connections."
    )
    summary_line: ClassVar[str] = (
        "Queries GitHub Issues + PRs for a repo; ideal upstream for slack_post / email_send."
    )
    params_model: ClassVar[type[BaseModel]] = GitHubDigestParams
    timeout_seconds: ClassVar[int] = 30
    requires_operator: ClassVar[bool] = False
    credential_mode: ClassVar[CredentialMode] = CredentialMode.oauth_connection
    required_provider: ClassVar[str | None] = "github"
    idempotent: ClassVar[bool] = False  # external side effect (GitHub API call)

    async def execute(self, run: Any, params: GitHubDigestParams) -> ActionResult:
        # Check connection validity (expiry + missing) before attempting the action.
        oauth_err = await check_oauth_for_execute(run.user_id, "github")
        if oauth_err is not None:
            return oauth_err

        try:
            token = await get_token(run.user_id, "github")
        except ConnectionMiss:
            return missing_connection_result("github")

        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
        }

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
                updated_at = datetime.fromisoformat(pr["updated_at"])
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
            error=(
                "GitHub API 401 Unauthorized — the stored GitHub connection is invalid "
                "or revoked; reconnect at /connections"
            ),
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
