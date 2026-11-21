#!/usr/bin/env bash
#
# One rig run on this host, meant to be detached (setsid nohup): the candidate's
# images, a wipe, a fresh install, the scripted model server and the runner. Each
# step writes <run dir>/status.json, which is what anyone waiting reads, and a line
# of cost.tsv: its seconds, the gigabytes it wrote to disk and the disk pressure.
#
#   vm-run.sh --run-dir DIR --candidate DIR --public-url URL --tag TAG
#       [--layer scripted|live|all|upgrade|standup] [--ref REF] [--no-build]
#       [--no-wipe] [--only a,b] [--inject model-error|broken-check|satellite-down]
#       [--from-version V] [--floor-script FILE]
#
# An image is built at most once per candidate: the build's key covers what the
# images copy, and a change to anything else (rig/ above all) builds nothing.
# --no-wipe runs against the install that is up, with its accounts and its state:
# that is how a scenario is iterated, since a wipe and a reinstall cost the disk
# every other VM shares.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rig/install/guard.sh
. "$HERE/guard.sh"

RUN="" CANDIDATE="" URL="" TAG="" LAYER=scripted REF="" BUILD=1 WIPE=1 ONLY="" INJECT="" FROM="" FLOOR_SCRIPT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --run-dir) RUN="$2"; shift 2 ;;
        --candidate) CANDIDATE="$2"; shift 2 ;;
        --public-url) URL="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --layer) LAYER="$2"; shift 2 ;;
        --ref) REF="$2"; shift 2 ;;
        --no-build) BUILD=0; shift ;;
        --no-wipe) WIPE=0; shift ;;
        --only) ONLY="$2"; shift 2 ;;
        --inject) INJECT="$2"; shift 2 ;;
        --from-version) FROM="$2"; shift 2 ;;
        --floor-script) FLOOR_SCRIPT="$2"; shift 2 ;;
        *) echo "vm-run: unknown argument $1" >&2; exit 2 ;;
    esac
done
mkdir -p "$RUN"
MODELS_ADDR=198.18.0.1
MODELS_PORT=18099
RIG_DIR="$HOME/.otodock-rig"
LOCK="$RIG_DIR/run.lock"
PY="$(command -v python3)"
DRIVER="$CANDIDATE/rig"

status() { # status <state> <step> [verdict]
    "$PY" - "$RUN/status.json" "$1" "$2" "${3:-}" <<'PY'
import json, os, sys, time
path, state, step, verdict = sys.argv[1:5]
with open(path + ".tmp", "w") as fh:
    json.dump({"state": state, "step": step, "verdict": verdict, "at": int(time.time())}, fh)
os.replace(path + ".tmp", path)
PY
}

DISK="$(lsblk -snlo NAME,TYPE "$(findmnt -no SOURCE -T "$HOME")" | awk '$2 == "disk" { print $1; exit }')"
written() { awk '{ print $7 }' "/sys/block/$DISK/stat"; }
MARK_T=$SECONDS
MARK_W="$(written)"
printf 'step\tseconds\twritten_gb\tio_some_avg60\tio_some_avg300\n' > "$RUN/cost.tsv"
# cost <step>: what the step that just ended took, for the report.
cost() {
    local now pressure
    now="$(written)"
    pressure="$(awk '/^some/ { sub(/avg60=/, "", $3); sub(/avg300=/, "", $4); print $3 "\t" $4 }' /proc/pressure/io)"
    printf '%s\t%s\t%s\t%s\n' "$1" "$((SECONDS - MARK_T))" \
        "$(awk -v a="$MARK_W" -v b="$now" 'BEGIN { printf "%.2f", (b - a) * 512 / 1e9 }')" "$pressure" >> "$RUN/cost.tsv"
    MARK_T=$SECONDS
    MARK_W="$now"
}
fail() { cost "$1"; status "done" "$1" fail; echo "vm-run: FAILED at $1" >&2; exit 1; }
# The last step of every run: the run folder, which is copied off the rig to be
# read, holds no secret of the install or of the run.
scan_secrets() {
    "$PY" "$DRIVER/lib/secret_scan.py" "$RUN" --env "$HOME/otodock/.env" --state-dir "$RIG_DIR/install" \
        > "$RIG_DIR/secret-scan.log" 2>&1
    local rc=$?
    cp "$RIG_DIR/secret-scan.log" "$RUN/secret-scan.log"
    [ "$rc" = 0 ] || fail secret-scan
}

