#!/usr/bin/env bash
#
# OtoDock: upgrade a Docker install to a newer release.
#
#   cd otodock          # the install folder: the one holding .env and docker-compose.yml
#   curl -fsSLO https://raw.githubusercontent.com/OtoDock/oto-dock/main/scripts/upgrade.sh
#   bash upgrade.sh --dry-run        # print every step, change nothing
#   bash upgrade.sh                  # upgrade to the latest release
#   bash upgrade.sh --to 1.7.0       # upgrade to one release
#
# What it does, in order (each step announces itself):
#   1. Checks that this folder is a Docker install and that Docker and the
#      Compose plugin work.
#   2. Downloads the target release's docker-compose.yml and
#      docker-compose.phone.yml to a temporary folder and checks them against
#      this install's own .env. Nothing has changed yet, so a value the new
#      files refuse (a host name in OTO_AUDIOSOCKET_PUBLIC_HOST, say) stops the
#      upgrade here with the old stack still running. A phone publish address
#      the files accept but this host does not hold draws a warning.
#   3. Dumps the database to backups/upgrade-<time>/ (--no-db-backup skips it)
#      and copies .env and the compose files next to the dump.
#   4. Puts the new compose files in place and sets at most two lines in .env:
#      OTODOCK_VERSION=<target>, and COMPOSE_FILE when an override file or the
#      phone overlay must be listed on it. No other line of .env is written; the
#      names of the settings new in the release are printed for you to review in
#      config.env.example.
#   5. Pulls every image. When the target release keeps the recover bin (the
#      deleted files the dashboard can restore for 7 days) on its own volume
#      and the running server still holds it inside its container, copies it
#      into that volume first, so the upgrade does not drop it. Then runs
#      docker compose up -d --remove-orphans and waits for the server to
#      report healthy: up to OTODOCK_UPGRADE_WAIT_S seconds (300 by default;
#      a release's first boot builds its tool environments and migrates the
#      database, minutes on a small host).
#
# Two failure regimes. A step that fails BEFORE docker compose up (a download,
# the file check, the pull) puts .env and the compose files back from the copy:
# the old stack keeps running its old images and nothing was migrated. Once up
# has started, the database may already be migrated forward, and the old
# compose files do not undo that: the script then leaves the new files in place
# and prints the recovery (fix the cause and re-run, or go back with the dump
# and restore.sh).
#
# Testing a release before it is published (maintainers):
#   bash upgrade.sh --to 1.7.0-rc1 --compose-dir <folder>
# reads docker-compose.yml, docker-compose.phone.yml and, when present,
# config.env.example from <folder> instead of the published release, and runs
# images that are present locally even when the registry does not have them.
set -euo pipefail

# The copies of .env and every dump hold secrets: keep them private.
umask 077

REPO="OtoDock/oto-dock"
RAW="https://raw.githubusercontent.com/${REPO}"
DOCS="https://docs.otodock.io/administration/upgrading"

say()  { echo "upgrade.sh: $*"; }
warn() { echo "upgrade.sh: $*" >&2; }
fail() { echo "upgrade.sh: $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
usage: bash upgrade.sh [--to <version>] [--dry-run] [--no-db-backup] [--compose-dir <folder>]

  --to <version>          the release to move to (1.7.0 or v1.7.0); default: the latest release
  --dry-run               print every step, change nothing
  --no-db-backup          skip the database dump (only when you have a fresh backup of your own)
  --compose-dir <folder>  read the compose files from <folder> instead of the published
                          release (testing a release candidate; needs --to)

Run it from the install folder, the one holding .env and docker-compose.yml.
EOF
}

# --- arguments ---------------------------------------------------------------
TO=""
DRY_RUN=false
DB_BACKUP=true
COMPOSE_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --to)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            TO="$2"; shift 2 ;;
        --to=*) TO="${1#--to=}"; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --no-db-backup) DB_BACKUP=false; shift ;;
        --compose-dir)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            COMPOSE_DIR="$2"; shift 2 ;;
        --compose-dir=*) COMPOSE_DIR="${1#--compose-dir=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

VERSION_RE='^v?[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$'
if [ -n "$TO" ] && ! [[ "$TO" =~ $VERSION_RE ]]; then
    fail "'$TO' is not a release version (expected 1.7.0 or v1.7.0)."
fi
if [ -n "$COMPOSE_DIR" ]; then
    [ -n "$TO" ] || fail "--compose-dir needs --to <version>: the version the candidate images carry."
    for f in docker-compose.yml docker-compose.phone.yml; do
        [ -f "$COMPOSE_DIR/$f" ] || fail "--compose-dir $COMPOSE_DIR has no $f."
    done
elif [[ "$TO" == *-rc* ]]; then
    fail "a release candidate is not published as a release: pass its compose files with --compose-dir."
fi

# --- 1. Preflight ------------------------------------------------------------
command -v curl >/dev/null 2>&1 || fail "curl is required: install it first (apt-get install curl)."
command -v docker >/dev/null 2>&1 || fail "Docker is not installed (the docker command is missing)."
docker compose version >/dev/null 2>&1 \
    || fail "the Docker Compose plugin is missing (docker compose version failed)."
docker info >/dev/null 2>&1 \
    || fail "cannot talk to the Docker daemon. Is it running, and is your user in the 'docker' group?"

