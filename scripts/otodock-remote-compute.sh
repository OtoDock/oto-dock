#!/usr/bin/env bash
#
# OtoDock compute machine on another server: the host service.
#
# Installed by remote-compute-install.sh (the command an admin pastes from
# Admin > Remote Machines > Compute machines) as
# /opt/otodock-remote-compute/otodock-remote-compute, and run by the systemd
# unit otodock-remote-compute.service:
#
#   run      ExecStart: the platform's image in a container, in the foreground
#   stop     ExecStop: the container stopped, its sessions given two minutes
#   after    ExecStopPost: what the machine's exit asks for, confirmed with the platform
#   remove   everything this machine keeps on this server (also by hand)
#   status   what runs, and what it waits for
#
# The machine rings this service with its exit code: 76 when the platform
# runs another image tag, 77 when it was removed. The code is a doorbell and
# carries nothing: this service then asks the platform itself, signed with
# the machine's sign-in, which tag to run (only a tag of the image it was
# installed with) and whether the machine was removed. The repository, the
# options, the mounts and every path are this script's own; changing them is
# a new command on the server ("Pair again"), which HOST_SHAPE tells the
# platform about.
#
# Every object it makes is named otodock-remote-compute and labelled
# io.otodock.remote-compute=<machine id>: the container, its network and its
# two volumes. Its files are /var/lib/otodock-remote-compute (the
# configuration and the sign-in), /opt/otodock-remote-compute and the unit
# file.
set -euo pipefail

# Tests only: every path this script writes lives under this folder.
ROOT="${OTODOCK_COMPUTE_HOST_ROOT:-}"
NAME="otodock-remote-compute"
STATE="$ROOT/var/lib/$NAME"
CONF="$STATE/compute.conf"
SIGN_IN="$STATE/sign-in"
TOOL_DIR="$ROOT/opt/$NAME"
UNIT="$ROOT/etc/systemd/system/$NAME.service"
LABEL="io.otodock.remote-compute"
VOLUMES=("$NAME-state" "$NAME-tools")
# The shape of the options, mounts and environment below: raised with any
# change to them, so the platform can say a server runs an older one.
HOST_SHAPE=1
UPDATE_EXIT=76
REMOVED_EXIT=77
# How often a wanted image is looked for; how long a tag that failed here is
# not tried again, and before the machine starts the image it runs again so
# the platform can speak to it; how long a new tag is on trial.
WAIT_S="${OTODOCK_COMPUTE_WAIT_S:-300}"
GIVE_UP_S=1800
TRIAL_S=600
# How long a look at an image may take; how long a ring the platform did not
# confirm waits before the unit starts the machine again (inside the unit's
# TimeoutStopSec, with the 30 s for the container and the 30 s of the ask).
CHECK_S="${OTODOCK_COMPUTE_CHECK_S:-300}"
UNCONFIRMED_S=60
TAG_RE='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'
REPO_RE='^(([A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:[0-9]+)?|localhost(:[0-9]+)?|[A-Za-z0-9-]+:[0-9]+)/)?[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$'
URL_RE='^https?://[]A-Za-z0-9.:[-]{1,300}$'
PULLED_RE='^[A-Za-z0-9_. -]{0,2000}$'
VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9]+)*$'
SECRET_RE='^[A-Za-z0-9_-]{20,128}$'
NUM_RE='^[0-9]{1,12}$'

say() { printf '%s: %s\n' "$NAME" "$*"; }
die() { printf '%s: %s\n' "$NAME" "$*" >&2; exit 1; }
now() { date +%s; }

