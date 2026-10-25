#!/usr/bin/env bash
# Restore an OtoDock install from a backup.sh run directory, or the database
# alone from a bare dump (the Backup and restore page of the documentation).
#
#   scripts/restore.sh ./backups/otodock-1.8.0-20261030-031500      # the database and the trees
#   scripts/restore.sh ./backups/otodock-1.8.0-20261030-031500 --db-only
#   scripts/restore.sh ./backups/otodock-otodock-20260627-031500.sql.gz   # a dump alone (the old shape)
#
# DESTRUCTIVE: the database is replaced whole (its public schema recreated,
# then the dump fed in), and the agents tree, the ssh keys and the sessions
# tree (the connected OAuth accounts' tokens) are replaced by the archives
# (the previous tree is set aside inside the volume until the restore
# succeeds). A run directory without sessions.tar.gz (a backup from before
# the sessions tree was archived, or one whose trees were pruned) restores
# without it and says so. Stop the proxy first; start it after (migrations
# run forward on boot).
#
# Before it changes anything the script refuses:
#   - while the platform runs (the project's proxy container in state
#     running, restarting or paused, or on bare metal the port the env files
#     publish);
#   - a backup taken on a NEWER release than the running images: the server
#     would refuse the database at boot (--allow-newer warns instead; then
#     start the server with OTODOCK_ALLOW_NEWER_DATABASE=true);
#   - a backup whose credentials were encrypted under keys this install
#     cannot read, naming the missing ones (compared by fingerprint between
#     the backup's manifest and what the server recorded at its last boot;
#     --ignore-credential-key restores anyway and every stored credential is
#     unreadable);
#   - an archive whose check fails: every archive is read once in full first
#     (nothing under an archived link, no .., no FIFO or device, a hard link
#     inside the root), and its members must fit the free space of the volume
#     or directory they go into.
# OTODOCK_POSTGRES_CONTAINER (a name or an id) names the Postgres outright, as
# for backup.sh.
#
# Topology-agnostic: the dump goes through `docker exec` into the running
# otodock-postgres container; on the containerised stack the trees go into
# the proxy's volumes through a one-off container on its image (the proxy is
# stopped), on bare metal into the directories on this host (OTODOCK_AGENTS_DIR,
# OTODOCK_SSH_KEYS_DIR, OTODOCK_SESSIONS_DIR, the defaults backup.sh has).
# Setting OTODOCK_AGENTS_DIR always means this host, as for backup.sh.
# OTODOCK_COMPOSE_PROJECT names the compose project (default otodock); set
# explicitly, no container is found by its bare name.
set -euo pipefail

DB_NAME="${POSTGRES_DB:-otodock}"
# The restore role is resolved after the container is found (see below): prefer
# the superuser otodock_admin so the schema recreation and the OWNER TO
# statements all apply; fall back to the app role on a not-yet-upgraded install
# (or T1).
DB_USER=""
PROJECT="${OTODOCK_COMPOSE_PROJECT:-otodock}"
NAME_FALLBACK=true
[ -z "${OTODOCK_COMPOSE_PROJECT:-}" ] || NAME_FALLBACK=false

SRC=""
DB_ONLY=false
ALLOW_NEWER=false
IGNORE_KEY=false
for arg in "$@"; do
  case "$arg" in
    --db-only) DB_ONLY=true ;;
    --allow-newer) ALLOW_NEWER=true ;;
    --ignore-credential-key) IGNORE_KEY=true ;;
    -*) echo "usage: $0 <run directory | dump.sql.gz | dump.sql> [--db-only] [--allow-newer] [--ignore-credential-key]" >&2; exit 1 ;;
    *) SRC="$arg" ;;
  esac
done
if [ -z "$SRC" ] || ! [ -e "$SRC" ]; then
  echo "usage: $0 <run directory | dump.sql.gz | dump.sql> [--db-only] [--allow-newer] [--ignore-credential-key]" >&2
  exit 1
fi

say()  { echo "restore.sh: $*"; }
warn() { echo "restore.sh: $*" >&2; }
fail() { echo "restore.sh: error: $*" >&2; exit 1; }

# The backup: a run directory (its manifest and files) or a bare dump.
MANIFEST=""
DUMP=""
if [ -d "$SRC" ]; then
  [ -f "$SRC/manifest" ] || fail "$SRC has no manifest: not a backup.sh run directory"
  MANIFEST="$SRC/manifest"
  # The dump's name is in the manifest (db.sql.gz from backup.sh; upgrade.sh's
  # snapshot keeps its timestamped name).
  DUMP="$SRC/$(sed -nE 's/^db=(.*)$/\1/p' "$MANIFEST" | head -n1)"
  [ "$DUMP" != "$SRC/" ] || DUMP="$SRC/db.sql.gz"
  [ -f "$DUMP" ] || fail "$SRC has no $(basename "$DUMP")"
