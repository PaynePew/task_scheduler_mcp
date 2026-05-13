"""ActionResult + ActionHandler Protocol — the contract every handler implements.

Adding a new action = 3 files: a Pydantic params model, a handler class, and
one entry in ``app/actions/registry.py``. The worker dispatches by name; no
worker code changes needed.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    """

    ok: bool
    result: dict | None
    error: str | None
    retryable: bool = True


class ActionHandler(Protocol):
    """Structural type implemented by every action class.

    ``params_model`` is parsed by the worker before ``execute`` is called, so
    handlers receive an already-validated Pydantic instance. ``timeout_seconds``
    is enforced via ``asyncio.wait_for`` — exceeding it is treated as a
    retryable failure (the message is left for SQS to redeliver).
    """

    name: ClassVar[str]
    description: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]
    timeout_seconds: ClassVar[int]

    async def execute(self, run: Any, params: BaseModel) -> ActionResult: ...