if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then
    status "done" locked fail
    echo "vm-run: another run holds this host (pid $(cat "$LOCK"))" >&2
    exit 3
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

bash "$HERE/host-setup.sh" on > "$RUN/io-cap.log" 2>&1 || fail host-setup

free_gb() { df -BG --output=avail "$HOME" | tail -1 | tr -dc 0-9; }
if [ "$(free_gb)" -lt 13 ]; then
    echo "vm-run: under 13 GB free: pruning the build cache"
    docker builder prune -f --keep-storage 8GB >/dev/null 2>&1 || true
fi

# The key of what the images copy; tests, docs and rig/ are not in it.
image_key() {
    (cd "$CANDIDATE" && {
        find proxy dashboard satellite scripts audio mcps phone docker-compose.build.yml -type f \
            ! -path '*/tests/*' ! -path '*/docs/*' ! -name '*.md' -print0 2>/dev/null
        printf 'VERSIONS.md\0'
    } | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
}

build_once() {
    local key key_file="$RIG_DIR/built-$TAG"
    key="$(image_key)"
    if [ -f "$key_file" ] && [ "$(cat "$key_file")" = "$key" ] \
            && docker image inspect "ghcr.io/otodock/otodock-proxy:$TAG" >/dev/null 2>&1; then
        echo "vm-run: the images of $TAG are this candidate's: no build"
        return 0
    fi
    status running build
    bash "$HERE/build.sh" "$CANDIDATE" "$TAG" > "$RUN/build.log" 2>&1 || return 1
    echo "$key" > "$key_file"
    cost build
}

models_up() {
    sudo -n ip addr add "$MODELS_ADDR/32" dev lo 2>/dev/null
    fuser -k "$MODELS_PORT/tcp" >/dev/null 2>&1
    sleep 0.3
    # On every address: a paired machine on another host reaches it too.
    setsid nohup "$PY" "$DRIVER/models/server.py" --bind 0.0.0.0 --port "$MODELS_PORT" \
        --log "$RUN/model-calls.jsonl" ${MODEL_INJECT:+--inject "$MODEL_INJECT"} </dev/null >"$RUN/model-server.log" 2>&1 &
    sleep 1
    curl -fsS --max-time 3 "http://$MODELS_ADDR:$MODELS_PORT/v1/models" >/dev/null
}

# A failure injected on purpose, to prove it shows: every model call refused, every
# check that has a broken form run against a setup made wrong, or the rig's own
# satellite stopped for the machine's scenarios.
MODEL_INJECT="" BROKEN=""
case "$INJECT" in
    "") ;;
    model-error) MODEL_INJECT=model-error ;;
    broken-check) BROKEN=--broken ;;
    satellite-down) ;;
    *) echo "vm-run: unknown injection $INJECT" >&2; exit 2 ;;
esac
satellite() { # satellite stop|start: the rig's own paired satellite only
    [ -f "$HOME/.oto-dock/.otodock-rig-satellite" ] || return 0
    systemctl --user "$1" oto-dock-satellite
}

runner() { # runner <layer>
    [ "$INJECT" != satellite-down ] || satellite stop
    # The runner starts a stopped machine again before a row on it, except under this injection.
    RIG_SATELLITE_DOWN="$([ "$INJECT" != satellite-down ] || echo 1)" "$PY" "$DRIVER/run.py" --base-url "$URL" --run-dir "$RUN" --layer "$1" ${BROKEN:+"$BROKEN"} \
        --models-url "http://$MODELS_ADDR:$MODELS_PORT" --models-log "$RUN/model-calls.jsonl" \
        ${ONLY:+--only "$ONLY"} ${TAG:+--upgrade-target "$TAG"} --candidate-dir "$CANDIDATE" \
        2>&1 | tee -a "$RUN/runner-$1.log"
    local rc="${PIPESTATUS[0]}"
    [ "$INJECT" != satellite-down ] || satellite start
    return "$rc"
}

if [ "$BUILD" = 1 ] && [ -z "$REF" ]; then
    build_once || fail build
fi

if [ "$WIPE" = 1 ]; then
    status running wipe
    bash "$HERE/wipe.sh" > "$RUN/wipe.log" 2>&1 || fail wipe
    cost wipe
