"""Echo action handler — pipeline smoke test."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from app.actions.base import ActionResult, CredentialMode


class EchoParams(BaseModel):
    message: str


class EchoHandler:
    name: ClassVar[str] = "echo"
    description: ClassVar[str] = (
        "Returns the input message unchanged. "
        "Use as a smoke-test to verify task creation and worker dispatch."
    )
    summary_line: ClassVar[str] = "Echoes the input message back; smoke test for create + dispatch."
    params_model: ClassVar[type[BaseModel]] = EchoParams
    timeout_seconds: ClassVar[int] = 10
    requires_operator: ClassVar[bool] = False
    credential_mode: ClassVar[CredentialMode] = CredentialMode.none
    required_provider: ClassVar[str | None] = None
    idempotent: ClassVar[bool] = True

    async def execute(self, run: Any, params: EchoParams) -> ActionResult:
        return ActionResult(ok=True, result={"echoed": params.message}, error=None)
