# Terraform — ChatGPT Task Scheduler (Fargate path)

This directory contains the Terraform IaC for the Fargate deployment path.
Per ADR-027, this path is a **design artifact**: code is validated in CI
(`terraform validate` + `tflint`) but **not applied** until the W4 demo
recording window (via `validate-fargate.yml` from slice #8).

## Module layout

```
terraform/
├── bootstrap/   # One-time: S3 bucket + DynamoDB table for remote state
├── vpc/         # VPC, subnets, IGW, route tables, S3 Gateway endpoint
├── iam/         # ECS execution role, task role, AWS Budgets alerts
├── ecr/         # ECR repository + lifecycle policy
└── cw_logs/     # CloudWatch Log Groups per ECS service
```

## Apply order

```
bootstrap  →  vpc  →  iam  →  ecr  →  cw_logs
```

`bootstrap` uses **local state** (chicken-and-egg: it creates the bucket that
the other modules use for remote state). All other modules use the S3 + DynamoDB
backend created by `bootstrap`.

## One-time bootstrap procedure

Run this once per AWS account / region before applying any other module:

```bash
cd terraform/bootstrap

# Initialise with local state
terraform init

# Review the plan
terraform plan \
  -var="state_bucket_name=chatgpt-task-terraform-state-<ACCOUNT_ID>" \
  -var="budget_alert_email=<your@email.com>"   # not required for bootstrap

# Apply — creates S3 bucket + DynamoDB lock table
terraform apply \
  -var="state_bucket_name=chatgpt-task-terraform-state-<ACCOUNT_ID>"
```

Note the `state_bucket_name` output; use it as the `bucket` value in the
backend blocks of all other modules.

## Applying the app stack

After bootstrap, apply in order. Each module needs a `backend.tfvars` that
points at the bootstrap-created bucket. Example `backend.tfvars`:

```hcl
bucket         = "chatgpt-task-terraform-state-<ACCOUNT_ID>"
key            = "<module>/terraform.tfstate"
region         = "ap-northeast-1"
dynamodb_table = "chatgpt-task-terraform-locks"
encrypt        = true
```

```bash
# vpc
cd terraform/vpc
terraform init -backend-config=../backend.tfvars -backend-config="key=vpc/terraform.tfstate"
terraform apply -var="aws_region=ap-northeast-1"

# iam
cd terraform/iam
terraform init -backend-config=../backend.tfvars -backend-config="key=iam/terraform.tfstate"
terraform apply -var="budget_alert_email=<your@email.com>"

# ecr
cd terraform/ecr
terraform init -backend-config=../backend.tfvars -backend-config="key=ecr/terraform.tfstate"
terraform apply

# cw_logs
cd terraform/cw_logs
terraform init -backend-config=../backend.tfvars -backend-config="key=cw_logs/terraform.tfstate"
terraform apply
```

## CI validation

`.github/workflows/terraform-ci.yml` runs on every PR touching `terraform/**`.
It executes (per module): `terraform fmt -check -recursive`, `terraform init -backend=false`,
`terraform validate`, and `tflint`. No `terraform plan` (that requires AWS
credentials and is reserved for `validate-fargate.yml` in slice #8).

## Network topology

See ADR-025 for the full rationale. Summary:

- **VPC**: `10.0.0.0/16`, 2× public subnets + 2× private subnets across
  `ap-northeast-1a` and `ap-northeast-1c`.
- **ECS tasks**: public subnets, `assignPublicIp = ENABLED`, no NAT Gateway.
- **RDS**: private subnets only.
- **S3 Gateway Endpoint**: attached to private route table (free; saves IGW
  traffic for the blob portion of ECR pulls).

## Cost safety net

The `iam` module provisions two AWS Budgets alerts:

| Budget | Threshold | Notification |
|--------|-----------|--------------|
| `monthly-warn` | $10 actual | EMAIL |
| `monthly-cap`  | $30 actual | EMAIL |

These are W3 development guard-rails. Adjust thresholds via `tfvars` as the
project scales.
