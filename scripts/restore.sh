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

DB_NAME="${POSTGRES_DB:-otodock}"
# The restore role is resolved after the container is found (see below): prefer
# the superuser otodock_admin so the --clean DROP/CREATE and OWNER TO statements
# all apply; fall back to the app role on a not-yet-upgraded install (or T1).
DB_USER=""

DUMP="${1:-}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "usage: $0 <dump.sql.gz|dump.sql>" >&2
  exit 1
fi

# The platform's own running container for a compose service: the containerised
# stack's (compose project `otodock`, pinned by the file's `name:`), else one
# whose WHOLE name is the bare-metal container_name, when one is given. Never a
# name substring: a community MCP container is named
# otodock-<install_id>-mcp-<manifest name>, and the manifest name is free text.
# More than one match is refused, never guessed.
platform_container() {  # <compose service> [<bare-metal container_name>]
  local ids
  ids="$(docker ps --filter "label=com.docker.compose.project=otodock" \
    --filter "label=com.docker.compose.service=$1" --format '{{.ID}}')"
  if [ -z "$ids" ] && [ -n "${2:-}" ]; then
    ids="$(docker ps --filter "name=^/?${2}\$" --format '{{.ID}}')"
  fi
  if [ "$(printf '%s\n' "$ids" | grep -c .)" -gt 1 ]; then
    echo "error: more than one running container is the platform's $1 ($(printf '%s' "$ids" | tr '\n' ' ')); refusing to guess, stop the extra one" >&2
    return 1
  fi
  printf '%s' "$ids"
}

# The running Postgres: T2's otodock-postgres service, or T1's container_name.
CID="$(platform_container otodock-postgres otodock-postgres)" || exit 1
if [ -z "$CID" ]; then
  echo "error: no running otodock-postgres container found (is the stack up?)" >&2
  exit 1
fi

# Resolve the restore role. Prefer the superuser otodock_admin (it can DROP and
# re-own every object a --clean dump recreates). The probe passes -d "$DB_NAME"
# on purpose: without it psql connects to a database named after the user, which
# does not exist, and every install would false-negative to the fallback. The
# local socket is `trust`, so no password is needed.
if [ -n "${POSTGRES_USER:-}" ]; then
  DB_USER="$POSTGRES_USER"
elif docker exec "$CID" psql -U otodock_admin -d "$DB_NAME" -tAc 'SELECT 1' >/dev/null 2>&1; then
  DB_USER="otodock_admin"
else
  DB_USER="otodock"
fi
echo "Restoring as database role '${DB_USER}'."

# A running platform writes mid-restore and serves the old rows from its
# in-process caches afterwards: refuse while it answers on its port.
PROXY_PORT="${PORT:-8400}"
if curl -fs -m 2 "http://127.0.0.1:${PROXY_PORT}/health" >/dev/null 2>&1; then
  echo "error: the platform is running on port ${PROXY_PORT} — stop the proxy first" >&2
  echo "       (systemctl stop otodock-proxy, or scripts/compose.sh stop otodock-proxy)" >&2
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

# A dump from a database whose public schema was recreated by hand names its
# owner (ALTER SCHEMA public OWNER TO <role>); on the containerised stack that
# can be the superuser, and the proxy's migrations then fail with "no schema
# has been selected to create in". The one-shot otodock-db-init would move it
# on the next `up -d`, but the proxy is usually restarted before that, so the
# app role is given the schema here. A no-op on a stock cluster (public follows
# the database owner) and on bare metal (otodock is the bootstrap superuser).
if [ "$DB_USER" != "otodock" ]; then
  owner="$(docker exec "$CID" psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'" 2>/dev/null || true)"
  app_role="$(docker exec "$CID" psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'otodock' AND NOT rolsuper" 2>/dev/null || true)"
  case "$owner" in
    ""|pg_database_owner|otodock) ;;
    *)
      if [ "$app_role" = 1 ]; then
        docker exec "$CID" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "ALTER SCHEMA public OWNER TO otodock" >/dev/null
        echo "Schema public was owned by ${owner}; the app role otodock owns it now."
      fi ;;
  esac
fi
echo "Restore complete. Start the proxy; migrations re-run idempotently on boot."