if [ ! -f docker-compose.yml ] || [ ! -f .env ]; then
    fail "no OtoDock install here: run this from the install folder, the one holding
  .env and docker-compose.yml (the folder install.sh was run in)."
fi
if [ -f docker-compose.build.yml ] || [ -d .git ]; then
    fail "this folder is a source checkout: upgrade it with git pull and
  scripts/compose.sh up -d --build ($DOCS)."
fi
if [ ! -r .env ] || [ ! -w .env ]; then
    fail "cannot read and write .env as $(id -un): run this as the account that runs docker compose here."
fi

# A shell variable wins over .env for docker compose: the upgrade must run on
# what .env says, which is also what every later docker compose command reads.
for _v in OTODOCK_VERSION COMPOSE_FILE; do
    if [ -n "${!_v+x}" ]; then
        warn "your shell sets $_v, which overrides .env for docker compose. This run
  ignores it; unset it in your shell too, or docker compose keeps using it."
        unset "$_v"
    fi
done

# --- .env helpers ------------------------------------------------------------
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

# The KEY names an env file defines, commented out or not, one per line, sorted.
env_keys() { # env_keys <file>
    awk '{ line = $0; sub(/^[ \t]*#?[ \t]*/, "", line); sub(/^export[ \t]+/, "", line)
           if (match(line, /^[A-Z][A-Z0-9_]*[ \t]*=/)) { k = substr(line, 1, RLENGTH - 1); sub(/[ \t]+$/, "", k); print k } }' "$1" \
        | LC_ALL=C sort -u
}

# .env rewritten with OTODOCK_VERSION=<version> (every line that sets it,
# commented out or not, else one appended line) and, when a fourth argument is
# given, COMPOSE_FILE=<value> (every active line, else one appended line).
# Every other line is copied as it is.
rewrite_env() { # rewrite_env <in> <out> <version> [<COMPOSE_FILE value>]
    awk -v ver="$3" -v cf="${4-}" -v setcf="${4+1}" '
        /^[ \t]*#?[ \t]*(export[ \t]+)?OTODOCK_VERSION[ \t]*=/ { print "OTODOCK_VERSION=" ver; v = 1; next }
        setcf && /^[ \t]*(export[ \t]+)?COMPOSE_FILE[ \t]*=/ { print "COMPOSE_FILE=" cf; c = 1; next }
        { print }
        END { if (!v) print "OTODOCK_VERSION=" ver; if (setcf && !c) print "COMPOSE_FILE=" cf }' "$1" > "$2"
}

# The platform's own container for a compose service of the `otodock` project
# (the compose file pins that name), found by its compose labels, never by a
# part of a name: a community MCP container's name is free text. -a includes a
# stopped one. More than one match is refused, never guessed.
platform_container() { # platform_container <compose service> [-a]
    local ids
    ids="$(docker ps ${2:+"$2"} --filter "label=com.docker.compose.project=otodock" \
        --filter "label=com.docker.compose.service=$1" --format '{{.ID}}')"
    if [ "$(printf '%s\n' "$ids" | grep -c .)" -gt 1 ]; then
        warn "more than one container is the platform's $1 ($(printf '%s' "$ids" | tr '\n' ' ')); refusing to guess, remove the extra one"
        return 1
    fi
    printf '%s' "$ids"
}

# `docker compose` with a stand-in POSTGRES_PASSWORD when neither .env nor the
# shell sets one: the compose files refuse to parse without it, and this is a
# check of the files, not of the password.
compose_check() {
    if [ -z "${POSTGRES_PASSWORD:-}" ] && [ -z "$(env_value POSTGRES_PASSWORD .env || true)" ]; then
        POSTGRES_PASSWORD=upgrade-check docker compose "$@"
    else
        docker compose "$@"
    fi
}

# Does this host hold the IPv4 address $1? The wildcard and loopback always
# bind; without a readable `ip` the answer is yes (nothing to warn about).
host_holds_ipv4() { # host_holds_ipv4 <address>
    local addrs
    case "$1" in 0.0.0.0|127.*) return 0 ;; esac
    command -v ip >/dev/null 2>&1 || return 0
    addrs="$(ip -o -4 addr show 2>/dev/null)" || return 0
    printf '%s\n' "$addrs" | awk '{ sub(/\/.*/, "", $4); print $4 }' | grep -qxF -- "$1"
}

# Is A an older release than B? The one version rule the server and
# restore.sh apply: X.Y.Z-rcN is older than X.Y.Z, X.Y.Z-dev older than every rc.
version_key() { # version_key <version>: "major minor patch pre n"
    local v="${1#v}" nums pre=0 n=0
    nums="${v%%-*}"
    case "$v" in
        *-dev) pre=-2 ;;
        *-rc*) pre=-1; n="${v##*-rc}" ;;
    esac
    printf '%s %s %s\n' "${nums//./ }" "$pre" "$n"
}
version_lt() { # version_lt <A> <B>
    local a b i
    read -r -a a <<< "$(version_key "$1")"
    read -r -a b <<< "$(version_key "$2")"
    for i in 0 1 2 3 4; do
        [ "${a[i]:-0}" -lt "${b[i]:-0}" ] && return 0
        [ "${a[i]:-0}" -gt "${b[i]:-0}" ] && return 1
    done
    return 1
}

