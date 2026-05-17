output "log_group_names" {
  description = "Map of service name to CloudWatch Log Group name."
  value       = { for k, v in aws_cloudwatch_log_group.ecs : k => v.name }
}

output "log_group_arns" {
  description = "Map of service name to CloudWatch Log Group ARN."
  value       = { for k, v in aws_cloudwatch_log_group.ecs : k => v.arn }
}