# The configuration: KEY=VALUE lines, each value checked, never sourced.
read_conf() {
    if [ ! -f "$CONF" ] || [ -L "$CONF" ]; then
        die "No compute machine is installed here ($CONF is missing). Nothing was touched."
    fi
    MACHINE_ID="" PLATFORM_URL="" REPOSITORY="" TAG="" WANTED_TAG="" WANTED_VERSION="" PREVIOUS_TAG=""
    WANTED_SINCE=0 IMAGE_PUBLIC=0 APPARMOR=unconfined MEM_LIMIT="" PULLED="" TRIAL_UNTIL=0 AVOID_TAG=""
    AVOID_UNTIL=0
    local key value
    while IFS='=' read -r key value; do
        case "$key" in
            MACHINE_ID) if [[ $value =~ ^[A-Za-z0-9-]{1,64}$ ]]; then MACHINE_ID=$value; fi ;;
            PLATFORM_URL) if [[ $value =~ $URL_RE ]]; then PLATFORM_URL=$value; fi ;;
            REPOSITORY) if [[ $value =~ $REPO_RE ]]; then REPOSITORY=$value; fi ;;
            TAG) if [[ $value =~ $TAG_RE ]]; then TAG=$value; fi ;;
            WANTED_TAG) if [[ $value =~ $TAG_RE ]]; then WANTED_TAG=$value; fi ;;
            WANTED_VERSION) if [[ $value =~ $VERSION_RE ]]; then WANTED_VERSION=$value; fi ;;
            PREVIOUS_TAG) if [[ $value =~ $TAG_RE ]]; then PREVIOUS_TAG=$value; fi ;;
            AVOID_TAG) if [[ $value =~ $TAG_RE ]]; then AVOID_TAG=$value; fi ;;
            WANTED_SINCE) if [[ $value =~ $NUM_RE ]]; then WANTED_SINCE=$value; fi ;;
            TRIAL_UNTIL) if [[ $value =~ $NUM_RE ]]; then TRIAL_UNTIL=$value; fi ;;
            AVOID_UNTIL) if [[ $value =~ $NUM_RE ]]; then AVOID_UNTIL=$value; fi ;;
            IMAGE_PUBLIC) if [[ $value =~ ^[01]$ ]]; then IMAGE_PUBLIC=$value; fi ;;
            APPARMOR) if [[ $value =~ ^(unconfined|otodock_userns)$ ]]; then APPARMOR=$value; fi ;;
            MEM_LIMIT) if [[ $value =~ ^[0-9]{1,15}[kmgKMG]?$ ]]; then MEM_LIMIT=$value; fi ;;
            PULLED) if [[ $value =~ $PULLED_RE ]]; then PULLED=$value; fi ;;
        esac
    done < "$CONF"
    if [ -z "$MACHINE_ID" ] || [ -z "$PLATFORM_URL" ] || [ -z "$REPOSITORY" ] || [ -z "$TAG" ]; then
        die "The configuration $CONF is not complete. Nothing was touched."
    fi
}

write_conf() {
    local tmp="$CONF.tmp"
    (umask 077 && printf '%s\n' \
        "MACHINE_ID=$MACHINE_ID" "PLATFORM_URL=$PLATFORM_URL" "REPOSITORY=$REPOSITORY" "TAG=$TAG" \
        "WANTED_TAG=$WANTED_TAG" "WANTED_VERSION=$WANTED_VERSION" "PREVIOUS_TAG=$PREVIOUS_TAG" \
        "WANTED_SINCE=$WANTED_SINCE" "TRIAL_UNTIL=$TRIAL_UNTIL" "AVOID_TAG=$AVOID_TAG" "AVOID_UNTIL=$AVOID_UNTIL" \
        "IMAGE_PUBLIC=$IMAGE_PUBLIC" "APPARMOR=$APPARMOR" "MEM_LIMIT=$MEM_LIMIT" "PULLED=$PULLED" > "$tmp")
    mv -f "$tmp" "$CONF"
}

# The options the sandbox needs, the same for the machine and for every look
# at an image before it runs (`preflight`).
sandbox_args() {
    SANDBOX_ARGS=(--user 1000:1000
        --security-opt seccomp=unconfined --security-opt "apparmor=$APPARMOR"
        --security-opt systempaths=unconfined --device /dev/net/tun
        --pids-limit 32768 --ulimit nofile=65536:524288 --oom-score-adj 250)
}

container_args() {
    sandbox_args
    ARGS=(--rm --name "$NAME" --label "$LABEL=$MACHINE_ID" --network "$NAME" "${SANDBOX_ARGS[@]}"
        --stop-timeout 120
        --health-cmd "grep -q '^running' /opt/otodock/compute/.oto-dock/compute-status"
        --health-interval 10s --health-timeout 5s --health-start-period 600s --health-retries 6
        -e HOME=/opt/otodock/compute -e TZ=UTC
        -e OTODOCK_COMPUTE_LINK_DIR=/opt/otodock/compute-link
        -e "OTODOCK_COMPUTE_PLATFORM_URL=$PLATFORM_URL"
        -e OTODOCK_COMPUTE_FILES=own
        -e OTODOCK_COMPUTE_AGENTS_DIR=/opt/otodock/compute/agents
        -e OTODOCK_COMPUTE_MCPS_DIR=/opt/otodock/mcps
        -e "OTODOCK_COMPUTE_IMAGE=$REPOSITORY:$TAG"
        -e "OTODOCK_COMPUTE_HOST_SHAPE=$HOST_SHAPE"
        -v "${VOLUMES[0]}:/opt/otodock/compute"
        -v "${VOLUMES[1]}:/opt/otodock/mcps/community"
        -v "$SIGN_IN:/opt/otodock/compute-link:ro")
    if [ -n "$MEM_LIMIT" ]; then ARGS+=(--memory "$MEM_LIMIT"); fi
}

