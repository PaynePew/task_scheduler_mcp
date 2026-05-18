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

# ADR-031: status.paynepew.dev → Better Stack status page custom domain.
# proxied = false because Better Stack issues its own Let's Encrypt cert via
# ACME against the CNAME target; Cloudflare proxying would replace the cert
# and break custom-domain TLS. The 1-click Cloudflare authorize flow in the
# Better Stack UI is deliberately bypassed — managing the record via IaC
# keeps `terraform plan` as the single source of truth for DNS state.
resource "cloudflare_record" "status" {
  zone_id = data.cloudflare_zone.main.id
  name    = "status"
  type    = "CNAME"
  content = "statuspage.betteruptime.com"
  ttl     = 300
  proxied = false

  comment = "status.paynepew.dev → Better Stack status page (managed by terraform/cloudflare, ADR-031)"
}