else
  case "$SRC" in
    *.sql|*.sql.gz) DUMP="$SRC" ;;
    *) fail "$SRC is neither a run directory nor a .sql or .sql.gz dump" ;;
  esac
fi
manifest_value() { # manifest_value <KEY>: '' when absent
  [ -n "$MANIFEST" ] || return 0
  sed -nE "s/^$1=(.*)$/\1/p" "$MANIFEST" | head -n1
}

# upgrade.sh's snapshot holds the database, .env and the compose files, never
# the trees: trees=none since 1.9.0. One written by 1.8.x says agents=skipped
# and is told apart from a backup.sh run by its shape (its dump keeps its
# timestamped name; every backup.sh run records db.sql.gz, host= and sessions=)
# or by its folder's name.
UPGRADE_SNAPSHOT=false
if [ -n "$MANIFEST" ]; then
  if [ "$(manifest_value trees)" = none ]; then
    UPGRADE_SNAPSHOT=true
  elif [ "$(manifest_value agents)" = skipped ]; then
    _db_name="$(manifest_value db)"
    if { [[ "$_db_name" == otodock-otodock-*.sql.gz ]] && [ -z "$(manifest_value host)" ] \
           && [ -z "$(manifest_value sessions)" ]; } \
        || [[ "$(basename "$SRC")" =~ ^upgrade-[0-9]{8}-[0-9]{6}(-[0-9]+)?$ ]]; then
      UPGRADE_SNAPSHOT=true
    fi
  fi
fi

# The value of the last active KEY= line of an env file (quotes and a trailing
# comment dropped); fails when there is none.
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

# A key's value from the operator's env files: the install folder's (the one
# above scripts/) .env, then its config.env (bare metal keeps PROXY_PORT
# there), then the same two in the current folder. The first regular file
# that sets the key wins; empty when none does.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
[ -f "$SCRIPT_DIR/docker-compose.yml" ] && INSTALL_DIR="$SCRIPT_DIR"
config_value() { # config_value <KEY>
  local f v
  for f in "$INSTALL_DIR/.env" "$INSTALL_DIR/config.env" .env config.env; do
    [ -f "$f" ] || continue
    v="$(env_value "$1" "$f" 2>/dev/null || true)"
    if [ -n "$v" ]; then
      printf '%s\n' "$v"
      return 0
    fi
  done
  return 0
}

# The platform's own container for a compose service: the named project's
# (by label), else, when no project was named, one whose WHOLE name is the
# bare-metal container_name. Never a name substring: a community MCP
# container is named otodock-<install_id>-mcp-<manifest name>, free text.
# More than one match is refused, never guessed. -a includes a stopped one.
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
    echo "error: more than one running container is the platform's $1 ($(printf '%s' "$ids" | tr '\n' ' ')); refusing to guess, stop the extra one" >&2
    return 1
  fi
  printf '%s' "$ids"
}

# The one version rule (storage/schema.py::parse_version). version_cmp prints
# -1, 0 or 1, or "?" when a side is not a release number.
VERSION_RE='^v?[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+|-dev)?$'
version_key() { # version_key <version>: "major minor patch pre n"
  local v="${1#v}" nums pre=0 n=0
  nums="${v%%-*}"
  case "$v" in
    *-dev) pre=-2 ;;
    *-rc*) pre=-1; n="${v##*-rc}" ;;
  esac
  printf '%s %s %s\n' "${nums//./ }" "$pre" "$n"
}
version_cmp() { # version_cmp <A> <B>
  if ! [[ "$1" =~ $VERSION_RE ]] || ! [[ "$2" =~ $VERSION_RE ]]; then echo "?"; return; fi
  local a b i
  read -r -a a <<< "$(version_key "$1")"
  read -r -a b <<< "$(version_key "$2")"
  for i in 0 1 2 3 4; do
    [ "${a[i]}" -lt "${b[i]}" ] && { echo -1; return; }
    [ "${a[i]}" -gt "${b[i]}" ] && { echo 1; return; }
  done
  echo 0
}

