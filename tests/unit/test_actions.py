"""Unit tests for action registry and echo handler."""

import pytest
from pydantic import ValidationError

from app.actions.echo import EchoHandler, EchoParams
from app.actions.registry import ACTION_REGISTRY


def test_registry_lookup_returns_echo_handler():
    assert "echo" in ACTION_REGISTRY
    assert isinstance(ACTION_REGISTRY["echo"], EchoHandler)


@pytest.mark.asyncio
async def test_echo_handler_happy_path():
    handler = ACTION_REGISTRY["echo"]
    params = EchoParams(message="hello world")
    result = await handler.execute(run=None, params=params)
    assert result.ok is True
    assert result.result == {"echoed": "hello world"}
    assert result.error is None


def test_echo_params_missing_required_field_raises_validation_error():
    with pytest.raises(ValidationError):
        EchoParams()  # type: ignore[call-arg]  # intentional: omit required `message` to assert pydantic raises