# The running server container that still keeps the recover bin in its own
# writable layer, when the target release mounts a volume there: printed, or
# nothing when there is nothing to carry. The bin is safe to carry again
# into a volume that already holds part of it (entry names are unique), so
# only the server's own mount decides, never whether the volume exists (a
# run stopped mid-copy, or an up that failed before the server, leaves one).
bin_to_carry() {
    local vols old mounts
    vols="$(compose_check "${CHECK_ARGS[@]}" config --volumes 2>/dev/null)" || return 0
    printf '%s\n' "$vols" | grep -qxF "$BIN_VOLUME" || return 0
    old="$(platform_container otodock-proxy -a)" || return 0
    [ -n "$old" ] || return 0
    mounts="$(docker inspect -f '{{range .Mounts}}{{println .Destination}}{{end}}' "$old" 2>/dev/null)" || return 0
    if printf '%s\n' "$mounts" | grep -qxF "$BIN_DIR"; then
        return 0
    fi
    # Into a file first: the diff of a server container runs to tens of
    # thousands of lines, and grep -q leaving the pipe early would fail the
    # pipeline under pipefail.
    docker diff "$old" > "$TMP/server.diff" 2>/dev/null || return 0
    grep -qxE "[AC] $BIN_DIR" "$TMP/server.diff" || return 0
    printf '%s' "$old"
}

# Copy the recover bin out of the old server container into the volume the
# target release mounts, through the target's own one-shot init service
# (root, no network), which owns it for the server afterwards. Never fatal:
# the bin is a safety net, and the database and the agents do not depend on
# it.
carry_recover_bin() { # carry_recover_bin <old server container>
    local old="$1" need_k="" bytes free_k root
    if [ "$(docker inspect -f '{{.State.Status}}' "$old" 2>/dev/null || true)" = running ]; then
        need_k="$(docker exec "$old" du -sk "$BIN_DIR" 2>/dev/null | awk '{print $1}' || true)"
    else
        bytes="$(docker inspect --size -f '{{.SizeRw}}' "$old" 2>/dev/null || true)"
        [[ "$bytes" =~ ^[0-9]+$ ]] && need_k=$((bytes / 1024))
    fi
    root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
    free_k=""
    [ -n "$root" ] && free_k="$(df -Pk "$root" 2>/dev/null | awk 'NR == 2 { print $4 }' || true)"
    if [[ "$need_k" =~ ^[0-9]+$ ]] && [[ "$free_k" =~ ^[0-9]+$ ]] \
            && [ $((need_k + 2 * 1024 * 1024)) -gt "$free_k" ]; then
        warn "not enough free disk to copy the recover bin ($((need_k / 1024)) MB, $((free_k / 1024)) MB free):
  the files deleted in the last 7 days cannot be restored after this upgrade."
        return 0
    fi
    say "copying the recover bin (files deleted in the last 7 days) into the $BIN_VOLUME volume"
    if ! docker cp "$old:$BIN_DIR" - | docker compose run --rm --no-deps -T otodock-agents-init \
            sh -c "mountpoint -q $BIN_DIR || exit 3; tar -x -C $BIN_DIR --strip-components=1 && chown -R 1000:1000 $BIN_DIR && chmod 0700 $BIN_DIR"; then
        warn "could not copy the recover bin: some files deleted in the last 7 days may not
  be restorable after the upgrade. The upgrade goes on."
    fi
}

# --- cleanup and restore on exit ----------------------------------------------
# STAGE: check (nothing changed yet) -> armed (config copied and verified, a
# failure puts it back) -> up (docker compose up started: the database may be
# migrated, so nothing is put back) -> finished.
STAGE=check
TMP=""
SNAP=""
DUMP=""
CONFIG_FILES=(.env docker-compose.yml docker-compose.phone.yml docker-compose.override.yml)
# The recover bin: the server's copies of deleted and overwritten files.
BIN_DIR=/opt/otodock/recover-bin
BIN_VOLUME=otodock-recover-bin

# Replace a compose file atomically, keeping the mode of the one it replaces.
put_file() { # put_file <source> <dest>
    if [ -f "$2" ]; then
        cp -p "$2" "$2.upgrade-tmp"
    else
        ( umask 022; : > "$2.upgrade-tmp" )
    fi
    cat "$1" > "$2.upgrade-tmp"
    mv -f "$2.upgrade-tmp" "$2"
}

restore_config() {
    local f bad=()
    for f in "${CONFIG_FILES[@]}"; do
        if [ -f "$SNAP/$f" ]; then
            if [ "$f" = .env ]; then
                # In place: the running proxy has this very file bind-mounted.
                cmp -s "$SNAP/$f" "$f" || cat "$SNAP/$f" > "$f" || bad+=("$f")
            else
                cmp -s "$SNAP/$f" "$f" || put_file "$SNAP/$f" "$f" || bad+=("$f")
            fi
        else
            rm -f "$f" || bad+=("$f")   # this run created it
        fi
    done
    if [ "${#bad[@]}" -gt 0 ]; then
        warn "could not put back ${bad[*]}: copy them from $SNAP by hand."
        return 1
    fi
}