# --- the containers and the topology -----------------------------------------
PG_CID="$(platform_container otodock-postgres otodock-postgres)" || exit 1
[ -n "$PG_CID" ] || fail "no running otodock-postgres container found (project $PROJECT; is the stack up?)"
PROXY_CID="$(platform_container otodock-proxy "" -a)" || exit 1
PROXY_RUNNING=false
PROXY_STATE=""
IMAGE=""
TOPOLOGY=host
if [ -n "$PROXY_CID" ]; then
  # Setting OTODOCK_AGENTS_DIR always means this host, as for backup.sh.
  [ -z "${OTODOCK_AGENTS_DIR:-}" ] && TOPOLOGY=docker
  PROXY_STATE="$(docker inspect -f '{{.State.Status}}' "$PROXY_CID" 2>/dev/null || true)"
  # A crash-looping proxy (restarting) comes back mid-restore and migrates a
  # half-restored database; a paused one resumes: both count as running.
  case "$PROXY_STATE" in running|restarting|paused) PROXY_RUNNING=true ;; esac
  # The image by id: a tag can move under a stopped container (a rebuild).
  IMAGE="$(docker inspect -f '{{.Image}}' "$PROXY_CID" 2>/dev/null || true)"
fi
mount_name() { # mount_name <destination>: the volume name the proxy mounts there
  docker inspect -f '{{range .Mounts}}{{if eq .Destination "'"$1"'"}}{{.Name}}{{end}}{{end}}' "$PROXY_CID" 2>/dev/null || true
}

# Resolve the restore role. Prefer the superuser otodock_admin (it can
# recreate the schema and re-own every object). The probe passes -d "$DB_NAME"
# on purpose: without it psql connects to a database named after the user,
# which does not exist, and every install would false-negative to the
# fallback. The local socket is `trust`, so no password is needed.
if [ -n "${POSTGRES_USER:-}" ]; then
  DB_USER="$POSTGRES_USER"
elif docker exec "$PG_CID" psql -U otodock_admin -d "$DB_NAME" -tAc 'SELECT 1' >/dev/null 2>&1; then
  DB_USER="otodock_admin"
else
  DB_USER="otodock"
fi
say "restoring as database role '${DB_USER}' (project $PROJECT, $TOPOLOGY)."

# --- the platform must be stopped --------------------------------------------------
# A running platform writes mid-restore and serves the old rows from its
# in-process caches afterwards. On Docker the project's proxy container says
# whether it runs; on bare metal the port is probed: PROXY_PORT from the
# shell, else PORT, else PROXY_PORT from the env files (config_value), else
# 8400, asked on 127.0.0.1 on that port and on 8400 (a compose .env without
# PROXY_PORT publishes 8400 whatever config.env says) and, when PROXY_BIND_IP
# names another address, that one on the port too; :: is asked as ::1.
if [ "$TOPOLOGY" = docker ]; then
  if [ "$PROXY_RUNNING" = true ]; then
    echo "error: the platform is running (container ${PROXY_CID}, state ${PROXY_STATE}): stop the proxy first" >&2
    echo "       (docker compose stop otodock-proxy, in the install folder)" >&2
    exit 1
  fi
else
  PROXY_PORT="${PROXY_PORT:-${PORT:-$(config_value PROXY_PORT)}}"
  PROXY_PORT="${PROXY_PORT:-8400}"
  bind_ip="${PROXY_BIND_IP:-$(config_value PROXY_BIND_IP)}"
  bind_ip="${bind_ip#[}"
  bind_ip="${bind_ip%]}"
  probes=("127.0.0.1 ${PROXY_PORT}")
  [ "$PROXY_PORT" = 8400 ] || probes+=("127.0.0.1 8400")
  case "$bind_ip" in
    ""|0.0.0.0|127.0.0.1) ;;
    ::)  probes+=("[::1] ${PROXY_PORT}") ;;
    *:*) probes+=("[${bind_ip}] ${PROXY_PORT}") ;;
    *)   probes+=("${bind_ip} ${PROXY_PORT}") ;;
  esac
  for probe in "${probes[@]}"; do
    host="${probe% *}"
    port="${probe##* }"
    if curl -fs -m 2 "http://${host}:${port}/health" >/dev/null 2>&1; then
      echo "error: the platform is running on ${host} port ${port}: stop the proxy first" >&2
      echo "       (systemctl stop otodock-proxy, or scripts/compose.sh stop otodock-proxy)" >&2
      exit 1
    fi
  done
fi

# --- the versions ---------------------------------------------------------------------
BACKUP_VERSION="$(manifest_value otodock_version)"
RUNNING_VERSION=""
if [ "$TOPOLOGY" = docker ] && [ -n "$IMAGE" ]; then
  RUNNING_VERSION="$(docker run --rm --network none --entrypoint sed "$IMAGE" -nE \
    's/^[[:space:]]*OTODOCK_VERSION[[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\1/p' /opt/otodock/VERSIONS.md 2>/dev/null | head -n1 || true)"
elif [ -f "$INSTALL_DIR/VERSIONS.md" ]; then
  RUNNING_VERSION="$(sed -nE 's/^[[:space:]]*OTODOCK_VERSION[[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\1/p' "$INSTALL_DIR/VERSIONS.md" | head -n1)"
