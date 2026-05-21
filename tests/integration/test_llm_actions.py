"""Integration tests for llm_summarize and llm_polish chain-fed mode.

Chain test: ``from_run_id`` resolution feeds the source run's output as the
LLM input. Verifies that the handler correctly reads the upstream JobRun from
the real database and passes the result as input text to the (fake) LLM client.

Requires running Postgres (DATABASE_URL set in environment).

Run with:
    uv run pytest -m integration tests/integration/test_llm_actions.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.actions.llm_polish import LlmPolishHandler, LlmPolishParams
from app.actions.llm_summarize import LlmSummarizeHandler, LlmSummarizeParams
from app.db.engine import create_async_engine
from app.db.models import Job, JobRun
from app.llm.budget import Allow
from app.llm.client import LLMClient, LLMResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    """Fresh engine per test; cleans all job and budget data on teardown."""
    engine = create_async_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(
                text(
                    "DO $$ BEGIN"
                    "  IF to_regclass('public.llm_token_budgets') IS NOT NULL"
                    "    THEN DELETE FROM llm_token_budgets;"
                    "  END IF;"
                    " END $$"
                )
            )
            await session.execute(text("DELETE FROM job_runs"))
            await session.execute(text("DELETE FROM jobs"))
    await engine.dispose()


async def _insert_job_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    result: str | None = None,
    error_message: str | None = None,
    status: str = "SUCCEEDED",
    user_id: str = "llm-chain-test",
) -> tuple[int, int]:
    """Insert a minimal Job + JobRun; return (job_id, run_id)."""
    scheduled = datetime.now(tz=UTC) - timedelta(hours=1)
    bucket = scheduled.replace(minute=0, second=0, microsecond=0).isoformat()

    async with factory() as session:
        async with session.begin():
            job = Job(
                user_id=user_id,
                description="upstream job for llm chain test",
                action="echo",
                action_params={"message": "hi"},
                job_type="one_shot",
                scheduled_at=scheduled,
            )
            session.add(job)
            await session.flush()

            run = JobRun(
                time_bucket=bucket,
                job_id=job.job_id,
                user_id=user_id,
                scheduled_at=scheduled,
                status=status,
                result=result,
                error_message=error_message,
            )
            session.add(run)
            await session.flush()
            return job.job_id, run.run_id


def _make_fake_llm_capturing(captured: dict) -> MagicMock:
    """Fake LLMClient that records the user_message it receives."""

    async def _complete(req):
        captured["user_message"] = req.user_message
        captured["system_prompt"] = req.system_prompt
        return LLMResponse(content="[fake summary]", input_tokens=10, output_tokens=5)

    client = MagicMock(spec=LLMClient)
    client.complete = _complete
    return client


# ---------------------------------------------------------------------------
# Chain test: from_run_id feeds upstream JSON result as LLM input
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_llm_summarize_from_run_id_feeds_upstream_json(session_factory):
    """from_run_id=<run_id> → handler reads JobRun.result from DB → passes to LLM."""
    upstream_data = {"issues": ["Fix login", "Update README"], "count": 2}
    _, run_id = await _insert_job_run(factory=session_factory, result=json.dumps(upstream_data))

    captured: dict = {}
    fake_llm = _make_fake_llm_capturing(captured)

    with patch("app.actions.llm_summarize.check_token_budget", return_value=Allow()):
        with patch("app.actions.llm_summarize.record_token_usage", return_value=None):
            handler = LlmSummarizeHandler(session_factory=session_factory, llm_client=fake_llm)
            run_mock = MagicMock()
            run_mock.user_id = "llm-chain-test"
            result = await handler.execute(
                run=run_mock,
                params=LlmSummarizeParams(from_run_id=run_id, style="bullet", length="short"),
            )

    assert result.ok is True, f"Expected ok=True, got error: {result.error}"
    assert result.result["summary"] == "[fake summary]"
    # The user_message must contain the upstream JSON content
    assert "Fix login" in captured["user_message"]
    assert "INPUT:" in captured["user_message"]


@pytest.mark.integration
async def test_llm_summarize_from_run_id_feeds_upstream_string_result(session_factory):
    """Upstream result that is a JSON-encoded string is unwrapped and used as-is."""
    upstream_text = "Weekly GitHub digest: 3 PRs merged, 5 issues closed."
    _, run_id = await _insert_job_run(factory=session_factory, result=json.dumps(upstream_text))

    captured: dict = {}
    fake_llm = _make_fake_llm_capturing(captured)

    with patch("app.actions.llm_summarize.check_token_budget", return_value=Allow()):
        with patch("app.actions.llm_summarize.record_token_usage", return_value=None):
            handler = LlmSummarizeHandler(session_factory=session_factory, llm_client=fake_llm)
            run_mock = MagicMock()
            run_mock.user_id = "llm-chain-test"
            result = await handler.execute(
                run=run_mock,
                params=LlmSummarizeParams(from_run_id=run_id),
            )

    assert result.ok is True
    assert upstream_text in captured["user_message"]


@pytest.mark.integration
async def test_llm_summarize_from_run_id_upstream_error_returns_failure(session_factory):
    """Upstream run with error_message → handler returns ok=False without calling LLM."""
    _, run_id = await _insert_job_run(
        factory=session_factory,
        result=None,
        error_message="upstream action failed: connection refused",
        status="FAILED",
    )

    captured: dict = {}
    fake_llm = _make_fake_llm_capturing(captured)

    with patch("app.actions.llm_summarize.check_token_budget", return_value=Allow()):
        handler = LlmSummarizeHandler(session_factory=session_factory, llm_client=fake_llm)
        run_mock = MagicMock()
        run_mock.user_id = "llm-chain-test"
        result = await handler.execute(
            run=run_mock,
            params=LlmSummarizeParams(from_run_id=run_id),
        )

    assert result.ok is False
    assert "upstream" in result.error.lower() or "failed" in result.error.lower()
    assert "user_message" not in captured  # LLM was not called


@pytest.mark.integration
async def test_llm_polish_from_run_id_feeds_upstream_result(session_factory):
    """llm_polish also supports from_run_id; verifies the same chain mechanism."""
    upstream_text = "rough text that needs polishing"
    _, run_id = await _insert_job_run(factory=session_factory, result=json.dumps(upstream_text))

    captured: dict = {}
    fake_llm = _make_fake_llm_capturing(captured)

    with patch("app.actions.llm_polish.check_token_budget", return_value=Allow()):
        with patch("app.actions.llm_polish.record_token_usage", return_value=None):
            handler = LlmPolishHandler(session_factory=session_factory, llm_client=fake_llm)
            run_mock = MagicMock()
            run_mock.user_id = "llm-chain-test"
            result = await handler.execute(
                run=run_mock,
                params=LlmPolishParams(from_run_id=run_id, tone="professional"),
            )

    assert result.ok is True
    assert upstream_text in captured["user_message"]
    assert "professional" in captured["system_prompt"]


# ---------------------------------------------------------------------------
# Token budget accounting against real DB
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_token_budget_recorded_in_db_after_successful_call(session_factory):
    """After a successful call, tokens are recorded in llm_token_budgets."""
    captured: dict = {}

    async def _complete(req):
        captured["called"] = True
        return LLMResponse(content="ok", input_tokens=20, output_tokens=10)

    fake_llm = MagicMock(spec=LLMClient)
    fake_llm.complete = _complete

    handler = LlmSummarizeHandler(session_factory=session_factory, llm_client=fake_llm)
    run_mock = MagicMock()
    run_mock.user_id = "budget-recording-test"

    result = await handler.execute(
        run=run_mock,
        params=LlmSummarizeParams(text="hello world"),
    )

    assert result.ok is True

    # Verify tokens were actually written to the DB

    from sqlalchemy import select

    from app.db.models import LlmTokenBudget

    async with session_factory() as session:
        async with session.begin():
            today = datetime.now(UTC).date()
            row = (
                await session.execute(
                    select(LlmTokenBudget.tokens_used).where(
                        LlmTokenBudget.user_id == "budget-recording-test",
                        LlmTokenBudget.budget_date == today,
                    )
                )
            ).scalar_one_or_none()

    assert row is not None
    assert row == 30  # 20 input + 10 output