on_exit() {
    local rc=$? f
    set +e
    for f in "${CONFIG_FILES[@]}"; do rm -f "$f.upgrade-tmp"; done
    [ -n "$DUMP" ] && rm -f "$DUMP.part"
    [ -n "$TMP" ] && rm -rf "$TMP"
    [ "$rc" -eq 0 ] && exit 0
    case "$STAGE" in
        check)
            [ -n "$SNAP" ] && rmdir "$SNAP" 2>/dev/null
            warn "stopped before changing anything: the running stack was not touched." ;;
        armed)
            if restore_config; then
                warn "stopped before docker compose up: .env and the compose files are back as
  they were (copies in $SNAP). The running stack was not touched and nothing
  was migrated."
            fi ;;
        up)
            warn "the upgrade failed after docker compose up started (the error is above).
  The $TARGET files stay in place: the database may already be migrated to
  $TARGET, and the ${FROM:-old} compose files do not undo a migration.
  To finish the upgrade: fix the cause (docker compose ps, then
  docker compose logs <service>), then re-run
      bash upgrade.sh --to $TARGET"
            if [ -n "$DUMP" ]; then
                warn "To go back to ${FROM:-the old release} instead: put back the files from $SNAP,
  then restore the database with restore.sh $SNAP
  ($DOCS#restoring-from-a-backup)."
            else
                warn "No dump was taken (--no-db-backup): going back to ${FROM:-the old release} needs
  your own backup of the database, restored with restore.sh."
            fi ;;
    esac
    exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# --- 2. The target release and its files -------------------------------------
FROM="$(env_value OTODOCK_VERSION .env || true)"
if [ -z "$FROM" ]; then
    FROM="$(sed -nE 's/.*otodock-proxy:\$\{OTODOCK_VERSION:-([^}]+)\}.*/\1/p' docker-compose.yml | head -n1)"
fi
[[ "$FROM" =~ $VERSION_RE ]] || FROM=""
FROM="${FROM#v}"

if [ -z "$TO" ]; then
    say "looking up the latest release"
    _url="$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/${REPO}/releases/latest")" \
        || fail "could not look up the latest release: check your network, or pass --to <version>."
    TO="${_url##*/tag/}"
    [[ "$TO" =~ $VERSION_RE ]] || fail "could not read the latest release from $_url: pass --to <version>."
fi
TARGET="${TO#v}"

if [ -n "$FROM" ] && version_lt "$TARGET" "$FROM"; then
    fail "this install runs $FROM; refusing to move it back to $TARGET: database
  migrations only run forward. To go back, restore a backup taken on $TARGET."
fi

# The database records the release that last migrated it (schema_meta, since
# 1.8.0): a row newer than the target is a downgrade whatever .env says, and
# one newer than .env's version (a hand-edited .env, a restore) is the real
# starting point for the notes. Read-only; absent on a 1.7.1 database or a
# stopped stack.
DB_VERSION=""
_pg="$(platform_container otodock-postgres || true)"
if [ -n "$_pg" ]; then
    DB_VERSION="$(docker exec "$_pg" psql -U otodock -d otodock -tAc \
        "SELECT last_migrated_by FROM schema_meta WHERE id = 1" 2>/dev/null | head -n1 || true)"
    [[ "$DB_VERSION" =~ $VERSION_RE ]] || DB_VERSION=""
    DB_VERSION="${DB_VERSION#v}"
fi
if [ -n "$DB_VERSION" ]; then
    if version_lt "$TARGET" "$DB_VERSION"; then
        fail "the database was last migrated by OtoDock $DB_VERSION: moving the images to $TARGET
  is a downgrade, and migrations only run forward. To go back, restore a backup
  taken on $TARGET with restore.sh."
    fi
    if [ -z "$FROM" ] || version_lt "$FROM" "$DB_VERSION"; then
        say "the database says it was last migrated by $DB_VERSION (.env says ${FROM:-nothing}): upgrading from there"
        FROM="$DB_VERSION"
    fi
fi
if [ "$FROM" = "$TARGET" ]; then
    say "this install is set to $TARGET already: its compose files and .env lines
  are brought in line with $TARGET again"
else
    say "upgrading from ${FROM:-an unknown release} to $TARGET"
fi

TMP="$(mktemp -d)"
NEW="$TMP/new"
mkdir "$NEW"

# Fetch a file atomically: a failed download never leaves a partial file.
fetch() { # fetch <url> <dest>
    curl -fsSL "$1" -o "$2.tmp" || { rm -f "$2.tmp"; return 1; }
    mv "$2.tmp" "$2"
}

if [ -n "$COMPOSE_DIR" ]; then
    say "reading the $TARGET compose files from $COMPOSE_DIR (release candidate)"
    cp "$COMPOSE_DIR/docker-compose.yml" "$COMPOSE_DIR/docker-compose.phone.yml" "$NEW/"
    for f in config.env.example CHANGELOG.md; do
        [ -f "$COMPOSE_DIR/$f" ] && cp "$COMPOSE_DIR/$f" "$NEW/"
    done
else
    say "downloading the $TARGET compose files"
    for f in docker-compose.yml docker-compose.phone.yml; do
        fetch "$RAW/v$TARGET/$f" "$NEW/$f" \
            || fail "could not download $f of $TARGET ($RAW/v$TARGET/$f): check the version and your network."
    done
    fetch "$RAW/v$TARGET/config.env.example" "$NEW/config.env.example" 2>/dev/null || true
    fetch "$RAW/v$TARGET/CHANGELOG.md" "$NEW/CHANGELOG.md" 2>/dev/null || true
fi

