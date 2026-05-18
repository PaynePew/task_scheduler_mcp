variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone.DNS:Edit scope on the paynepew.dev zone. Set via TF_VAR_cloudflare_api_token env var; do not commit to terraform.tfvars."
  type        = string
  sensitive   = true
}

variable "zone_name" {
  description = "Cloudflare DNS zone (apex domain). Looked up via Cloudflare API to obtain zone_id at plan time."
  type        = string
  default     = "paynepew.dev"
}

variable "subdomain" {
  description = "Subdomain to create the A record for (e.g. 'scheduler' creates scheduler.paynepew.dev)."
  type        = string
  default     = "scheduler"
}

variable "vps_ip" {
  description = "Lightsail static IP address the A record points to."
  type        = string
}
