output "cluster_name" {
  description = "ECS cluster name (consumed by validate-fargate.yml smoke test and #8)."
  value       = aws_ecs_cluster.main.name
}

output "cluster_arn" {
  description = "ECS cluster ARN."
  value       = aws_ecs_cluster.main.arn
}

output "service_names" {
  description = "Map of service key to ECS service name (consumed by #8 validate-fargate.yml)."
  value       = { for k, v in aws_ecs_service.service : k => v.name }
}

output "service_arns" {
  description = "Map of service key to ECS service ARN."
  value       = { for k, v in aws_ecs_service.service : k => v.id }
}

output "ecs_tasks_sg_id" {
  description = "ECS tasks security group ID (referenced by rds module and other consumers)."
  value       = aws_security_group.ecs_tasks.id
}

output "migrate_task_definition_arn" {
  description = "ARN of the one-shot migrate task definition."
  value       = aws_ecs_task_definition.migrate.arn
}
