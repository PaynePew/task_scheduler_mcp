# Lightsail Tokyo manual deployment

Interim deployment to a Lightsail Tokyo VPS until the W3 Fargate Terraform
migration lands (see [[project_w3-design-pivot]] / ADR-024..030).

## Layout

The VPS expects a single working directory (default `/opt/task_scheduler_mcp/`)
containing **these 6 runtime files** at the root:

| File                | Source in repo                | Notes                                   |
|---------------------|-------------------------------|-----------------------------------------|
| `docker-compose.yml`| `infra/vps/docker-compose.yml`| pulls ghcr.io image; restart policies   |
| `Caddyfile`         | `infra/vps/Caddyfile`         | TLS termination + reverse proxy         |
| `vector.toml`       | `infra/vps/vector.toml`       | Docker logs → Better Stack              |
| `elasticmq.conf`    | `elasticmq.conf` (repo root)  | shared with local dev                   |
| `static/`           | `static/` (repo root)         | served at `/` by Caddy                  |
| `.env`              | `infra/vps/.env.example` →    | **secrets — gitignored**                |

Everything else in the repo (app code, tests, docs, terraform) is **not**
needed on the VPS at runtime; the Python application code lives inside the
docker image `ghcr.io/paynepew/task_scheduler_mcp:<tag>`.

In practice the deploy directory ends up holding the whole repo (because the
operator clones the repo there for convenience and copies the 6 files to the
root). That works but is not the minimum-surface-area setup. Don't rely on
files outside the table above being present.

## First-time bootstrap

Assumes Ubuntu, Docker installed, ports 80/443 open, DNS pointing at the VPS.

```bash
# 1. SSH as the deploy user (created out-of-band)
sudo -iu deploy
cd /opt
sudo git clone https://github.com/PaynePew/task_scheduler_mcp.git
sudo chown -R deploy:deploy /opt/task_scheduler_mcp
cd /opt/task_scheduler_mcp

# 2. Stage the 6 runtime files at the root
cp infra/vps/docker-compose.yml .
cp infra/vps/Caddyfile .
cp infra/vps/vector.toml .

# 3. Fill in secrets
cp infra/vps/.env.example .env
nano .env   # set POSTGRES_PASSWORD, R2_*, BETTER_STACK_*, etc.
chmod 600 .env

# 4. Bring it up
docker compose up -d
docker compose ps
```

## Updating an existing deploy

When the repo's `infra/vps/*` files change, mirror those into the working
directory and reload:

```bash
sudo -iu deploy
cd /opt/task_scheduler_mcp
git pull

# Re-copy any file that changed (`git diff HEAD@{1} -- infra/vps/` lists them):
cp infra/vps/docker-compose.yml .
cp infra/vps/vector.toml .
# cp infra/vps/Caddyfile .  # only if changed

# Apply.  `up -d` recreates only services whose definition or env_file changed.
# DO NOT use `restart` after editing .env — restart skips env_file re-read.
docker compose up -d
```

Verify:
```bash
docker compose ps
docker compose logs vector --tail 20   # should show "Vector has started."
curl -sf https://scheduler.paynepew.dev/healthz
```

## Adding a new env var

1. Document it in `infra/vps/.env.example` (committed).
2. On the VPS, append it to `/opt/task_scheduler_mcp/.env` (gitignored).
3. `docker compose up -d` — recreates any service that reads `.env`.

## Single-service operations

| Goal                         | Command                              |
|------------------------------|--------------------------------------|
| Restart one service in place | `docker compose restart <service>`   |
| Recreate one service         | `docker compose up -d <service>`     |
| Recreate all that changed    | `docker compose up -d`               |
| Stop everything (data safe)  | `docker compose down`                |
| Stop + wipe volumes          | `docker compose down -v`  ⚠ destroys postgres data |

## Pitfalls observed

- **`timberio/vector` image ships with a default `/etc/vector/*.yaml`** that
  enables `demo_logs` + `console` sink. Without an explicit `command:
  ["--config", "/etc/vector/vector.toml"]` in the compose service, Vector
  loads both configs and floods stdout with fake syslog data.
- **`docker compose restart` does NOT re-read `env_file`** after you edit
  `.env`. Use `docker compose up -d` instead.
- **The root `docker-compose.yml` and the `infra/vps/docker-compose.yml`
  must be kept in sync manually.** A future improvement is to use
  `docker compose -f infra/vps/docker-compose.yml` directly and stop copying
  files; not done now because Lightsail is being retired in W3.