# The Upgrade notes of every release this upgrade crosses (FROM < X <= TARGET;
# the target's alone when FROM is unknown), from the target's CHANGELOG.md:
# the entries under a "### Upgrade notes" heading and those whose bold lead
# is "Upgrade note:". Printed before anything changes, in the dry run and
# the real run.
print_notes() {
    local notes
    if [ ! -f "$NEW/CHANGELOG.md" ]; then
        say "no CHANGELOG.md for $TARGET could be read; the release notes: https://github.com/${REPO}/releases/tag/v$TARGET"
        return 0
    fi
    notes="$(awk -v from="${FROM:-}" -v target="$TARGET" '
        function key(v,   a, n, pre, num) {
            # the one version rule: X.Y.Z; -rcN older than the release; -dev older than every rc
            pre = 0; num = 0
            if (v ~ /-dev$/) { pre = -2; sub(/-dev$/, "", v) }
            else if (v ~ /-rc[0-9]+$/) { pre = -1; num = v; sub(/.*-rc/, "", num); sub(/-rc[0-9]+$/, "", v) }
            n = split(v, a, ".")
            return sprintf("%06d%06d%06d%03d%06d", a[1], a[2], a[3], pre + 10, num)
        }
        function flush() {
            if (entry != "" && (heading ~ /^[Uu]pgrade notes/ || entry ~ /^\*\*[Uu]pgrade note/)) {
                gsub(/\*\*/, "", entry); gsub(/[ \t]+/, " ", entry)
                print "  - " entry
            }
            entry = ""
        }
        /^## \[[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?\]/ {
            flush(); heading = ""
            v = $0; sub(/^## \[/, "", v); sub(/\].*/, "", v)
            take = (key(v) <= key(target)) && (from == "" ? v == target : key(v) > key(from))
            if (take) print "" v ":"
            next
        }
        /^## / { flush(); take = 0; next }
        !take { next }
        /^### / { flush(); heading = substr($0, 5); next }
        /^- / { flush(); entry = substr($0, 3); next }
        /^[ \t]+[^ \t]/ && entry != "" { line = $0; sub(/^[ \t]+/, "", line); entry = entry " " line; next }
        { flush() }
        END { flush() }
    ' "$NEW/CHANGELOG.md")"
    if printf '%s\n' "$notes" | grep -q '^  - '; then
        say "upgrade notes from ${FROM:-the current release} to $TARGET (the entries that change a running install):"
        printf '%s\n' "$notes" | grep -v '^$' | sed 's/^/    /' | fold -s -w 96
        echo
    else
        say "no upgrade notes for $TARGET in its CHANGELOG.md; the release notes: https://github.com/${REPO}/releases/tag/v$TARGET"
    fi
}

# --- which compose files the upgraded stack loads -----------------------------
# Compose loads docker-compose.override.yml by itself only while COMPOSE_FILE
# is unset, so a set line lists it too. The list is built base first:
# docker-compose.yml, then the phone overlay when it is in use, then the
# override. Files are added by exact token (a leading ./ ignored), so a re-run
# adds nothing twice; the order of the tokens already there is kept.
has_token() { # has_token <file> <token>...
    local want="${1#./}" t
    shift
    for t in "$@"; do [ "${t#./}" = "$want" ] && return 0; done
    return 1
}

HAVE_OVERRIDE=false
[ -f docker-compose.override.yml ] && HAVE_OVERRIDE=true
CF_OLD=""
CF_HAS_LINE=false
if CF_OLD="$(env_value COMPOSE_FILE .env)"; then CF_HAS_LINE=true; fi
TOKENS=()
if $CF_HAS_LINE; then
    IFS=: read -r -a _toks <<< "$CF_OLD"
    for t in "${_toks[@]}"; do [ -n "$t" ] && TOKENS+=("$t"); done
    PHONE=false
    if has_token docker-compose.phone.yml "${TOKENS[@]}"; then PHONE=true; fi
    if ! has_token docker-compose.yml "${TOKENS[@]}"; then
        TOKENS=(docker-compose.yml "${TOKENS[@]}")
    fi
    if $HAVE_OVERRIDE && ! has_token docker-compose.override.yml "${TOKENS[@]}"; then
        TOKENS+=(docker-compose.override.yml)
    fi
else
    # No line: compose loads docker-compose.yml (and the override) by itself,
    # so the phone overlay is in use only when its container was started with
    # -f; the line is written then, else not at all.
    PHONE=false
    if [ -n "$(platform_container otodock-phone -a)" ]; then
        PHONE=true
        TOKENS=(docker-compose.yml docker-compose.phone.yml)
        $HAVE_OVERRIDE && TOKENS+=(docker-compose.override.yml)
    fi
fi
CF_NEW=""
if [ "${#TOKENS[@]}" -gt 0 ]; then
    CF_NEW="$(IFS=:; printf '%s' "${TOKENS[*]}")"
fi
CF_WRITE=false
if [ -n "$CF_NEW" ] && { ! $CF_HAS_LINE || [ "$CF_NEW" != "$CF_OLD" ]; }; then
    CF_WRITE=true
fi

# The -f list of the upgraded stack, the new files standing in for the old.
EFFECTIVE=()
if [ "${#TOKENS[@]}" -gt 0 ]; then
    EFFECTIVE=("${TOKENS[@]}")
else
    EFFECTIVE=(docker-compose.yml)
    $HAVE_OVERRIDE && EFFECTIVE+=(docker-compose.override.yml)
