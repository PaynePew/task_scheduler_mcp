"""ActionResult dataclass and ActionHandler Protocol for the action registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    pass


@dataclass
class ActionResult:
    ok: bool
    result: dict | None
    error: str | None
    retryable: bool = field(default=True)


class ActionHandler(Protocol):
    name: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]
    timeout_seconds: ClassVar[int]

    async def execute(self, run: Any, params: BaseModel) -> ActionResult: ...
