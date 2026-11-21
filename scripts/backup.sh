#!/usr/bin/env bash
# Back up an OtoDock install: one run directory per backup with everything a
# restore needs (restore with restore.sh; the Backup and restore page of the
# documentation has the details).
#
#   backups/otodock-<version>-<YYYYmmdd-HHMMSS>/
#     db.sql.gz        the database (pg_dump --clean --if-exists)
#     env              the env file (JWT_SECRET, the credential key, every secret), 0600
#     agents.tar.gz    the agents tree: workspaces, knowledge, config, users, apps;
#                      every SQLite database copied through the SQLite backup API
#     ssh-keys.tar.gz  the ssh-hosts private keys the ssh_hosts rows name
#     sessions.tar.gz  the sessions tree: the connected OAuth accounts' token files
#                      (<provider>-tokens/<username>/, the only copy), the session index
#     manifest         KEY=value: the version, the time, the topology, the credential
#                      key fingerprints, what is in the directory, complete=1 last
#
#   scripts/backup.sh                        # the full set, into ./backups
#   scripts/backup.sh --db-only              # the database, the env file and the manifest
#   OTODOCK_BACKUP_DIR=/srv/backups scripts/backup.sh
#   OTODOCK_BACKUP_RETAIN=30 scripts/backup.sh         # keep the 30 newest runs (0 = all)
#   OTODOCK_BACKUP_RETAIN_TREES=5 scripts/backup.sh    # keep the trees of the 5 newest (0 = all)
#   OTODOCK_COMPOSE_PROJECT=other scripts/backup.sh    # a second install on this daemon
#   OTODOCK_POSTGRES_CONTAINER=otodock-postgres scripts/backup.sh   # a Postgres that is not the
#                                                      # project's otodock-postgres service
#
# Topology-agnostic: the dump runs through `docker exec` into the running
# otodock-postgres container, so it works for the bare-metal stack
# (docker-compose.t1.yml) and the containerised one (docker-compose.yml). On
# the containerised stack the env file and the trees are the proxy
# container's: the env file is its bind mount, the trees its volumes, read
# inside the running container or, when it is stopped, through a one-off
# container on its image. On bare metal the env file is config.env
# (OTODOCK_ENV_FILE, $PLATFORM_CONFIG_DIR/config.env, the install's
# config.env or .env) and the trees are the agents directory on this host
# (OTODOCK_AGENTS_DIR, else $PLATFORM_DATA_DIR/agents, else ./agents), the
# ssh keys under the install's mcps/custom/ssh-hosts/keys (OTODOCK_SSH_KEYS_DIR)
# and the sessions directory (OTODOCK_SESSIONS_DIR, else
# $PLATFORM_DATA_DIR/sessions, else ./sessions; absent: skipped with a note).
# Setting OTODOCK_AGENTS_DIR always means this host.
#
# A live backup is crash-consistent per part: the dump is a snapshot, the
# tree is read after it. Stop the proxy first for a set that agrees to the
# second. The version in the directory name is the one the database records
# (schema_meta), else the running image's, else VERSIONS.md next to this
# script's parent folder, else "unknown".
#
# Exit status: 0 when every file was written; 3 when the run is usable but
# not whole: a tree archive was written but one or more files or folders could
# not be read (each named on stderr, the archive holds the rest, the manifest
# says errors=1), or the trees did not fit the output disk and the run holds the
# dump, the env file and the manifest alone (agents=skipped); 1 on any other
# failure, with the run directory removed.
#
# Retention runs before the new run is written and touches only directories
# named like a run (otodock-<version>-<YYYYmmdd-HHMMSS>[-N]), removing the
# files this script writes and then the empty directory: a folder holding
# anything else is never emptied. A `.pre-restore-<TS>` folder a failed
# restore left at the root of the tree is not archived.
#
# Schedule it from cron or a systemd timer. A run started while another one
# writes into the same folder is refused (the folder's .backup.lock). The run
# directory holds every secret of the install: keep OTODOCK_BACKUP_DIR as
# private as .env.
set -euo pipefail

umask 077

DB_USER="${POSTGRES_USER:-otodock}"
DB_NAME="${POSTGRES_DB:-otodock}"
OUT_DIR="${OTODOCK_BACKUP_DIR:-./backups}"
RETAIN="${OTODOCK_BACKUP_RETAIN:-14}"
RETAIN_TREES="${OTODOCK_BACKUP_RETAIN_TREES:-3}"
# The compose project the platform's containers belong to. Set explicitly, a
# container is never found by its bare name (a second install on one daemon).
PROJECT="${OTODOCK_COMPOSE_PROJECT:-otodock}"
NAME_FALLBACK=true
[ -z "${OTODOCK_COMPOSE_PROJECT:-}" ] || NAME_FALLBACK=false
DB_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --db-only) DB_ONLY=true ;;
    --apps) ;;   # the apps are part of the tree now; older cron lines keep working
    *) echo "usage: $0 [--db-only]" >&2; exit 1 ;;
  esac
done
for n in "$RETAIN" "$RETAIN_TREES"; do
  [[ "$n" =~ ^[0-9]+$ ]] || { echo "error: OTODOCK_BACKUP_RETAIN and OTODOCK_BACKUP_RETAIN_TREES take whole numbers" >&2; exit 1; }
done

say()  { echo "backup.sh: $*"; }
warn() { echo "backup.sh: $*" >&2; }
fail() { echo "backup.sh: error: $*" >&2; exit 1; }

# A run that stops before its manifest leaves nothing behind.
RUN_DIR=""
COMPLETE=false
COMPUTE_CID=""
HOLD_PID=""
DRAIN_ASKED=false
cleanup() {
  local rc=$?
  compute_back
  if [ -n "$RUN_DIR" ] && [ -d "$RUN_DIR" ] && [ "$COMPLETE" = false ]; then
    find "$RUN_DIR" -mindepth 1 -delete 2>/dev/null || true
    rmdir "$RUN_DIR" 2>/dev/null || true
  fi
  exit "$rc"
}
trap cleanup EXIT

