output "ecs_task_execution_role_arn" {
  description = "ARN of the ECS task execution role."
  value       = aws_iam_role.ecs_task_execution.arn
}

output "ecs_task_execution_role_name" {
  description = "Name of the ECS task execution role."
  value       = aws_iam_role.ecs_task_execution.name
}

output "ecs_task_role_arn" {
  description = "ARN of the ECS task role (app-level permissions)."
  value       = aws_iam_role.ecs_task.arn
}

output "ecs_task_role_name" {
  description = "Name of the ECS task role (app-level permissions)."
  value       = aws_iam_role.ecs_task.name
}

output "budget_warn_name" {
  description = "Name of the monthly warning budget alert."
  value       = aws_budgets_budget.monthly_warn.name
}

output "budget_cap_name" {
  description = "Name of the monthly cap budget alert."
  value       = aws_budgets_budget.monthly_cap.name
}

output "github_actions_role_arn" {
  description = "ARN of the IAM role assumed by validate-fargate.yml via OIDC."
  value       = aws_iam_role.github_actions_fargate_validation.arn
}

output "github_actions_role_name" {
  description = "Name of the IAM role assumed by validate-fargate.yml via OIDC."
  value       = aws_iam_role.github_actions_fargate_validation.name
}

output "github_oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC identity provider."
  value       = aws_iam_openid_connect_provider.github_actions.arn
}
