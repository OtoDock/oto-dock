#!/usr/bin/env bash
#
# Boot the three release images and prove each one answers, before any
# version tag points at them (.github/workflows/release-images.yml runs it on
# the digest-addressed images of each architecture; a maintainer runs it on a
# local build the same way).
#
#   scripts/image-boot-check.sh PROXY_IMAGE FILE_TOOLS_IMAGE PHONE_IMAGE
#
# In order, on one throwaway network: a Postgres at VERSIONS.md's
# POSTGRES_IMAGE; the proxy with the security options the compose file gives
# it, until /health answers (a first boot builds the bundled MCP venvs and
# migrates); the phone service pointed at that proxy, until its own /health
# answers (it serves only once the proxy pushed its configuration); the file
# tools, until /health answers with the headless browser present. Each image's
# org.opencontainers.image.version label must equal VERSIONS.md's
# OTODOCK_VERSION (an rc image carries the release's version).
#
# Nothing is published on a host port and every name carries a random
# suffix, so a running install on the same host is never touched; the probes
# run with each image's own python3 (the file-tools image has no curl). The
# containers and the network are removed on exit; on a failure the process
# tables, the proxy's thread stacks and the log tails are printed first.
#
# Environment: OTODOCK_BOOT_WAIT_S (the proxy's first-boot budget, default
# 900), OTODOCK_BOOT_SERVICE_WAIT_S (phone and file tools, default 180).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ $# -eq 3 ] || { echo "usage: $0 PROXY_IMAGE FILE_TOOLS_IMAGE PHONE_IMAGE" >&2; exit 2; }
PROXY_IMAGE="$1" FILE_TOOLS_IMAGE="$2" PHONE_IMAGE="$3"
PROXY_WAIT_S="${OTODOCK_BOOT_WAIT_S:-900}"
SERVICE_WAIT_S="${OTODOCK_BOOT_SERVICE_WAIT_S:-180}"
POSTGRES_IMAGE="$(bash "$ROOT/scripts/versions.sh" POSTGRES_IMAGE)"
VERSION="$(bash "$ROOT/scripts/versions.sh" OTODOCK_VERSION)"
[ -n "$POSTGRES_IMAGE" ] && [ -n "$VERSION" ] || { echo "POSTGRES_IMAGE or OTODOCK_VERSION missing from VERSIONS.md" >&2; exit 2; }

SUFFIX="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
NET="otodock-boot-$SUFFIX"
PG="otodock-boot-pg-$SUFFIX"
PROXY="otodock-boot-proxy-$SUFFIX"
PHONE="otodock-boot-phone-$SUFFIX"
FILE_TOOLS="otodock-boot-file-tools-$SUFFIX"
PG_PASSWORD="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
API_KEY="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
STARTED=()

say() { echo "==> $*"; }

cleanup() {
    local c
    for c in "${STARTED[@]}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# On a failure: every container's process table (host pids, state and kernel
# wait channel: a probe stuck in the kernel shows as D there), the proxy's
# Python stacks (it registers SIGUSR1 for them, proxy/startup.py), the log
# tails, and the kernel's AppArmor and audit lines where this user may read
# them (a namespace the host denies is logged there, not in the container).
fail() {
    local c kmsg
    echo "FAIL: $*" >&2
    for c in "${STARTED[@]}"; do
        echo "--- processes of $c ---" >&2
        docker top "$c" -eo pid,ppid,stat,wchan:32,etime,args >&2 2>&1 || true
    done
    if [ "$(docker inspect -f '{{.State.Running}}' "$PROXY" 2>/dev/null)" = true ]; then
        docker kill --signal=USR1 "$PROXY" >/dev/null 2>&1 || true
        sleep 2
    fi
    for c in "${STARTED[@]}"; do
        echo "--- last log lines of $c ---" >&2
        docker logs --tail 120 "$c" >&2 2>&1 || true
    done
    kmsg="$(dmesg 2>/dev/null || sudo -n dmesg 2>/dev/null || true)"
    if [ -n "$kmsg" ]; then
        echo "--- kernel apparmor and audit lines ---" >&2
        printf '%s\n' "$kmsg" | grep -i -E 'apparmor|audit' | tail -40 >&2 || true
    fi
    exit 1
}

# A GET from inside the container, with the image's own Python: exit 0 when
# the URL answers 200 and its JSON holds every KEY=VALUE given.
probe() { # probe <container> <url> [key=value ...]
    local container="$1" url="$2"; shift 2
    docker exec "$container" python3 -c '
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=5) as r:
    body = json.load(r)
for pair in sys.argv[2:]:
    key, _, want = pair.partition("=")
    if str(body.get(key)).lower() != want.lower():
        sys.exit(1)
' "$url" "$@" >/dev/null 2>&1
}

