"""Module-level ACTION_REGISTRY mapping action names to handler instances."""

from app.actions.base import ActionHandler
from app.actions.echo import EchoHandler

ACTION_REGISTRY: dict[str, ActionHandler] = {
    EchoHandler.name: EchoHandler(),
}