fi
case "$(version_cmp "$BACKUP_VERSION" "$RUNNING_VERSION")" in
  1)
    msg="this backup was taken on OtoDock $BACKUP_VERSION and this install runs $RUNNING_VERSION: the server would
  refuse the database at boot (migrations only run forward). Upgrade first (upgrade.sh --to $BACKUP_VERSION)
  and restore then, or pass --allow-newer and start the server with OTODOCK_ALLOW_NEWER_DATABASE=true."
    if [ "$ALLOW_NEWER" = true ]; then warn "WARNING: $msg"; else fail "$msg"; fi ;;
  -1)
    if [ "$UPGRADE_SNAPSHOT" = true ]; then
      warn "this snapshot was taken on OtoDock $BACKUP_VERSION and this install runs $RUNNING_VERSION: its
  next start migrates the restored database to $RUNNING_VERSION again. To go back to $BACKUP_VERSION, copy
  .env and the compose files from $SRC into the install folder before you start it."
    fi ;;
  "?")
    if [ -z "$MANIFEST" ]; then
      warn "a dump alone: its release is not recorded, so the version check is skipped (a run directory carries it)"
    else
      warn "the version check is skipped (backup ${BACKUP_VERSION:-unknown}, running ${RUNNING_VERSION:-unknown})"
    fi ;;
esac

# --- the credential keys --------------------------------------------------------------
# The backup's manifest carries the fingerprints of the keys its source could
# decrypt with (credential_key_fingerprints, written at every boot since
# 1.8.0; a 1.7.1 source has the secret floor's record instead). The target's
# own record is read from its database before the restore. Every fingerprint
# of the source must be one the target holds. No secret is parsed here.
# The keys that encrypted the source's rows are JWT_SECRET and the first
# CREDENTIAL_ENCRYPTION_KEY entry (a 1.7.1 source: the last value of each in
# the secret floor's record): one of those missing refuses; a missing
# decrypt-only entry (an older key kept for rotation) only warns.
SRC_KEYS_JSON="$(manifest_value credential_keys)"
src_required=""
if [ -n "$SRC_KEYS_JSON" ]; then
  src_required="$(printf '%s' "$SRC_KEYS_JSON" | grep -oE '"[0-9a-f]{32}"[[:space:]]*:[[:space:]]*"(JWT_SECRET|CREDENTIAL_ENCRYPTION_KEY entry 1)"' | grep -oE '[0-9a-f]{32}' | sort -u || true)"
else
  SRC_KEYS_JSON="$(manifest_value credential_keys_floor)"
  if [ -n "$SRC_KEYS_JSON" ]; then
    src_required="$(printf '%s' "$SRC_KEYS_JSON" | grep -oE '"[A-Z_]+"[[:space:]]*:[[:space:]]*\[[^]]*\]' \
      | sed -E 's/.*"([0-9a-f]{32})"[^0-9a-f]*$/\1/' | grep -oE '^[0-9a-f]{32}$' | sort -u || true)"
  fi
fi
TGT_KEYS_JSON="$(docker exec "$PG_CID" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
  "SELECT value FROM platform_settings WHERE key = 'credential_key_fingerprints'" 2>/dev/null || true)"
key_label() { # key_label <fingerprint>: its label in the source's record
  local label
  label="$(printf '%s' "$SRC_KEYS_JSON" | sed -nE 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' | head -n1)"
  [ -n "$label" ] || label="$(printf '%s' "$SRC_KEYS_JSON" | sed -nE 's/.*"([A-Z_]+)"[[:space:]]*:[[:space:]]*\[[^]]*"'"$1"'".*/\1/p' | head -n1)"
  printf '%s' "${label:-a key}"
}
if [ -n "$SRC_KEYS_JSON" ] && [ -n "$TGT_KEYS_JSON" ]; then
  tgt_all="$(printf '%s' "$TGT_KEYS_JSON" | grep -oE '[0-9a-f]{32}' | sort -u || true)"
  src_all="$(printf '%s' "$SRC_KEYS_JSON" | grep -oE '[0-9a-f]{32}' | sort -u || true)"
  missing_required="$(comm -23 <(printf '%s\n' "$src_required" | grep .) <(printf '%s\n' "$tgt_all") || true)"
  missing_other="$(comm -23 <(comm -23 <(printf '%s\n' "$src_all") <(printf '%s\n' "$src_required" | grep .)) <(printf '%s\n' "$tgt_all") || true)"
  if [ -n "$missing_required" ]; then
    named=""
    while IFS= read -r fp; do
      [ -n "$fp" ] || continue
      named="$named $(key_label "$fp") ${fp:0:8}…;"
    done <<< "$missing_required"
    msg="the credentials in this backup were encrypted under keys this install cannot read
  (missing:${named%;}). Copy these lines from the backup's env file into .env before restoring:
  JWT_SECRET, CREDENTIAL_ENCRYPTION_KEY, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, WOPI_SECRET,
  OTODOCK_INSTALL_ID (never POSTGRES_PASSWORD or the ports), start the server once so it
  records them (docker compose up -d), stop it and run this again; or add the backup's
  JWT_SECRET as a further comma-separated entry of CREDENTIAL_ENCRYPTION_KEY (older entries
  only decrypt). With --ignore-credential-key the restore goes on and the credentials under
  the missing keys are unreadable: engine sign-ins, MCP accounts, SMTP and 2FA secrets, so an
  account with 2FA cannot sign in until its 2FA is cleared
  (UPDATE users SET totp_enabled = false, totp_secret_enc = NULL WHERE email = '...')."
    if [ "$IGNORE_KEY" = true ]; then warn "WARNING: $msg"; else fail "$msg"; fi
  elif [ -n "$missing_other" ]; then
    named=""
    while IFS= read -r fp; do
      [ -n "$fp" ] || continue
      named="$named $(key_label "$fp") ${fp:0:8}…;"
    done <<< "$missing_other"
    warn "WARNING: the backup's install also held older decrypt-only keys this install lacks
  (${named%;}): rows written before a key rotation would be unreadable. Add them as further
  comma-separated entries of CREDENTIAL_ENCRYPTION_KEY if such rows exist."
  else
    say "credential keys: every key the backup needs is held here."
  fi