# The compute service stopped for the run starts again and the platform takes
# new work again, in that order and whatever ended the run (a start of a
# running one does nothing). Work comes back to a compute machine that signed
# in: it is waited for, at most COMPUTE_SIGNIN_S seconds. Past that the
# platform takes work again all the same, since held work must not wait on a
# service that may never start: chats, tasks and calls are then refused until
# the compute service signs in, and admins are told.
ADMIN=(/opt/otodock/proxy/venv/bin/python /opt/otodock/proxy/admin_cli.py)
COMPUTE_SIGNIN_S=120
take_work_again() {
  $DRAIN_ASKED || return 0
  docker exec "$PROXY_CID" "${ADMIN[@]}" resume >/dev/null 2>&1 || true
  DRAIN_ASKED=false
}
compute_back() {
  # First, and waited for: nobody asks for the hold again once work comes back.
  [ -z "$HOLD_PID" ] || { kill "$HOLD_PID" 2>/dev/null || true; wait "$HOLD_PID" 2>/dev/null || true; HOLD_PID=""; }
  if [ -n "$COMPUTE_CID" ]; then
    if ! docker start "$COMPUTE_CID" >/dev/null 2>&1; then
      warn "the compute service did not start again: docker compose start otodock-compute"
    elif $DRAIN_ASKED && ! docker exec "$PROXY_CID" "${ADMIN[@]}" compute --wait "$COMPUTE_SIGNIN_S" >/dev/null 2>&1; then
      warn "the compute service did not sign in within $COMPUTE_SIGNIN_S seconds: chats, tasks and calls are refused until it does. To see why, run: docker compose logs otodock-compute"
    fi
    COMPUTE_CID=""
  fi
  take_work_again
}

# The server ends a hold nobody asks for again (drain.HELD_TTL_S), and a copy
# can outlast it: the drain is asked again every HOLD_RENEW_S seconds until
# compute_back stops the asking. Each step runs behind `wait`, so a TERM ends
# the loop and its step at once: a sleep left behind would keep the output
# and the lock of the run open. A killed script runs no trap: the loop ends
# with it, and the server's own lapse then ends the hold.
# OTODOCK_BACKUP_TEST_HOLD_RENEW_S is the tests' stand-in for the five minutes.
HOLD_RENEW_S="${OTODOCK_BACKUP_TEST_HOLD_RENEW_S:-300}"
renew_hold() {
  trap 'kill "${!-}" 2>/dev/null; exit 0' TERM
  while :; do
    sleep "$HOLD_RENEW_S" & wait "$!" || exit 0
    kill -0 "$$" 2>/dev/null || exit 0
    docker exec "$PROXY_CID" "${ADMIN[@]}" drain --quiet & wait "$!" || true
  done
}

# The platform's own container for a compose service: the named project's
# (by label), else, when no project was named, one whose WHOLE name is the
# bare-metal container_name. Never a name substring: a community MCP
# container is named otodock-<install_id>-mcp-<manifest name>, free text.
# More than one match is refused, never guessed. -a includes a stopped one.
# OTODOCK_POSTGRES_CONTAINER (a name or an id) names the Postgres outright: an
# install whose Postgres is not its project's otodock-postgres service (a
# bare-metal checkout beside a compose stack on one daemon).
platform_container() {  # <compose service> [<bare-metal container_name>] [-a]
  local ids all="" st
  if [ "$1" = otodock-postgres ] && [ -n "${OTODOCK_POSTGRES_CONTAINER:-}" ]; then
    st="$(docker inspect -f '{{.Id}} {{.State.Status}}' "$OTODOCK_POSTGRES_CONTAINER" 2>/dev/null)" \
      || { echo "error: OTODOCK_POSTGRES_CONTAINER=$OTODOCK_POSTGRES_CONTAINER is not a container on this daemon" >&2; return 1; }
    [ "${st#* }" = running ] || { echo "error: OTODOCK_POSTGRES_CONTAINER=$OTODOCK_POSTGRES_CONTAINER is not running (${st#* })" >&2; return 1; }
    printf '%s' "${st%% *}"
    return 0
  fi
  [ "${3:-}" = "-a" ] && all="-a"
  ids="$(docker ps $all --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=$1" --format '{{.ID}}')"
  if [ -z "$ids" ] && [ -n "${2:-}" ] && [ "$NAME_FALLBACK" = true ]; then
    ids="$(docker ps $all --filter "name=^/?${2}\$" --format '{{.ID}}')"
  fi
  if [ "$(printf '%s\n' "$ids" | grep -c .)" -gt 1 ]; then
    echo "error: more than one container is the platform's $1 ($(printf '%s' "$ids" | tr '\n' ' ')); refusing to guess, stop the extra one" >&2
    return 1
  fi
  printf '%s' "$ids"
}

