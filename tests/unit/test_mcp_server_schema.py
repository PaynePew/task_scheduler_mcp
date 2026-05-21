"""Unit tests for the inputSchema of task.create.v1 (fix for #187).

The action enum was historically hard-coded to ["echo"] (S05 bootstrap restriction)
and never widened as W2-W4 added 7 more handlers. This test pins the enum to the
ACTION_REGISTRY so future handlers automatically appear in the schema.
"""

from app.actions.registry import ACTION_REGISTRY
from app.mcp.server import _TASK_CREATE_SCHEMA


def test_action_enum_matches_registry():
    schema_enum = _TASK_CREATE_SCHEMA["properties"]["action"]["enum"]
    assert schema_enum == sorted(ACTION_REGISTRY.keys())


def test_action_enum_contains_all_registered_handlers():
    schema_enum = set(_TASK_CREATE_SCHEMA["properties"]["action"]["enum"])
    assert schema_enum == set(ACTION_REGISTRY.keys())


def test_action_and_action_params_remain_required():
    assert set(_TASK_CREATE_SCHEMA["required"]) == {"action", "action_params"}
