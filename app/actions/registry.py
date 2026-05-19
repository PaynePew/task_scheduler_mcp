"""Module-level ACTION_REGISTRY mapping action names to handler instances."""

from app.actions.base import ActionHandler
from app.actions.calendar_digest_ics import CalendarDigestICSHandler
from app.actions.echo import EchoHandler
from app.actions.http_call import HttpCallHandler
from app.actions.slack_post import SlackPostHandler

ACTION_REGISTRY: dict[str, ActionHandler] = {
    EchoHandler.name: EchoHandler(),
    HttpCallHandler.name: HttpCallHandler(),
    CalendarDigestICSHandler.name: CalendarDigestICSHandler(),
    SlackPostHandler.name: SlackPostHandler(),
}
