variable "aws_region" {
  description = "AWS region for the ECR repository."
  type        = string
  default     = "ap-northeast-1"
}

variable "project" {
  description = "Project name prefix used for resource naming and tagging."
  type        = string
  default     = "task-scheduler-mcp"
}

variable "repository_name" {
  description = "Name of the ECR repository."
  type        = string
  default     = "task-scheduler-mcp"
}

variable "untagged_expiry_days" {
  description = "Days after which untagged images are expired by the lifecycle policy."
  type        = number
  default     = 7
}
