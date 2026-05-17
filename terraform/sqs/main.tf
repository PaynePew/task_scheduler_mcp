# DLQ created first so its ARN is available for the redrive policy on the main queue.
resource "aws_sqs_queue" "dlq" {
  name                      = "${var.project}-dlq"
  message_retention_seconds = 1209600 # 14 days — SQS maximum

  tags = {
    Name    = "${var.project}-dlq"
    Project = var.project
  }
}

resource "aws_sqs_queue" "main" {
  name                       = "${var.project}-main"
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = 1209600 # 14 days

  tags = {
    Name    = "${var.project}-main"
    Project = var.project
  }
}

# Redrive policy — routes messages to DLQ after maxReceiveCount failures (ADR-008: 3).
resource "aws_sqs_queue_redrive_policy" "main" {
  queue_url = aws_sqs_queue.main.id
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

# IAM policy document for the ECS task role (consumed by the ecs module in slice #7c).
# Watcher: SendMessage on main queue.
# Worker: ReceiveMessage + DeleteMessage + ChangeMessageVisibility on main queue.
# Both: GetQueueAttributes + GetQueueUrl for queue introspection.
# DLQ: SendMessage for manual dead-lettering from ops tooling.
data "aws_iam_policy_document" "ecs_task_sqs" {
  statement {
    sid    = "MainQueueAccess"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [aws_sqs_queue.main.arn]
  }

  statement {
    sid    = "DLQSendAccess"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueUrl",
    ]
    resources = [aws_sqs_queue.dlq.arn]
  }
}
