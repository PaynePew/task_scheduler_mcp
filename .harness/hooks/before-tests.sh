#!/usr/bin/env bash
# Runs on the HOST (not inside the agent container) before any phase of
# an Issue-bound `.harness/run.ps1` invocation starts. Brings up the
# shared Postgres + ElasticMQ services and waits for both to be
# reachable, so subsequent provisioning + integration-test traffic
# doesn't race on a half-started stack.
#
# Why on the host: the agent container has no docker.sock and no
# compose, so it can't bring up its own stack. This hook plus the
# --add-host wiring in run.ps1 is what makes `pytest -m integration`
# work end-to-end inside the implement / review / merge phases.
#
# Per-issue Postgres DB + queues are NOT provisioned here — run.ps1
# calls .harness/lib/issue-infra.sh provision directly after this hook
# so that a provisioning failure aborts the run instead of being
# downgraded to a warning by Invoke-HarnessHook.
#
# Idempotent: `docker compose up -d` is a no-op when services are
# already healthy.
#
# Env (provided by run.ps1):
#   HARNESS_ISSUE  — issue number being worked on
#   HARNESS_BRANCH — branch name
#   HARNESS_PHASE  — pipeline phase ("implement", "review", "merge", ...)

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo_root"

echo "[before-tests] bringing up postgres + elasticmq for issue ${HARNESS_ISSUE:-?} (${HARNESS_PHASE:-?})"

docker compose up -d postgres elasticmq

# Postgres readiness. Compose has a healthcheck but issue-infra.sh's
# follow-up `docker compose exec postgres psql ...` call needs it
# gated on the same condition explicitly.
echo "[before-tests] waiting for postgres healthcheck..."
deadline=$(( $(date +%s) + 60 ))
while ! docker compose exec -T postgres pg_isready -U app -d app >/dev/null 2>&1; do
    if [[ $(date +%s) -gt $deadline ]]; then
        echo "[before-tests] postgres did not become ready within 60s" >&2
        exit 1
    fi
    sleep 1
done

# ElasticMQ readiness. The container being "running" doesn't mean its
# REST endpoint is bound yet; the CreateQueue calls in issue-infra.sh
# would fail with connection-refused otherwise. ListQueues is the
# cheapest action — empty result is fine.
echo "[before-tests] waiting for elasticmq..."
deadline=$(( $(date +%s) + 30 ))
while ! curl -fsS -o /dev/null "http://localhost:9324/?Action=ListQueues" 2>/dev/null; do
    if [[ $(date +%s) -gt $deadline ]]; then
        echo "[before-tests] elasticmq did not become ready within 30s" >&2
        exit 1
    fi
    sleep 1
done

echo "[before-tests] services ready"
