#!/usr/bin/env bash
#
# OtoDock: add this server as a compute machine of an OtoDock install.
#
# The platform serves this body for one compute machine through
# /v1/satellite/bootstrap, after a header with its values (VALUES_B64) and two
# payloads: HOST_TOOL_B64 (scripts/otodock-remote-compute.sh, the host service)
# and APPARMOR_SETUP_B64 (scripts/setup-apparmor-userns.sh). An admin runs it
# with the command the Compute card shows:
#
#   curl -fsSL -H "X-Pairing-Token: ..." "https://.../v1/satellite/bootstrap?os=linux" | sudo bash
#
# It says what it will do, checks this server, makes the machine's secret
# here and sends only its hash to pair, then starts the platform's image under
# the systemd unit otodock-remote-compute.service. Until the pairing nothing on
# this server is changed but the image it downloads and, where the server
# limits user namespaces, the AppArmor profile the sandbox needs. Everything
# but the last line is a definition, so a download cut short runs nothing.

# Tests only: every path this script writes lives under this folder.
ROOT="${OTODOCK_COMPUTE_HOST_ROOT:-}"
NAME="otodock-remote-compute"
STATE="$ROOT/var/lib/$NAME"
TOOL_DIR="$ROOT/opt/$NAME"
UNIT="$ROOT/etc/systemd/system/$NAME.service"
TAG_RE='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'
REPO_RE='^(([A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:[0-9]+)?|localhost(:[0-9]+)?|[A-Za-z0-9-]+:[0-9]+)/)?[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$'
URL_RE='^https?://[]A-Za-z0-9.:[-]{1,300}$'
UNCHANGED="Nothing was changed."

say() { printf '%s\n' "$*"; }
refuse() { printf 'OtoDock: %s\n' "$*" >&2; exit 1; }

read_values() {
    local decoded key value
    decoded=$(printf '%s' "${VALUES_B64:-}" | base64 -d 2> /dev/null) || refuse "This command is damaged. $UNCHANGED"
    MACHINE_ID="" PAIRING_TOKEN="" PLATFORM_URL="" IMAGE="" IMAGE_PUBLIC="" SATELLITE_VERSION=""
    while IFS='=' read -r key value; do
        case "$key" in
            MACHINE_ID) if [[ $value =~ ^[A-Za-z0-9-]{1,64}$ ]]; then MACHINE_ID=$value; fi ;;
            PAIRING_TOKEN) if [[ $value =~ ^[A-Za-z0-9_-]{20,128}$ ]]; then PAIRING_TOKEN=$value; fi ;;
            PLATFORM_URL) if [[ $value =~ $URL_RE ]]; then PLATFORM_URL=$value; fi ;;
            IMAGE) IMAGE=$value ;;
            IMAGE_PUBLIC) if [[ $value =~ ^[01]$ ]]; then IMAGE_PUBLIC=$value; fi ;;
            SATELLITE_VERSION) if [[ $value =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9]+)*$ ]]; then SATELLITE_VERSION=$value; fi ;;
        esac
    done <<< "$decoded"
    REPOSITORY=${IMAGE%:*}
    TAG=${IMAGE##*:}
    if [ -z "$MACHINE_ID" ] || [ -z "$PAIRING_TOKEN" ] || [ -z "$PLATFORM_URL" ] || [ -z "$IMAGE_PUBLIC" ] \
        || [ -z "$SATELLITE_VERSION" ] || [[ ! $REPOSITORY =~ $REPO_RE ]] || [[ ! $TAG =~ $TAG_RE ]]; then
        refuse "This command is damaged. $UNCHANGED"
    fi
}

