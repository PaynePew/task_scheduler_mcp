resource "aws_cloudwatch_log_group" "ecs" {
  for_each = toset(var.services)

  name              = "/ecs/${var.project}/${each.key}"
  retention_in_days = var.retention_in_days

  tags = {
    Project = var.project
    Service = each.key
  }
}
