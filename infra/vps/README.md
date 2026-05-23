# Lightsail Tokyo deployment

Interim deployment to a Lightsail Tokyo VPS until the W3 Fargate Terraform
migration lands (see [[project_w3-design-pivot]] / ADR-024..030).

## Two-path layout (ADR-062)

The VPS uses two directories with separate responsibilities:

| Path | Role | Written by |
|---|---|---|
| `/opt/task_scheduler_mcp_src/` | Full git clone — source of truth for config files | CI only (`git reset --hard`) |
| `/opt/task_scheduler_mcp/` | Runtime dir — only the 6 files docker compose needs | CI only (file copy) |

**Operators never `ssh` in to `git pull`.** CI is the only writer to both
directories. On every push to `main`, the deploy workflow:

1. Resets `_src/` to `origin/main`
2. Copies the 6 runtime files from `_src/` into the runtime dir
3. Runs `docker compose pull && migrate && up -d` from the runtime dir

### Runtime dir contents (`/opt/task_scheduler_mcp/`)

Exactly 6–8 items — never more:

| Entry | Source in repo |
|---|---|
| `docker-compose.yml` | `infra/vps/docker-compose.yml` |
| `Caddyfile` | `infra/vps/Caddyfile` |
| `vector.toml` | `infra/vps/vector.toml` |
| `elasticmq.conf` | `elasticmq.conf` (repo root) |
| `static/` | `static/` (repo root) |
| `.env.docker` | operator-managed, **gitignored** — secrets live here |
| `.env.legacy.*` | optional, present only during migration windows |

Everything else (app code, tests, docs, terraform, build artifacts) belongs
inside the Docker image or the `_src/` clone — **never** in the runtime dir.

## First-time bootstrap

Assumes Ubuntu, Docker + rsync installed, ports 80/443 open, DNS pointing at the VPS.

```bash
# 1. SSH as an admin user (sudo access required for directory creation)
sudo mkdir -p /opt/task_scheduler_mcp_src /opt/task_scheduler_mcp
sudo git clone https://github.com/PaynePew/task_scheduler_mcp.git /opt/task_scheduler_mcp_src
sudo chown -R deploy:deploy /opt/task_scheduler_mcp_src /opt/task_scheduler_mcp

# 2. Populate the runtime dir with the 4 config files + static/
#    (.env.docker is added in step 3 — total 6 entries.)
sudo -u deploy bash -c '
  cd /opt/task_scheduler_mcp_src
  cp infra/vps/docker-compose.yml /opt/task_scheduler_mcp/
  cp infra/vps/Caddyfile /opt/task_scheduler_mcp/
  cp infra/vps/vector.toml /opt/task_scheduler_mcp/
  cp elasticmq.conf /opt/task_scheduler_mcp/
  rsync -a static/ /opt/task_scheduler_mcp/static/
'

# 3. Fill in secrets (use root .env.docker.example as the template)
sudo -u deploy bash -c '
  cp /opt/task_scheduler_mcp_src/.env.docker.example /opt/task_scheduler_mcp/.env.docker
  nano /opt/task_scheduler_mcp/.env.docker   # set all required values
  chmod 600 /opt/task_scheduler_mcp/.env.docker
'

# 4. Bring it up (first IMAGE_TAG — pick any recent SHA from GHCR)
sudo -u deploy bash -c '
  cd /opt/task_scheduler_mcp
  export IMAGE_TAG=<commit-sha>
  docker compose pull
  docker compose run --rm migrate
  docker compose up -d
  docker compose ps
'
```

From this point on, **do not manually edit files in `/opt/task_scheduler_mcp/`** except
`.env.docker`. All config changes flow through a commit to `main` → CI deploy.

## Updating config

Config file changes (`docker-compose.yml`, `Caddyfile`, `vector.toml`, `elasticmq.conf`,
`static/`) should be committed to the repo and pushed to `main`. The CI deploy workflow
will sync them to the VPS automatically on the next push.

There is no manual update procedure for config files. This is intentional.

## Adding a new env var

1. Document it in the root `.env.docker.example` (committed).
2. On the VPS, append it to `/opt/task_scheduler_mcp/.env.docker` (gitignored).
3. `docker compose up -d` — recreates any service that reads `.env.docker`.

## Migrating an existing `.env` deploy to `.env.docker`

For deploys created before the `.env` → `.env.docker` rename, run this one-time
migration to keep your secrets intact:

```bash
sudo -iu deploy
cd /opt/task_scheduler_mcp

# 1. Copy secrets to the new filename FIRST (before pulling new compose,
#    otherwise `docker compose up -d` errors with "env file not found").
cp .env .env.docker
chmod 600 .env.docker

# 2. The next CI deploy will copy the updated docker-compose.yml that
#    references .env.docker. Trigger a push or wait for the next deploy.
grep env_file ./docker-compose.yml   # confirm all lines say .env.docker

# 3. Recreate the stack — picks up the new env_file reference.
docker compose up -d --force-recreate
curl -sf https://scheduler.paynepew.dev/healthz

# 4. After a day or two of stable operation, clean up the legacy file.
mv .env .env.legacy.$(date +%Y%m%d)
# rm .env.legacy.* once you trust the new setup
```

## Single-service operations

| Goal | Command |
|---|---|
| Restart one service in place | `docker compose restart <service>` |
| Recreate one service | `docker compose up -d <service>` |
| Recreate all that changed | `docker compose up -d` |
| Stop everything (data safe) | `docker compose down` |
| Stop + wipe volumes | `docker compose down -v`  ⚠ destroys postgres data |

All commands run from `/opt/task_scheduler_mcp/`.

## Pitfalls observed

- **`timberio/vector` image ships with a default `/etc/vector/*.yaml`** that
  enables `demo_logs` + `console` sink. Without an explicit `command:
  ["--config", "/etc/vector/vector.toml"]` in the compose service, Vector
  loads both configs and floods stdout with fake syslog data.
- **`docker compose restart` does NOT re-read `env_file`** after you edit
  `.env.docker`. Use `docker compose up -d` instead.
- **Do not edit config files directly in the runtime dir** — they are
  overwritten on the next CI deploy. Commit the change to the repo instead.