fi
CHECK_ARGS=(--project-directory . --env-file ./.env)
for t in "${EFFECTIVE[@]}"; do
    case "${t#./}" in
        docker-compose.yml|docker-compose.phone.yml) CHECK_ARGS+=(-f "$NEW/${t#./}") ;;
        *) CHECK_ARGS+=(-f "$t") ;;
    esac
done

say "checking the $TARGET compose files against this install's .env"
if ! compose_check "${CHECK_ARGS[@]}" config -q; then
    fail "the $TARGET compose files do not accept this install's .env (the message
  above). A host name in OTO_AUDIOSOCKET_PUBLIC_HOST is refused by the phone
  overlay: set OTO_PHONE_BIND=<this host's LAN IP> in .env. Fix .env, then re-run."
fi
if ! $PHONE; then
    if ! compose_check --project-directory . --env-file ./.env -f "$NEW/docker-compose.yml" \
            -f "$NEW/docker-compose.phone.yml" config -q >/dev/null 2>&1; then
        warn "the phone overlay is not in use here, and $TARGET's would not accept this .env:
  before you turn it on, set OTO_PHONE_BIND=<this host's LAN IP> in .env."
    fi
fi
# The phone ports publish on OTO_PHONE_BIND, else on OTO_AUDIOSOCKET_PUBLIC_HOST.
# A literal address there passes the file check even when this host does not
# hold it, and the phone container then fails to start after the swap. A host
# name is the file check's to refuse, never judged here.
if $PHONE && [ -z "$(env_value OTO_PHONE_BIND .env || true)" ]; then
    _pub="$(env_value OTO_AUDIOSOCKET_PUBLIC_HOST .env || true)"
    if [[ "$_pub" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] && ! host_holds_ipv4 "$_pub"; then
        warn "OTO_AUDIOSOCKET_PUBLIC_HOST=$_pub is not an address of this host, and the
  phone ports publish on it while OTO_PHONE_BIND is unset: the phone container
  would not start after the upgrade. Set OTO_PHONE_BIND=<this host's LAN IP> in .env."
    fi
fi

# Since 1.8.0 every yes/no key takes 1/true/yes/on and 0/false/no/off, and
# another word stops the server at start, where older releases read it as
# off. Two lists before anything changes, when the target is 1.8.0 or later:
# the values the new server refuses (the dry run warns, the real run stops,
# since the upgraded proxy would exit in a loop), and the values that flip
# from off to on at this upgrade: the keys releases before 1.8.0 read as on
# for the word "true" alone (TRUE_ONLY_KEYS: 1, yes and on were off there;
# MCP_AUTO_INSTALL_SYSTEM_DEPS took 1 and yes, not on), so an operator who
# wrote OTODOCK_CLOUD=1 for "off" learns it before the server reads it as on.
# tests/config/test_config_keys.py keeps YESNO_KEYS equal to config.py's
# _cfg_bool keys.
YESNO_KEYS="OIDC_ENABLED OTODOCK_AIR_GAPPED OTODOCK_CLOUD DASHBOARD_ENABLED AUTH_PROVIDER_BYPASS
  OIDC_CONFIRM_FRESH_LOGIN OIDC_CONFIRM_REQUIRE_AUTH_TIME MCP_AUTO_INSTALL_SYSTEM_DEPS
  OTODOCK_MCP_BUILD_LOCAL OTODOCK_RELEASE_CHECK OTODOCK_ALLOW_NEWER_DATABASE COOKIE_SECURE
  OTODOCK_LOCAL_PBX_ENABLED RUNNING_IN_DOCKER"
TRUE_ONLY_KEYS="DASHBOARD_ENABLED OTODOCK_CLOUD OTODOCK_AIR_GAPPED AUTH_PROVIDER_BYPASS OIDC_ENABLED
  OIDC_CONFIRM_FRESH_LOGIN OIDC_CONFIRM_REQUIRE_AUTH_TIME"
_bad_yesno=""
_now_on=""
yesno_value() { # yesno_value <KEY>: .env's value, lower case, trimmed
    env_value "$1" .env 2>/dev/null | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || true
}
if ! version_lt "$TARGET" "1.8.0"; then
    for _k in $YESNO_KEYS; do
        _v="$(yesno_value "$_k")"
        case "$_v" in
            ""|1|true|yes|on|0|false|no|off) ;;
            auto) case "$_k" in COOKIE_SECURE|RUNNING_IN_DOCKER|OTODOCK_LOCAL_PBX_ENABLED) ;; *) _bad_yesno="$_bad_yesno $_k=$_v" ;; esac ;;
            *) _bad_yesno="$_bad_yesno $_k=$_v" ;;
        esac
    done
    if [ -z "$FROM" ] || version_lt "$FROM" "1.8.0"; then
        for _k in $TRUE_ONLY_KEYS; do
            _v="$(yesno_value "$_k")"
            case "$_v" in 1|yes|on) _now_on="$_now_on $_k=$_v" ;; esac
        done
        _v="$(yesno_value MCP_AUTO_INSTALL_SYSTEM_DEPS)"
        [ "$_v" != on ] || _now_on="$_now_on MCP_AUTO_INSTALL_SYSTEM_DEPS=on"
    fi
