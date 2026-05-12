"""Echo action handler — pipeline smoke test."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from app.actions.base import ActionResult


class EchoParams(BaseModel):
    message: str


class EchoHandler:
    name: ClassVar[str] = "echo"
    params_model: ClassVar[type[BaseModel]] = EchoParams
    timeout_seconds: ClassVar[int] = 10

    async def execute(self, run: Any, params: EchoParams) -> ActionResult:
        return ActionResult(ok=True, result={"echoed": params.message}, error=None)
