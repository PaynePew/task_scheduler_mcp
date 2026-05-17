data "aws_iam_policy_document" "ecs_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ECS task execution role — used by the ECS agent to pull images from ECR
# and push logs to CloudWatch Logs. Attach AWS-managed policy.
resource "aws_iam_role" "ecs_task_execution" {
  name               = "${var.project}-ecs-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json

  tags = {
    Project = var.project
  }
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ECS task role — assumed by the application code at runtime.
# App-specific permissions (SQS, Secrets Manager) are added in slice #7c.
resource "aws_iam_role" "ecs_task" {
  name               = "${var.project}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json

  tags = {
    Project = var.project
  }
}

# AWS Budgets — cost safety net for W3 development.
# $10 warn / $30 cap, email-only notifications.
resource "aws_budgets_budget" "monthly_warn" {
  name         = "${var.project}-monthly-warn"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_warn_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}

resource "aws_budgets_budget" "monthly_cap" {
  name         = "${var.project}-monthly-cap"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_cap_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