fi
if [ -n "$_now_on" ]; then
    say "these .env values were read as OFF by releases before 1.8.0 and count as ON from
  1.8.0 (1, yes and on are yes words now):$_now_on. Set each to false if off is what
  you want, before the upgrade."
fi
if [ -n "$_bad_yesno" ]; then
    _msg="these .env values are not yes/no words (true/false, 1/0, yes/no, on/off):$_bad_yesno.
  OtoDock 1.8.0 and later refuse to start on them (older releases read them as off):
  set each to true or false, then run the upgrade again."
    if $DRY_RUN; then warn "$_msg (the real run stops here)"; else fail "$_msg"; fi
fi

# Settings the target release documents that the old one did not and that
# .env does not carry in any form. Only the names are printed; nothing is written.
NEW_KEYS=""
KEYS_NOTE=""
if [ -f "$NEW/config.env.example" ] && [ -n "$FROM" ] && [[ "$FROM" != *-rc* ]] \
        && fetch "$RAW/v$FROM/config.env.example" "$TMP/old.example" 2>/dev/null; then
    NEW_KEYS="$(LC_ALL=C comm -13 <(env_keys "$TMP/old.example") <(env_keys "$NEW/config.env.example") \
        | LC_ALL=C comm -23 - <(env_keys .env) | grep -vxE 'OTODOCK_VERSION|COMPOSE_FILE' || true)"
else
    KEYS_NOTE="review config.env.example of $TARGET for new settings: $RAW/v$TARGET/config.env.example"
fi

print_keys() {
    if [ -n "$NEW_KEYS" ]; then
        say "settings new in $TARGET that .env does not set (their defaults apply; see
  config.env.example of $TARGET, $RAW/v$TARGET/config.env.example):"
        printf '%s\n' "$NEW_KEYS" | tr '\n' ' ' | fold -s -w 72 | sed 's/^/    /'
        echo
    elif [ -n "$KEYS_NOTE" ]; then
        say "$KEYS_NOTE"
    fi
}

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_ROOT="${OTODOCK_BACKUP_DIR:-./backups}"
SNAP="$BACKUP_ROOT/upgrade-$TS"
_n=1
while [ -e "$SNAP" ]; do
    _n=$((_n + 1))
    SNAP="$BACKUP_ROOT/upgrade-$TS-$_n"
done

print_notes

if $DRY_RUN; then
    say "dry run: nothing is changed. The upgrade would:"
    if $DB_BACKUP; then
        echo "  - dump the database to $SNAP/otodock-otodock-$TS.sql.gz"
        if [ -z "$(platform_container otodock-postgres || true)" ]; then
            echo "    (no otodock-postgres container runs now: start the stack first)"
        fi
    else
        echo "  - skip the database dump (--no-db-backup)"
    fi
    _copies=()
    for f in "${CONFIG_FILES[@]}"; do [ -f "$f" ] && _copies+=("$f"); done
    echo "  - copy ${_copies[*]} to $SNAP"
    echo "  - replace docker-compose.yml and docker-compose.phone.yml with $TARGET's"
    echo "  - set OTODOCK_VERSION=$TARGET in .env"
    if $CF_WRITE; then
        echo "  - set COMPOSE_FILE=$CF_NEW in .env"
    else
        echo "  - leave COMPOSE_FILE in .env as it is"
    fi
    echo "  - run docker compose pull"
    _old="$(bin_to_carry)"
    if [ -n "$_old" ]; then
        echo "  - copy the recover bin out of $_old into the $BIN_VOLUME volume"
    fi
    echo "  - run docker compose up -d --remove-orphans"
    print_keys
    exit 0
fi

# --- 3. Back up the database and the config -----------------------------------
mkdir -p "$BACKUP_ROOT"
mkdir "$SNAP"
if $DB_BACKUP; then
    PG="$(platform_container otodock-postgres)" || exit 1
    [ -n "$PG" ] || fail "no running otodock-postgres container found: start the stack
  (docker compose up -d) and re-run, or pass --no-db-backup when you have a
  fresh backup of your own."
    DUMP="$SNAP/otodock-otodock-$TS.sql.gz"
    say "backing up the database to $DUMP"
    docker exec "$PG" pg_dump -U otodock --clean --if-exists otodock | gzip > "$DUMP.part"
    mv "$DUMP.part" "$DUMP"
else
    say "skipping the database dump (--no-db-backup)"
fi

say "copying .env and the compose files to $SNAP"
for f in "${CONFIG_FILES[@]}"; do
    if [ -f "$f" ]; then
        cp "$f" "$SNAP/$f"
        cmp -s "$f" "$SNAP/$f" || fail "the copy of $f in $SNAP does not match it."
    fi
done
# The snapshot is a restore.sh run directory (the database and the env copy,
# no trees): its manifest carries the release and the credential key
# fingerprints, so a rollback through restore.sh gets the same checks.
if [ -n "$DUMP" ]; then
    _keys="$(docker exec "$PG" psql -U otodock -d otodock -tAc \
        "SELECT value FROM platform_settings WHERE key = 'credential_key_fingerprints'" 2>/dev/null | head -n1 || true)"
    # A 1.7.1 database has no fingerprint row yet; its secret floor record
    # lets restore.sh check the rollback's keys the way backup.sh's manifest does.
    _floor="$(docker exec "$PG" psql -U otodock -d otodock -tAc \
        "SELECT value FROM platform_settings WHERE key = 'secret_floor_fingerprints'" 2>/dev/null | head -n1 || true)"
    {
        echo "otodock_version=${FROM:-unknown}"
        echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "topology=docker"
        echo "project=otodock"
        echo "database=otodock"
        echo "db=$(basename "$DUMP")"
        echo "env=1"
        echo "agents=skipped"
        echo "ssh_keys=skipped"
        case "$_keys" in \{*\}) echo "credential_keys=$_keys" ;; esac
        case "$_floor" in \{*\}) echo "credential_keys_floor=$_floor" ;; esac
        echo "errors=0"
        echo "complete=1"
    } > "$SNAP/manifest"
