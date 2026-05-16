#!/usr/bin/env bash
# Per-issue Postgres database + ElasticMQ queue provisioning / teardown.
#
# Why: every `.harness/run.ps1 -Issue N` run forwards the same DATABASE_URL
# and QUEUE_URL into its docker blocks. With multiple slices in flight,
# integration tests (pytest -m integration) and runtime services race on
# the shared `app` DB and `task-queue` queue — table truncate / seed,
# message visibility, and DLQ counts all collide.
#
# This helper carves out per-issue resources so each slice has its own
# universe:
#   - Postgres database `app_issue_<N>` with the full schema applied.
#   - SQS queues `task-queue-<N>` and `task-dlq-<N>` (with redrive
#     policy linking them, matching elasticmq.conf for the shared pair).
#
# Idempotent: re-running provision on an existing DB / queue is a no-op.
# Destroy tolerates missing resources.
#
# Usage:
#   .harness/lib/issue-infra.sh provision <issue-number>
#   .harness/lib/issue-infra.sh destroy   <issue-number>
#
# Assumes `docker compose up -d postgres elasticmq` has already run
# (handled by before-tests.sh) and the services are healthy.

set -euo pipefail

action="${1:-}"
issue="${2:-}"

if [[ -z "$action" || -z "$issue" ]]; then
    echo "usage: $0 {provision|destroy} <issue-number>" >&2
    exit 2
fi
if ! [[ "$issue" =~ ^[0-9]+$ ]]; then
    echo "issue must be a positive integer, got: '$issue'" >&2
    exit 2
fi

db_name="app_issue_${issue}"
queue_main="task-queue-${issue}"
queue_dlq="task-dlq-${issue}"
elasticmq_url="http://localhost:9324"

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo_root"

pg_exec() {
    docker compose exec -T postgres "$@"
}

provision() {
    echo "[issue-infra] provisioning resources for issue #${issue}"

    # 1. Postgres database. Use a SELECT/CREATE pattern instead of
    # CREATE IF NOT EXISTS (CREATE DATABASE has no IF NOT EXISTS).
    db_created=0
    if pg_exec psql -U app -d postgres -tAc \
            "SELECT 1 FROM pg_database WHERE datname='${db_name}'" 2>/dev/null | grep -q '^1$'; then
        echo "[issue-infra]   db ${db_name} already exists"
    else
        echo "[issue-infra]   creating db ${db_name}"
        pg_exec psql -U app -d postgres -c "CREATE DATABASE ${db_name} OWNER app;" >/dev/null
        db_created=1
    fi

    # 2. Run alembic against the per-issue DB via the migrate service —
    # but only on a fresh DB. The migrate service is built from the
    # host repo's main-branch state, so its `migrations/` dir holds
    # only revisions that have landed on main. If a previous slice's
    # implementer added a new revision (say 0003) and stamped the DB
    # with it, a resume call would crash with:
    #   "Can't locate revision identified by '0003'"
    # since the DB head outranks what host-side migrate knows about.
    # On resume the DB already carries whatever revision the agent
    # left it at, and the agent's own container reads migrations from
    # the bind-mounted worktree (which has every branch-local
    # revision), so re-running migrate from the host adds no value
    # and only introduces this failure mode.
    if [[ "$db_created" -eq 1 ]]; then
        echo "[issue-infra]   running alembic upgrade head against ${db_name}"
        docker compose run --rm \
            -e "ALEMBIC_DATABASE_URL=postgresql+psycopg://app:app@postgres:5432/${db_name}" \
            migrate >/dev/null
    else
        echo "[issue-infra]   skipping migrate (db pre-exists; agent owns its schema)"
    fi

    # 3. DLQ first — main queue's redrive policy references it by ARN.
    echo "[issue-infra]   creating queue ${queue_dlq}"
    curl -fsS -X POST "${elasticmq_url}/" \
        --data-urlencode "Action=CreateQueue" \
        --data-urlencode "QueueName=${queue_dlq}" \
        --data-urlencode "Attribute.1.Name=VisibilityTimeout" \
        --data-urlencode "Attribute.1.Value=60" >/dev/null

    # 4. Main queue with redrive to DLQ. ElasticMQ uses a fixed
    # account-id (000000000000) and a fixed region ("elasticmq") in
    # queue ARNs; this matches what gets generated for the static
    # task-queue / task-dlq pair in elasticmq.conf.
    echo "[issue-infra]   creating queue ${queue_main}"
    redrive_policy="{\"deadLetterTargetArn\":\"arn:aws:sqs:elasticmq:000000000000:${queue_dlq}\",\"maxReceiveCount\":\"3\"}"
    curl -fsS -X POST "${elasticmq_url}/" \
        --data-urlencode "Action=CreateQueue" \
        --data-urlencode "QueueName=${queue_main}" \
        --data-urlencode "Attribute.1.Name=VisibilityTimeout" \
        --data-urlencode "Attribute.1.Value=30" \
        --data-urlencode "Attribute.2.Name=RedrivePolicy" \
        --data-urlencode "Attribute.2.Value=${redrive_policy}" >/dev/null

    echo "[issue-infra] provision complete (db=${db_name} queues=${queue_main},${queue_dlq})"
}

destroy() {
    echo "[issue-infra] destroying resources for issue #${issue}"

    # 1. Drop DB. Force-disconnect first — DROP DATABASE refuses while
    # any session is connected. The REVOKE blocks new connections; the
    # pg_terminate_backend kills existing ones.
    if pg_exec psql -U app -d postgres -tAc \
            "SELECT 1 FROM pg_database WHERE datname='${db_name}'" 2>/dev/null | grep -q '^1$'; then
        echo "[issue-infra]   dropping db ${db_name}"
        pg_exec psql -U app -d postgres -c \
            "REVOKE CONNECT ON DATABASE ${db_name} FROM PUBLIC;" >/dev/null 2>&1 || true
        pg_exec psql -U app -d postgres -c \
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${db_name}' AND pid <> pg_backend_pid();" >/dev/null 2>&1 || true
        pg_exec psql -U app -d postgres -c "DROP DATABASE IF EXISTS ${db_name};" >/dev/null
    else
        echo "[issue-infra]   db ${db_name} not present"
    fi

    # 2. Delete queues. Main first so the DLQ has no source pointing at
    # it when we delete it (some elasticmq builds enforce that).
    for q in "${queue_main}" "${queue_dlq}"; do
        # ElasticMQ delete: POST against the queue URL with Action=DeleteQueue.
        if curl -fsS -X POST "${elasticmq_url}/queue/${q}" \
                --data-urlencode "Action=DeleteQueue" >/dev/null 2>&1; then
            echo "[issue-infra]   deleted queue ${q}"
        else
            echo "[issue-infra]   queue ${q} not present"
        fi
    done

    echo "[issue-infra] destroy complete (db=${db_name} queues=${queue_main},${queue_dlq})"
}

case "$action" in
    provision) provision ;;
    destroy)   destroy   ;;
    *)
        echo "unknown action: ${action} (expected provision|destroy)" >&2
        exit 2
        ;;
esac
