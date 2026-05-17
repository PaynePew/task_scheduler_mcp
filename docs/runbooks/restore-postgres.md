# Runbook: Restore Postgres from R2 Backup

**When to use**: VPS data loss (volume deleted, instance destroyed) or corruption requiring point-in-time recovery.

---

## Prerequisites

- `rclone` installed and R2 remote configured (done by `bin/setup-vps.sh`)
- `docker compose` available in the working directory
- Target Postgres container is running and healthy

---

## 1. List available backups

```bash
rclone ls r2:chatgpt-task-backups/
```

Backups are named `scheduler-YYYYMMDD-HHMMSS.sql.gz`. Retention is 7 days.

---

## 2. Pull the snapshot from R2

```bash
rclone copy r2:chatgpt-task-backups/scheduler-YYYYMMDD-HHMMSS.sql.gz /tmp/
```

Replace `YYYYMMDD-HHMMSS` with the timestamp of the snapshot you want to restore.

---

## 3. Restore against a running Postgres container

**Full restore (drops existing data):**

```bash
# Stop app services to avoid writes during restore
cd /opt/chatgpt_task
docker compose stop mcp-server watcher worker recurring-watcher chain-watcher

# Drop and recreate the database
docker compose exec -T postgres psql -U postgres -c "DROP DATABASE IF EXISTS scheduler;"
docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE scheduler;"

# Restore from the dump
gunzip -c /tmp/scheduler-YYYYMMDD-HHMMSS.sql.gz \
    | docker compose exec -T postgres psql -U postgres scheduler

# Restart app services
docker compose start mcp-server watcher worker recurring-watcher chain-watcher
```

---

## 4. Verify row counts

```bash
docker compose exec -T postgres psql -U postgres scheduler -c "
SELECT
    (SELECT count(*) FROM jobs)      AS jobs,
    (SELECT count(*) FROM job_runs)  AS job_runs,
    (SELECT count(*) FROM run_events) AS run_events;
"
```

Expected output: row counts matching your last known state before data loss.

---

## 5. Restore drill against a fresh local Compose (L5 acceptance)

Use this to validate the runbook without touching the live VPS.

```bash
# 1. Start a fresh local postgres
docker compose up -d postgres
docker compose run --rm migrate

# 2. Confirm schema is empty
docker compose exec -T postgres psql -U app app -c "\dt"

# 3. Pull a recent backup (requires rclone configured locally)
rclone copy r2:chatgpt-task-backups/scheduler-YYYYMMDD-HHMMSS.sql.gz /tmp/

# 4. Restore (note: local DB is named 'app', not 'scheduler' — adapt as needed)
#    For drill purposes, restore into a separate 'scheduler_restore' database:
docker compose exec -T postgres psql -U app -c "CREATE DATABASE scheduler_restore;"
gunzip -c /tmp/scheduler-YYYYMMDD-HHMMSS.sql.gz \
    | docker compose exec -T postgres psql -U app scheduler_restore

# 5. Verify row counts
docker compose exec -T postgres psql -U app scheduler_restore -c "
SELECT
    (SELECT count(*) FROM jobs)       AS jobs,
    (SELECT count(*) FROM job_runs)   AS job_runs,
    (SELECT count(*) FROM run_events) AS run_events;
"

# 6. Cleanup
docker compose exec -T postgres psql -U app -c "DROP DATABASE scheduler_restore;"
```

Drill passes when row counts match the R2 snapshot and no errors appear.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `rclone: command not found` | rclone not installed | Run `bin/setup-vps.sh` or install rclone manually |
| `FATAL: database "scheduler" does not exist` | DB was deleted | `docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE scheduler;"` |
| Role missing (`ERROR: role "postgres" does not exist`) | Restore to wrong Postgres instance | Confirm `POSTGRES_USER=postgres` in `.env` |
| `pg_restore: error: could not execute query` | Schema version mismatch | Run `docker compose run --rm migrate` after restore |

---

## References

- Backup script: `/etc/cron.daily/pg-backup.sh`
- R2 bucket: `chatgpt-task-backups`
- ADR-030: Operational concerns — backup rationale and retention policy