# The value of the last active KEY= line of an env file (quotes and a
# trailing comment dropped); fails when there is none.
env_value() { # env_value <KEY> <file>
  awk -v k="$1" -v q="'" '
    { line = $0; sub(/\r$/, "", line); sub(/^[ \t]*/, "", line); sub(/^export[ \t]+/, "", line)
      if (match(line, "^" k "[ \t]*=")) { v = substr(line, RLENGTH + 1); found = 1 } }
    END {
      if (!found) exit 1
      sub(/^[ \t]+/, "", v)
      f = substr(v, 1, 1)
      if ((f == "\"" || f == q) && length(v) > 1 && index(substr(v, 2), f)) {
        v = substr(v, 2); v = substr(v, 1, index(v, f) - 1)
      } else {
        sub(/[ \t]+#.*$/, "", v); sub(/[ \t]+$/, "", v)
      }
      print v
    }' "$2"
}

# The one version rule (storage/schema.py::parse_version): X.Y.Z, X.Y.Z-rcN
# older than it, X.Y.Z-dev older than every rc; anything else is unknown.
VERSION_RE='^v?[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+|-dev)?$'

# --- the containers and the topology -----------------------------------------
PG_CID="$(platform_container otodock-postgres otodock-postgres)" || exit 1
[ -n "$PG_CID" ] || fail "no running otodock-postgres container found (project $PROJECT; is the stack up?)"
PROXY_CID="$(platform_container otodock-proxy "" -a)" || exit 1
PROXY_RUNNING=false
IMAGE=""
if [ -n "$PROXY_CID" ]; then
  [ "$(docker inspect -f '{{.State.Status}}' "$PROXY_CID" 2>/dev/null)" = running ] && PROXY_RUNNING=true
  # The image by id: a tag can move under a stopped container (a rebuild).
  IMAGE="$(docker inspect -f '{{.Image}}' "$PROXY_CID" 2>/dev/null || true)"
fi
TOPOLOGY=host
[ -n "$PROXY_CID" ] && [ -z "${OTODOCK_AGENTS_DIR:-}" ] && TOPOLOGY=docker

# Where this script lives: the install folder is the one above scripts/, or
# the script's own folder when it sits beside docker-compose.yml.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
[ -f "$SCRIPT_DIR/docker-compose.yml" ] && INSTALL_DIR="$SCRIPT_DIR"

# A path inside the proxy container, as the running container or a one-off
# on its image sees it. `in_proxy <cmd...>` runs a command there.
in_proxy() {
  if [ "$PROXY_RUNNING" = true ]; then
    docker exec "$PROXY_CID" "$@"
  else
    docker run --rm --network none --entrypoint "$1" "$IMAGE" "${@:2}"
  fi
}
# The same with a volume mounted read-only and the program on stdin.
in_proxy_volume() { # in_proxy_volume <volume> <path in container> <cmd...>
  local vol="$1" path="$2"; shift 2
  if [ "$PROXY_RUNNING" = true ]; then
    docker exec -i "$PROXY_CID" "$@"
  else
    docker run --rm -i --network none -v "$vol:$path:ro" --entrypoint "$1" "$IMAGE" "${@:2}"
  fi
}
mount_name() { # mount_name <destination>: the volume name mounted there
  docker inspect -f '{{range .Mounts}}{{if eq .Destination "'"$1"'"}}{{.Name}}{{end}}{{end}}' "$PROXY_CID" 2>/dev/null || true
}
mount_source() { # mount_source <destination>: the host path bind-mounted there
  docker inspect -f '{{range .Mounts}}{{if eq .Destination "'"$1"'"}}{{.Source}}{{end}}{{end}}' "$PROXY_CID" 2>/dev/null || true
}

# --- the env file ------------------------------------------------------------
ENV_FILE=""
ENV_READER=""   # how to read it: "host" (cat) or "docker" (through the container)
if [ "$TOPOLOGY" = docker ]; then
  ENV_FILE="$(mount_source /opt/otodock/config.env)"
  if [ -n "$ENV_FILE" ]; then
    if [ -r "$ENV_FILE" ]; then ENV_READER=host; else ENV_READER=docker; fi
  fi
else
  for f in "${OTODOCK_ENV_FILE:-}" "${PLATFORM_CONFIG_DIR:-}/config.env" \
           "$INSTALL_DIR/config.env" "$INSTALL_DIR/.env" ./config.env ./.env; do
    case "$f" in /config.env|/.env|"") continue ;; esac
    if [ -f "$f" ]; then ENV_FILE="$f"; ENV_READER=host; break; fi
  done
fi
read_env_file() {
  if [ "$ENV_READER" = docker ]; then
    if [ "$PROXY_RUNNING" = true ]; then
      docker exec "$PROXY_CID" cat /opt/otodock/config.env
    else
      docker run --rm --network none -v "$ENV_FILE:/env:ro" --entrypoint cat "$IMAGE" /env
    fi
  else
    cat "$ENV_FILE"
  fi
}
if [ -z "$ENV_FILE" ]; then
  if [ "$DB_ONLY" = true ]; then
    warn "no env file found: the backup holds the database alone, and its credentials need the install's JWT_SECRET and CREDENTIAL_ENCRYPTION_KEY to open"
  else
    fail "no env file found (the proxy container's config.env mount on Docker; OTODOCK_ENV_FILE, \$PLATFORM_CONFIG_DIR/config.env or the install's config.env or .env on bare metal): a backup without it cannot restore the install's credentials. Set OTODOCK_ENV_FILE, or pass --db-only."
  fi
fi

# --- the trees ---------------------------------------------------------------
# The sessions tree holds the connected OAuth accounts' token files, the only
# copy of them (the account rows in the database name the account, the file
# holds the tokens), beside the session index and the build outputs the boot
# rebuilds. Absent (a volume the proxy does not mount, a directory this host
# does not have): skipped with a note, the rest of the backup unchanged.
AGENTS_DIR=""
AGENTS_VOL=""
KEYS_DIR=""
KEYS_VOL=""
SESSIONS_DIR=""
SESSIONS_VOL=""
if [ "$DB_ONLY" = false ]; then
  if [ "$TOPOLOGY" = docker ]; then
    AGENTS_VOL="$(mount_name /opt/otodock/agents)"
    KEYS_VOL="$(mount_name /opt/otodock/mcps/custom/ssh-hosts/keys)"
    SESSIONS_VOL="$(mount_name /opt/otodock/sessions)"
    [ -n "$AGENTS_VOL" ] || fail "the proxy container mounts no volume at /opt/otodock/agents"
    [ -n "$SESSIONS_VOL" ] || warn "the proxy container mounts no volume at /opt/otodock/sessions: the connected OAuth accounts' tokens are not in this backup"
  else
    AGENTS_DIR="${OTODOCK_AGENTS_DIR:-${PLATFORM_DATA_DIR:-.}/agents}"
    { [ -d "$AGENTS_DIR" ] && [ -r "$AGENTS_DIR" ] && [ -x "$AGENTS_DIR" ]; } || fail "agents directory not readable at $AGENTS_DIR (set OTODOCK_AGENTS_DIR)"
    KEYS_DIR="${OTODOCK_SSH_KEYS_DIR:-$INSTALL_DIR/mcps/custom/ssh-hosts/keys}"
    SESSIONS_DIR="${OTODOCK_SESSIONS_DIR:-${PLATFORM_DATA_DIR:-.}/sessions}"
    if [ ! -d "$SESSIONS_DIR" ]; then
      warn "no sessions directory at $SESSIONS_DIR (set OTODOCK_SESSIONS_DIR): the connected OAuth accounts' tokens are not in this backup"
      SESSIONS_DIR=""
    fi
  fi
