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
├── cw_logs/     # CloudWatch Log Groups per ECS service
└── cloudflare/  # Cloudflare DNS — A record scheduler.paynepew.dev → Lightsail IP
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
  -var="state_bucket_name=task-scheduler-mcp-terraform-state-<ACCOUNT_ID>"

# Apply — creates S3 bucket + DynamoDB lock table
terraform apply \
  -var="state_bucket_name=task-scheduler-mcp-terraform-state-<ACCOUNT_ID>"
```

Note the `state_bucket_name` output; use it as the `bucket` value in the
backend blocks of all other modules.

## Applying the app stack

After bootstrap, apply in order. Each module needs a `backend.tfvars` that
points at the bootstrap-created bucket. Example `backend.tfvars`:

```hcl
bucket         = "task-scheduler-mcp-terraform-state-<ACCOUNT_ID>"
key            = "<module>/terraform.tfstate"
region         = "ap-northeast-1"
dynamodb_table = "task-scheduler-mcp-terraform-locks"
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

## Cloudflare DNS module

The `cloudflare/` module is independent of AWS — it uses its own Cloudflare provider and
keeps **local state** (no S3 backend needed; the record is trivially recreatable from the
variables, and the state file is just a thin pointer to the Cloudflare record ID).

Prerequisites: `paynepew.dev` registered via Cloudflare Registrar, plus a Cloudflare API
token with `Zone.DNS:Edit` scope on `paynepew.dev` only.

```bash
cd terraform/cloudflare

# Initialise with local state
terraform init

# Set the token via TF_VAR_ env var (sensitive, do NOT commit to terraform.tfvars)
export TF_VAR_cloudflare_api_token="<token>"
# PowerShell: $env:TF_VAR_cloudflare_api_token = "<token>"

# Copy the example tfvars and edit vps_ip
cp terraform.tfvars.example terraform.tfvars
# edit: zone_name = "paynepew.dev", vps_ip = "<LIGHTSAIL_STATIC_IP>"

# Review the plan — proxied=false (ADR-028) so Caddy can complete the ACME challenge
terraform plan

# Apply — creates scheduler.paynepew.dev A record
terraform apply
```

Verify propagation (globally, within ~1-5 minutes):

```bash
dig +short scheduler.paynepew.dev @1.1.1.1
dig +short scheduler.paynepew.dev @8.8.8.8
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
