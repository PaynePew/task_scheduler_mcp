data "cloudflare_zone" "main" {
  name = var.zone_name
}

# ADR-028: proxied must be OFF so Caddy sees the real client IP and the
# Let's Encrypt HTTP-01 / TLS-ALPN-01 challenge reaches the VPS on port 80/443.
resource "cloudflare_record" "scheduler" {
  zone_id = data.cloudflare_zone.main.id
  name    = var.subdomain
  type    = "A"
  content = var.vps_ip
  ttl     = 300
  proxied = false

  comment = "scheduler.paynepew.dev → Lightsail Tokyo VPS (managed by terraform/cloudflare, ADR-028)"
}
