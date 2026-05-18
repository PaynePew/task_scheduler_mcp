terraform {
  required_version = ">= 1.9"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }
}

# Provider reads token from CLOUDFLARE_API_TOKEN env var.
# Token must have Zone.DNS:Edit scope on the paynepew.dev zone only.
provider "cloudflare" {}