fi

# --- the version and the fingerprints -----------------------------------------
psql_value() { # psql_value <sql>: one value from the platform database, '' on any failure
  docker exec "$PG_CID" psql -U "$DB_USER" -d "$DB_NAME" -tAc "$1" 2>/dev/null || true
}
VERSION="$(psql_value "SELECT last_migrated_by FROM schema_meta WHERE id = 1")"
if ! [[ "$VERSION" =~ $VERSION_RE ]]; then
  VERSION=""
  if [ "$TOPOLOGY" = docker ] && [ -n "$IMAGE" ]; then
    VERSION="$(in_proxy sed -nE 's/^[[:space:]]*OTODOCK_VERSION[[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\1/p' /opt/otodock/VERSIONS.md 2>/dev/null | head -n1 || true)"
  elif [ -f "$INSTALL_DIR/VERSIONS.md" ]; then
    VERSION="$(sed -nE 's/^[[:space:]]*OTODOCK_VERSION[[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\1/p' "$INSTALL_DIR/VERSIONS.md" | head -n1)"
  fi
  [[ "$VERSION" =~ $VERSION_RE ]] || VERSION=unknown
fi
VERSION="${VERSION#v}"
KEYS_JSON="$(psql_value "SELECT value FROM platform_settings WHERE key = 'credential_key_fingerprints'")"
FLOOR_JSON="$(psql_value "SELECT value FROM platform_settings WHERE key = 'secret_floor_fingerprints'")"