fi
STAGE=armed

# --- 4. The new files and the two .env lines ----------------------------------
say "replacing docker-compose.yml and docker-compose.phone.yml with $TARGET's"
put_file "$NEW/docker-compose.phone.yml" docker-compose.phone.yml
put_file "$NEW/docker-compose.yml" docker-compose.yml

if $CF_WRITE; then
    rewrite_env .env "$TMP/env" "$TARGET" "$CF_NEW"
    say "setting OTODOCK_VERSION=$TARGET and COMPOSE_FILE=$CF_NEW in .env"
else
    rewrite_env .env "$TMP/env" "$TARGET"
    say "setting OTODOCK_VERSION=$TARGET in .env"
fi
# In place, never a new file: the running proxy has .env bind-mounted, and its
# owner and mode stay what they are.
cmp -s "$TMP/env" .env || cat "$TMP/env" > .env

compose_check config -q \
    || fail "docker compose does not accept the upgraded files (the message above)."

# --- 5. Pull, then start -----------------------------------------------------
if [ -n "$COMPOSE_DIR" ]; then
    say "pulling the images (a release candidate: images present only locally are kept)"
    docker compose pull --ignore-pull-failures || true
    _images="$(compose_check config --images)"
    _missing=()
    while IFS= read -r img; do
        [ -n "$img" ] || continue
        docker image inspect "$img" >/dev/null 2>&1 || _missing+=("$img")
    done <<< "$_images"
    [ "${#_missing[@]}" -eq 0 ] || fail "these images are neither on the registry nor on this host: ${_missing[*]}"
else
    say "pulling the $TARGET images (the running stack keeps serving meanwhile)"
    docker compose pull
fi

_old="$(bin_to_carry)"
if [ -n "$_old" ]; then
    carry_recover_bin "$_old"
fi

STAGE=up
say "starting $TARGET (docker compose up -d --remove-orphans)"
# Compose starts the services that depend on otodock-proxy only once it is
# healthy, and stops waiting the moment Docker calls the container unhealthy.
# A first boot can outlast that on a slow host while the proxy is still
# starting, so only a proxy that is not running is a failure here: a running
# one gets the wait below, and up runs once more after it for the services
# that were waiting.
UP_AGAIN=false
if ! docker compose up -d --remove-orphans; then
    _proxy="$(platform_container otodock-proxy -a || true)"
    _status=""
    [ -n "$_proxy" ] && _status="$(docker inspect -f '{{.State.Status}}' "$_proxy" 2>/dev/null || true)"
    [ "$_status" = running ] || fail "docker compose up failed (the message above)."
    warn "docker compose up stopped waiting for otodock-proxy, which is still starting: waiting for it here"
    UP_AGAIN=true
fi

say "waiting for the server to report healthy"
_limit="${OTODOCK_UPGRADE_WAIT_S:-300}"
_waited=0
while :; do
    _proxy="$(platform_container otodock-proxy -a || true)"
    _status=""
    _state=""
    if [ -n "$_proxy" ]; then
        _status="$(docker inspect -f '{{.State.Status}}' "$_proxy" 2>/dev/null || true)"
        _state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$_proxy" 2>/dev/null || true)"
    fi
    [ "$_state" = healthy ] && break
    if [ -n "$_proxy" ] && [ "$_status" != running ]; then
        fail "otodock-proxy is not running (state: ${_status:-unknown}): its log has the cause
  (docker compose logs otodock-proxy)."
    fi
    [ "$_waited" -lt "$_limit" ] || fail "otodock-proxy did not report healthy within ${_limit}s (state: ${_state:-not running}).
  A large database can take longer to migrate: docker compose ps shows when it
  turns healthy (OTODOCK_UPGRADE_WAIT_S raises this wait)."
    sleep 5
    _waited=$((_waited + 5))
done
if $UP_AGAIN; then
    say "starting the services that waited on the server (docker compose up -d --remove-orphans)"
    docker compose up -d --remove-orphans || fail "docker compose up failed (the message above)."
fi
STAGE=finished

echo
say "done: this install runs $TARGET (was ${FROM:-unknown})."
if [ -n "$DUMP" ]; then
    echo "  Database dump:      $DUMP"
fi
echo "  Old files:          $SNAP"
echo "  Replaced:           docker-compose.yml, docker-compose.phone.yml"
if $CF_WRITE; then
    echo "  .env:               OTODOCK_VERSION=$TARGET, COMPOSE_FILE=$CF_NEW"
else
    echo "  .env:               OTODOCK_VERSION=$TARGET"
fi
print_keys
echo "  The release notes: https://github.com/${REPO}/releases/tag/v$TARGET"
echo "  The old images stay on disk until you remove them (docker image ls 'ghcr.io/otodock/*')."
