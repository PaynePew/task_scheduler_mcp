output "alb_dns_name" {
  description = "DNS name of the Application Load Balancer (consumed by DNS CNAME / #8 smoke test)."
  value       = aws_lb.main.dns_name
}

output "alb_zone_id" {
  description = "Canonical hosted zone ID of the ALB (used for Route 53 alias records)."
  value       = aws_lb.main.zone_id
}

output "alb_sg_id" {
  description = "Security group ID of the ALB (referenced by ecs module for ECS tasks ingress rule)."
  value       = aws_security_group.alb.id
}

output "alb_arn" {
  description = "ARN of the Application Load Balancer."
  value       = aws_lb.main.arn
}

output "mcp_server_target_group_arn" {
  description = "ARN of the mcp-server target group (consumed by ecs module load_balancer block)."
  value       = aws_lb_target_group.mcp_server.arn
}

output "acm_certificate_arn" {
  description = "ARN of the ACM certificate."
  value       = aws_acm_certificate.main.arn
}

output "acm_certificate_domain_validation_options" {
  description = "DNS records required to validate the ACM certificate (add these to Cloudflare DNS)."
  value       = aws_acm_certificate.main.domain_validation_options
}
