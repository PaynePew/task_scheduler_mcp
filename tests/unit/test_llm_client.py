"""Unit tests for app/llm/client — prompt assembly and error classification.

Uses a fake httpx.AsyncClient (injected via the constructor) to avoid real
network calls. Tests cover: success path, token-usage mapping, error retryability.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm.client import LLMClient, LLMClientError, LLMRequest, LLMResponse

# ---------------------------------------------------------------------------
# Fake httpx client helpers
# ---------------------------------------------------------------------------


def _make_http_client(status_code: int, body: dict | str) -> AsyncMock:
    """Return a fake AsyncClient whose ``post`` returns the given response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json = MagicMock(return_value=body if isinstance(body, dict) else json.loads(body))
    resp.text = json.dumps(body) if isinstance(body, dict) else body

    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    return client


def _success_body(content: str, prompt_tokens: int = 50, completion_tokens: int = 20) -> dict:
    return {
        "choices": [{"message": {"content": content, "role": "assistant"}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _make_client(status_code: int, body: dict | str) -> LLMClient:
    http = _make_http_client(status_code, body)
    return LLMClient(
        api_key="test-key",
        base_url="https://api.example.com/v1",
        model="test-model",
        http_client=http,
    )


# ---------------------------------------------------------------------------
# Success path — prompt assembly and token mapping
# ---------------------------------------------------------------------------


async def test_complete_returns_content_on_200():
    client = _make_client(200, _success_body("Hello world"))
    req = LLMRequest(
        system_prompt="You are helpful.", user_message="INPUT:\nfoo", max_output_tokens=100
    )
    resp = await client.complete(req)
    assert isinstance(resp, LLMResponse)
    assert resp.content == "Hello world"


async def test_complete_maps_prompt_tokens_to_input_tokens():
    client = _make_client(200, _success_body("Hello", prompt_tokens=42, completion_tokens=8))
    req = LLMRequest(system_prompt="sys", user_message="user", max_output_tokens=50)
    resp = await client.complete(req)
    assert resp.input_tokens == 42
    assert resp.output_tokens == 8
    assert resp.total_tokens == 50


async def test_complete_sends_system_and_user_messages(monkeypatch):
    """Verify the request payload is assembled correctly."""
    http = _make_http_client(200, _success_body("ok"))
    client = LLMClient(
        api_key="my-key",
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
        http_client=http,
    )
    req = LLMRequest(
        system_prompt="You are a summarizer.",
        user_message="INPUT:\nsome text",
        max_output_tokens=256,
    )
    await client.complete(req)

    call_kwargs = http.post.call_args
    sent_payload = call_kwargs.kwargs["json"]
    assert sent_payload["model"] == "gpt-4o-mini"
    assert sent_payload["max_tokens"] == 256
    messages = sent_payload["messages"]
    assert messages[0] == {"role": "system", "content": "You are a summarizer."}
    assert messages[1] == {"role": "user", "content": "INPUT:\nsome text"}


async def test_complete_sends_bearer_auth_header():
    http = _make_http_client(200, _success_body("ok"))
    client = LLMClient(
        api_key="sk-secret",
        base_url="https://api.example.com/v1",
        model="m",
        http_client=http,
    )
    await client.complete(LLMRequest(system_prompt="s", user_message="u", max_output_tokens=10))
    headers = http.post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer sk-secret"


async def test_total_tokens_property():
    r = LLMResponse(content="x", input_tokens=30, output_tokens=10)
    assert r.total_tokens == 40


# ---------------------------------------------------------------------------
# Missing usage fields — graceful fallback to zero
# ---------------------------------------------------------------------------


async def test_complete_missing_usage_returns_zero_tokens():
    body = {"choices": [{"message": {"content": "ok", "role": "assistant"}}]}
    client = _make_client(200, body)
    req = LLMRequest(system_prompt="s", user_message="u", max_output_tokens=10)
    resp = await client.complete(req)
    assert resp.input_tokens == 0
    assert resp.output_tokens == 0


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


async def test_429_raises_retryable_error():
    client = _make_client(429, {"error": "rate limited"})
    with pytest.raises(LLMClientError) as exc_info:
        await client.complete(LLMRequest(system_prompt="s", user_message="u", max_output_tokens=10))
    assert exc_info.value.retryable is True
    assert "429" in str(exc_info.value)


async def test_500_raises_retryable_error():
    client = _make_client(500, {"error": "internal server error"})
    with pytest.raises(LLMClientError) as exc_info:
        await client.complete(LLMRequest(system_prompt="s", user_message="u", max_output_tokens=10))
    assert exc_info.value.retryable is True


async def test_401_raises_non_retryable_error():
    client = _make_client(401, {"error": "unauthorized"})
    with pytest.raises(LLMClientError) as exc_info:
        await client.complete(LLMRequest(system_prompt="s", user_message="u", max_output_tokens=10))
    assert exc_info.value.retryable is False


async def test_400_raises_non_retryable_error():
    client = _make_client(400, {"error": "bad request"})
    with pytest.raises(LLMClientError) as exc_info:
        await client.complete(LLMRequest(system_prompt="s", user_message="u", max_output_tokens=10))
    assert exc_info.value.retryable is False


async def test_malformed_response_raises_non_retryable_error():
    """Missing 'choices' key → non-retryable LLMClientError."""
    client = _make_client(200, {"no_choices_here": []})
    with pytest.raises(LLMClientError) as exc_info:
        await client.complete(LLMRequest(system_prompt="s", user_message="u", max_output_tokens=10))
    assert exc_info.value.retryable is False
