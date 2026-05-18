variable "zone_id" {
  description = "Cloudflare zone ID for paynepew.dev (from dashboard overview page)."
  type        = string
}

variable "subdomain" {
  description = "Subdomain hostname under paynepew.dev (e.g. 'scheduler' → scheduler.paynepew.dev)."
  type        = string
  default     = "scheduler"
}

variable "vps_ip" {
  description = "Lightsail Tokyo static IP for the scheduler VPS."
  type        = string
}

variable "record_ttl" {
  description = "DNS record TTL in seconds. Cloudflare requires 1 for proxied or >= 60 otherwise."
  type        = number
  default     = 300
}
