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
# not whole: a tree archive was written but one or more files could not be
# copied (each named on stderr, the archive holds the rest, the manifest says
# errors=1), or the trees did not fit the output disk and the run holds the
# dump, the env file and the manifest alone (agents=skipped); 1 on any other
# failure, with the run directory removed.
#
# Retention runs before the new run is written and touches only directories
# named like a run (otodock-<version>-<YYYYmmdd-HHMMSS>[-N]), removing the
# files this script writes and then the empty directory: a folder holding
# anything else is never emptied. A `.pre-restore-<TS>` folder a failed
# restore left at the root of the tree is not archived.
#
# Schedule it from cron or a systemd timer. The run directory holds every
# secret of the install: keep OTODOCK_BACKUP_DIR as private as .env.
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
cleanup() {
  local rc=$?
  if [ -n "$RUN_DIR" ] && [ -d "$RUN_DIR" ] && [ "$COMPLETE" = false ]; then
    find "$RUN_DIR" -mindepth 1 -delete 2>/dev/null || true
    rmdir "$RUN_DIR" 2>/dev/null || true
  fi
  exit "$rc"
}
trap cleanup EXIT

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
    { [ -d "$AGENTS_DIR" ] && [ -r "$AGENTS_DIR" ]; } || fail "agents directory not readable at $AGENTS_DIR (set OTODOCK_AGENTS_DIR)"
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
fi
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
# its header, anywhere) is copied through the SQLite backup API, consistent
# while the app or the engine writes it (a plain copy of a live WAL database is
# corrupt), and its -wal/-shm/-journal siblings are left out; symlinks are kept
# as links, never followed; FIFOs and devices are skipped. Per-file errors
# (unreadable, a database that would not copy) are counted and the program
# exits 3 with a whole archive; a file that vanished or changed size between
# the walk and the read is skipped and counted; only a failure of the stream
# itself ends the program mid-archive (the script then keeps no archive). A
# `.pre-restore-<TS>` folder at the root (a failed restore's set-aside tree,
# ignored by the platform) is left out.
TREE_PY=$(cat <<'PY'
import os
import sqlite3
import stat
import sys
import tarfile
import tempfile

root = os.path.abspath(sys.argv[1])
out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz")
errors = skipped = n_db = n_files = 0
MAGIC = b"SQLite format 3\x00"


def is_sqlite(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == MAGIC
    except OSError:
        return False


def arcname(path):
    return os.path.join(".", os.path.relpath(path, root))


def open_live(path):
    """The live database read-only: never create or modify the file; NOT
    immutable=1 (that returns an empty database while a writer holds it).
    Percent-encoded: a raw '#', '?' or '%' would end the path. A WAL database
    in a read-only directory (a stopped proxy's volume, mounted :ro) cannot
    be opened without creating its -shm file: its bytes and its -wal are
    copied to a scratch folder and opened there."""
    from pathlib import Path
    import shutil
    try:
        con = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con, None
    except sqlite3.OperationalError as e:
        if "readonly" not in str(e).lower() and "unable to open" not in str(e).lower():
            raise
    scratch = tempfile.mkdtemp(prefix="otodock-backup-")
    staged = os.path.join(scratch, os.path.basename(path))
    shutil.copy2(path, staged)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, staged + suffix)
    return sqlite3.connect(staged), scratch


def add_sqlite(path, st):
    global errors, n_db
    scratch = None
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        try:
            live, scratch = open_live(path)
            copy = sqlite3.connect(tmp.name)
            with copy:
                live.backup(copy)
            live.close()
            copy.close()
            info = out.gettarinfo(tmp.name, arcname=arcname(path))
            info.mode = st.st_mode & 0o7777
            info.mtime = st.st_mtime
            info.uid, info.gid = st.st_uid, st.st_gid
            fh = open(tmp.name, "rb")
        except Exception as e:  # one database must not cancel everyone's backup
            errors += 1
            print(f"error copying database {path}: {e}", file=sys.stderr)
            return
        finally:
            if scratch:
                import shutil
                shutil.rmtree(scratch, ignore_errors=True)
        with fh:
            out.addfile(info, fh)
        n_db += 1


def add_file(path, st):
    global errors, skipped, n_files
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        skipped += 1
        return
    except OSError as e:
        errors += 1
        print(f"error reading {path}: {e}", file=sys.stderr)
        return
    with fh:
        now = os.fstat(fh.fileno())
        if now.st_size != st.st_size:
            skipped += 1
            print(f"skip (changed while being read): {path}", file=sys.stderr)
            return
        info = out.gettarinfo(path, arcname=arcname(path), fileobj=fh)
        out.addfile(info, fh)
    n_files += 1


for dirpath, dirnames, filenames in os.walk(root):
    if dirpath == root:
        dirnames[:] = [d for d in dirnames if not d.startswith(".pre-restore-")]
    dirnames.sort()
    filenames.sort()
    if dirpath != root:
        try:
            out.add(dirpath, arcname=arcname(dirpath), recursive=False)
        except FileNotFoundError:
            dirnames[:] = []
            continue
    for name in list(dirnames):
        path = os.path.join(dirpath, name)
        if os.path.islink(path):  # a link to a directory: the link itself, never its target
            dirnames.remove(name)
            try:
                out.add(path, arcname=arcname(path), recursive=False)
            except FileNotFoundError:
                skipped += 1
    for name in filenames:
        path = os.path.join(dirpath, name)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            skipped += 1
            continue
        if stat.S_ISLNK(st.st_mode):
            try:
                out.add(path, arcname=arcname(path), recursive=False)
            except FileNotFoundError:
                skipped += 1
            continue
        if not stat.S_ISREG(st.st_mode):
            skipped += 1
            print(f"skip (not a regular file): {path}", file=sys.stderr)
            continue
        base = next((path[: -len(s)] for s in ("-wal", "-shm", "-journal") if name.endswith(s)), None)
        if base is not None and is_sqlite(base):
            continue  # the database itself is copied consistently
        if is_sqlite(path):
            add_sqlite(path, st)
        else:
            add_file(path, st)
out.close()
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
