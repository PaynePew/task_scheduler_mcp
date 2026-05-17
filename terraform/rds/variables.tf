variable "aws_region" {
  description = "AWS region to deploy into."
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

variable "private_subnet_ids" {
  description = "Private subnet IDs from the vpc module — must cover 2 AZs for the DB subnet group."
  type        = list(string)
}

variable "ecs_tasks_sg_id" {
  description = "Security group ID of the ECS tasks; granted Postgres ingress to the RDS instance (ADR-025 sg layering)."
  type        = string
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "chatgpt_task"
}

variable "db_username" {
  description = "Master DB username."
  type        = string
  default     = "chatgpt_task"
}

variable "rds_multi_az" {
  description = "Enable RDS Multi-AZ standby. Off in W3; flip to true in W4+ for HA with no other changes required."
  type        = bool
  default     = false
}

variable "instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}
