output "main_queue_url" {
  description = "URL of the main SQS queue."
  value       = aws_sqs_queue.main.id
}

output "main_queue_arn" {
  description = "ARN of the main SQS queue."
  value       = aws_sqs_queue.main.arn
}

output "dlq_queue_url" {
  description = "URL of the dead-letter queue."
  value       = aws_sqs_queue.dlq.id
}

output "dlq_queue_arn" {
  description = "ARN of the dead-letter queue."
  value       = aws_sqs_queue.dlq.arn
}

output "task_iam_policy_json" {
  description = "IAM policy JSON granting ECS tasks SendMessage/ReceiveMessage/DeleteMessage/ChangeMessageVisibility on the main queue and SendMessage on the DLQ. Attach to the ECS task role in slice #7c."
  value       = data.aws_iam_policy_document.ecs_task_sqs.json
}
