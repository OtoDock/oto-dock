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
# POSTGRES_IMAGE; the proxy as the compose file runs it, with no security
# option and no device, until /health says the database, the tools and the
# scheduler are ok (a first boot migrates and listens; the bundled tools'
# environments come with the image, which this check fails without), and
# whose container cannot start the sandbox; the same image started as the
# compose file starts the compute service (the three security options and the
# tun device), whose pasta must be VERSIONS.md's PASST_VERSION and must run
# the sandbox's shape (the netns launcher wrapping bwrap with a fresh /proc),
# and whose own start check (compute_start.py --check) must answer ok;
# the phone service pointed at that proxy, until its own /health
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
PASST_PIN="$(bash "$ROOT/scripts/versions.sh" PASST_VERSION)"
[ -n "$POSTGRES_IMAGE" ] && [ -n "$VERSION" ] && [ -n "$PASST_PIN" ] \
    || { echo "POSTGRES_IMAGE, OTODOCK_VERSION or PASST_VERSION missing from VERSIONS.md" >&2; exit 2; }
# pasta sets up the sandbox's tap through /dev/net/tun, which the compose file
# maps into the compute service: a host without it cannot run the image the
# way an install does, and docker run would fail with an opaque device error.
[ -c /dev/net/tun ] || {
    echo "this host has no /dev/net/tun: the compute service needs it mapped in (docker-compose.yml: devices: /dev/net/tun)." >&2
    echo "Load the tun module (sudo modprobe tun) or run the check on a host that has the device." >&2
    exit 2
}

SUFFIX="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
NET="otodock-boot-$SUFFIX"
PG="otodock-boot-pg-$SUFFIX"
PROXY="otodock-boot-proxy-$SUFFIX"
COMPUTE="otodock-boot-compute-$SUFFIX"
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
# the URL answers 200 and its JSON holds every KEY=VALUE given. A dot in KEY
# goes one block down: parts.tools is the tools' word inside parts.
probe() { # probe <container> <url> [key=value ...]
    local container="$1" url="$2"; shift 2
    docker exec "$container" python3 -c '
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=5) as r:
    body = json.load(r)
for pair in sys.argv[2:]:
    key, _, want = pair.partition("=")
    node = body
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if str(node).lower() != want.lower():
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

# The proxy as the compose file runs it: no security option, no device. It
# starts no sandbox, so it boots with none of them.
STARTED+=("$PROXY")
docker run -d --name "$PROXY" --network "$NET" --user 1000:1000 \
    -e RUNNING_IN_DOCKER=true -e PLATFORM_DATA_DIR=/opt/otodock -e PROXY_PORT=8400 \
    -e DATABASE_URL="postgresql://otodock:$PG_PASSWORD@$PG:5432/otodock" \
    -e PROXY_API_KEY="$API_KEY" \
    "$PROXY_IMAGE" >/dev/null
wait_for "$PROXY_WAIT_S" "the proxy's database" "$PROXY" http://127.0.0.1:8400/health parts.database=ok
# The image carries the bundled tools' environments. A boot that found one to
# build says so once and leaves the tool out, and its /health never reads the
# tools ok: named here, before the wait below would run out (captured, no
# pipe into grep: a reader that closes first fails the pipeline).
boot_log="$(docker logs "$PROXY" 2>&1)"
if grep -q 'need their environment built' <<<"$boot_log"; then
    fail "the proxy's boot found tool environments to build: the image must carry them"
fi
# This proxy runs alone. No compute machine signs in to it, so its /health
# never says ready (proxy/services/infra/health_parts.py). The wait is for
# what a proxy brings up by itself: the database answers, the tools are
# ready, the scheduler runs.
wait_for "$PROXY_WAIT_S" "the proxy" "$PROXY" http://127.0.0.1:8400/health \
    status=ok parts.database=ok parts.tools=ok parts.scheduler=ok
# A boot that answered but warned (a probe that timed out, a pin that
# drifted) is worth reading here: the warnings are the only trace it leaves.
# No pipe into a reader that may close first: under pipefail a writer killed
# by a broken pipe is a failed pipeline, which hides the warnings or ends the
# script.
warnings="$(docker logs "$PROXY" 2>&1 | grep -E ' (WARNING|ERROR) ' || true)"
if [ -n "$warnings" ]; then
    say "the proxy's warnings:"
    head -20 <<<"$warnings"