check_server() {
    if [ "$(id -u)" != "0" ]; then refuse "Run the command with sudo. $UNCHANGED"; fi
    if [ "$(uname -s)" != "Linux" ]; then refuse "A compute machine runs on Linux. $UNCHANGED"; fi
    case "$(uname -m)" in
        x86_64 | amd64) ARCH=amd64 ;;
        aarch64 | arm64) ARCH=arm64 ;;
        *) refuse "A compute machine needs an x86-64 or ARM64 processor. $UNCHANGED" ;;
    esac
    local systemd_version
    systemd_version=$(systemctl --version 2> /dev/null | sed -n '1s/^systemd \([0-9]*\).*/\1/p')
    if [ ! -d "$ROOT/run/systemd/system" ] || [ -z "$systemd_version" ] || [ "$systemd_version" -lt 236 ]; then
        refuse "This server needs systemd 236 or later as its service manager. $UNCHANGED"
    fi
    local docker_bin
    docker_bin=$(command -v docker || true)
    if [ -z "$docker_bin" ]; then refuse "Docker is not installed. Install Docker Engine, then run the command again. $UNCHANGED"; fi
    case "$(readlink -f "$docker_bin")" in
        /snap/*) refuse "Docker from snap cannot run a compute machine. Install Docker Engine from Docker's own packages. $UNCHANGED" ;;
    esac
    local info version
    info=$(docker info --format '{{.ServerVersion}}|{{.OperatingSystem}}' 2> /dev/null) \
        || refuse "Docker is installed but its service does not answer. Start it, then run the command again. $UNCHANGED"
    case "$info" in
        *"Docker Desktop"*) refuse "Docker Desktop cannot run a compute machine. Use Docker Engine. $UNCHANGED" ;;
    esac
    version=${info%%|*}
    if [[ ! $version =~ ^([0-9]+)\.([0-9]+) ]] || [ "${BASH_REMATCH[1]}" -lt 20 ] \
        || { [ "${BASH_REMATCH[1]}" -eq 20 ] && [ "${BASH_REMATCH[2]}" -lt 10 ]; }; then
        refuse "Docker ${version:-of an unknown version} is too old. A compute machine needs Docker 20.10 or later. $UNCHANGED"
    fi
    if [ ! -e "$ROOT/dev/net/tun" ]; then refuse "This server has no /dev/net/tun, which the sandbox needs. $UNCHANGED"; fi
    AGAIN=0
    if [ -f "$STATE/compute.conf" ]; then
        local installed
        installed=$(sed -n 's/^MACHINE_ID=\([A-Za-z0-9-]*\)$/\1/p' "$STATE/compute.conf")
        if [ "$installed" != "$MACHINE_ID" ]; then
            refuse "This server already runs the compute machine ${installed:-of another install}. Remove it first: sudo $TOOL_DIR/$NAME remove. $UNCHANGED"
        fi
        AGAIN=1
    fi
    APPARMOR=unconfined
    if [ "$(cat "$ROOT/proc/sys/kernel/apparmor_restrict_unprivileged_userns" 2> /dev/null || true)" = "1" ]; then
        APPARMOR=otodock_userns
    fi
    RUNS_AS=$(getent passwd 1000 2> /dev/null | cut -d: -f1 || true)
}

tell_plan() {
    say "OtoDock adds this server as a compute machine. It will:"
    say "  - use the image $IMAGE$([ "$IMAGE_PUBLIC" = 1 ] && printf ' (downloaded if it is not here, about 4 GB)')"
    if [ "$APPARMOR" = otodock_userns ]; then
        say "  - let the sandbox use user namespaces, with the AppArmor profile otodock_userns"
    fi
    say "  - pair with OtoDock at $PLATFORM_URL"
    say "  - keep its files in /var/lib/$NAME and its program in /opt/$NAME"
    say "  - run as the service $NAME, in a Docker container, network and volumes of that name"
    if [ "$AGAIN" = 1 ]; then say "  - keep what this machine already holds here: it is paired again"; fi
    if [ -n "$RUNS_AS" ]; then
        say "The account $RUNS_AS (user id 1000) on this server can read what the machine runs and its sign-in."
    fi
}

# Room where Docker keeps its images: the image (about 6 GB unpacked) when it
# is not here yet, and the tools the machine builds there (a few GB).
check_disk() {
    local root free_kb need_gb=5
    if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then need_gb=10; fi
    root=$(docker info --format '{{.DockerRootDir}}' 2> /dev/null || true)
    free_kb=$(df -Pk "${root:-/var/lib/docker}" 2> /dev/null | awk 'NR == 2 {print $4}' || true)
    if [[ $free_kb =~ ^[0-9]+$ ]] && [ "$free_kb" -lt $(( need_gb * 1024 * 1024 )) ]; then
        refuse "This server has $(( free_kb / 1024 / 1024 )) GB free where Docker keeps its images. A compute machine needs about $need_gb GB. $UNCHANGED"
    fi
}

check_image() {
    PULLED_NOW=0
    check_disk
    if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
        if [ "$IMAGE_PUBLIC" != 1 ]; then
            refuse "The image $IMAGE is not on this server. This install was built from source, so load its image here first (docker load), then run this command again. $UNCHANGED"
        fi
        say "Downloading $IMAGE ..."
        docker pull --quiet "$IMAGE" > /dev/null || refuse "The image $IMAGE could not be downloaded. $UNCHANGED"
        PULLED_NOW=1
        UNCHANGED="The image stays here for the next try. Nothing else was changed."
    fi
    local arch found
    arch=$(docker image inspect --format '{{.Architecture}}' "$IMAGE" 2> /dev/null || true)
    if [ "$arch" != "$ARCH" ]; then refuse "The image $IMAGE is built for ${arch:-another processor}, not this server's $ARCH. $UNCHANGED"; fi
    found=$(docker run --rm --network none --entrypoint /bin/sh "$IMAGE" -c \
        "sed -n 's/^SATELLITE_VERSION = \"\\(.*\\)\"\$/\\1/p' /opt/otodock/satellite/config.py" 2> /dev/null || true)
    if [ "$found" != "$SATELLITE_VERSION" ]; then
        refuse "The image $IMAGE runs satellite ${found:-of no known version}, this platform runs $SATELLITE_VERSION. Load the platform's own image here, then run this command again. $UNCHANGED"
    fi
}

check_sandbox() {
    if [ "$APPARMOR" = otodock_userns ]; then
        say "Adding the AppArmor profile otodock_userns ..."
        if [ -n "$ROOT" ]; then
            say "(a test folder: the AppArmor setup does not run)"
        else
            printf '%s' "$APPARMOR_SETUP_B64" | base64 -d > "$WORK/setup-apparmor-userns.sh"
            bash "$WORK/setup-apparmor-userns.sh" > /dev/null || refuse "The AppArmor profile otodock_userns could not be added."
        fi
        UNCHANGED="The AppArmor profile otodock_userns stays. ${UNCHANGED/Nothing was/Nothing else was}"
    fi
    local said
    said=$(bash "$WORK/$NAME" preflight "$IMAGE" "$SATELLITE_VERSION" "$APPARMOR" 2>&1 | tail -n 1 || true)
    if [ "$said" != "ok" ]; then refuse "This server cannot run the platform's sandbox: ${said:-no answer}. $UNCHANGED"; fi
}

pair() {
    local secret hash code detail
    secret=$(head -c 48 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')
    hash=$(printf '%s' "$secret" | sha256sum | cut -d' ' -f1)
    printf '{"machine_id": "%s", "machine_secret": "%s"}\n' "$MACHINE_ID" "$secret" > "$WORK/credential"
    secret=""
    printf 'X-Pairing-Token: %s\nContent-Type: application/json\n' "$PAIRING_TOKEN" > "$WORK/headers"
    printf '{"secret_hash": "%s"}' "$hash" > "$WORK/body"
    local proto=()
    case "$PLATFORM_URL" in https://*) proto=(--proto '=https') ;; esac
    code=$(curl -sS --max-time 60 ${proto[@]+"${proto[@]}"} -o "$WORK/answer" -w '%{http_code}' \
        -H "@$WORK/headers" --data-binary "@$WORK/body" \
        "$PLATFORM_URL/v1/satellite/bootstrap" 2> /dev/null) || refuse "OtoDock at $PLATFORM_URL did not answer. $UNCHANGED"
    if [ "$code" = "200" ]; then return 0; fi
    detail=$(sed -n 's/.*"detail": *"\([^"]*\)".*/\1/p' "$WORK/answer" 2> /dev/null | head -n 1 || true)
    if [ "$code" = "401" ]; then
        refuse "The pairing command was used or has expired. Pair the machine again from its card. $UNCHANGED"
    fi
    refuse "OtoDock refused the pairing: ${detail:-answer $code}. $UNCHANGED"
}

