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

variable "visibility_timeout_seconds" {
  description = "SQS visibility timeout in seconds. Workers extend via ChangeMessageVisibility every 30 s (ADR-008)."
  type        = number
  default     = 60
}

variable "max_receive_count" {
  description = "Number of receive attempts before SQS routes a message to the DLQ. ADR-008 specifies 3."
  type        = number
  default     = 3
}