image_present() { docker image inspect "$1" > /dev/null 2>&1; }

# The tool's own containers, by its label, never by a name alone.
own_containers() { docker ps -aq --filter "label=$LABEL"; }

# Its network and volumes, each made once with its label; a new volume's top
# folder is given to the container's user (the image may not have the folder).
ensure_objects() {
    if ! docker network inspect "$NAME" > /dev/null 2>&1; then
        docker network create --label "$LABEL=$MACHINE_ID" "$NAME" > /dev/null
    fi
    local volume mount
    for volume in "${VOLUMES[@]}"; do
        if ! docker volume inspect "$volume" > /dev/null 2>&1; then
            docker volume create --label "$LABEL=$MACHINE_ID" "$volume" > /dev/null
            mount=$(docker volume inspect --format '{{ .Mountpoint }}' "$volume")
            if [ -n "$mount" ] && [ -d "$ROOT$mount" ]; then chown 1000:1000 "$ROOT$mount"; fi
        fi
    done
}

pull() {
    if docker pull --quiet "$REPOSITORY:$1" > /dev/null; then
        PULLED="${PULLED:+$PULLED }$1"
        return 0
    fi
    return 1
}

# An image tag this tool pulled, removed (never with force: a tag another
# container uses stays); a tag that was loaded on the server is kept.
drop_pulled() {
    local tag=$1 kept="" item
    for item in $PULLED; do
        if [ "$item" = "$tag" ]; then
            docker image rm "$REPOSITORY:$tag" > /dev/null 2>&1 || true
        else
            kept="${kept:+$kept }$item"
        fi
    done
    PULLED=$kept
}

# The image's sandbox, run once with the machine's options before it runs:
# its satellite must be the platform's, and the sandbox must start here. A
# look that does not answer in CHECK_S says nothing, and its container goes.
preflight_says() {
    sandbox_args
    docker rm -f "$NAME-check" > /dev/null 2>&1 || true
    timeout "$CHECK_S" docker run --rm --name "$NAME-check" "${SANDBOX_ARGS[@]}" -e HOME=/tmp/otodock-check \
        -e OTODOCK_COMPUTE_MCPS_DIR=/opt/otodock/mcps \
        "$1" /opt/otodock/satellite/venv/bin/python /opt/otodock/proxy/compute_start.py --check "$2" 2>&1 \
        | tail -n 1 || true
    docker rm -f "$NAME-check" > /dev/null 2>&1 || true
}

switch_to() {
    local new=$1 old=$TAG
    if [ -n "$PREVIOUS_TAG" ] && [ "$PREVIOUS_TAG" != "$new" ] && [ "$PREVIOUS_TAG" != "$old" ]; then
        drop_pulled "$PREVIOUS_TAG"
    fi
    PREVIOUS_TAG=$old TAG=$new WANTED_TAG="" WANTED_VERSION="" WANTED_SINCE=0 TRIAL_UNTIL=$(( $(now) + TRIAL_S ))
    write_conf
    say "it now runs $REPOSITORY:$TAG (the previous tag $old is kept)"
}

# A tag that failed here: not tried again for GIVE_UP_S, while the machine
# runs the tag it ran, which signs in and hears the platform again.
avoid() {
    AVOID_TAG=$1 AVOID_UNTIL=$(( $(now) + GIVE_UP_S )) WANTED_TAG="" WANTED_VERSION="" WANTED_SINCE=0
    write_conf
}

