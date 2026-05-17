# ADR-029: VPS deployment mechanics — build on GitHub Actions, push to ghcr.io, SSH pull on VPS, containerized data layer

- **Status**: Accepted
- **Date**: 2026-05-17
- **Deciders**: PaynePew
- **Source**: Grilling Session #4, Q-W3-6
- **Related**: ADR-027 (VPS-first deployment target), ADR-028 (Caddy 2)

## Context

ADR-027 picked AWS Lightsail Tokyo (1 vCPU, 2 GB RAM) as the deployment target.
ADR-028 picked Caddy 2 as the HTTPS proxy. What remains is the **deployment
pipeline** — how code reaches the running VPS — and the **runtime data layer**
choice (managed services vs containerised).

Four deployment patterns are common for single-VPS hobby/portfolio projects:

| Pattern | Build location | Image transport | VPS action |
|---|---|---|---|
| A. git pull + build on VPS | VPS | git | git pull + `docker build` |
| B. Build on CI + push registry + SSH pull | CI runner | OCI registry | `docker compose pull` over SSH |
| C. Build on CI + push registry + Watchtower poll | CI runner | OCI registry | Watchtower auto-pulls on poll interval |
| D. Ansible playbook from CI | CI runner | Ansible | Ansible runs against VPS |

The data layer choice is between **managed services** (RDS for Postgres, SQS for
queue) — matching the Fargate Terraform path — and **containerised** (Postgres
+ ElasticMQ as Docker containers on the VPS) — matching the local development
environment.

## Decision

### Deployment mechanics

**Pattern B: Build on GitHub Actions → push to `ghcr.io` → SSH pull on VPS.**

GitHub Actions workflow (`.github/workflows/deploy-vps.yml`):

1. On push to `main`, checkout + Docker buildx setup
2. Login to `ghcr.io` using `GITHUB_TOKEN` (no separate registry credentials)
3. `docker buildx build` and push two tags:
   - `ghcr.io/paynepew/chatgpt_task:latest`
   - `ghcr.io/paynepew/chatgpt_task:${{ github.sha }}` (pinned)
4. SSH into VPS (`appleboy/ssh-action`) using `secrets.VPS_SSH_KEY`
5. On VPS, `IMAGE_TAG=${git_sha} docker compose pull && docker compose run --rm migrate && docker compose up -d --remove-orphans`
6. Post-deploy smoke test: `curl -fsS https://scheduler.paynepew.dev/healthz`

**Image tag strategy**: `latest` (for the default `docker compose up`) and
`${git_sha}` (for pinned rollback). No semver — portfolio code does not have
releases that justify versioning ceremony.

**Rollback procedure**: `IMAGE_TAG=<prev_sha> docker compose up -d` on the VPS.
The previous image stays in `ghcr.io` storage (free for public packages).

### Runtime data layer on the VPS

**Containerised Postgres 16 + ElasticMQ, both via `docker-compose.yml`.**

Concretely the VPS `docker-compose.yml`:

```yaml
services:
  mcp-server:
    image: ghcr.io/paynepew/chatgpt_task:${IMAGE_TAG:-latest}
    command: python -m app.entrypoints.mcp_server
    env_file: .env
    restart: unless-stopped
  watcher:           # ... same image, different command
  worker:            # ...
  recurring_watcher: # ...
  chain_watcher:     # ...

  postgres:
    image: postgres:16
    volumes: [pgdata:/var/lib/postgresql/data]
    env_file: .env
    restart: unless-stopped

  elasticmq:
    image: softwaremill/elasticmq-native:latest
    restart: unless-stopped

  caddy:
    image: caddy:2-alpine
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    ports: ["80:80", "443:443"]
    restart: unless-stopped

volumes:
  pgdata:
  caddy_data:
```

**This is the same `docker-compose.yml` as local development** (with the
`mcp-server` image swapped from a `build:` directive to an `image:` reference
on `ghcr.io`). Local dev → VPS deploy mental model is identical.

