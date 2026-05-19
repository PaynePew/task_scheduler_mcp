# terraform/rds — Postgres RDS module

Stateful data layer for the Fargate design artifact (ADR-027). Creates the RDS Postgres instance in private subnets with a custom parameter group and stores credentials in AWS Secrets Manager.

## Resources created

| Resource | Purpose |
|---|---|
| `aws_security_group.rds` | Allows 5432 ingress from ECS tasks SG only (ADR-025 layering) |
| `aws_db_subnet_group.main` | Covers both private subnets (2 AZs) |
| `aws_db_parameter_group.postgres16` | `max_connections=170`, `log_statement=mod` |
| `random_password.db_master` | 32-char random password (special chars restricted to avoid shell-escape issues in URLs) |
| `aws_secretsmanager_secret.db_password` | Stores `{username, password}` JSON |
| `aws_secretsmanager_secret.db_url` | Stores full `postgresql+asyncpg://` URL — injected into ECS tasks via `secrets:` |
| `aws_db_instance.main` | `db.t4g.micro`, `gp3 20 GB`, encrypted, 7-day backup, port 5432 |

## Secrets Manager design decisions

**DB_URL is stored as a secret** (`${project}/rds/db-url`). The ECS task definition in slice #7c references this via the `secrets:` field so the password never appears in CloudWatch logs or the task definition JSON.

**MCP_USER_ID and MCP_USER_TZ are NOT stored as secrets.** These values identify the user's timezone preference (`Asia/Taipei`) and their MCP identity string — neither carries credential or PII value that would warrant Secrets Manager overhead ($0.40/secret/month). They are passed as plain `environment:` entries in the ECS task definition. This keeps the Secrets Manager namespace minimal (one secret per actual secret, not per env var).

## Variables

| Variable | Default | Description |
|---|---|---|
| `vpc_id` | required | VPC ID from `vpc` module |
| `private_subnet_ids` | required | Private subnet IDs from `vpc` module (2 AZs) |
| `ecs_tasks_sg_id` | required | ECS tasks SG ID (from `ecs` module in slice #7c) |
| `rds_multi_az` | `false` | Multi-AZ toggle — flip to `true` in W4+ for HA |
| `instance_class` | `db.t4g.micro` | RDS instance class |
| `db_name` | `task_scheduler_mcp` | Initial database name |
| `db_username` | `task_scheduler_mcp` | Master DB username |

## Outputs

| Output | Description |
|---|---|
| `db_endpoint` | `host:port` endpoint — consumed by `ecs` module for health checks |
| `db_url_secret_arn` | ARN of the DB URL secret — referenced in ECS task `secrets:` |
| `db_sg_id` | RDS SG ID — consumed by `ecs` module to add egress rules |

## Multi-AZ posture

`var.rds_multi_az` (default `false`) is the only change needed to enable Multi-AZ. The subnet group already covers two AZs, so promoting to Multi-AZ is a one-line variable change.

## Apply order

```
bootstrap → vpc → iam → ecr → cw_logs → rds → sqs → ecs → alb
```

The `rds` module depends on outputs from `vpc` (subnet IDs + VPC ID) and a security group ID from `ecs`. Because `ecs` hasn't been applied yet at the time `rds` is first applied, use the placeholder SG approach: apply `rds` with a temporary SG ID from the console, then update after `ecs` is applied.