fi

# The candidate's first start on the oldest Docker Compose the install scripts
# accept, once per build of its images, while nothing else holds the memory.
# FLOOR_SCRIPT boots the images on the Compose binary it is given; without it,
# no floor boot.
floor_boot() {
    local key result="$RUN/floor.json" bin="$RIG_DIR/bin/docker-compose-floor" version sum
    key="$(image_key)"
    if [ -f "$RIG_DIR/floor-$key.json" ]; then
        cp "$RIG_DIR/floor-$key.json" "$result"
        return 0
    fi
    status running floor
    version="$(sed -n 's/^COMPOSE_FLOOR_VERSION=//p' "$CANDIDATE/VERSIONS.md" | head -n1)"
    sum="$(sed -n 's/^COMPOSE_FLOOR_SHA256=//p' "$CANDIDATE/VERSIONS.md" | head -n1)"
    if ! { [ -x "$bin" ] && echo "$sum  $bin" | sha256sum -c --status; }; then
        mkdir -p "$RIG_DIR/bin"
        curl -fsSL -o "$bin.tmp" "https://github.com/docker/compose/releases/download/v$version/docker-compose-linux-x86_64" \
            && echo "$sum  $bin.tmp" | sha256sum -c --status && chmod +x "$bin.tmp" && mv "$bin.tmp" "$bin" \
            || { rm -f "$bin.tmp"; fail floor-compose; }
    fi
    local verdict=fail
    OTODOCK_BOOT_WAIT_S=1200 bash "$FLOOR_SCRIPT" --compose "$bin" --version "$TAG" \
        --compose-file "$CANDIDATE/docker-compose.yml" --port 8491 > "$RUN/floor.log" 2>&1 && verdict=pass
    printf '{"verdict": "%s", "compose": "%s", "tag": "%s", "key": "%s"}\n' "$verdict" "$version" "$TAG" "$key" > "$result"
    [ "$verdict" = fail ] || cp "$result" "$RIG_DIR/floor-$key.json"
    cost floor
}
if [ -n "$FLOOR_SCRIPT" ] && [ "$WIPE" = 1 ] && [ -z "$REF" ] && [ "$LAYER" != upgrade ]; then
    floor_boot
fi

if [ "$LAYER" = standup ]; then
    status running install
    bash "$HERE/fresh.sh" --public-url "$URL" --tag "$TAG" --plain ${REF:+--ref "$REF"} \
        ${CANDIDATE:+--candidate "$CANDIDATE"} > "$RUN/install.log" 2>&1 || fail install
    cost install
    scan_secrets
    status "done" standup pass
    exit 0
fi

models_up || fail model-server
bash "$HERE/engines.sh" scripted || fail engines

if [ "$LAYER" = upgrade ]; then
    [ -n "$FROM" ] || fail "upgrade-needs-from-version"
    status running "install-$FROM"
    bash "$HERE/fresh.sh" --public-url "$URL" --tag "$FROM" --ref "v$FROM" > "$RUN/install.log" 2>&1 || fail "install-$FROM"
    cost "install-$FROM"
    status running upgrade
    runner upgrade; rc=$?
    cost upgrade
    scan_secrets
    status "done" upgrade "$([ "$rc" = 0 ] && echo pass || echo fail)"
    exit "$rc"
fi

if [ "$WIPE" = 1 ]; then
    status running install
    if [ -n "$REF" ]; then
        bash "$HERE/fresh.sh" --public-url "$URL" --tag "$TAG" --ref "$REF" > "$RUN/install.log" 2>&1 || fail install
    else
        bash "$HERE/fresh.sh" --public-url "$URL" --tag "$TAG" --candidate "$CANDIDATE" > "$RUN/install.log" 2>&1 || fail install
    fi
    cost install
fi

verdict=pass
for layer in scripted live; do
    case "$LAYER" in "$layer"|all) ;; *) continue ;; esac
    status running "$layer"
    # A paired machine's Claude Code talks to the vendor only when the keys are here.
    if [ "$layer" = live ] && [ -s "$RIG_DIR/accounts" ]; then bash "$HERE/engines.sh" vendor; fi
    runner "$layer" || verdict=fail
    cost "$layer"
done
scan_secrets
status "done" "$LAYER" "$verdict"
[ "$verdict" = pass ]