elif [ -z "$MANIFEST" ]; then
  warn "a dump alone: the credential keys cannot be checked (a run directory carries their fingerprints)"
elif [ -z "$TGT_KEYS_JSON" ]; then
  warn "this install has not recorded its credential keys yet (it never booted 1.8.0 or later): the key check is skipped"
else
  warn "the backup carries no credential key fingerprints: the key check is skipped"
fi

TREE_ERRORS="$(manifest_value errors)"
if [ -n "$TREE_ERRORS" ] && [ "$TREE_ERRORS" != 0 ]; then
  warn "WARNING: this backup's tree is incomplete (its run reported errors=$TREE_ERRORS: a file or a
  database that could not be copied, named in that run's output). The restore replaces the current
  tree with this archive, so what the archive lacks is gone after it."
fi

# --- the trees to restore ----------------------------------------------------------------
AGENTS_VOL=""
KEYS_VOL=""
SESSIONS_VOL=""
AGENTS_DIR=""
KEYS_DIR=""
SESSIONS_DIR=""
RESTORE_TREES=false
if [ -n "$MANIFEST" ] && [ "$DB_ONLY" = false ]; then
  if [ -f "$SRC/agents.tar.gz" ]; then
    RESTORE_TREES=true
    if [ "$TOPOLOGY" = docker ]; then
      AGENTS_VOL="$(mount_name /opt/otodock/agents)"
      KEYS_VOL="$(mount_name /opt/otodock/mcps/custom/ssh-hosts/keys)"
      SESSIONS_VOL="$(mount_name /opt/otodock/sessions)"
      if [ -z "$AGENTS_VOL" ] || [ -z "$IMAGE" ]; then
        warn "the proxy container's agents volume cannot be found: start the stack once first (docker compose up -d), stop the proxy and run this again. The database alone is restored now."
        RESTORE_TREES=false
      fi
    else
      AGENTS_DIR="${OTODOCK_AGENTS_DIR:-${PLATFORM_DATA_DIR:-.}/agents}"
      KEYS_DIR="${OTODOCK_SSH_KEYS_DIR:-$INSTALL_DIR/mcps/custom/ssh-hosts/keys}"
      SESSIONS_DIR="${OTODOCK_SESSIONS_DIR:-${PLATFORM_DATA_DIR:-.}/sessions}"
      [ -d "$AGENTS_DIR" ] || fail "agents directory not found at $AGENTS_DIR (set OTODOCK_AGENTS_DIR)"
    fi
  elif [ "$UPGRADE_SNAPSHOT" = true ]; then
    warn "this is the snapshot upgrade.sh takes before it changes anything: it holds the database, .env and
  the compose files, never the trees. The database is restored and the files on disk stay as they are.
  restore.sh puts back the database only: to go back to the release the snapshot was taken on, copy .env
  and the compose files from $SRC into the install folder first."
  elif [ "$(manifest_value agents)" = pruned ]; then
    warn "this run's tree archives were pruned by the retention: the database and nothing else is restored"
  elif [ "$(manifest_value agents)" = skipped ]; then
    warn "this run's backup skipped the trees (the agents directory was not readable when it ran): the database and nothing else is restored"
  fi
fi

