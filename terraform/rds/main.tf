# RDS security group — allows Postgres ingress from ECS tasks only (ADR-025 sg layering).
resource "aws_security_group" "rds" {
  name        = "${var.project}-rds-sg"
  description = "Postgres ingress from ECS tasks only."
  vpc_id      = var.vpc_id

  ingress {
    description     = "Postgres from ECS tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.ecs_tasks_sg_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.project}-rds-sg"
    Project = var.project
  }
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db-subnet-group"
  subnet_ids = var.private_subnet_ids

  tags = {
    Name    = "${var.project}-db-subnet-group"
    Project = var.project
  }
}

resource "aws_db_parameter_group" "postgres16" {
  name        = "${var.project}-pg16"
  family      = "postgres16"
  description = "Postgres 16: max_connections=170 (W3 PRD reconciliation), log_statement=mod (audit visibility)."

  parameter {
    name  = "max_connections"
    value = "170"
  }

  parameter {
    name  = "log_statement"
    value = "mod"
  }

  tags = {
    Name    = "${var.project}-pg16"
    Project = var.project
  }
}

# Master password — random, stored in Secrets Manager; never appears in Terraform state plaintext.
resource "random_password" "db_master" {
  length           = 32
  special          = true
  override_special = "!#$%&*-_=+?"
}

resource "aws_secretsmanager_secret" "db_password" {
  name                    = "${var.project}/rds/master-password"
  description             = "RDS master password for ${var.project}."
  recovery_window_in_days = 7

  tags = {
    Name    = "${var.project}-rds-master-password"
    Project = var.project
  }
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id = aws_secretsmanager_secret.db_password.id
  secret_string = jsonencode({
    username = var.db_username
    password = random_password.db_master.result
  })
}

# Full DB URL secret — referenced by ECS task definitions via secrets: injection.
# MCP_USER_ID and MCP_USER_TZ are passed as plain environment variables (not secrets):
# they carry no credential value, only user-preference data, so Secrets Manager
# overhead is unwarranted. See terraform/rds/README.md for the full decision record.
resource "aws_secretsmanager_secret" "db_url" {
  name                    = "${var.project}/rds/db-url"
  description             = "Postgres connection URL for ${var.project} ECS tasks (injected via secrets: in task definitions)."
  recovery_window_in_days = 7

  tags = {
    Name    = "${var.project}-rds-db-url"
    Project = var.project
  }
}

resource "aws_secretsmanager_secret_version" "db_url" {
  secret_id     = aws_secretsmanager_secret.db_url.id
  secret_string = "postgresql+asyncpg://${var.db_username}:${random_password.db_master.result}@${aws_db_instance.main.endpoint}/${var.db_name}"
}

resource "aws_db_instance" "main" {
  identifier        = "${var.project}-postgres"
  engine            = "postgres"
  engine_version    = "16"
  instance_class    = var.instance_class
  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db_master.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  parameter_group_name   = aws_db_parameter_group.postgres16.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  publicly_accessible = false
  multi_az            = var.rds_multi_az

  backup_retention_period   = 7
  backup_window             = "16:00-16:30"
  maintenance_window        = "Mon:17:00-Mon:17:30"
  auto_minor_version_upgrade = true

  # deletion_protection off: this module is applied/destroyed during W4 demo validation.
  # Flip to true for any persistent environment.
  deletion_protection = false
  skip_final_snapshot = false

  final_snapshot_identifier = "${var.project}-postgres-final-snapshot"

  tags = {
    Name    = "${var.project}-postgres"
    Project = var.project
  }
}
