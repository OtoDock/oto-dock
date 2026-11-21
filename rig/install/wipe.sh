#!/usr/bin/env bash
#
# Remove the rig's OtoDock install from this host: the compose project, its
# volumes, the install folder and the satellite the rig paired. Images and the
# build cache stay. THE ONLY SCRIPT OF THE RIG THAT REMOVES AN INSTALL.
#
# guard.sh proves this host is a rig. Then, before anything changes, this script
# reads what it would act on and refuses when any of it is not the rig's own:
#   - the running install must be in ~/otodock, the one place fresh.sh installs;
#   - ~/otodock must carry the file fresh.sh writes first, and hold no git checkout
#     at any depth a checkout could sit (a rig install is never one). With no
#     ~/otodock at all, nothing of an install may be on the host either;
#   - the satellite unit and ~/.oto-dock are touched only when ~/.oto-dock holds the
#     file the rig's pairing writes, and the unit's working directory is that
#     install's satellite folder (as satellite/uninstall.sh decides it); another
#     install's unit is refused.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rig/install/guard.sh
. "$HERE/guard.sh"

INSTALL="$HOME/otodock"
OTO_DIR="$HOME/.oto-dock"
RIG_INSTALL_FILE="$INSTALL/.otodock-rig-install"
RIG_SATELLITE_FILE="$OTO_DIR/.otodock-rig-satellite"
UNIT=oto-dock-satellite
UNIT_FILE="$HOME/.config/systemd/user/$UNIT.service"
# Under a test the runtime folder is the test's own, empty one: a real systemctl
# reached by mistake must find no user manager there.
if [ -z "${RIG_TEST_FAKES:-}" ]; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
fi
say() { echo "wipe: $*"; }

# --- what would be touched, read and judged; nothing has changed yet -----------
folder="$(docker ps -a --filter label=com.docker.compose.project=otodock \
    --format '{{.Label "com.docker.compose.project.working_dir"}}' | head -n1)"
[ -z "$folder" ] || [ "$folder" = "$INSTALL" ] \
    || rig_refuse "the install that runs here is in $folder, and the rig's is in $INSTALL"
unit_dir="$(systemctl --user show -p WorkingDirectory --value "$UNIT" 2>/dev/null || true)"
[ -z "$unit_dir" ] || [ "$unit_dir" = "$OTO_DIR/satellite" ] \
    || rig_refuse "the $UNIT unit runs $unit_dir, another install"
if [ -e "$INSTALL" ]; then
    checkout="$(find "$INSTALL" -maxdepth 4 -name .git -print -quit 2>/dev/null || true)"
    [ -z "$checkout" ] || rig_refuse "$checkout is a git checkout, and a rig install never holds one"
    [ -f "$RIG_INSTALL_FILE" ] \
        || rig_refuse "$INSTALL was not made by the rig (no $(basename "$RIG_INSTALL_FILE") in it)"
else
    # Nothing of the rig's install: anything of an install here is someone else's.
    vols="$(docker volume ls -q --filter label=com.docker.compose.project=otodock)"
    [ -z "$folder" ] && [ -z "$vols" ] \
        || rig_refuse "nothing here was made by the rig, and an OtoDock project has containers or volumes on this host"
    [ ! -e "$OTO_DIR" ] || [ -f "$RIG_SATELLITE_FILE" ] \
        || rig_refuse "nothing here was made by the rig, and $OTO_DIR holds a satellite it did not pair"
fi
rig_satellite=0
[ -f "$RIG_SATELLITE_FILE" ] && rig_satellite=1

# --- from here on, things change ----------------------------------------------
if [ -f "$INSTALL/docker-compose.yml" ]; then
    say "stopping the stack in $INSTALL"
    (cd "$INSTALL" && docker compose down -v --remove-orphans --timeout 20) \
        || say "compose down failed: removing by label"
fi
if [ -e "$INSTALL" ]; then
    ids="$(docker ps -aq --filter label=com.docker.compose.project=otodock)"
    # shellcheck disable=SC2086  # one id per word
    [ -z "$ids" ] || docker rm -f $ids >/dev/null
    vols="$(docker volume ls -q --filter label=com.docker.compose.project=otodock)"
    # A tool the server runs in a container of its own belongs to another compose
    # project. A server removed by force could not stop it, and it holds a volume.
    for vol in $vols; do
        holders="$(docker ps -aq --filter "volume=$vol")"
        # shellcheck disable=SC2086
        [ -z "$holders" ] || docker rm -f $holders >/dev/null
    done
    # shellcheck disable=SC2086
    [ -z "$vols" ] || docker volume rm -f $vols >/dev/null
    nets="$(docker network ls -q --filter label=com.docker.compose.project=otodock)"
    # shellcheck disable=SC2086
    [ -z "$nets" ] || docker network rm $nets >/dev/null 2>&1 || true
    # A container a claim starts itself (an index), left by a run that was cut: by its
    # label, and by the name it had before it carried one.
    ids="$( { docker ps -aq --filter label=io.otodock.rig; docker ps -aq --filter name='^otodock-claim-index$'; } | sort -u)"
    # shellcheck disable=SC2086
    [ -z "$ids" ] || docker rm -f $ids >/dev/null
    # The server starts each tool as a compose project of its own,
    # otodock-<install id>-mcp-<tool>. One that holds none of the volumes above
    # would outlive the install, with its own volumes, on a host only the rig uses.
    tools="$( { docker ps -a --format '{{.Label "com.docker.compose.project"}}'
                docker volume ls --format '{{.Label "com.docker.compose.project"}}'; } \
        | grep -E '^otodock-[a-z0-9]+-mcp-[a-z0-9._-]+$' | sort -u || true)"
    for project in $tools; do
        say "removing the tool project $project"
        ids="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
        # shellcheck disable=SC2086
        [ -z "$ids" ] || docker rm -f $ids >/dev/null
        vols="$(docker volume ls -q --filter "label=com.docker.compose.project=$project")"
        # shellcheck disable=SC2086
        [ -z "$vols" ] || docker volume rm -f $vols >/dev/null
        nets="$(docker network ls -q --filter "label=com.docker.compose.project=$project")"
        # shellcheck disable=SC2086
        [ -z "$nets" ] || docker network rm $nets >/dev/null 2>&1 || true
    done
fi

if [ "$rig_satellite" = 1 ]; then
    if [ "$unit_dir" = "$OTO_DIR/satellite" ]; then
        say "removing the rig's satellite service"
        systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
        rm -f "$UNIT_FILE"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
    rm -rf "$OTO_DIR" "$HOME/.codex" "$HOME/.config/otodock"
elif [ -e "$OTO_DIR" ]; then
    say "$OTO_DIR holds a satellite the rig did not pair: left alone"
fi

say "removing the install folder and the run's accounts"
rm -rf "$HOME/.otodock-rig/install"
# A first boot may leave a file a container owns in the folder.
[ ! -d "$INSTALL" ] || rm -rf "$INSTALL" 2>/dev/null || sudo -n rm -rf "$INSTALL"
say "done"
