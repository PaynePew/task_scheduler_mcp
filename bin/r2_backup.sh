#!/usr/bin/env bash
# r2_backup.sh — operator R2 upload replacement for the demoted r2_upload MCP action.
#
# Usage (run as the deploy user on the VPS):
#   bash bin/r2_backup.sh <local-file> <r2-key>
#
# Required environment variables (set in /opt/task_scheduler_mcp/.env or cron env):
#   R2_ACCOUNT_ID          — Cloudflare account ID
#   R2_ACCESS_KEY_ID       — R2 S3-compat access key
#   R2_SECRET_ACCESS_KEY   — R2 S3-compat secret key
#   R2_BUCKET              — destination bucket name
#
# Example cron (daily pg_dump → R2 at 02:00 server time):
#   0 2 * * * deploy /bin/bash /opt/task_scheduler_mcp/bin/r2_backup.sh \
#       /tmp/pg_dump.sql.gz backups/pg_dump-$(date +\%Y\%m\%d).sql.gz
#
# The script uses the AWS CLI (v2) which must be installed on the VPS.
# Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
#
# Background: r2_upload was removed from the MCP action surface in issue #132
# (ADR-051) because it has no public use case — it writes to the operator's
# bucket. Keeping it as an MCP action would mean blocking it with
# requires_operator=True; demoting it to a cron script is cleaner.

set -euo pipefail

LOCAL_FILE="${1:?Usage: $0 <local-file> <r2-key>}"
R2_KEY="${2:?Usage: $0 <local-file> <r2-key>}"

: "${R2_ACCOUNT_ID:?R2_ACCOUNT_ID must be set}"
: "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID must be set}"
: "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY must be set}"
: "${R2_BUCKET:?R2_BUCKET must be set}"

ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
aws s3 cp "$LOCAL_FILE" "s3://${R2_BUCKET}/${R2_KEY}" \
    --endpoint-url "$ENDPOINT" \
    --region auto

echo "[r2_backup] uploaded $LOCAL_FILE → s3://${R2_BUCKET}/${R2_KEY}"