fi
# The sandbox's shape: the netns launcher wrapping one bwrap level with the
# sessions' pid, ipc and uts namespaces and a fresh /proc, as the compute
# machine's own check at its start runs it (proxy/core/sandbox/sandbox_render.py,
# preflight).
SHAPE=(/opt/otodock/proxy/scripts/oto-sandbox-net --block-private --
       bwrap --unshare-pid --unshare-ipc --unshare-uts --die-with-parent --share-net
       --unshare-user --uid 1000 --gid 1000 --ro-bind / / --dev /dev --proc /proc -- true)
# The proxy's container holds none of the sandbox's privileges: the shape
# must not start there (no tun device for pasta's tap, and Docker's default
# profile refuses the user namespace).
if timeout 60 docker exec "$PROXY" "${SHAPE[@]}" >/dev/null 2>&1; then
    fail "the sandbox shape runs in the proxy's own container: it must hold none of the sandbox's privileges"
fi
say "the proxy's container cannot start the sandbox"
# The pinned pasta, then the shape, in the image started as the compose file
# starts the compute service. A pasta change breaks this part (the 2026
# builds clone the command into new mount and pid namespaces and mount /proc
# themselves), and /health never reaches it. A nested level (bwrap in bwrap)
# is not asked for: a GitHub arm64 runner cannot start the second one.
# pasta's FIRST line must be its version: the image carries the passt.avx2
# build beside passt, so an AVX2 host (the GitHub amd64 runner) runs it with
# no warning ahead, and a missing one ("Can't run AVX2 build, using non-AVX2
# version" first) fails here. The whole output is read and the first line cut
# in the shell: a reader that closes early kills the writer with a broken
# pipe, and pipefail makes that the script's own exit.
STARTED+=("$COMPUTE")
COMPUTE_OPTS=(--rm --name "$COMPUTE" --network "$NET" --user 1000:1000
              --security-opt seccomp=unconfined --security-opt "apparmor=$APPARMOR"
              --security-opt systempaths=unconfined --device /dev/net/tun)
AS_COMPUTE=(docker run "${COMPUTE_OPTS[@]}" "$PROXY_IMAGE")
got="$("${AS_COMPUTE[@]}" pasta --version 2>&1)"
got="${got%%$'\n'*}"
[ "$got" = "pasta $PASST_PIN" ] || fail "the proxy image's pasta says '$got', VERSIONS.md pins $PASST_PIN"
if ! out="$(timeout 60 "${AS_COMPUTE[@]}" "${SHAPE[@]}" 2>&1)"; then
    fail "the sandbox shape (pasta wrapping bwrap) does not run in the image started as the compute service: ${out:-timed out or no output}"
fi
say "pasta $PASST_PIN runs the sandbox shape in the image started as the compute service"
# The compute service's own start check, as scripts/otodock-remote-compute.sh runs it before it starts a
# machine: the image's satellite is the platform's version, and the sandbox's preflight passes with the
# compute service's options. An image whose satellite does not import fails here, not at an install.
SAT_VERSION="$(sed -nE 's/^SATELLITE_VERSION = "([^"]+)".*/\1/p' "$ROOT/satellite/config.py")"
checked="$(timeout 120 docker run "${COMPUTE_OPTS[@]}" -e HOME=/tmp/otodock-check -e OTODOCK_COMPUTE_MCPS_DIR=/opt/otodock/mcps \
    "$PROXY_IMAGE" /opt/otodock/satellite/venv/bin/python /opt/otodock/proxy/compute_start.py --check "$SAT_VERSION" 2>&1 || true)"
checked="${checked##*$'\n'}"
[ "$checked" = ok ] || fail "the compute service's own start check does not pass in the image: ${checked:-no answer}"
say "the compute service's own start check passes in the image (satellite $SAT_VERSION)"

# The kit file check_app measures a client's brand against, where the
# proxy serves it from: without it every brand check measures nothing.
docker exec "$PROXY" test -s /opt/otodock/dashboard/dist/ui-kit/otodock-tokens.css \
    || fail "the proxy image carries no dashboard/dist/ui-kit/otodock-tokens.css"
say "the kit file is in the proxy image"

STARTED+=("$PHONE")
docker run -d --name "$PHONE" --network "$NET" --user 1000:1000 \
    -e PROXY_URL="http://$PROXY:8400" -e PROXY_API_KEY="$API_KEY" \
    "$PHONE_IMAGE" >/dev/null
wait_for "$SERVICE_WAIT_S" "the phone service" "$PHONE" http://127.0.0.1:9093/health status=ok service=phone-server

STARTED+=("$FILE_TOOLS")
docker run -d --name "$FILE_TOOLS" --network "$NET" "$FILE_TOOLS_IMAGE" >/dev/null
wait_for "$SERVICE_WAIT_S" "the file tools" "$FILE_TOOLS" http://127.0.0.1:8932/health status=ok render=true

say "all three images boot and answer"
