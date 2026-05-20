"""Module-level ACTION_REGISTRY mapping action names to handler instances.

r2_upload was removed in issue #132 (ADR-051): it has no public use case and
is replaced by a VPS cron/shell script (bin/r2_backup.sh).
"""

from app.actions.base import ActionHandler
from app.actions.calendar_digest_ics import CalendarDigestICSHandler
from app.actions.echo import EchoHandler
from app.actions.email_send import EmailSendHandler
from app.actions.github_digest import GitHubDigestHandler
from app.actions.http_call import HttpCallHandler
from app.actions.slack_post import SlackPostHandler

ACTION_REGISTRY: dict[str, ActionHandler] = {
    EchoHandler.name: EchoHandler(),
    HttpCallHandler.name: HttpCallHandler(),
    CalendarDigestICSHandler.name: CalendarDigestICSHandler(),
    SlackPostHandler.name: SlackPostHandler(),
    GitHubDigestHandler.name: GitHubDigestHandler(),
    EmailSendHandler.name: EmailSendHandler(),
}
