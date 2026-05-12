"""ActionResult dataclass and ActionHandler Protocol for the action registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel


@dataclass
class ActionResult:
    ok: bool
    result: dict | None
    error: str | None
    retryable: bool = True


class ActionHandler(Protocol):
    name: ClassVar[str]
    description: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]
    timeout_seconds: ClassVar[int]

    async def execute(self, run: Any, params: BaseModel) -> ActionResult: ...
