output "dns_record_id" {
  description = "Cloudflare DNS record ID (for API calls / debugging)."
  value       = cloudflare_record.scheduler.id
}

output "dns_record_hostname" {
  description = "Fully-qualified domain name of the scheduler A record."
  value       = cloudflare_record.scheduler.hostname
}

output "dns_record_ip" {
  description = "IP address the A record resolves to."
  value       = cloudflare_record.scheduler.content
}

output "status_page_hostname" {
  description = "Fully-qualified domain name of the Better Stack status page CNAME (ADR-031)."
  value       = cloudflare_record.status.hostname
}

output "status_page_target" {
  description = "CNAME target the status hostname resolves to (Better Stack edge)."
  value       = cloudflare_record.status.content
}