wait_for() { # wait_for <seconds> <what> <container> <url> [key=value ...]
    local budget="$1" what="$2" container="$3" start=$SECONDS
    shift 3
    until probe "$container" "$@"; do
        [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = true ] \
            || fail "$what exited before it answered"
        [ $((SECONDS - start)) -lt "$budget" ] || fail "$what did not answer $1 within ${budget}s"
        sleep 5
    done
    say "$what answers $1 after $((SECONDS - start))s"
}

check_label() { # check_label <image>
    local got
    docker image inspect "$1" >/dev/null 2>&1 || docker pull -q "$1" >/dev/null || fail "cannot pull $1"
    got="$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$1")"
    [ "$got" = "$VERSION" ] || fail "$1 is labelled version '$got', VERSIONS.md says $VERSION"
}

for image in "$PROXY_IMAGE" "$FILE_TOOLS_IMAGE" "$PHONE_IMAGE"; do check_label "$image"; done
say "the three images carry version $VERSION"

# The agent sandbox needs unprivileged user namespaces. A kernel that
# restricts them to processes whose AppArmor profile grants it (Ubuntu
# 24.04, GitHub's runners included) gets the scoped otodock_userns profile,
# the documented path; the system-wide restriction stays on.
APPARMOR=unconfined
if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = 1 ]; then
    sudo -n bash "$ROOT/scripts/setup-apparmor-userns.sh" >/dev/null \
        || fail "this host restricts user namespaces and the otodock_userns profile could not be loaded (needs sudo)"
    APPARMOR=otodock_userns
fi

docker network create "$NET" >/dev/null

STARTED+=("$PG")
docker run -d --name "$PG" --network "$NET" \
    -e POSTGRES_DB=otodock -e POSTGRES_USER=otodock -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    "$POSTGRES_IMAGE" >/dev/null
start=$SECONDS
# Over TCP: the entrypoint's first server listens on the socket only and is
# stopped right after initdb.
until docker exec "$PG" pg_isready -q -h 127.0.0.1 -U otodock; do
    [ $((SECONDS - start)) -lt 120 ] || fail "Postgres did not accept connections within 120s"
    sleep 2
done
say "Postgres ($POSTGRES_IMAGE) accepts connections"

STARTED+=("$PROXY")
docker run -d --name "$PROXY" --network "$NET" --user 1000:1000 \
    --security-opt seccomp=unconfined --security-opt "apparmor=$APPARMOR" \
    --security-opt systempaths=unconfined \
    -e RUNNING_IN_DOCKER=true -e PLATFORM_DATA_DIR=/opt/otodock -e PROXY_PORT=8400 \
    -e DATABASE_URL="postgresql://otodock:$PG_PASSWORD@$PG:5432/otodock" \
    -e PROXY_API_KEY="$API_KEY" \
    "$PROXY_IMAGE" >/dev/null
wait_for "$PROXY_WAIT_S" "the proxy" "$PROXY" http://127.0.0.1:8400/health status=ok
# A boot that answered but warned (a probe that timed out, a pin that
# drifted) is worth reading here: the warnings are the only trace it leaves.
if docker logs "$PROXY" 2>&1 | grep -q -E ' (WARNING|ERROR) '; then
    say "the proxy's warnings:"
    docker logs "$PROXY" 2>&1 | grep -E ' (WARNING|ERROR) ' | head -20
fi

STARTED+=("$PHONE")
docker run -d --name "$PHONE" --network "$NET" --user 1000:1000 \
    -e PROXY_URL="http://$PROXY:8400" -e PROXY_API_KEY="$API_KEY" \
    "$PHONE_IMAGE" >/dev/null
wait_for "$SERVICE_WAIT_S" "the phone service" "$PHONE" http://127.0.0.1:9093/health status=ok service=phone-server

STARTED+=("$FILE_TOOLS")
docker run -d --name "$FILE_TOOLS" --network "$NET" "$FILE_TOOLS_IMAGE" >/dev/null
wait_for "$SERVICE_WAIT_S" "the file tools" "$FILE_TOOLS" http://127.0.0.1:8932/health status=ok render=true

say "all three images boot and answer"
