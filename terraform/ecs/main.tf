locals {
  # coalesce() returns the first non-null, non-empty argument, so an empty
  # alembic_db_url_secret_arn falls back to db_url_secret_arn.
  alembic_secret_arn = coalesce(var.alembic_db_url_secret_arn, var.db_url_secret_arn)

  # Long-running service definitions (ADR-026).
  services = {
    mcp-server = {
      command       = ["python", "-m", "app.entrypoints.mcp_http"]
      desired_count = 2
    }
    watcher = {
      command       = ["python", "-m", "app.entrypoints.watcher"]
      desired_count = 2
    }
    worker = {
      command       = ["python", "-m", "app.entrypoints.worker"]
      desired_count = 1
    }
    recurring-watcher = {
      command       = ["python", "-m", "app.entrypoints.recurring_watcher"]
      desired_count = 1
    }
  }

  # Common environment variables (non-sensitive) passed to every container.
  common_environment = [
    { name = "QUEUE_URL", value = var.queue_url },
    { name = "QUEUE_DLQ_URL", value = var.queue_dlq_url },
  ]

  # Common secrets (sensitive) injected from Secrets Manager into every container.
  common_secrets = [
    { name = "DATABASE_URL", valueFrom = var.db_url_secret_arn },
  ]
}

# ECS Tasks Security Group — ADR-025 SG layering (alb-sg → ecs-tasks-sg → rds-sg).
resource "aws_security_group" "ecs_tasks" {
  name        = "${var.project}-ecs-tasks-sg"
  description = "ECS tasks: inbound 8080 from ALB SG only; outbound to RDS 5432 + HTTPS."
  vpc_id      = var.vpc_id

  ingress {
    description     = "HTTP from ALB only (no 0.0.0.0/0)"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [var.alb_sg_id]
  }

  egress {
    description     = "Postgres to RDS security group"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.rds_sg_id]
  }

  egress {
    description = "HTTPS to AWS public endpoints (SQS, Secrets Manager, ECR API)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.project}-ecs-tasks-sg"
    Project = var.project
  }
}

# ECS Cluster — Container Insights off (ADR-024 Tier 3 cost cut).
resource "aws_ecs_cluster" "main" {
  name = var.project

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = {
    Name    = var.project
    Project = var.project
  }
}

# Task definitions for the 5 long-running services.
resource "aws_ecs_task_definition" "service" {
  for_each = local.services

  family                   = "${var.project}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = var.ecs_task_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name        = each.key
      image       = "${var.ecr_repository_url}:${var.image_tag}"
      command     = each.value.command
      environment = local.common_environment
      secrets     = local.common_secrets
      portMappings = each.key == "mcp-server" ? [
        { containerPort = 8080, protocol = "tcp" }
      ] : []
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/${var.project}/${each.key}"
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  tags = {
    Project = var.project
    Service = each.key
  }
}

# migrate task definition — one-shot Alembic run before each deploy.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.project}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = var.ecs_task_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name    = "migrate"
      image   = "${var.ecr_repository_url}:${var.image_tag}"
      command = ["alembic", "upgrade", "head"]
      secrets = [
        { name = "DATABASE_URL", valueFrom = var.db_url_secret_arn },
        { name = "ALEMBIC_DATABASE_URL", valueFrom = local.alembic_secret_arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/${var.project}/migrate"
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  tags = {
    Project = var.project
    Service = "migrate"
  }
}

# ECS Services for the 5 long-running roles.
resource "aws_ecs_service" "service" {
  for_each = local.services

  name            = "${var.project}-${each.key}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.service[each.key].arn
  desired_count   = each.value.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = true
  }

  # Attach ALB target group to mcp-server only.
  dynamic "load_balancer" {
    for_each = each.key == "mcp-server" ? [1] : []
    content {
      target_group_arn = var.mcp_server_target_group_arn
      container_name   = each.key
      container_port   = 8080
    }
  }

  # Allow external autoscaling changes without Terraform drift.
  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = {
    Project = var.project
    Service = each.key
  }
}

# Application AutoScaling target for worker only (ADR-026).
resource "aws_appautoscaling_target" "worker" {
  max_capacity       = 4
  min_capacity       = 1
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.service["worker"].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# Target-tracking policy: scale on SQS ApproximateNumberOfMessagesVisible (target=10).
resource "aws_appautoscaling_policy" "worker_sqs" {
  name               = "${var.project}-worker-sqs-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 10
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    customized_metric_specification {
      metric_name = "ApproximateNumberOfMessagesVisible"
      namespace   = "AWS/SQS"
      statistic   = "Average"

      dimensions {
        name  = "QueueName"
        value = var.sqs_queue_name
      }
    }
  }
}
