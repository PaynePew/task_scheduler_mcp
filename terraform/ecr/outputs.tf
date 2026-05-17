output "repository_url" {
  description = "URL of the ECR repository (used for docker push/pull)."
  value       = aws_ecr_repository.main.repository_url
}

output "repository_arn" {
  description = "ARN of the ECR repository."
  value       = aws_ecr_repository.main.arn
}

output "repository_name" {
  description = "Name of the ECR repository."
  value       = aws_ecr_repository.main.name
}

output "registry_id" {
  description = "Registry ID (AWS account ID) of the ECR repository."
  value       = aws_ecr_repository.main.registry_id
}
