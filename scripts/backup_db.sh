#!/usr/bin/env bash
# Manual/cron PostgreSQL backup for the workroom_db database.
#
# Usage: ./scripts/backup_db.sh [output-directory]
#
# In a real deployment prefer your host's managed backup product (e.g. RDS
# automated snapshots) over this script where available -- this is the
# fallback for a self-managed Postgres instance. See DEPLOYMENT.md.
set -euo pipefail

OUT_DIR="${1:-./backups}"
mkdir -p "$OUT_DIR"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="$OUT_DIR/workroom_db_${TIMESTAMP}.sql.gz"

: "${DB_NAME:?DB_NAME is not set}"
: "${DB_USERNAME:?DB_USERNAME is not set}"
: "${DB_HOST:=localhost}"
: "${DB_PORT:=5432}"

echo "Backing up $DB_NAME from $DB_HOST:$DB_PORT to $OUT_FILE"
pg_dump --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USERNAME" \
  --format=plain --no-owner --no-privileges "$DB_NAME" | gzip > "$OUT_FILE"

echo "Done: $OUT_FILE"