# The tag the platform named, once its image is on the server and its
# preflight passes; after GIVE_UP_S without it, the tag it runs, so the
# platform can speak again.
wait_for_image() {
    local said
    if [ -z "$WANTED_TAG" ] || [ "$WANTED_TAG" = "$TAG" ]; then return 0; fi
    while :; do
        if [ "$WANTED_TAG" = "$AVOID_TAG" ] && [ "$(now)" -lt "$AVOID_UNTIL" ]; then
            say "the image tag $WANTED_TAG failed here: it is tried again in $(( AVOID_UNTIL - $(now) )) s"
            sleep "$WAIT_S"
            continue
        fi
        if image_present "$REPOSITORY:$WANTED_TAG" || { [ "$IMAGE_PUBLIC" = 1 ] && pull "$WANTED_TAG"; }; then
            said=$(preflight_says "$REPOSITORY:$WANTED_TAG" "${WANTED_VERSION:-0.0.0}")
            if [ "$said" = "ok" ]; then
                switch_to "$WANTED_TAG"
                return 0
            fi
            say "the image $REPOSITORY:$WANTED_TAG cannot run here (${said:-no answer}): it starts $TAG again"
            avoid "$WANTED_TAG"
            return 0
        fi
        if [ $(( $(now) - WANTED_SINCE )) -ge "$GIVE_UP_S" ]; then
            say "the image $REPOSITORY:$WANTED_TAG is not on this server: it starts $TAG again"
            WANTED_TAG="" WANTED_VERSION="" WANTED_SINCE=0
            write_conf
            return 0
        fi
        if [ "$IMAGE_PUBLIC" = 1 ]; then
            say "the image $REPOSITORY:$WANTED_TAG could not be downloaded: trying again in $WAIT_S s"
        else
            say "waiting for the image $REPOSITORY:$WANTED_TAG: load it on this server"
        fi
        sleep "$WAIT_S"
    done
}

cmd_run() {
    read_conf
    # Docker's own failure, as the docker client's is (125): the unit tries
    # again, and a new tag on trial is not blamed for it.
    if ! docker info > /dev/null 2>&1; then
        say "Docker does not answer: the machine starts once it does"
        exit 125
    fi
    wait_for_image
    local id
    for id in $(own_containers); do docker rm -f "$id" > /dev/null; done
    ensure_objects
    container_args
    exec docker run "${ARGS[@]}" "$REPOSITORY:$TAG" \
        /opt/otodock/satellite/venv/bin/python /opt/otodock/proxy/compute_start.py
}

cmd_stop() {
    local id
    for id in $(own_containers); do docker stop -t 120 "$id" > /dev/null || true; done
}

# What the platform says, asked with the machine's sign-in (read by root from
# the file the container only reads): ANSWER_CODE and ANSWER_BODY.
ask_platform() {
    local secret out proto=()
    ANSWER_CODE=000 ANSWER_BODY=""
    secret=$(sed -n 's/.*"machine_secret": *"\([^"]*\)".*/\1/p' "$SIGN_IN/credential" 2> /dev/null | head -n 1 || true)
    if [[ ! $secret =~ $SECRET_RE ]]; then return 0; fi
    case "$PLATFORM_URL" in https://*) proto=(--proto '=https') ;; esac
    out=$(printf 'X-Machine-Id: %s\nX-Machine-Secret: %s\n' "$MACHINE_ID" "$secret" \
        | curl -sS --max-time 30 ${proto[@]+"${proto[@]}"} -H @- -w '\n%{http_code}' \
            "$PLATFORM_URL/v1/satellite/compute-host" 2> /dev/null) || true
    ANSWER_CODE=${out##*$'\n'}
    ANSWER_BODY=${out%$'\n'*}
}

answer_field() {
    printf '%s' "$ANSWER_BODY" | sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p" | head -n 1 || true
}

# A ring the platform did not confirm changes nothing, and the unit starts
# the machine again a minute later, so a ring nobody confirms cannot spin.
unconfirmed() {
    say "$1"
    sleep "$UNCONFIRMED_S"
}

# The doorbell for a new tag: the platform's own answer is the tag, never the
# machine's word.
on_new_tag() {
    local image repo tag version
    ask_platform
    if [ "$ANSWER_CODE" != 200 ]; then
        unconfirmed "the platform did not confirm a new image (answer ${ANSWER_CODE:-none}): nothing changes"
        return 0
    fi
    image=$(answer_field image) version=$(answer_field satellite_version)
    repo=${image%:*} tag=${image##*:}
    if [ "$repo" != "$REPOSITORY" ] || [[ ! $tag =~ $TAG_RE ]] || [[ ! $version =~ $VERSION_RE ]]; then
        unconfirmed "the platform runs the image ${image:-it did not name}, not a tag of $REPOSITORY: pair this server again from its card"
        return 0
    fi
    if [ "$tag" = "$TAG" ]; then
        unconfirmed "the platform runs the image tag $tag, which this machine runs: nothing changes"
        return 0
    fi
    WANTED_TAG=$tag WANTED_VERSION=$version WANTED_SINCE=$(now)
    write_conf
    say "the platform runs the image tag $tag: it follows"
}

# The doorbell for a removal: removed only when the platform confirms it.
on_removed() {
    ask_platform
    if [ "$ANSWER_CODE" != 410 ]; then
        unconfirmed "the platform did not confirm a removal (answer ${ANSWER_CODE:-none}): nothing is removed"
        return 0
    fi
    say "the platform removed this machine: removing it from this server"
    systemd-run --unit "$NAME-removal" --collect --quiet "$TOOL_DIR/$NAME" remove --yes
}

# A new tag that ends with a status of its own (a number, not Docker's 125)
# other than a doorbell or a stop while on trial: back to the tag it ran, once.
go_back() {
    local failed=$TAG
    say "the image tag $failed ended with $1 within $TRIAL_S s of its start: it goes back to $PREVIOUS_TAG"
    TAG=$PREVIOUS_TAG PREVIOUS_TAG="" TRIAL_UNTIL=0
    avoid "$failed"
}

cmd_after() {
    read_conf
    local code="${EXIT_STATUS:-}" tries=0
    while [ -n "$(own_containers)" ] && [ "$tries" -lt 30 ]; do
        sleep 1
        tries=$((tries + 1))
    done
    case "$code" in
        "$UPDATE_EXIT") TRIAL_UNTIL=0; write_conf; on_new_tag ;;
        "$REMOVED_EXIT") TRIAL_UNTIL=0; write_conf; on_removed ;;
        0 | 75) if [ "$TRIAL_UNTIL" != 0 ]; then TRIAL_UNTIL=0; write_conf; fi ;;
        125) ;;
        *)
            if [[ $code =~ ^[0-9]{1,3}$ ]] && [ "$(now)" -lt "$TRIAL_UNTIL" ] && [ -n "$PREVIOUS_TAG" ]; then
                go_back "$code"
            fi
            ;;
    esac
}

