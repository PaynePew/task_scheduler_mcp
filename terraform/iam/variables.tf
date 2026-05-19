variable "aws_region" {
  description = "AWS region (used for provider configuration)."
  type        = string
  default     = "ap-northeast-1"
}

variable "project" {
  description = "Project name prefix used for resource naming and tagging."
  type        = string
  default     = "task-scheduler-mcp"
}

variable "budget_alert_email" {
  description = "Email address to receive AWS Budgets cost alerts."
  type        = string
}

variable "budget_warn_limit_usd" {
  description = "Monthly spend threshold (USD) that triggers a WARNING notification."
  type        = number
  default     = 10
}

variable "budget_cap_limit_usd" {
  description = "Monthly spend threshold (USD) that triggers a CRITICAL notification."
  type        = number
  default     = 30
}