# --- the trees ------------------------------------------------------------------------------
# The same tarfile filter checks and extracts: names stay under the root (no
# absolute name, no .., nothing under an archived link), symlinks are kept
# verbatim (the engines and the agents' venvs make absolute ones), a hard
# link may point only inside the root, FIFOs and devices are refused, no
# owner is taken from the archive. The whole archive is read once before
# anything is touched; then the current children are moved aside inside the
# root, the archive is extracted, ownership follows the root's when running
# as root, and the set-aside tree is deleted.
#
# tar_filter's realpath check sees only what is on disk: a symlink the same
# archive created earlier and a member written through it (a/esc -> /x, then
# a/esc/f) pass the read-only check, where nothing is on disk yet, and fail
# only at the extraction, after the database was replaced. So the filter
# remembers the archived links and refuses any later member whose path is at
# or under one, and any hard link whose target is.
FILTER_PY=$(cat <<'PY'
import os
import sys
import tarfile

if not hasattr(tarfile, "data_filter"):
    print("error: this Python's tarfile has no extraction filters (3.12, or 3.10.12 / 3.11.4 and later): refusing to extract", file=sys.stderr)
    sys.exit(2)

_links = set()


def _norm(name):
    return os.path.normpath("/" + name).lstrip("/")


def _under_a_link(name):
    parts = _norm(name).split("/")
    return next(("/".join(parts[:i]) for i in range(1, len(parts) + 1) if "/".join(parts[:i]) in _links), None)


def oto_filter(member, dest):
    member = tarfile.tar_filter(member, dest)
    if member.isdev() or member.isfifo():
        raise tarfile.SpecialFileError(member)
    link = _under_a_link(member.name)
    if link is not None:
        raise tarfile.FilterError(f"{member.name!r} is at or under the archived link {link!r}")
    if member.islnk():
        link = _under_a_link(member.linkname)
        if link is not None:
            raise tarfile.FilterError(f"hard link {member.name!r} points through the archived link {link!r}")
        root = os.path.realpath(dest)
        target = os.path.realpath(os.path.join(dest, member.linkname))
        if os.path.commonpath([target, root]) != root:
            raise tarfile.LinkOutsideDestinationError(member, target)
    if member.issym():
        _links.add(_norm(member.name))
    return member.replace(uid=None, gid=None, uname=None, gname=None, deep=False)
PY
)
# The check: every member through the filter, every file read in full, the
# sizes summed against the free space of the place the tree goes (argv[1]:
# the volume mounted read-only, or the directory), with room per member.
VALIDATE_PY="$FILTER_PY"$(cat <<'PY'


import shutil

dest = "/otodock-restore-check"
n = total = 0
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as tf:
    for member in tf:
        oto_filter(member, dest)
        if member.isreg():
            fh = tf.extractfile(member)
            while fh.read(1 << 20):
                pass
            total += member.size
        total += 4096
        n += 1
where = sys.argv[1] if len(sys.argv) > 1 else ""
if where and os.path.isdir(where):
    free = shutil.disk_usage(where).free
    if total > free:
        print(f"error: the archive unpacks to about {total >> 20} MB and {where} has {free >> 20} MB free: "
              "free space there first (the previous tree is set aside, not deleted, while the archive is extracted)",
              file=sys.stderr)
        sys.exit(1)
print(f"{n} member(s) checked, {total >> 20} MB", file=sys.stderr)
PY
)
RESTORE_PY="$FILTER_PY"$(cat <<'PY'


import shutil

root = os.path.abspath(sys.argv[1])
aside = os.path.join(root, ".pre-restore-" + sys.argv[2])
try:
    os.mkdir(aside)
    for child in os.listdir(root):
        if child.startswith(".pre-restore-"):
            if os.path.join(root, child) != aside:
                print(f"note: {child} is left from an earlier restore; it stays in place", file=sys.stderr)
            continue
        os.rename(os.path.join(root, child), os.path.join(aside, child))
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as tf:
        tf.extractall(root, filter=oto_filter)
except BaseException as e:
    print(f"error: the restore stopped: {e}. The previous tree is under {aside} (whatever was moved there): "
          "put it back by hand, or run the restore again.", file=sys.stderr)
    sys.exit(1)
if os.geteuid() == 0:
    st = os.lstat(root)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath == aside or dirpath.startswith(aside + os.sep):
            dirnames[:] = []
            continue
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if path == aside or name.startswith(".pre-restore-"):
                continue
            try:
                os.lchown(path, st.st_uid, st.st_gid)
            except OSError:
                pass
shutil.rmtree(aside)
print("restored", file=sys.stderr)
PY
)

