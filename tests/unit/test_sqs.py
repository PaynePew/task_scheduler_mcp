"""Unit tests for app/queue/sqs.py — mocks boto3, no network required."""

from unittest.mock import MagicMock, patch

import pytest

from app.queue.sqs import SQSClient, _endpoint_from_queue_url


@pytest.mark.parametrize(
    "queue_url,expected",
    [
        ("http://elasticmq:9324/queue/task-queue", "http://elasticmq:9324"),
        (
            "https://sqs.us-east-1.amazonaws.com/123456789/task-queue",
            "https://sqs.us-east-1.amazonaws.com",
        ),
        ("http://localhost:9324/queue/test", "http://localhost:9324"),
    ],
)
def test_endpoint_from_queue_url(queue_url, expected):
    assert _endpoint_from_queue_url(queue_url) == expected


@pytest.fixture
def mock_boto3_client():
    """Patch boto3.client so SQSClient.__init__ doesn't hit the network."""
    with patch("app.queue.sqs.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        yield mock_client


def test_send_message_batch_delegates_to_boto3(mock_boto3_client):
    sqs = SQSClient("http://localhost:9324/queue/test")
    entries = [{"Id": "1", "MessageBody": "hello", "DelaySeconds": 0}]
    sqs.send_message_batch(entries)
    mock_boto3_client.send_message_batch.assert_called_once_with(
        QueueUrl="http://localhost:9324/queue/test",
        Entries=entries,
    )


def test_receive_messages_returns_list(mock_boto3_client):
    mock_boto3_client.receive_message.return_value = {
        "Messages": [{"MessageId": "m1", "Body": "{}"}]
    }
    sqs = SQSClient("http://localhost:9324/queue/test")
    msgs = sqs.receive_messages(max_messages=5)
    assert msgs == [{"MessageId": "m1", "Body": "{}"}]
    mock_boto3_client.receive_message.assert_called_once_with(
        QueueUrl="http://localhost:9324/queue/test",
        MaxNumberOfMessages=5,
        WaitTimeSeconds=0,
        VisibilityTimeout=60,
        AttributeNames=["ApproximateReceiveCount"],
    )


def test_receive_messages_empty_when_no_messages(mock_boto3_client):
    mock_boto3_client.receive_message.return_value = {}
    sqs = SQSClient("http://localhost:9324/queue/test")
    assert sqs.receive_messages() == []


def test_delete_message_delegates(mock_boto3_client):
    sqs = SQSClient("http://localhost:9324/queue/test")
    sqs.delete_message("rh-xyz")
    mock_boto3_client.delete_message.assert_called_once_with(
        QueueUrl="http://localhost:9324/queue/test",
        ReceiptHandle="rh-xyz",
    )


def test_change_message_visibility_delegates(mock_boto3_client):
    sqs = SQSClient("http://localhost:9324/queue/test")
    sqs.change_message_visibility("rh-abc", visibility_timeout=120)
    mock_boto3_client.change_message_visibility.assert_called_once_with(
        QueueUrl="http://localhost:9324/queue/test",
        ReceiptHandle="rh-abc",
        VisibilityTimeout=120,
    )