# Every write ends the function at its first failure: a caller's || turns the
# shell's own stop-on-error off for everything inside it.
write_files() {
    install -d -m 0700 "$STATE" "$STATE/sign-in" || return 1
    install -m 0600 "$WORK/credential" "$STATE/sign-in/credential" || return 1
    rm -f -- "$WORK/credential" || return 1
    chown 1000:1000 "$STATE/sign-in" "$STATE/sign-in/credential" || return 1
    local pulled="" mem=""
    if [ "$AGAIN" = 1 ]; then
        pulled=$(sed -n 's/^PULLED=\([A-Za-z0-9_. -]*\)$/\1/p' "$STATE/compute.conf") || return 1
        mem=$(sed -n 's/^MEM_LIMIT=\([0-9]\{1,15\}[kmgKMG]\{0,1\}\)$/\1/p' "$STATE/compute.conf") || return 1
    fi
    if [ "$PULLED_NOW" = 1 ]; then pulled="${pulled:+$pulled }$TAG"; fi
    (umask 077 && printf '%s\n' "MACHINE_ID=$MACHINE_ID" "PLATFORM_URL=$PLATFORM_URL" "REPOSITORY=$REPOSITORY" \
        "TAG=$TAG" "IMAGE_PUBLIC=$IMAGE_PUBLIC" "APPARMOR=$APPARMOR" "MEM_LIMIT=$mem" "PULLED=$pulled" \
        > "$STATE/compute.conf.tmp") || return 1
    mv -f "$STATE/compute.conf.tmp" "$STATE/compute.conf" || return 1
    install -d -m 0755 "$TOOL_DIR" || return 1
    install -m 0755 "$WORK/$NAME" "$TOOL_DIR/$NAME" || return 1
    install -d -m 0755 "$(dirname "$UNIT")" || return 1
    # Wants, not Requires: a stop of Docker alone must not leave the machine
    # stopped after Docker starts again (run retries while Docker is away).
    # Every exit restarts: a removal ring the platform did not confirm must
    # start the machine again; a confirmed one stops the unit in `remove`.
    # OOMScoreAdjust protects the docker run client in this unit's group, so
    # the kernel never ends it while the container under dockerd lives on.
    # A change to these lines, like one to the tool's options, raises the
    # tool's HOST_SHAPE.
    printf '%s\n' \
        "[Unit]" \
        "Description=OtoDock compute machine" \
        "After=docker.service network-online.target" \
        "Wants=docker.service network-online.target" \
        "" \
        "[Service]" \
        "Type=simple" \
        "ExecStart=/opt/$NAME/$NAME run" \
        "ExecStop=-/opt/$NAME/$NAME stop" \
        "ExecStopPost=/opt/$NAME/$NAME after" \
        "Restart=always" \
        "RestartSec=5" \
        "TimeoutStopSec=150" \
        "OOMScoreAdjust=-500" \
        "" \
        "[Install]" \
        "WantedBy=multi-user.target" > "$UNIT" || return 1
}

start() {
    systemctl daemon-reload
    systemctl enable --quiet "$NAME.service"
    systemctl restart "$NAME.service"
    local health=""
    for _ in $(seq 1 24); do
        health=$(docker inspect --format '{{.State.Health.Status}}' "$NAME" 2> /dev/null || true)
        if [ "$health" = "healthy" ]; then break; fi
        sleep 5
    done
    if [ "$health" = "healthy" ]; then
        say "Started. It shows as Online in OtoDock once it signs in."
    else
        say "It is still starting. To see what it says: sudo journalctl -u $NAME -f"
    fi
}

main() {
    set -euo pipefail
    umask 077
    read_values
    check_server
    tell_plan
    WORK=$(mktemp -d)
    trap 'rm -rf -- "$WORK"' EXIT
    printf '%s' "${HOST_TOOL_B64:-}" | base64 -d > "$WORK/$NAME" || refuse "This command is damaged. $UNCHANGED"
    check_image
    check_sandbox
    pair
    say "Paired."
    write_files || refuse "The machine was paired but its files were not written, and it was not started. Pair it again from its card once the cause is fixed."
    start
}

main "$@"
