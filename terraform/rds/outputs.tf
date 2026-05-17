output "db_endpoint" {
  description = "RDS instance endpoint in host:port format."
  value       = aws_db_instance.main.endpoint
}

output "db_url_secret_arn" {
  description = "ARN of the Secrets Manager secret containing the full Postgres connection URL."
  value       = aws_secretsmanager_secret.db_url.arn
}

output "db_sg_id" {
  description = "Security group ID of the RDS instance (ecs module references this to build egress rules)."
  value       = aws_security_group.rds.id
}
