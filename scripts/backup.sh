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
#   scripts/backup.sh --apps          # + the apps: every app database copied
#                                     #   consistently (the SQLite backup API,
#                                     #   never a plain copy of a live WAL file)
#                                     #   with the release copies and chat
#                                     #   snapshots, as otodock-apps-<TS>.tar.gz
#
# --apps reads the agents directory where the proxy has it. On the
# containerised stack that is a Docker volume this host cannot read, so the
# copy runs inside the running proxy container (`docker exec`, the same way
# the dump runs inside Postgres) and streams the archive out; on a bare-metal
# stack it reads the directory on this host (OTODOCK_AGENTS_DIR, else
# $PLATFORM_DATA_DIR/agents, else ./agents). Setting OTODOCK_AGENTS_DIR always
# means this host. The rest of the agent tree (workspaces, knowledge, config)
# is the operator's own file backup, as the install docs say.
#
# Exit status: 0 when every file asked for was written; 3 when the apps
# archive was written but one or more app databases could not be copied
# (each named on stderr, the archive holds the rest); 1 on any other failure,
# with no partial file left behind.
#
# Schedule it from cron/systemd-timer for unattended backups. Restore with restore.sh.
set -euo pipefail

# Backups hold every user's data and password hashes: keep them private. umask
# 077 covers the dump, the --apps archive and any directory this script creates.
umask 077
# Never leave a truncated archive a failed dump wrote: write to .part, mv on
# success; the trap removes a leftover .part on any exit (the mv'd file is gone
# by then, so this is a no-op on success). ${OUT:-}/${APPS_OUT:-} because they
# are set later and APPS_OUT only under --apps (set -u would abort otherwise).
OUT=""
APPS_OUT=""
trap 'rm -f "${OUT:-}.part" "${APPS_OUT:-}.part"' EXIT

DB_USER="${POSTGRES_USER:-otodock}"
DB_NAME="${POSTGRES_DB:-otodock}"
OUT_DIR="${OTODOCK_BACKUP_DIR:-./backups}"
RETAIN="${OTODOCK_BACKUP_RETAIN:-14}"   # keep the N newest dumps; 0 = keep all
WITH_APPS=false
for arg in "$@"; do
  case "$arg" in
    --apps) WITH_APPS=true ;;
    *) echo "usage: $0 [--apps]" >&2; exit 1 ;;
  esac
done

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

# umask 077 (above) makes any directory created here 0700. If it already existed
# group/world-accessible, warn rather than silently changing an operator's dir.
mkdir -p "$OUT_DIR"
if [ "$(stat -c '%a' "$OUT_DIR" 2>/dev/null || echo '')" != "700" ]; then
  echo "warning: $OUT_DIR is not 0700: backups there are readable by other accounts; chmod 700 it" >&2
fi
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$OUT_DIR/otodock-${DB_NAME}-${TS}.sql.gz"

echo "Backing up database '$DB_NAME' from container ${CID} → ${OUT}"
docker exec "$CID" pg_dump -U "$DB_USER" --clean --if-exists "$DB_NAME" | gzip > "$OUT.part"
mv "$OUT.part" "$OUT"
echo "Done: $(du -h "$OUT" | cut -f1)  ${OUT}"

# Retention: prune all but the N newest dumps for this database.
if [ "$RETAIN" -gt 0 ]; then
  # shellcheck disable=SC2012
  ls -1t "$OUT_DIR"/otodock-"${DB_NAME}"-*.sql.gz 2>/dev/null \
    | tail -n +"$((RETAIN + 1))" | xargs -r rm -f
fi

