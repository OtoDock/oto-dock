#!/usr/bin/env bash
# Restore the OtoDock PostgreSQL database from a backup.sh dump (.sql.gz or .sql).
#
# DESTRUCTIVE: the dump is --clean --if-exists, so it DROPs and recreates every
# object. Stop the proxy first (so nothing writes mid-restore), then bring it back
# up — migrations re-run idempotently on boot.
#
#   scripts/restore.sh ./backups/otodock-otodock-20260627-031500.sql.gz
#
# Topology-agnostic: it pipes the dump into psql via `docker exec` against the
# running otodock-postgres container (T1 or T2).
set -euo pipefail

DB_USER="${POSTGRES_USER:-otodock}"
DB_NAME="${POSTGRES_DB:-otodock}"

DUMP="${1:-}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "usage: $0 <dump.sql.gz|dump.sql>" >&2
  exit 1
fi

CID="$(docker ps --filter "name=otodock-postgres" --format '{{.ID}}' | head -1)"
if [ -z "$CID" ]; then
  echo "error: no running otodock-postgres container found (is the stack up?)" >&2
  exit 1
fi

echo "WARNING: restoring '${DUMP}' into database '${DB_NAME}' (container ${CID})."
echo "This OVERWRITES current data. Stop the proxy before continuing."
printf "Type 'yes' to continue: "
read -r confirm
[ "$confirm" = "yes" ] || { echo "Aborted."; exit 1; }

case "$DUMP" in
  *.gz) gunzip -c "$DUMP" | docker exec -i "$CID" psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" ;;
  *)    docker exec -i "$CID" psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" < "$DUMP" ;;
esac
echo "Restore complete. Start the proxy; migrations re-run idempotently on boot."
