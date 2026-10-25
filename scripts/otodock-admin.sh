#!/usr/bin/env bash
#
# OtoDock's admin command line: sign-in recovery when nobody can sign in,
# support information for a bug report, and the platform's background jobs.
#
#   bash otodock-admin.sh user reset-password you@example.com
#   bash otodock-admin.sh user reset-password you@example.com --password-stdin
#   bash otodock-admin.sh user disable-2fa you@example.com [--passkeys]
#   bash otodock-admin.sh support-info
#   bash otodock-admin.sh jobs list
#   bash otodock-admin.sh jobs run recover-bin-reap
#
# A Docker install: run it in the install folder, the one holding .env and
# docker-compose.yml (fetch it there with
#   curl -fsSLO https://raw.githubusercontent.com/OtoDock/oto-dock/v<version>/scripts/otodock-admin.sh
# for the release the install runs). It runs the command inside the
# platform's running otodock-proxy container, which reads the database the
# way the server does: no password is ever typed on a command line.
#
# A source install (a checkout with proxy/venv): it runs proxy/admin_cli.py
# with that venv and this shell's environment, so run it as the account the
# platform runs as and with the service's environment (on a systemd install
# the unit's EnvironmentFile and Environment lines).
#
# --password-stdin reads the new password from stdin; typed at a terminal it
# is asked twice and never echoed. OTODOCK_ADMIN_MODE=docker|source picks the
# branch; OTODOCK_COMPOSE_PROJECT names the compose project (default otodock).
# Exit status: 0 done, 1 refused, 2 usage.
set -euo pipefail

fail() { echo "otodock-admin: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
# The install folder: this script's own when it sits beside the compose
# files, else the one above scripts/ in a checkout.
INSTALL_DIR=""
if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    INSTALL_DIR="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../docker-compose.yml" ] || [ -d "$SCRIPT_DIR/../proxy" ]; then
    INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
fi
PROJECT="${OTODOCK_COMPOSE_PROJECT:-otodock}"
MODE="${OTODOCK_ADMIN_MODE:-}"
ACTOR="$(id -un 2>/dev/null || id -u)@$(hostname 2>/dev/null || echo unknown)"
CONTAINER_PROXY=/opt/otodock/proxy

# The running otodock-proxy container of this install: the compose project's,
# and when the install folder holds the compose files, the one started from
# that folder (one host may run a second stack under the same project name).
# More than one is refused, never guessed.
proxy_container() {
    command -v docker >/dev/null 2>&1 || return 0
    local ids id dir mine=""
    ids="$(docker ps --filter "label=com.docker.compose.project=$PROJECT" \
        --filter "label=com.docker.compose.service=otodock-proxy" --format '{{.ID}}' 2>/dev/null || true)"
    for id in $ids; do
        if [ -n "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
            dir="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
                "$id" 2>/dev/null || true)"
            [ -n "$dir" ] && [ -d "$dir" ] && dir="$(cd "$dir" && pwd -P)"
            [ "$dir" = "$INSTALL_DIR" ] || continue
        fi
        mine="$mine $id"
    done
    # shellcheck disable=SC2086  # the ids split on purpose
    set -- $mine
    if [ $# -gt 1 ]; then
        fail "more than one running otodock-proxy container belongs to this install ($*); stop the extra one"
    fi
    printf '%s' "${1:-}"
}

CID=""
if [ "$MODE" != source ]; then
    CID="$(proxy_container)" || exit 1
fi
if [ -z "$MODE" ]; then
    if [ -n "$CID" ]; then
        MODE=docker
    elif [ -n "$INSTALL_DIR" ] && [ -x "$INSTALL_DIR/proxy/venv/bin/python" ] \
            && [ -f "$INSTALL_DIR/proxy/admin_cli.py" ]; then
        MODE=source
    else
        MODE=docker
    fi
fi

# --password-stdin at a terminal: asked here, never echoed, and piped in (the
# container's stdin is no terminal, so it could not ask without echoing).
PASSWORD=""
ASKED=false
if [ "$MODE" = docker ] && [ -t 0 ]; then
    for arg in "$@"; do
        if [ "$arg" = --password-stdin ]; then
            read -rs -p "New password: " PASSWORD; echo >&2
            read -rs -p "Again: " _again; echo >&2
            [ "$PASSWORD" = "$_again" ] || fail "the two passwords differ"
            ASKED=true
        fi
    done
fi

case "$MODE" in
    docker)
        if [ -z "$CID" ]; then
            fail "no running otodock-proxy container belongs to this install (project $PROJECT).
  Start the stack (docker compose up -d) and run this again. While the platform
  cannot start, run it from the install folder with the database up:
    docker compose run --rm --no-deps --entrypoint $CONTAINER_PROXY/venv/bin/python otodock-proxy $CONTAINER_PROXY/admin_cli.py <command>"
        fi
        docker exec "$CID" test -f "$CONTAINER_PROXY/admin_cli.py" \
            || fail "this install's platform has no admin command line: it needs OtoDock 1.9.0 or later (upgrade.sh)"
        if $ASKED; then
            printf '%s\n' "$PASSWORD" | docker exec -i -e "OTODOCK_ADMIN_ACTOR=$ACTOR" "$CID" \
                "$CONTAINER_PROXY/venv/bin/python" "$CONTAINER_PROXY/admin_cli.py" "$@"
            exit $?
        fi
        exec docker exec -i -e "OTODOCK_ADMIN_ACTOR=$ACTOR" "$CID" \
            "$CONTAINER_PROXY/venv/bin/python" "$CONTAINER_PROXY/admin_cli.py" "$@"
        ;;
    source)
        [ -n "$INSTALL_DIR" ] && [ -x "$INSTALL_DIR/proxy/venv/bin/python" ] \
            || fail "no proxy/venv beside this script: run it from a source checkout, or from a Docker install's folder"
        cd "$INSTALL_DIR/proxy"
        OTODOCK_ADMIN_ACTOR="$ACTOR" exec venv/bin/python admin_cli.py "$@"
        ;;
    *)
        fail "OTODOCK_ADMIN_MODE is '$MODE': docker or source"
        ;;
esac