# The apps archive, streamed as a gzip'd tar to stdout by one Python program
# that runs where the agents directory is readable (this host, or the proxy
# container): every app-data/**/*.db copied through the SQLite backup API —
# consistent even while the app is writing (a tar of a live WAL database is
# corrupt) — then each agent's app-releases/ and shares/ as plain files.
APPS_PY=$(cat <<'PY'
import os
import pathlib
import stat
import sqlite3
import sys
import tarfile
import tempfile

agents = sys.argv[1]
out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz")
n_db = 0
errors = 0


def _is_regular(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)  # lstat: a symlink is NOT regular
    except OSError:
        return False


def _drop_links(tarinfo):
    # tar filter for the app-releases/ and shares/ trees: never archive a symlink
    # (nor a hard link): it could point outside the agent's own tree.
    if tarinfo.issym() or tarinfo.islnk():
        return None
    return tarinfo


for agent in sorted(os.listdir(agents)):
    adir = os.path.join(agents, agent)
    if not os.path.isdir(adir) or os.path.islink(adir):
        continue
    for root, dirs, files in os.walk(os.path.join(adir, "app-data")):
        # never descend into a symlinked directory
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
        for name in sorted(files):
            if not name.endswith(".db"):
                continue
            src = os.path.join(root, name)
            if not _is_regular(src):
                print(f"skip (symlink or special file): {src}", file=sys.stderr)
                continue
            with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
                try:
                    # read-only URI: never create/modify the live DB; NOT
                    # immutable=1 (that returns an empty DB while a writer holds it).
                    # The path is percent-encoded: the file name is the app's
                    # choice, and a raw '#', '?' or '%' would end the path or
                    # decode into another one.
                    uri = pathlib.Path(os.path.abspath(src)).as_uri() + "?mode=ro"
                    live = sqlite3.connect(uri, uri=True)
                    copy = sqlite3.connect(tmp.name)
                    with copy:
                        live.backup(copy)
                    live.close()
                    copy.close()
                    info = out.gettarinfo(tmp.name, arcname=os.path.join(".", os.path.relpath(src, agents)))
                    st = os.stat(src)
                    info.mode = st.st_mode & 0o7777
                    info.mtime = st.st_mtime
                    fh = open(tmp.name, "rb")
                except Exception as e:  # one bad app must not cancel everyone's backup
                    errors += 1
                    print(f"error backing up {src}: {e}", file=sys.stderr)
                    continue
                # Not caught: a failure while the member is written leaves a
                # partial member in the stream, so the program ends there and
                # the script keeps no archive.
                with fh:
                    out.addfile(info, fh)
                n_db += 1
    for tree in ("app-releases", "shares"):
        path = os.path.join(adir, tree)
        if os.path.isdir(path) and not os.path.islink(path):
            out.add(path, arcname=os.path.join(".", agent, tree), filter=_drop_links)
out.close()
msg = f"{n_db} app database(s) copied"
if errors:
    msg += f", {errors} error(s)"
print(msg, file=sys.stderr)
# 3: the archive is whole, the databases named above are missing from it.
sys.exit(3 if errors else 0)
PY
)

if [ "$WITH_APPS" = true ]; then
  APPS_OUT="$OUT_DIR/otodock-apps-${TS}.tar.gz"
  # The proxy is a container only on the containerised stack; bare metal runs
  # it on the host, so there is no container name to fall back to.
  PROXY_CID=""
  if [ -z "${OTODOCK_AGENTS_DIR:-}" ]; then
    PROXY_CID="$(platform_container otodock-proxy)" || exit 1
  fi
  AGENTS_DIR="${OTODOCK_AGENTS_DIR:-${PLATFORM_DATA_DIR:-.}/agents}"
  if [ -n "${OTODOCK_AGENTS_DIR:-}" ] || { [ -z "$PROXY_CID" ] && [ -d "$AGENTS_DIR" ]; }; then
    if [ ! -d "$AGENTS_DIR" ] || [ ! -r "$AGENTS_DIR" ]; then
      echo "error: agents directory not readable at $AGENTS_DIR (set OTODOCK_AGENTS_DIR)" >&2
      exit 1
    fi
    echo "Apps: reading ${AGENTS_DIR} on this host → ${APPS_OUT}"
    apps_rc=0
    printf '%s\n' "$APPS_PY" | python3 - "$AGENTS_DIR" > "$APPS_OUT.part" || apps_rc=$?
  elif [ -n "$PROXY_CID" ]; then
    echo "Apps: reading the agents volume inside the proxy container ${PROXY_CID} → ${APPS_OUT}"
    apps_rc=0
    printf '%s\n' "$APPS_PY" | docker exec -i "$PROXY_CID" python3 - /opt/otodock/agents > "$APPS_OUT.part" || apps_rc=$?
  else
    echo "error: no running otodock-proxy container and no agents directory at $AGENTS_DIR (set OTODOCK_AGENTS_DIR, or start the stack)" >&2
    exit 1
  fi
  # 0: the archive is complete. 3: the program's own status, the archive is
  # whole but databases are missing (named above): kept, and the run exits 3.
  # Anything else (no python3, the container gone, a crash or a failed write
  # mid-stream) leaves a cut archive: the trap removes the .part.
  case "$apps_rc" in
    0|3) mv "$APPS_OUT.part" "$APPS_OUT" ;;
    *) echo "Apps: backup FAILED (exit $apps_rc, see above); no archive kept" >&2; exit 1 ;;
  esac
  echo "Apps: $(du -h "$APPS_OUT" | cut -f1)  ${APPS_OUT}"
  # A kept archive counts for the retention whether it is complete or not,
  # so a database that keeps failing never stops old archives from going.
  if [ "$RETAIN" -gt 0 ]; then
    # shellcheck disable=SC2012
    ls -1t "$OUT_DIR"/otodock-apps-*.tar.gz 2>/dev/null \
      | tail -n +"$((RETAIN + 1))" | xargs -r rm -f
  fi
  [ "$apps_rc" = 0 ] || { echo "Apps: backup INCOMPLETE: a database errored (see above); the archive holds the rest" >&2; exit 3; }
fi