HOST_PY=python3
[ -x "$INSTALL_DIR/proxy/venv/bin/python" ] && HOST_PY="$INSTALL_DIR/proxy/venv/bin/python"
# The archive goes to the program on stdin: the file stays readable to whoever
# ran the backup (0600, root from cron), never mounted into the container.
check_tree() { # check_tree <archive> <volume or ''> <path in the container> <host dir or ''>
  local archive="$1" vol="$2" cpath="$3" hdir="$4"
  say "checking $(basename "$archive")"
  if [ "$TOPOLOGY" = docker ]; then
    docker run --rm -i --network none -v "$vol:$cpath:ro" --entrypoint python3 "$IMAGE" -c "$VALIDATE_PY" "$cpath" < "$archive" \
      || fail "$(basename "$archive") did not pass the check (above); nothing was changed"
  else
    "$HOST_PY" -c "$VALIDATE_PY" "$hdir" < "$archive" || fail "$(basename "$archive") did not pass the check (above); nothing was changed"
  fi
}
restore_tree() { # restore_tree <archive> <volume or ''> <path in the container> <host dir or ''>
  local archive="$1" vol="$2" cpath="$3" hdir="$4" ts
  ts="$(date +%Y%m%d-%H%M%S)"
  if [ "$TOPOLOGY" = docker ]; then
    say "restoring $(basename "$archive") into volume $vol"
    docker run --rm -i --network none -v "$vol:$cpath" --entrypoint python3 "$IMAGE" -c "$RESTORE_PY" "$cpath" "$ts" < "$archive" \
      || fail "the tree restore failed (above)"
  else
    [ -d "$hdir" ] || mkdir -p "$hdir"
    say "restoring $(basename "$archive") into $hdir"
    "$HOST_PY" -c "$RESTORE_PY" "$hdir" "$ts" < "$archive" || fail "the tree restore failed (above)"
  fi
}
RESTORE_KEYS=false
if [ "$RESTORE_TREES" = true ] && [ -f "$SRC/ssh-keys.tar.gz" ]; then
  if { [ "$TOPOLOGY" = docker ] && [ -n "$KEYS_VOL" ]; } || [ "$TOPOLOGY" = host ]; then
    RESTORE_KEYS=true
  else
    warn "the proxy mounts no ssh-keys volume: ssh-keys.tar.gz will not be restored"
  fi
fi
# The sessions tree holds the connected OAuth accounts' token files, the only
# copy: a run without the archive restores the rest and says what is missing.
RESTORE_SESSIONS=false
if [ "$RESTORE_TREES" = true ]; then
  if [ -f "$SRC/sessions.tar.gz" ]; then
    if { [ "$TOPOLOGY" = docker ] && [ -n "$SESSIONS_VOL" ]; } || [ "$TOPOLOGY" = host ]; then
      RESTORE_SESSIONS=true
    else
      warn "the proxy mounts no sessions volume: sessions.tar.gz will not be restored (the connected OAuth accounts need connecting again)"
    fi
  elif [ "$(manifest_value sessions)" = pruned ]; then
    warn "this run's sessions archive was pruned by the retention: the connected OAuth accounts' tokens are not restored, connect the accounts again"
  elif [ "$(manifest_value sessions)" = skipped ]; then
    warn "this run's backup skipped the sessions tree (not readable when it ran): the connected OAuth accounts' tokens are not restored, connect the accounts again"
  else
    warn "this run has no sessions archive (a backup from before the sessions tree was archived): the connected OAuth accounts' tokens are not restored, connect the accounts again"
  fi
fi

if [ "$RESTORE_TREES" = true ]; then
  check_tree "$SRC/agents.tar.gz" "$AGENTS_VOL" /opt/otodock/agents "$AGENTS_DIR"
  [ "$RESTORE_KEYS" = false ] || check_tree "$SRC/ssh-keys.tar.gz" "$KEYS_VOL" /opt/otodock/mcps/custom/ssh-hosts/keys "$KEYS_DIR"
  [ "$RESTORE_SESSIONS" = false ] || check_tree "$SRC/sessions.tar.gz" "$SESSIONS_VOL" /opt/otodock/sessions "$SESSIONS_DIR"
fi

# --- confirm ---------------------------------------------------------------------------
echo "WARNING: restoring '${SRC}' into database '${DB_NAME}' (container ${PG_CID}, project ${PROJECT})."
echo "The database is replaced whole."
if [ "$RESTORE_TREES" = true ]; then
  _sess=""
  if [ "$TOPOLOGY" = docker ]; then
    [ "$RESTORE_SESSIONS" = false ] || _sess=", the sessions tree in ${SESSIONS_VOL}"
    echo "The agents tree in volume ${AGENTS_VOL}${KEYS_VOL:+, the ssh keys in ${KEYS_VOL}}${_sess} are replaced by the archives."
  else
    [ "$RESTORE_SESSIONS" = false ] || _sess=", the sessions tree at ${SESSIONS_DIR}"
    echo "The agents tree at ${AGENTS_DIR}${KEYS_DIR:+, the ssh keys at ${KEYS_DIR}}${_sess} are replaced by the archives."
  fi