cmd_remove() {
    local yes=0 answer id volume tag
    if [ "${1:-}" = "--yes" ]; then yes=1; fi
    if [ "$(id -u)" != "0" ]; then die "Run it as root: sudo $0 remove. Nothing was touched."; fi
    read_conf
    case "$STATE" in */var/lib/otodock-remote-compute) ;; *) die "Unexpected folder $STATE. Nothing was touched." ;; esac
    if ! docker info > /dev/null 2>&1; then
        die "Docker does not answer. Start it and run the removal again. Nothing was removed."
    fi
    if [ "$yes" != 1 ]; then
        printf 'Remove the compute machine %s and everything it keeps on this server? [y/N] ' "$MACHINE_ID"
        read -r answer < /dev/tty || answer=""
        if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then die "Nothing was touched."; fi
    fi
    say "removing the compute machine $MACHINE_ID"
    systemctl disable --now "$NAME.service" > /dev/null 2>&1 || true
    for id in $(own_containers); do docker rm -f "$id" > /dev/null || true; done
    for volume in $(docker volume ls -q --filter "label=$LABEL"); do docker volume rm "$volume" > /dev/null || true; done
    for id in $(docker network ls -q --filter "label=$LABEL"); do docker network rm "$id" > /dev/null || true; done
    for tag in $PULLED; do docker image rm "$REPOSITORY:$tag" > /dev/null 2>&1 || true; done
    rm -f -- "$UNIT"
    systemctl daemon-reload > /dev/null 2>&1 || true
    systemctl reset-failed "$NAME.service" > /dev/null 2>&1 || true
    rm -rf -- "$STATE"
    say "removed. The AppArmor profile otodock_userns stays, if this server has it: other software may use it."
    rm -rf -- "$TOOL_DIR"
}

cmd_status() {
    read_conf
    say "machine $MACHINE_ID, image $REPOSITORY:$TAG${WANTED_TAG:+, waiting for the tag $WANTED_TAG}"
    say "platform $PLATFORM_URL"
    systemctl is-active "$NAME.service" 2> /dev/null || true
}

# The install's one look at an image before it pairs.
cmd_preflight() {
    APPARMOR=${3:-unconfined}
    if [[ ! $APPARMOR =~ ^(unconfined|otodock_userns)$ ]]; then die "Unknown AppArmor profile."; fi
    preflight_says "$1" "$2"
}

case "${1:-}" in
    run) cmd_run ;;
    stop) cmd_stop ;;
    after) cmd_after ;;
    remove) shift; cmd_remove "$@" ;;
    status) cmd_status ;;
    preflight) shift; cmd_preflight "$@" ;;
    *) die "usage: $NAME run|stop|after|remove [--yes]|status" ;;
esac
