"""R3: tasks://actions — action registry snapshot."""

from __future__ import annotations

import json

from mcp.server.lowlevel.helper_types import ReadResourceContents

from app.actions.registry import ACTION_REGISTRY

_MIME = "application/json"


def read_tasks_actions() -> list[ReadResourceContents]:
    """Handle resources/read for tasks://actions (synchronous — no DB needed)."""
    payload = [
        {
            "name": handler.name,
            "description": handler.description,
            "timeout_seconds": handler.timeout_seconds,
            "params_schema": handler.params_model.model_json_schema(),
        }
        for handler in ACTION_REGISTRY.values()
    ]
    return [ReadResourceContents(content=json.dumps(payload), mime_type=_MIME)]
