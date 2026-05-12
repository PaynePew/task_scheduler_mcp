#!/usr/bin/env bash
# Runs on the HOST (not inside the agent container) before the implement
# phase starts. Brings up Postgres + ElasticMQ on host ports so the agent's
# integration tests (reaching them via host.docker.internal) succeed on the
# first try.
#
# Why on the host: the agent container has no docker.sock and no compose,
# so it can't bring up its own stack. This hook plus the --add-host wiring
# in run.ps1 is what makes `pytest -m integration` work end-to-end inside
# the implement and review phases.
#
# Idempotent: `docker compose up -d` is a no-op when the services are
# already running. `alembic upgrade head` is a no-op when schema is current.
#
# Env (provided by run.ps1):
#   HARNESS_ISSUE  — issue number being worked on
#   HARNESS_BRANCH — branch name
#   HARNESS_PHASE  — pipeline phase ("implement", "review", ...)

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo_root"

echo "[before-tests] bringing up postgres + elasticmq for issue ${HARNESS_ISSUE:-?} (${HARNESS_PHASE:-?})"

docker compose up -d postgres elasticmq

# Wait for postgres to be ready before running migrations. Compose's
# healthcheck takes care of `mcp-server`-style waits, but our migrate-only
# step needs to gate on the same condition manually.
echo "[before-tests] waiting for postgres healthcheck..."
deadline=$(( $(date +%s) + 60 ))
while ! docker compose exec -T postgres pg_isready -U app -d app >/dev/null 2>&1; do
    if [[ $(date +%s) -gt $deadline ]]; then
        echo "[before-tests] postgres did not become ready within 60s" >&2
        exit 1
    fi
    sleep 1
done

echo "[before-tests] running migrations via compose service (no host uv needed)"
docker compose run --rm migrate

echo "[before-tests] services ready"
