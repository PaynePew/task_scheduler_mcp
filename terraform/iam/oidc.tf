# GitHub Actions OIDC provider — lets the validate-fargate.yml workflow assume
# an AWS role without storing long-lived credentials as GitHub secrets.
# The thumbprint list references GitHub's OIDC CA; AWS validates OIDC tokens
# directly via the provider URL, so the thumbprint is a secondary check only.
resource "aws_iam_openid_connect_provider" "github_actions" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = {
    Project = var.project
  }
}

data "aws_iam_policy_document" "github_actions_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Restrict to this repo only — no other repo can assume this role.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:PaynePew/task_scheduler_mcp:*"]
    }
  }
}

# Role assumed by validate-fargate.yml.  max_session_duration = 14400 s (4 h)
# covers a 120-min stack-live window plus apply/destroy overhead (~60 min each).
resource "aws_iam_role" "github_actions_fargate_validation" {
  name                 = "${var.project}-github-actions-fargate-validation"
  assume_role_policy   = data.aws_iam_policy_document.github_actions_assume_role.json
  max_session_duration = 14400

  tags = {
    Project = var.project
  }
}

# Permissions required to apply and destroy the full Fargate stack.
data "aws_iam_policy_document" "fargate_validation_permissions" {
  # S3 + DynamoDB for Terraform remote state
  statement {
    sid       = "TerraformState"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = ["*"]
  }

  statement {
    sid       = "TerraformLock"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:DescribeTable"]
    resources = ["*"]
  }

  # VPC, subnets, security groups, route tables, IGW, endpoints, ALB
  statement {
    sid       = "EC2Full"
    effect    = "Allow"
    actions   = ["ec2:*"]
    resources = ["*"]
  }

  statement {
    sid       = "ELBFull"
    effect    = "Allow"
    actions   = ["elasticloadbalancing:*"]
    resources = ["*"]
  }

  # ECS cluster, services, task definitions
  statement {
    sid       = "ECSFull"
    effect    = "Allow"
    actions   = ["ecs:*"]
    resources = ["*"]
  }

  # RDS instances, subnet groups, parameter groups
  statement {
    sid       = "RDSFull"
    effect    = "Allow"
    actions   = ["rds:*"]
    resources = ["*"]
  }

  # SQS queues
  statement {
    sid       = "SQSFull"
    effect    = "Allow"
    actions   = ["sqs:*"]
    resources = ["*"]
  }

  # ECR — pull task images, manage lifecycle policies
  statement {
    sid       = "ECRFull"
    effect    = "Allow"
    actions   = ["ecr:*"]
    resources = ["*"]
  }

  # IAM — create/delete execution + task roles, pass them to ECS, manage OIDC provider
  statement {
    sid    = "IAMScoped"
    effect = "Allow"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole",
      "iam:AttachRolePolicy", "iam:DetachRolePolicy",
      "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
      "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
      "iam:PassRole",
      "iam:CreatePolicy", "iam:DeletePolicy", "iam:GetPolicy",
      "iam:CreatePolicyVersion", "iam:DeletePolicyVersion",
      "iam:GetPolicyVersion", "iam:ListPolicyVersions", "iam:SetDefaultPolicyVersion",
      "iam:CreateOpenIDConnectProvider", "iam:DeleteOpenIDConnectProvider",
      "iam:GetOpenIDConnectProvider", "iam:TagOpenIDConnectProvider",
      "iam:ListOpenIDConnectProviderTags",
    ]
    resources = ["*"]
  }

  # Secrets Manager — RDS credentials secret
  statement {
    sid       = "SecretsManager"
    effect    = "Allow"
    actions   = ["secretsmanager:*"]
    resources = ["*"]
  }

  # CloudWatch Logs — ECS task log groups
  statement {
    sid       = "CWLogs"
    effect    = "Allow"
    actions   = ["logs:*"]
    resources = ["*"]
  }

  # CloudWatch metrics + ACM + Budgets (cost guard)
  statement {
    sid    = "Monitoring"
    effect = "Allow"
    actions = [
      "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData",
      "cloudwatch:ListMetrics", "cloudwatch:DescribeAlarms",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ACM"
    effect    = "Allow"
    actions   = ["acm:*"]
    resources = ["*"]
  }

  statement {
    sid       = "Budgets"
    effect    = "Allow"
    actions   = ["budgets:*"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "fargate_validation" {
  name   = "fargate-validation-permissions"
  role   = aws_iam_role.github_actions_fargate_validation.id
  policy = data.aws_iam_policy_document.fargate_validation_permissions.json
}