# --- the output directory --------------------------------------------------------
mkdir -p "$OUT_DIR"
OUT_ABS="$(cd "$OUT_DIR" && pwd -P)"
if [ -n "$AGENTS_DIR" ]; then
  case "$OUT_ABS/" in "$(cd "$AGENTS_DIR" && pwd -P)"/*) fail "OTODOCK_BACKUP_DIR is inside the agents directory: the env file would be readable by agents and synced to remote machines, and the archive would hold itself" ;; esac
  # The bare-metal compute unit reads the whole install folder but its
  # backups/ folder (proxy/compute.service, InaccessiblePaths).
  _install="$(cd "$INSTALL_DIR" && pwd -P)"
  case "$OUT_ABS/" in
    "$_install/backups/"*) ;;
    "$_install"/*) warn "$OUT_DIR is inside the install folder, where the platform's own sessions can read it. Keep backups in $_install/backups, or outside the install folder." ;;
  esac
fi
# One backup of the tree at a time into a folder. A second run beside the
# first would still copy when the first starts the compute service again, and
# so read a live compute machine. A database-only run reads no tree and stops
# nothing: it takes no lock and waits for none. The lock is the open file: it
# ends with the run, however the run ends. Status 40 is flock's answer for a
# lock another run holds and for nothing else: a host with no flock, or whose
# flock fails for another reason, backs up without a lock.
_lock=0
[ "$DB_ONLY" = true ] || { exec 9>>"$OUT_DIR/.backup.lock" && flock -n -E 40 9; } 2>/dev/null || _lock=$?
[ "$_lock" != 40 ] || fail "Another backup is running. Wait for it to finish, then run this one again."
if [ "$(stat -c '%a' "$OUT_DIR" 2>/dev/null || echo '')" != "700" ]; then
  warn "$OUT_DIR is not 0700: backups there are readable by other accounts; chmod 700 it"
fi

# --- retention, before the new run -----------------------------------------------------
# Runs first, so the space it frees is there for the tree, and counts the run
# about to be written as the newest: the N newest complete runs stay whole
# (RETAIN), the trees of the runs past RETAIN_TREES go (the dump, the env file
# and the manifest stay, the manifest marks the agents, ssh_keys and sessions
# trees pruned). Only a directory
# named like a run is a candidate, and pruning removes the files this script
# writes and then the empty directory, so a folder holding anything else
# survives (a backup folder in $HOME or /srv has siblings). A run directory
# without a manifest older than a day (a run killed before its trap) goes the
# same way. The flat files of releases before 1.8.0 (otodock-<db>-*.sql.gz,
# otodock-apps-*.tar.gz) are never counted or pruned. Ordered by the time in
# the directory name (pruning a tree touches the directory, so its mtime
# would say "newest"). The awk below spells the digit classes out: mawk, the
# default awk of Debian and Ubuntu, has no interval expressions ({8}), and a
# match that never matches would sort every run as "0" and prune by name.
RUN_RE='^otodock-(unknown|v?[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+|-dev)?)-[0-9]{8}-[0-9]{6}(-[0-9]+)?$'
RUN_FILES="db.sql.gz env agents.tar.gz ssh-keys.tar.gz sessions.tar.gz manifest"
remove_run() { # remove_run <dir>: the files this script writes, then the directory when empty
  local f
  for f in $RUN_FILES; do rm -f "$1/$f" "$1/$f.part"; done
  rmdir "$1" 2>/dev/null || warn "$1 holds files this script did not write: left in place"
}
prune_runs() {
  local runs dir tree i=1
  runs="$(find "$OUT_DIR" -mindepth 1 -maxdepth 1 -type d -name 'otodock-*' 2>/dev/null \
    | awk 'BEGIN { d = "[0-9]"; re = d d d d d d d d "-" d d d d d d "(-[0-9]+)?$" }
           { t = $0; if (match(t, re)) t = substr(t, RSTART); else t = "0"; print t " " $0 }' \
    | sort -r | awk '{ $1 = ""; sub(/^ /, ""); print }')"
  while IFS= read -r dir; do
    [ -n "$dir" ] || continue
    [[ "$(basename "$dir")" =~ $RUN_RE ]] || continue
    if [ ! -f "$dir/manifest" ]; then
      [ -z "$(find "$dir" -maxdepth 0 -mtime +0 2>/dev/null)" ] || remove_run "$dir"
      continue
    fi
    i=$((i + 1))
    if [ "$RETAIN" -gt 0 ] && [ "$i" -gt "$RETAIN" ]; then
      remove_run "$dir"
      continue
    fi
    if [ "$RETAIN_TREES" -gt 0 ] && [ "$i" -gt "$RETAIN_TREES" ]; then
      for tree in agents ssh-keys sessions; do
        if [ -f "$dir/$tree.tar.gz" ]; then
          rm -f "$dir/$tree.tar.gz"
          sed -i "s/^$(printf '%s' "$tree" | tr - _)=present$/$(printf '%s' "$tree" | tr - _)=pruned/" "$dir/manifest"
        fi
      done
    fi
  done <<< "$runs"
}
prune_runs

# The free-space preflight: the trees, uncompressed, against the output disk.
# Trees that do not fit are not written: the run holds the dump, the env
# file and the manifest (agents=skipped) and exits 3, so a nightly line still
# leaves the database behind.
tree_kib() { # tree_kib <volume or ''> <path in the container> <host dir or ''>: du -sk, '' when absent or on failure
  if [ "$TOPOLOGY" = docker ]; then
    [ -z "$1" ] || in_proxy_volume "$1" "$2" du -sk "$2" 2>/dev/null </dev/null | awk '{print $1}' || true
  else
    [ -z "$3" ] || du -sk "$3" 2>/dev/null | awk '{print $1}' || true
  fi
}
TREES_SKIPPED=""
if [ "$DB_ONLY" = false ]; then
  NEED_K="$(tree_kib "$AGENTS_VOL" /opt/otodock/agents "$AGENTS_DIR")"
  SESSIONS_K="$(tree_kib "$SESSIONS_VOL" /opt/otodock/sessions "$SESSIONS_DIR")"
  if [[ "$NEED_K" =~ ^[0-9]+$ ]] && [[ "$SESSIONS_K" =~ ^[0-9]+$ ]]; then NEED_K=$((NEED_K + SESSIONS_K)); fi
  FREE_K="$(df -Pk "$OUT_DIR" 2>/dev/null | awk 'NR == 2 { print $4 }' || true)"
  if [[ "$NEED_K" =~ ^[0-9]+$ ]] && [[ "$FREE_K" =~ ^[0-9]+$ ]] && [ "$NEED_K" -gt "$FREE_K" ]; then
    TREES_SKIPPED="not enough free space in $OUT_DIR for the trees ($((NEED_K / 1024)) MB, $((FREE_K / 1024)) MB free): the database, the env file and the manifest are written, the trees are not. Free space, point OTODOCK_BACKUP_DIR elsewhere, or pass --db-only."
    warn "$TREES_SKIPPED"
  fi
fi

TS="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$OUT_DIR/otodock-$VERSION-$TS"
_n=1
while ! mkdir "$RUN_DIR" 2>/dev/null; do
  _n=$((_n + 1))
  [ "$_n" -le 20 ] || fail "cannot create $RUN_DIR"
  RUN_DIR="$OUT_DIR/otodock-$VERSION-$TS-$_n"
done
say "writing $RUN_DIR (OtoDock $VERSION, $TOPOLOGY)"

# --- the compute machine -----------------------------------------------------------
# A backup never reads a live compute machine: the server waits for the open
# work (its drain) and holds new work, the compute service stops, and at the
# end it starts and signs in again before the server takes new work
# (compute_back). Chats, tasks and calls wait meanwhile, as in an update, for
# the whole copy (renew_hold). One that does not stop fails the run. A
# database-only run reads no tree and leaves it running.
_compute="$(platform_container otodock-compute "" || true)"
if [ -n "$_compute" ] && [ "$TOPOLOGY" = docker ] && [ "$DB_ONLY" = false ] \
    && [ "$(docker inspect -f '{{.State.Status}}' "$_compute" 2>/dev/null)" = running ]; then
  warn "waiting for open work, then stopping the compute service for the backup"
  # Set before the wait: an exit inside it starts the service and takes work again.
  COMPUTE_CID="$_compute"
  if $PROXY_RUNNING; then
    DRAIN_ASKED=true
    docker exec "$PROXY_CID" "${ADMIN[@]}" drain --quiet >&2 || true
    renew_hold >/dev/null 2>&1 9>&- &
    HOLD_PID=$!
  fi
  docker stop -t 120 "$_compute" >/dev/null \
    || fail "the compute service did not stop: a backup never reads a live compute machine"
fi

# --- the database ----------------------------------------------------------------
DB_OUT="$RUN_DIR/db.sql.gz"
docker exec "$PG_CID" pg_dump -U "$DB_USER" --clean --if-exists "$DB_NAME" | gzip > "$DB_OUT.part"
mv "$DB_OUT.part" "$DB_OUT"
say "database: $(du -h "$DB_OUT" | cut -f1)"

# --- the env file -----------------------------------------------------------------
HAS_ENV=0
if [ -n "$ENV_FILE" ]; then
  read_env_file > "$RUN_DIR/env.part"
  mv "$RUN_DIR/env.part" "$RUN_DIR/env"
  chmod 600 "$RUN_DIR/env"
  HAS_ENV=1
  say "env file: $ENV_FILE"
fi

# --- the trees -------------------------------------------------------------------
# One Python program, run where the tree is readable (this host, or the proxy
# container): a gzip'd tar streamed to stdout. Every SQLite database (found by
# its header) is copied through the SQLite backup API, consistent while it is
# written (a plain copy of a live WAL database is corrupt), its -wal/-shm/-journal
# left out. Nothing is followed or waited on: the walk holds one folder handle per
# level and opens each folder and file from its parent's handle with no link
# followed, a file read only once it is a regular file; links are kept as links,
# FIFOs and devices skipped. SQLite opens by name, so its open runs in a child
# with a time limit, and the files it opened must be the ones the walk holds.
# Per-file errors are counted (exit 3, the archive whole), and so is a folder
# that cannot be read; a file that vanished or changed between the walk and the
# read, and an entry whose path is too long for restore.sh to write back, are
# skipped and counted; only a failure of the stream itself, and a file that
# shrinks while it is read, end the program mid-archive. A `.pre-restore-<TS>`
# folder at the root is left out.
TREE_PY=$(cat <<'PY'
import sys

# Run as `python3 -`, and in each folder it walks: no folder it reads is ever
# a place to import from.
sys.path[:] = [p for p in sys.path if p not in ("", ".")]

import errno  # noqa: E402
import os  # noqa: E402
import resource  # noqa: E402
import select  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import sqlite3  # noqa: E402
import stat  # noqa: E402
import struct  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

root = os.path.abspath(sys.argv[1])
out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz")
errors = skipped = n_db = n_files = 0
MAGIC = b"SQLite format 3\x00"
SIDES = ("-wal", "-shm", "-journal")
# The first bytes of SQLite's side files: a WAL (either byte order), a rollback
# journal, a WAL index (its version, either byte order).
SIDE_MAGIC = (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83", b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7",
              struct.pack("<I", 3007000), struct.pack(">I", 3007000))
LEAF = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# SQLite's open and first read: milliseconds, or "locked" after its own 5 s.
OPEN_LIMIT_S = float(os.environ.get("OTODOCK_BACKUP_TEST_OPEN_LIMIT_S") or 30)
CHANGED = 4  # the child's status when SQLite opened a file other than the one checked
# restore.sh writes a member back by its whole path, which the system caps at
# 4,096 bytes: room is left for the folder a restore puts the tree in.
LONGEST = 3840
scratch = tempfile.mkdtemp(prefix="otodock-backup-")
# One descriptor per folder level: as many as the host allows.
_, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
try:
    resource.setrlimit(resource.RLIMIT_NOFILE, (_hard, _hard))
except (ValueError, OSError):
    pass


def skip(rel, why=""):
    global skipped
    skipped += 1
    if why:
        print(f"skip ({why}): {os.path.join(root, rel)}", file=sys.stderr)


def fail(rel, what, detail):
    global errors
    errors += 1
    print(f"{what} {os.path.join(root, rel)}: {detail}", file=sys.stderr)


def leaf(name, folder=None):
    """``(fd, fstat)`` of the regular file ``name`` (in the folder ``folder``
    holds, or a path), opened with no link followed and without waiting, or
    None when something else is there. Read from the descriptor only."""
    fd = os.open(name, LEAF, dir_fd=folder)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None
        os.set_blocking(fd, True)
        return fd, st
    except BaseException:
        os.close(fd)
        raise


def head(name, folder):
    """The first 16 bytes of the regular file ``name``, b"" when not one."""
    try:
        held = leaf(name, folder)
    except OSError:
        return b""
    if held is None:
        return b""
    try:
        return os.pread(held[0], 16, 0)
    except OSError:
        return b""
    finally:
        os.close(held[0])


def side_left_out(folder, name, base):
    """Whether the side file ``name`` belongs to the database ``base``, copied
    with it. A link at ``base`` is never followed: the side file is left out
    when its own header is SQLite's (SQLite below 3.32 puts it beside the link)."""
    try:
        st = os.stat(base, dir_fd=folder, follow_symlinks=False)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return head(name, folder).startswith(SIDE_MAGIC)
    return stat.S_ISREG(st.st_mode) and head(base, folder) == MAGIC


def copy_out(fd, dest):
    """The bytes of the open file ``fd`` into a new private file ``dest``."""
    to = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        offset = 0
        while chunk := os.pread(fd, 1 << 20, offset):
            view = memoryview(chunk)
            while view:
                view = view[os.write(to, view):]
            offset += len(chunk)
    finally:
        os.close(to)


def open_now():
    """This process's open descriptors: number -> fstat, read by number and
    never opened again (closing a descriptor of a database's file would drop
    SQLite's locks). The listing's own descriptor is closed by then."""
    found = {}
    for n in os.listdir("/proc/self/fd"):
        try:
            found[n] = os.fstat(int(n))
        except OSError:
            continue
    return found


def database_child(path, folder, name, fd, work, w):
    """In a forked child: SQLite's copy of the live database ``path`` into
    ``work/copy.db``. It tells the parent "o" once SQLite opened and read it
    once: holding that read, the copy opens no further file of the tree. Every
    file SQLite opened must be the one the walk holds (``fd``, open until
    SQLite's connection closes) or a side file that is a regular file at its
    name now, else Changed. Read-only, never immutable=1 (that returns an empty
    database while a writer holds it), percent-encoded. Two kinds cannot be
    opened read-only where they are: a WAL database in a read-only folder (a
    stopped proxy's volume, :ro), which needs its -shm, and a database whose
    writer stopped in the middle of a write, which SQLite must first put right
    from its -journal. For both, the side files, the journal first, and then
    the database's bytes are copied to ``work`` and opened there."""
    before = set(open_now())
    try:
        live = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
        live.execute("BEGIN")
        live.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.OperationalError as e:
        if "readonly" not in str(e).lower() and "unable to open" not in str(e).lower():
            raise
        os.write(w, b"o")
        staged = os.path.join(work, "staged.db")
        # The journal first: SQLite puts a database its writer left half
        # written right from it when it opens the copy.
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                held = leaf(name + suffix, folder)
            except FileNotFoundError:
                held = None
            if held is not None:
                try:
                    copy_out(held[0], staged + suffix)
                finally:
                    os.close(held[0])
        copy_out(fd, staged)
        live = sqlite3.connect(staged)
    else:
        os.write(w, b"o")
        checked = os.fstat(fd)
        allowed = {(checked.st_dev, checked.st_ino)}
        for suffix in SIDES:
            try:
                side = os.stat(name + suffix, dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(side.st_mode):
                allowed.add((side.st_dev, side.st_ino))
        opened = [(st.st_dev, st.st_ino) if stat.S_ISREG(st.st_mode) else None
                  for n, st in open_now().items() if n not in before]
        if (checked.st_dev, checked.st_ino) not in opened or not set(opened) <= allowed:
            raise Changed()
    copy = sqlite3.connect(os.path.join(work, "copy.db"))
    with copy:
        live.backup(copy)
    live.close()
    copy.close()


class Changed(Exception):
    pass


def copy_database(path, folder, name, fd, work):
    """``database_child`` in a child, waited for at most OPEN_LIMIT_S until it
    is open, then for the whole copy: ``(0 | CHANGED | 1, error text)``."""
    try:
        r, w = os.pipe()
        pid = os.fork()
    except OSError as e:
        return 1, str(e)
    if pid == 0:
        code = 1
        try:
            os.close(r)
            # fd 1 is the parent's archive stream, its buffers came with the
            # fork: the child writes nothing there and never leaves through the
            # interpreter, which would flush them a second time.
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 1)
            os.close(null)
            database_child(path, folder, name, fd, work, w)
            code = 0
        except Changed:
            code = CHANGED
        except BaseException as e:
            try:
                os.write(w, b"\n" + str(e).encode(errors="replace"))
            except BaseException:
                pass
        finally:
            os._exit(code)
    os.close(w)
    said = b""
    try:
        ready = select.poll()
        ready.register(r, select.POLLIN)
        if not ready.poll(OPEN_LIMIT_S * 1000):
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            return 1, f"it did not open within {OPEN_LIMIT_S:g} seconds"
        while chunk := os.read(r, 65536):
            said += chunk
    finally:
        os.close(r)
    _, status = os.waitpid(pid, 0)
    text = said.decode(errors="replace").lstrip("o").strip()
    code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
    if code == CHANGED or (code == 0 and not text):
        return code, ""
    return 1, text or f"the copy ended with status {status}"


def add_sqlite(folder, name, rel, st, fd):
    """A database: its side files must be regular files, then SQLite copies it
    in a child; one whose files changed under SQLite's open is skipped."""
    global n_db
    for suffix in SIDES:
        try:
            side = os.stat(name + suffix, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as e:
            return fail(rel, "error copying database", str(e))
        if not stat.S_ISREG(side.st_mode):
            return fail(rel, "error copying database", f"{name}{suffix} is not a regular file")
    work = tempfile.mkdtemp(dir=scratch)
    try:
        code, problem = copy_database(os.path.join(root, rel), folder, name, fd, work)
        if code == CHANGED:
            return skip(rel, "changed while being read")
        if code:
            return fail(rel, "error copying database", problem)
        held = leaf(os.path.join(work, "copy.db"))
        with os.fdopen(held[0], "rb") as fh:
            info = out.gettarinfo(arcname="./" + rel, fileobj=fh)
            info.mode = st.st_mode & 0o7777
            info.mtime = st.st_mtime
            info.uid, info.gid = st.st_uid, st.st_gid
            out.addfile(info, fh)
        n_db += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def add_regular(folder, name, rel, st):
    """A regular file the walk listed, opened through ``leaf``: a database is
    copied through SQLite, any other file archived from the descriptor."""
    global n_files
    try:
        held = leaf(name, folder)
    except FileNotFoundError:
        return skip(rel)
    except OSError as e:
        if e.errno == errno.ELOOP:
            return skip(rel, "changed while being read")
        return fail(rel, "error reading", str(e))
    if held is None:
        return skip(rel, "not a regular file")
    fd, now = held
    try:
        is_db = os.pread(fd, 16, 0) == MAGIC
    except OSError:
        is_db = False
    if is_db:
        try:
            return add_sqlite(folder, name, rel, st, fd)
        finally:
            os.close(fd)
    with os.fdopen(fd, "rb") as fh:
        if now.st_size != st.st_size:
            return skip(rel, "changed while being read")
        out.addfile(out.gettarinfo(arcname="./" + rel, fileobj=fh), fh)
    n_files += 1


# A scope's engine folder (<agent>/workspace/.claude, <agent>/users/<u>/.codex,
# an outside caller's <agent>/externals/<channel>/<caller>/.codex): its sign-in
# file and its sessions' homes (own/) hold session tokens or a key, and every
# start writes them again, so a backup holds none; nor a temporary sign-in
# file a killed Codex save left (.auth-XXXXXX).
SIGN_IN = {".claude": ".credentials.json", ".codex": "auth.json"}


def engine_folder(rel):
    parts = rel.split("/")
    if len(parts) == 3 and parts[1] == "workspace" and parts[2] in SIGN_IN:
        return parts[2]
    if len(parts) == 4 and parts[1] == "users" and parts[3] in SIGN_IN:
        return parts[3]
    if len(parts) == 5 and parts[1] == "externals" and parts[4] in SIGN_IN:
        return parts[4]
    if len(parts) == 6 and parts[1] == "externals" and parts[3] == "_ephemeral" and parts[5] in SIGN_IN:
        return parts[5]
    return ""


def enter(folder, rel):
    """Archive the folder ``folder`` holds (``rel``, "" for the root) and its
    files, links and other entries in name order, each looked at from the
    folder's handle (tarfile reads the folder and its links in it, the working
    folder); its folders' names to walk next, or None when it cannot be
    entered (a failure of the archive stream is never caught: it ends the run)."""
    try:
        os.fchdir(folder)
        names = sorted(os.listdir(folder))
    except PermissionError:
        return fail(rel, "error reading", "the folder cannot be read")
    except OSError as e:
        return fail(rel, "error reading", str(e))
    if rel:
        out.addfile(out.gettarinfo(".", arcname="./" + rel))
    engine = engine_folder(rel)
    folders = []
    for name in names:
        if engine and (name in ("own", SIGN_IN[engine])
                       or (engine == ".codex" and name.startswith(".auth-"))):
            continue
        sub = f"{rel}/{name}" if rel else name
        if len(os.fsencode(sub)) > LONGEST:
            skip(sub, "path too long to restore")
            continue
        try:
            st = os.stat(name, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            skip(sub)
            continue
        except OSError as e:
            fail(sub, "error reading", str(e))
            continue
        if stat.S_ISDIR(st.st_mode):
            if rel or not name.startswith(".pre-restore-"):
                folders.append(name)
        elif stat.S_ISLNK(st.st_mode):
            try:
                info = out.gettarinfo(name, arcname="./" + sub)
            except FileNotFoundError:
                skip(sub)
                continue
            if info is not None and info.issym():
                out.addfile(info)
            else:
                skip(sub, "changed while being read")
        elif not stat.S_ISREG(st.st_mode):
            skip(sub, "not a regular file")
        else:
            base = next((name[: -len(s)] for s in SIDES if name.endswith(s)), None)
            if base is None or not side_left_out(folder, name, base):
                add_regular(folder, name, sub, st)
    return folders


def walk():
    """The tree, one folder handle per level, each folder entered from its
    parent's handle with no link followed (one swapped for a link since the
    listing is skipped; one that cannot be read or is past the descriptors
    left is an error, the top folder too: an empty archive is never whole). The root is opened as given: an admin's agents folder may be
    a link to another disk."""
    top = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    stack = [(top, "", iter(enter(top, "") or []))]
    while stack:
        folder, rel, names = stack[-1]
        name = next(names, None)
        if name is None:
            stack.pop()
            os.close(folder)
            continue
        sub = f"{rel}/{name}" if rel else name
        try:
            fd = os.open(name, FOLDER, dir_fd=folder)
        except FileNotFoundError:
            skip(sub)
            continue
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                skip(sub, "changed while being read")
            elif e.errno == errno.EACCES:
                fail(sub, "error reading", "the folder cannot be read")
            else:
                fail(sub, "error reading", str(e))
            continue
        inner = enter(fd, sub)
        if inner is None:
            os.close(fd)
            continue
        stack.append((fd, sub, iter(inner)))


try:
    walk()
    out.close()
finally:
    shutil.rmtree(scratch, ignore_errors=True)
print(f"{n_files} file(s), {n_db} database(s) copied, {skipped} skipped, {errors} error(s)", file=sys.stderr)
sys.exit(3 if errors else 0)
PY
)

TREE_RC=0
archive_tree() { # archive_tree <out file> <volume or ''> <path in the container> <host dir or ''>
  local out="$1" vol="$2" cpath="$3" hdir="$4" rc=0
  if [ "$TOPOLOGY" = docker ]; then
    printf '%s\n' "$TREE_PY" | in_proxy_volume "$vol" "$cpath" python3 - "$cpath" > "$out.part" || rc=$?
  else
    printf '%s\n' "$TREE_PY" | python3 - "$hdir" > "$out.part" || rc=$?
  fi
  # 0: whole. 3: whole, files missing (named above). Anything else (no
  # python3, the container gone, a crash, a failed write mid-stream) is a cut
  # archive: fail, the trap removes the run directory.
  case "$rc" in
    0|3) mv "$out.part" "$out" ;;
    *) fail "the tree archive failed (exit $rc, see above); no backup kept" ;;
  esac
  [ "$rc" = 0 ] || TREE_RC=3
  say "$(basename "$out"): $(du -h "$out" | cut -f1)"
}

HAS_AGENTS=skipped
HAS_KEYS=skipped
HAS_SESSIONS=skipped
if [ "$DB_ONLY" = false ] && [ -z "$TREES_SKIPPED" ]; then
  archive_tree "$RUN_DIR/agents.tar.gz" "$AGENTS_VOL" /opt/otodock/agents "$AGENTS_DIR"
  HAS_AGENTS=present
  if { [ "$TOPOLOGY" = docker ] && [ -n "$KEYS_VOL" ]; } || { [ "$TOPOLOGY" = host ] && [ -d "$KEYS_DIR" ]; }; then
    archive_tree "$RUN_DIR/ssh-keys.tar.gz" "$KEYS_VOL" /opt/otodock/mcps/custom/ssh-hosts/keys "$KEYS_DIR"
    HAS_KEYS=present
  fi
  if [ -n "$SESSIONS_VOL" ] || [ -n "$SESSIONS_DIR" ]; then
    archive_tree "$RUN_DIR/sessions.tar.gz" "$SESSIONS_VOL" /opt/otodock/sessions "$SESSIONS_DIR"
    HAS_SESSIONS=present
  fi
fi

compute_back

# --- the manifest, last --------------------------------------------------------------
# errors=1 when a file or database could not be copied (the names are on stderr).
TREE_ERRORS=0
[ "$TREE_RC" = 0 ] || TREE_ERRORS=1
{
  echo "otodock_version=$VERSION"
  echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host=$(hostname 2>/dev/null || echo unknown)"
  echo "topology=$TOPOLOGY"
  echo "project=$PROJECT"
  echo "database=$DB_NAME"
  echo "db=db.sql.gz"
  echo "env=$HAS_ENV"
  echo "agents=$HAS_AGENTS"
  echo "ssh_keys=$HAS_KEYS"
  echo "sessions=$HAS_SESSIONS"
  [ -z "$KEYS_JSON" ] || echo "credential_keys=$KEYS_JSON"
  [ -z "$FLOOR_JSON" ] || echo "credential_keys_floor=$FLOOR_JSON"
  echo "errors=$TREE_ERRORS"
  echo "complete=1"
} > "$RUN_DIR/manifest.part"
mv "$RUN_DIR/manifest.part" "$RUN_DIR/manifest"
COMPLETE=true

say "done: $RUN_DIR"
if [ -n "$TREES_SKIPPED" ]; then
  warn "INCOMPLETE: the trees were not archived ($TREES_SKIPPED)"
  exit 3
fi
if [ "$TREE_RC" != 0 ]; then
  warn "INCOMPLETE: a file or database could not be copied (see above); the archive holds the rest"
  exit 3
fi
