variable "aws_region" {
  description = "AWS region for the state backend resources."
  type        = string
  default     = "ap-northeast-1"
}

variable "project" {
  description = "Project name prefix used for resource naming."
  type        = string
  default     = "task-scheduler-mcp"
}

variable "state_bucket_name" {
  description = "Name for the S3 bucket that stores Terraform state."
  type        = string
}

variable "lock_table_name" {
  description = "Name for the DynamoDB table used for state locking."
  type        = string
  default     = "task-scheduler-mcp-terraform-locks"
}
