resource "cloudflare_record" "scheduler" {
  zone_id = var.zone_id
  name    = var.subdomain
  type    = "A"
  content = var.vps_ip
  ttl     = var.record_ttl

  # ADR-028: proxied must be OFF so Caddy sees the real client IP and the
  # Let's Encrypt HTTP-01 challenge reaches the VPS on port 80.
  proxied = false

  comment = "scheduler.paynepew.dev → Lightsail Tokyo VPS (managed by terraform/cloudflare, ADR-028)"
}