### Secrets

| Secret | Lives where | How loaded |
|---|---|---|
| `DB_URL`, `MCP_USER_ID`, `MCP_USER_TZ` | `/opt/chatgpt_task/.env` on VPS (chmod 0600) | Docker `env_file:` |
| `GITHUB_PAT` (W3.5 action sprint) | same `.env` | same |
| `SLACK_WEBHOOK_URL` (W3.5 action sprint) | same `.env` | same |
| `VPS_SSH_KEY` (deploy private key) | GitHub Actions secret | SSH action consumes |
| `GITHUB_TOKEN` (registry write) | auto-injected by Actions | `docker login` |

Nothing in `.env` enters Git. Nothing in GitHub Actions secrets is needed on
the VPS (except for the SSH key, which transits CI → VPS via SSH itself).

## Alternatives considered

### Pattern A (git pull + build on VPS)

Rejected on two grounds:

- **Build CPU contention**: Lightsail $5 plan has 1 vCPU. Building the Docker
  image during a deploy steals CPU from `mcp-server` / `worker` / `watcher`
  for 1-3 minutes. A daily ops digest action (Action Sprint) running during
  the build window would slow or timeout.
- **Source code on production is anti-pattern**: VPS having a working tree +
  `git pull` blurs the deploy boundary; the version running is "whatever
  git happens to be on now", not "this image".

### Pattern C (Watchtower polling)

Rejected: introduces a second deploy mechanism that runs autonomously, making
it harder to reason about what's deployed. Useful for fleet of VPSes where
SSH-orchestrated push doesn't scale; not for one VPS.

### Pattern D (Ansible)

Rejected: Ansible is the right tool for >3 hosts, configuration drift
management, or repeatable role assignment. For one VPS provisioned once and
deployed-to weekly, the inline SSH commands in the GitHub Actions workflow
are shorter and more auditable.

### Managed RDS + SQS instead of containerised data layer

Rejected for the VPS path. RDS / SQS make sense for the Fargate path (ADR-005,
ADR-008) where they're region-native, integrated with VPC, and the cost is
amortised over scale. For a $5/mo single VPS, running Postgres in a container
on the same host:

- **costs $0 extra** (vs RDS $12/mo minimum for db.t4g.micro);
- **simplifies the deploy** (no separate provisioning + connection pool tuning);
- **mirrors local development exactly** (no "works on Compose, breaks on RDS"
  failure modes);
- **acceptable for portfolio reliability** (loss-of-VPS = lose Postgres data,
  but daily backups (ADR-030 / Q-W3-7) cover this).

The Fargate Terraform path **still uses RDS + SQS**; the deciders maintain
both paths so the AWS architecture remains valid even though it's not the
running deployment.

## Consequences

- `.github/workflows/deploy-vps.yml` is the new authoritative deploy path —
  every push to `main` reaches the VPS within ~3-5 minutes.
- A second workflow `.github/workflows/validate-fargate.yml` exists for the
  one-shot Terraform validation (separate ADR / Q-W3-7).
- Image storage on `ghcr.io`: free for public packages. The deciders' image
  will be public (consistent with the open portfolio narrative).
- VPS-side data lives in named Docker volumes (`pgdata`, `caddy_data`). Backup
  strategy decided in Q-W3-7.
- Migrations run as `docker compose run --rm migrate` before `up -d`. Failure
  to migrate fails the deploy (workflow exits non-zero, smoke test never runs).
- The deploy workflow is a tight 30-line YAML — auditable in one screen, no
  "magic" external actions beyond `docker/build-push-action` and
  `appleboy/ssh-action` (both widely-used + maintained).

## References

- `docker/build-push-action`: https://github.com/docker/build-push-action
- `appleboy/ssh-action`: https://github.com/appleboy/ssh-action
- `ghcr.io` documentation: https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry
- ADR-027 (Lightsail target), ADR-028 (Caddy proxy)
