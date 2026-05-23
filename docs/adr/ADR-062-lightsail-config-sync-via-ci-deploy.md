# ADR-062: Lightsail config sync via CI deploy step

- **Status**: Accepted
- **Date**: 2026-05-23
- **Deciders**: PaynePew
- **Source**: issue #193 (minimize production deploy directory surface)
- **Related**: ADR-024–030 (W3 Fargate design), ADR-031 (Better Stack monitor pause)
- **Superseded by**: this ADR is invalidated when Fargate cutover lands (no host file system)

## Context

`/opt/task_scheduler_mcp/` on the production Lightsail VPS holds a full git clone of the
repo, but only 6 files are actually needed at runtime:

| File | Source |
|---|---|
| `docker-compose.yml` | `infra/vps/docker-compose.yml` |
| `Caddyfile` | `infra/vps/Caddyfile` |
| `vector.toml` | `infra/vps/vector.toml` |
| `elasticmq.conf` | repo root |
| `static/` | repo root |
| `.env.docker` | operator-managed, gitignored |

The surplus ~35 items (source code, tests, docs, terraform, build artifacts) create:

1. **Attack surface** — anyone with shell access reads source, test fixtures, and any
   terraform variables referencing AWS credentials.
2. **Wrong-file edits** — multiple env-related files in one directory caused confusion
   during the #190/#191 debug session.
3. **Source drift** — VPS `app/` is git source; the running container uses the CI image.
   They diverge silently; operators debugging on the host may read the wrong code.
4. **Dead artifacts** — `terraform/`, `.harness/`, `.github/`, `tests/`, `docs/` update
   on `git pull` with no runtime purpose.

## Options considered

### Option A — two-path layout

- `/opt/task_scheduler_mcp_src/` — full git clone (where `git pull` runs)
- `/opt/task_scheduler_mcp/` — runtime dir with 6 entries (4 config files + `static/` as
  a symlink to `_src/static/` + `.env.docker`)
- `deploy-vps.yml` unchanged

Rejected: source still fully exposed at `_src/`; symlink for `static/` adds operational
complexity without commensurate security gain.

### Option B — single-path with `-f infra/vps/docker-compose.yml`

- Keep one clone; run `docker compose -f infra/vps/docker-compose.yml up -d`
- Removes root-level runtime-file duplicates

Rejected: `docker-compose.yml` references `./elasticmq.conf`, `./static`, `./Caddyfile`,
`./vector.toml` as relative paths. With `-f`, the default project dir becomes the
compose-file dir (`infra/vps/`) — but `elasticmq.conf` and `static/` live at the repo
root, so the references break. Fixing that requires moving files and invalidating local-dev
`docker-compose.yml`.

### Option D-e — CI-driven file sync (chosen)

- Runtime dir (`/opt/task_scheduler_mcp/`) is **not** a git clone. It holds only the 6
  runtime files.
- Source clone lives at `/opt/task_scheduler_mcp_src/`. Operators never `ssh` in to run
  `git pull`.
- `deploy-vps.yml` ssh script syncs on every deploy: pulls source, copies the 6 files to
  the runtime dir, then runs docker compose against the runtime dir.

## Decision

**Option D-e.**

The deploy script becomes:

```bash
set -euo pipefail
cd /opt/task_scheduler_mcp_src
git fetch origin
git reset --hard origin/main
cp infra/vps/docker-compose.yml /opt/task_scheduler_mcp/
cp infra/vps/Caddyfile /opt/task_scheduler_mcp/
cp infra/vps/vector.toml /opt/task_scheduler_mcp/
cp elasticmq.conf /opt/task_scheduler_mcp/
rsync -a --delete static/ /opt/task_scheduler_mcp/static/
cd /opt/task_scheduler_mcp
export IMAGE_TAG=<sha>
docker compose pull
docker compose run --rm migrate
docker compose up -d --remove-orphans
```

CI is the **only writer** to the runtime dir. Manual edits to runtime files are overwritten
on the next deploy — this is intentional (GitOps property).

## Why not Terraform for file-in-instance config?

Terraform manages **infrastructure** (Lightsail instance, DNS, IAM, network) — not files
inside a running instance. To get config changes via Terraform on Lightsail you would need
`user_data` (one-shot, instance-creation only) or Ansible/Chef (full config-management
stack). For the few months Lightsail is still in use before Fargate cutover, the CI ssh
script is the simplest GitOps approximation.

## Consequences

- `deploy-vps.yml` ssh script updated: source pull + file copy precede docker compose
  commands.
- `infra/vps/README.md` rewritten: runtime dir is not a git clone; operator's role is
  set up `_src/` once during first-time bootstrap, then never touch.
- `infra/vps/.env.docker.example` deleted (closes #192): root `.env.docker.example` is the
  canonical template.
- Phase 3 (production cutover) is a one-shot manual ops step performed after this PR
  merges — not automated.
- Attack surface is improved (no source at runtime path); source at `_src/` is still
  present on host. Truly-zero host footprint requires Fargate (ADR-024–030, W5+ work).
