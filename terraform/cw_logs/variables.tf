variable "aws_region" {
  description = "AWS region for the CloudWatch Log Groups."
  type        = string
  default     = "ap-northeast-1"
}

variable "project" {
  description = "Project name prefix used for resource naming and tagging."
  type        = string
  default     = "task-scheduler-mcp"
}

variable "retention_in_days" {
  description = "Number of days to retain log events in each log group."
  type        = number
  default     = 30
}

variable "services" {
  description = "List of ECS service names to create log groups for."
  type        = list(string)
  default = [
    "mcp-server",
    "watcher",
    "worker",
    "recurring-watcher",
    "migrate",
  ]
}
