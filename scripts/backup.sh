#!/usr/bin/env bash
# Back up the OtoDock PostgreSQL database to a timestamped, gzip'd pg_dump.
#
# Topology-agnostic: it dumps via `docker exec` into the running otodock-postgres
# container, so it works for both the bare-metal stack (docker-compose.t1.yml) and the
# fully-containerised stack (docker-compose.yml). The dump is a plain-SQL
# --clean --if-exists script (restore.sh feeds it straight back through psql).
#
#   scripts/backup.sh                 # → ./backups/otodock-otodock-YYYYmmdd-HHMMSS.sql.gz
#   OTODOCK_BACKUP_DIR=/srv/backups scripts/backup.sh
#   OTODOCK_BACKUP_RETAIN=30 scripts/backup.sh   # keep the 30 newest (0 = keep all)
#
# Schedule it from cron/systemd-timer for unattended backups. Restore with restore.sh.
set -euo pipefail

DB_USER="${POSTGRES_USER:-otodock}"
DB_NAME="${POSTGRES_DB:-otodock}"
OUT_DIR="${OTODOCK_BACKUP_DIR:-./backups}"
RETAIN="${OTODOCK_BACKUP_RETAIN:-14}"   # keep the N newest dumps; 0 = keep all

# Find the running Postgres container. T1 names it `otodock-postgres` explicitly;
# T2 (compose project `otodock`) names it `otodock-otodock-postgres-1` — both match.
CID="$(docker ps --filter "name=otodock-postgres" --format '{{.ID}}' | head -1)"
if [ -z "$CID" ]; then
  echo "error: no running otodock-postgres container found (is the stack up?)" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$OUT_DIR/otodock-${DB_NAME}-${TS}.sql.gz"

echo "Backing up database '$DB_NAME' from container ${CID} → ${OUT}"
docker exec "$CID" pg_dump -U "$DB_USER" --clean --if-exists "$DB_NAME" | gzip > "$OUT"
echo "Done: $(du -h "$OUT" | cut -f1)  ${OUT}"

# Retention: prune all but the N newest dumps for this database.
if [ "$RETAIN" -gt 0 ]; then
  # shellcheck disable=SC2012
  ls -1t "$OUT_DIR"/otodock-"${DB_NAME}"-*.sql.gz 2>/dev/null \
    | tail -n +"$((RETAIN + 1))" | xargs -r rm -f
fi
