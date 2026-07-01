"""ActionResult + ActionHandler Protocol — the contract every handler implements.

Adding a new action = 3 files: a Pydantic params model, a handler class, and
one entry in ``app/actions/registry.py``. The worker dispatches by name; no
worker code changes needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel


@dataclass
class ActionResult:
    """Return value of every action handler.

    The worker's terminal-write logic branches on these fields:

      ok=True                  → JobRun goes to SUCCEEDED, message DeleteMessage'd.
      ok=False, retryable=True → message left on the queue; SQS visibility
                                 expiry redelivers it. After ``max_receive_count``
                                 redeliveries (3) SQS routes it to the DLQ.
                                 Use for transient failures (HTTP 5xx, timeouts).
      ok=False, retryable=False → JobRun goes to FAILED immediately, message
                                  DeleteMessage'd. *No* retry. Use for permanent
                                  failures (HTTP 4xx, validation errors). Issue
                                  #26 fixed the bug where these used to loop.

    error_code: when set, propagated into JobRun.error_code so task.status.v1
    can surface the canonical error envelope (code + message) instead of a
    freeform string. Must be from the 7-code vocabulary (CONTEXT.md §6, ADR-060).
    """

    ok: bool
    result: dict | None
    error: str | None
    error_code: str | None = None
    retryable: bool = True


class CredentialMode(StrEnum):
    """How an action resolves its credentials (ADR-051).

    none            — no credentials needed (e.g. echo).
    oauth_connection — per-user OAuth token from the connection store (future slices).
    operator_env    — operator ${VAR}-env secrets (ADR-032); operator-only.
    """

    none = "none"
    oauth_connection = "oauth_connection"
    operator_env = "operator_env"


class ActionHandler(Protocol):
    """Structural type implemented by every action class.

    ``params_model`` is parsed by the worker before ``execute`` is called, so
    handlers receive an already-validated Pydantic instance. ``timeout_seconds``
    is enforced via ``asyncio.wait_for`` — exceeding it is treated as a
    retryable failure (the message is left for SQS to redeliver).

    ``requires_operator`` and ``credential_mode`` implement action-surface tiering
    (ADR-051): ``task.create`` rejects operator-only actions for non-operator callers.

    ``summary_line`` is a one-line capability blurb (<=80 chars) injected into
    the MCP ``instructions`` string at server startup. ``required_provider`` names
    the OAuth provider the action needs (``"slack"``, ``"github"``, ``"google"``);
    ``None`` for actions that don't need a per-user OAuth connection. Together they
    make the server self-describe at handshake time (ADR-061).

    ``idempotent`` declares whether re-executing the action with the same params
    is safe (no duplicate external effect). There is no default here on purpose
    (Protocol carries no implementation) — every handler must set it explicitly;
    `tests/unit/test_action_idempotency.py` fails closed if a registered handler
    omits it. Pure / output-only actions (``echo``, ``llm_summarize``,
    ``llm_polish``, ``calendar_digest_ics``) are ``True``; actions with an
    external side effect (``email_send``, ``slack_post``, ``github_digest``,
    ``http_call``) are ``False``. Consumed by the reconciler's `RUNNING`-orphan
    recovery (issue #268; PRD #266) to decide retry-in-place vs fail-and-alert.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    summary_line: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]
    timeout_seconds: ClassVar[int]
    requires_operator: ClassVar[bool]
    credential_mode: ClassVar[CredentialMode]
    required_provider: ClassVar[str | None]
    idempotent: ClassVar[bool]

    async def execute(self, run: Any, params: BaseModel) -> ActionResult: ...
