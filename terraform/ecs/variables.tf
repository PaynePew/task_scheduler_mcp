variable "aws_region" {
  description = "AWS region for ECS resources and CloudWatch log configuration."
  type        = string
  default     = "ap-northeast-1"
}

variable "project" {
  description = "Project name prefix used for resource naming and tagging."
  type        = string
  default     = "chatgpt-task"
}

variable "vpc_id" {
  description = "VPC ID (from vpc module output)."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs for ECS task placement (assignPublicIp required; no NAT per ADR-025)."
  type        = list(string)
}

variable "alb_sg_id" {
  description = "ALB security group ID (from alb module output); grants 8080 ingress to ECS tasks."
  type        = string
}

variable "rds_sg_id" {
  description = "RDS security group ID (from rds module output); ECS tasks are allowed 5432 egress to it."
  type        = string
}

variable "ecs_task_execution_role_arn" {
  description = "ECS task execution role ARN (from iam module output); used by the ECS agent."
  type        = string
}

variable "ecs_task_role_arn" {
  description = "ECS task role ARN (from iam module output); assumed by application code at runtime."
  type        = string
}

variable "ecr_repository_url" {
  description = "ECR repository URL (from ecr module output); image is pulled as <url>:<image_tag>."
  type        = string
}

variable "image_tag" {
  description = "Docker image tag to deploy (git SHA or 'latest')."
  type        = string
  default     = "latest"
}

variable "db_url_secret_arn" {
  description = "Secrets Manager ARN of the DATABASE_URL secret (from rds module output)."
  type        = string
}

variable "alembic_db_url_secret_arn" {
  description = "Secrets Manager ARN of the ALEMBIC_DATABASE_URL secret. Defaults to db_url_secret_arn if not provided."
  type        = string
  default     = ""
}

variable "queue_url" {
  description = "SQS main queue URL passed as a plain environment variable (not sensitive)."
  type        = string
}

variable "queue_dlq_url" {
  description = "SQS DLQ URL passed as a plain environment variable (not sensitive)."
  type        = string
}

variable "sqs_queue_name" {
  description = "SQS main queue name; used as the AutoScaling metric dimension for worker scaling."
  type        = string
}

variable "mcp_server_target_group_arn" {
  description = "ALB target group ARN for mcp-server (from alb module output); attached to the mcp-server ECS service."
  type        = string
}
