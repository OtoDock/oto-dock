#!/usr/bin/env bash
#
# Install OtoDock in ~/otodock the way a person does: install.sh, then the install
# guide's one manual step (the public address in .env). The rig's own additions are
# the file wipe.sh looks for, written first so that a failed install can be wiped,
# and the override that mounts its Claude Code wrapper, placed before install.sh
# because install.sh lists it only when it is already there.
#
#   fresh.sh --public-url URL --tag TAG --candidate DIR [--ref REF] [--plain]
#
# --ref takes install.sh from that ref of the public repository with the real
# curl (a tag, or an rc's commit); without it the candidate's tree answers.
# --plain leaves the wrapper out: exactly what a person gets, for a walk in a browser.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rig/install/guard.sh
. "$HERE/guard.sh"
DIR="$HOME/otodock" URL="" TAG="" CANDIDATE="" REF="" PLAIN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --public-url) URL="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --candidate) CANDIDATE="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --plain) PLAIN=1; shift ;;
        *) echo "fresh: unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$URL" ] && [ -n "$TAG" ] || { echo "fresh: --public-url and --tag are required" >&2; exit 2; }
[ ! -e "$DIR" ] || { echo "fresh: $DIR exists: wipe first" >&2; exit 2; }

# The override, with the compute service's part where the install's compose
# file has that service (a previous release's has none, and compose refuses an
# override of a service it does not define).
rig_override() {
    cp "$HERE/scripted-engine/compose-override.yml" "$DIR/docker-compose.override.yml"
    if grep -q '^  otodock-compute:' "$DIR/docker-compose.yml" 2>/dev/null; then
        cat "$HERE/scripted-engine/compose-override.compute.yml" >> "$DIR/docker-compose.override.yml"
    fi
}

mkdir -p "$DIR"
: > "$DIR/.otodock-rig-install"
if [ "$PLAIN" = 0 ]; then
    mkdir -p "$DIR/rig-bin"
    cp "$HERE/scripted-engine/claude" "$DIR/rig-bin/claude"
    rig_override
fi
cd "$DIR"

export OTODOCK_VERSION="$TAG"
if [ -n "$REF" ]; then
    curl -fsSLO "https://raw.githubusercontent.com/OtoDock/oto-dock/$REF/scripts/install.sh"
    OTODOCK_REF="$REF" bash install.sh
else
    [ -n "$CANDIDATE" ] || { echo "fresh: --candidate or --ref is required" >&2; exit 2; }
    shim="$(mktemp -d)"
    trap 'rm -rf "$shim"' EXIT
    cp "$HERE/curl-shim" "$shim/curl"
    PATH="$shim:$PATH" RIG_CANDIDATE="$CANDIDATE" curl -fsSLO \
        "https://raw.githubusercontent.com/OtoDock/oto-dock/main/scripts/install.sh"
    PATH="$shim:$PATH" RIG_CANDIDATE="$CANDIDATE" bash install.sh
fi

tz="${RIG_TZ:-$(cat /etc/timezone 2>/dev/null || true)}"
{
    echo "DASHBOARD_PUBLIC_URL=$URL"
    echo "TZ=${tz:-UTC}"
    echo "OTODOCK_VERSION=$TAG"
    if [ "$PLAIN" = 0 ]; then
        echo "CLAUDE_BIN=/opt/rig-bin/claude"
        # The scenarios leave one person many warm chats, each a session.
        echo "OTODOCK_MAX_SESSIONS_PER_USER=64"
    fi
} >> .env
unset OTODOCK_VERSION
[ "$PLAIN" = 1 ] || rig_override
docker compose up -d
for _ in $(seq 1 90); do
    if curl -fsS --max-time 5 http://127.0.0.1:8400/health >/dev/null 2>&1; then
        echo "fresh: the install answers"
        exit 0
    fi
    sleep 5
done
echo "fresh: /health did not answer" >&2
docker compose ps -a >&2 || true
exit 1
