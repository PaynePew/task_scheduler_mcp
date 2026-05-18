output "fqdn" {
  description = "Fully-qualified domain name for the scheduler."
  value       = cloudflare_record.scheduler.hostname
}

output "record_id" {
  description = "Cloudflare DNS record ID (for debugging / API calls)."
  value       = cloudflare_record.scheduler.id
}
