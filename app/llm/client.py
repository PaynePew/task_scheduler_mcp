"""Provider-agnostic LLM client backed by OpenAI-compatible Chat Completions API.

Implements the narrow surface needed by llm_summarize and llm_polish: a single
``complete`` call that sends a fixed system prompt + user message and returns
the generated text with token-usage counts.

Provider selection is fully external: point ``LLM_API_BASE_URL`` at any
OpenAI-compatible endpoint (OpenAI, Together AI, Groq, LiteLLM proxy, …) and
set ``LLM_MODEL`` + ``LLM_API_KEY`` accordingly. No SDK dependency — uses httpx
so the dep footprint stays unchanged.

Usage errors (e.g. invalid API key, model not found) surface as ``LLMClientError``
with ``retryable`` indicating whether retry makes sense.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class LLMRequest:
    """Everything the handler passes to the LLM for one completion call."""

    system_prompt: str
    user_message: str
    max_output_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    """Successful LLM completion result."""

    content: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMClientError(Exception):
    """Raised when the LLM provider returns an error or the call fails."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMClient:
    """Thin httpx wrapper around the OpenAI Chat Completions API.

    Pass *_http_client* to override the real httpx client in tests — the
    injected client's ``post`` method is called with the same kwargs.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._http_client = http_client

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """Send one Chat Completions request and return the result.

        Raises ``LLMClientError`` on any provider error, network failure, or
        unexpected response shape.
        """
        payload = {
            "model": self._model,
            "max_tokens": req.max_output_tokens,
            "messages": [
                {"role": "system", "content": req.system_prompt},
                {"role": "user", "content": req.user_message},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            if self._http_client is not None:
                response = await self._http_client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            else:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
        except httpx.TimeoutException as exc:
            raise LLMClientError(f"LLM request timed out: {exc}", retryable=True) from exc
        except httpx.RequestError as exc:
            raise LLMClientError(f"LLM network error: {exc}", retryable=True) from exc

        if response.status_code == 429:
            raise LLMClientError(
                f"LLM provider rate-limited (429): {response.text[:200]}",
                retryable=True,
            )
        if response.status_code >= 500:
            raise LLMClientError(
                f"LLM provider server error ({response.status_code}): {response.text[:200]}",
                retryable=True,
            )
        if not response.is_success:
            raise LLMClientError(
                f"LLM provider error ({response.status_code}): {response.text[:200]}",
                retryable=False,
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMClientError(f"Unexpected LLM response shape: {exc}", retryable=False) from exc

        return LLMResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