elif [ -z "$MANIFEST" ]; then
  echo "A dump alone: the agents tree and the env file are not in it."
fi
echo "This OVERWRITES current data. Stop the proxy before continuing."
printf "Type 'yes' to continue: "
read -r confirm
[ "$confirm" = "yes" ] || { echo "Aborted."; exit 1; }

# --- the database -----------------------------------------------------------------------
# The public schema is recreated first: a table the dump does not know (one
# of a newer release, schema_meta included) cannot survive the restore nor
# block a DROP TABLE under ON_ERROR_STOP through a foreign key.
# The schema recreation and the dump run under this guard: a dump that stops
# under ON_ERROR_STOP leaves a partial database, said so, with the schema
# handed to the app role all the same.
DB_STAGE=""
schema_owner_guard() {
  [ "$DB_USER" != "otodock" ] || return 0
  local owner app_role
  owner="$(docker exec "$PG_CID" psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'" 2>/dev/null || true)"
  app_role="$(docker exec "$PG_CID" psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'otodock' AND NOT rolsuper" 2>/dev/null || true)"
  case "$owner" in
    ""|pg_database_owner|otodock) ;;
    *)
      if [ "$app_role" = 1 ]; then
        docker exec "$PG_CID" psql -q -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "ALTER SCHEMA public OWNER TO otodock" >/dev/null
        echo "Schema public was owned by ${owner}; the app role otodock owns it now."
      fi ;;
  esac
}
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "$DB_STAGE" = replacing ]; then
    schema_owner_guard 2>/dev/null || true
    echo "restore.sh: error: the database restore stopped part way (above): the database is PARTIAL." >&2
    echo "       Fix the cause and run the restore again; do not start the proxy on it." >&2
  fi
  exit "$rc"
}
trap on_exit EXIT
DB_STAGE=replacing
# Captured, then the notices filtered out: a pipe into grep would hide psql's
# exit status, and a failed recreation must not feed the dump into the old schema.
_out="$(docker exec "$PG_CID" psql -q -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" \
  -c "DROP SCHEMA IF EXISTS public CASCADE" -c "CREATE SCHEMA public" 2>&1)" \
  || fail "the public schema could not be recreated: $_out"
printf '%s\n' "$_out" | grep -v "^NOTICE\|^DETAIL\|^drop cascades\|^$" || true
case "$DUMP" in
  *.gz) gunzip -c "$DUMP" | docker exec -i "$PG_CID" psql -q -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" ;;
  *)    docker exec -i "$PG_CID" psql -q -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" < "$DUMP" ;;
esac
# The restored record of the keys is the SOURCE's; the next boot writes this
# install's own, and a second restore before that must not compare against it.
docker exec "$PG_CID" psql -q -U "$DB_USER" -d "$DB_NAME" \
  -c "DELETE FROM platform_settings WHERE key = 'credential_key_fingerprints'" >/dev/null 2>&1 || true
DB_STAGE=finished

# The recreated schema belongs to the restore role; on the containerised stack
# that is the superuser, and the proxy's migrations then fail with "no schema
# has been selected to create in". The one-shot otodock-db-init would move it
# on the next `up -d`, but the proxy is usually restarted before that, so the
# app role is given the schema here. A no-op on bare metal (otodock is the
# bootstrap superuser).
schema_owner_guard
say "database restored."

# --- the trees, into place --------------------------------------------------------------------
if [ "$RESTORE_TREES" = true ]; then
  restore_tree "$SRC/agents.tar.gz" "$AGENTS_VOL" /opt/otodock/agents "$AGENTS_DIR"
  [ "$RESTORE_KEYS" = false ] || restore_tree "$SRC/ssh-keys.tar.gz" "$KEYS_VOL" /opt/otodock/mcps/custom/ssh-hosts/keys "$KEYS_DIR"
  [ "$RESTORE_SESSIONS" = false ] || restore_tree "$SRC/sessions.tar.gz" "$SESSIONS_VOL" /opt/otodock/sessions "$SESSIONS_DIR"
fi
# A compose stack is started with up -d, not start/restart: a container keeps the
# env file it mounted, and an editor that wrote .env as a new file (sed -i too)
# leaves a started container on the old contents.
if [ "$TOPOLOGY" = docker ]; then
  echo "Restore complete. Start the proxy with 'docker compose up -d' (a recreate, so it reads the .env you edited); migrations re-run idempotently on boot."
else
  echo "Restore complete. Start the proxy; migrations re-run idempotently on boot."
fi
