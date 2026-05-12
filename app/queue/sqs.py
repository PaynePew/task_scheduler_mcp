"""SQS client wrapper per ADR-008 / ADR-010.

Thin boto3 shim used by the Watcher (publish) and Worker (consume / heartbeat).
Pointed at ElasticMQ locally via the configured queue URL; real SQS in production.
"""

from __future__ import annotations

from urllib.parse import urlparse

import boto3
from botocore.config import Config

from app.config.settings import settings as _settings


def _endpoint_from_queue_url(queue_url: str) -> str:
    """Extract scheme + netloc from a queue URL, e.g. http://elasticmq:9324/queue/… → http://elasticmq:9324."""
    p = urlparse(queue_url)
    return f"{p.scheme}://{p.netloc}"


class SQSClient:
    """Thin wrapper around boto3 SQS that hides queue-URL and credential plumbing."""

    def __init__(
        self,
        queue_url: str | None = None,
        *,
        endpoint_url: str | None = None,
    ) -> None:
        self._queue_url = queue_url or _settings.queue_url
        endpoint = endpoint_url or _endpoint_from_queue_url(self._queue_url)
        # Credentials are only meaningful for ElasticMQ; production uses the ECS task role.
        self._client = boto3.client(
            "sqs",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id="local",
            aws_secret_access_key="local",
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )

    def send_message_batch(self, entries: list[dict]) -> dict:
        """Send up to 10 messages in one batch.

        Each entry: {Id: str, MessageBody: str, DelaySeconds?: int}
        Returns the boto3 response dict ({Successful, Failed}).
        """
        return self._client.send_message_batch(
            QueueUrl=self._queue_url,
            Entries=entries,
        )

    def receive_messages(
        self,
        *,
        max_messages: int = 1,
        wait_seconds: int = 0,
        visibility_timeout: int = 60,
    ) -> list[dict]:
        """Poll for messages. Returns a (possibly empty) list of SQS message dicts."""
        resp = self._client.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
            VisibilityTimeout=visibility_timeout,
        )
        return resp.get("Messages", [])

    def delete_message(self, receipt_handle: str) -> None:
        """Acknowledge a message after successful processing."""
        self._client.delete_message(
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
        )

    def change_message_visibility(self, receipt_handle: str, *, visibility_timeout: int) -> None:
        """Extend the visibility timeout while a long action runs (Worker heartbeat)."""
        self._client.change_message_visibility(
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=visibility_timeout,
        )
