variable "aws_region" {
  description = "AWS region for the ALB and related resources."
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
  description = "IDs of the public subnets for the ALB (must span >= 2 AZs)."
  type        = list(string)
}

variable "domain_name" {
  description = "Apex domain name for the ACM certificate (e.g. scheduler.paynepew.dev)."
  type        = string
}
