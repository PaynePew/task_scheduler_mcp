terraform {
  required_version = ">= 1.9"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }
}

# Token must have Zone.DNS:Edit scope on the paynepew.dev zone only.
# Set via TF_VAR_cloudflare_api_token env var or `-var` on apply.
provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
