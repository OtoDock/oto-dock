#!/usr/bin/env bash
#
# OtoDock self-host Compose wrapper — the build-from-source / full-featured
# flow. Sources the pinned image versions from VERSIONS.md (scripts/versions.sh)
# and runs `docker compose` with the overlay stack:
#
#   docker-compose.yml        the pull-only base (what end users run standalone)
#   docker-compose.build.yml  build contexts + VERSIONS.md-pinned base images
#   docker-compose.phone.yml  optional telephony daemon — included automatically
#                             when the file is present; OTODOCK_PHONE=0 skips it
#
# This is the single entry point that keeps the IMAGE BUILD in
# lockstep with VERSIONS.md: bumping PYTHON_IMAGE / NODE_IMAGE there flows into
# the build args with no Dockerfile edit.
#
#     scripts/compose.sh up -d --build      # build + start the full stack
#     scripts/compose.sh down               # stop it
#     scripts/compose.sh logs -f otodock-proxy
#
# The dev/source flow keeps its secrets in config.env (proxy/setup.sh writes
# it); OTODOCK_ENV_FILE points the compose bind-mount at it. End users running
# the bare base file use a `.env` next to it instead (auto-loaded by compose).
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
_root="$(cd "$_here/.." && pwd)"

# --- Prerequisites: Docker Engine + the Compose v2 plugin -------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "compose.sh: docker not found — install Docker Engine + the Compose plugin first." >&2
    echo "  Ubuntu/Debian (official convenience script):" >&2
    echo "    curl -fsSL https://get.docker.com | sh" >&2
    echo "    sudo usermod -aG docker \$USER   # then log out/in (or run: newgrp docker)" >&2
    echo "  Docs: https://docs.docker.com/engine/install/" >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "compose.sh: docker is installed but not usable from this shell." >&2
    echo "  Daemon not running?  sudo systemctl enable --now docker" >&2
    echo "  Permission denied?   sudo usermod -aG docker \$USER, then log out/in (or: newgrp docker)" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "compose.sh: the Docker Compose v2 plugin is missing (\`docker compose\` failed)." >&2
    echo "  Ubuntu/Debian: sudo apt-get install docker-compose-plugin" >&2
    echo "  (the get.docker.com script above installs it automatically;" >&2
    echo "   the legacy python \`docker-compose\` v1 binary is NOT supported)" >&2
    exit 1
fi

# shellcheck source=versions.sh
source "$_here/versions.sh"   # exports PYTHON_IMAGE / NODE_IMAGE / POSTGRES_IMAGE / OTODOCK_VERSION

if [ ! -f "$_root/config.env" ]; then
    echo "compose.sh: $_root/config.env not found." >&2
    printf '%s\n' \
      "  Container-only flow:" \
      "    (umask 077; printf 'POSTGRES_PASSWORD=%s\\n' \"\$(openssl rand -hex 24)\" > config.env)" >&2
    echo "  Native/dev flow:     cd proxy && ./setup.sh   (or scripts/dev-setup.sh for the full bootstrap)" >&2
    exit 1
fi

# Collabora's ssl.termination follows the scheme of DASHBOARD_PUBLIC_URL, as
# Collabora's own entrypoint derived it at every start before 26.04 (whose
# image has no shell): exported for the compose file's
# ${COLLABORA_SSL_TERMINATION}, so this flow is always current. The shell wins.
if [ -z "${COLLABORA_SSL_TERMINATION:-}" ]; then
    _url="${DASHBOARD_PUBLIC_URL:-$(awk '
        { line = $0; sub(/^[ \t]*/, "", line); sub(/^export[ \t]+/, "", line)
          if (match(line, /^DASHBOARD_PUBLIC_URL[ \t]*=[ \t]*/)) v = substr(line, RLENGTH + 1) }
        END { sub(/[ \t]+#.*$/, "", v); gsub(/["\047]/, "", v); print v }' "$_root/config.env")}"
    case "$_url" in
        [Hh][Tt][Tt][Pp][Ss]://*) export COLLABORA_SSL_TERMINATION=true ;;
        *) export COLLABORA_SSL_TERMINATION=false ;;
    esac
fi

# Collabora's WOPI proof key is mounted as one file of a volume (a subpath):
# Docker Engine 26 (API 1.45) and Docker Compose 2.26 or later. An older engine,
# or a Compose that parses the field but does not pass it on (2.25,
# moby#47687), mounts the whole volume and Collabora runs without its key,
# every document refused.
_at_least() { # _at_least <version> <major> <minor>
    awk -v v="${1#v}" -v M="$2" -v m="$3" 'BEGIN { n = split(v, a, "."); exit !(n >= 2 && (a[1] + 0 > M || (a[1] + 0 == M && a[2] + 0 >= m))) }'
}
case " $* " in
    *" up "*|*" create "*|*" run "*|*" start "*)
        _api="$(docker version --format '{{.Server.APIVersion}}' 2>/dev/null || true)"
        if ! _at_least "$_api" 1 45; then
            echo "compose.sh: this host's Docker Engine (API ${_api:-unknown}) is older than 26:" >&2
            echo "  OtoDock's Collabora needs Docker Engine 26 or later. Update Docker, then re-run." >&2
            exit 1
        fi
        _cv="$(docker compose version --short 2>/dev/null || true)"
        if ! _at_least "$_cv" 2 26; then
            echo "compose.sh: this host's Docker Compose (${_cv:-unknown}) is older than 2.26:" >&2
            echo "  OtoDock's Collabora needs Docker Compose 2.26 or later. Update Docker, then re-run." >&2
            exit 1
        fi ;;
esac

_files=(-f "$_root/docker-compose.yml" -f "$_root/docker-compose.build.yml")
if [ -f "$_root/docker-compose.phone.yml" ] && [ "${OTODOCK_PHONE:-1}" != "0" ]; then
    _files+=(-f "$_root/docker-compose.phone.yml")
fi

# --- Ubuntu 24.04+ unprivileged-userns restriction --------------------------
# When kernel.apparmor_restrict_unprivileged_userns=1 the proxy container
# (unprivileged + apparmor=unconfined) cannot create the user namespaces the
# agent sandbox is built on — the proxy hard-fails its boot preflight. The fix
# is the SCOPED `otodock_userns` AppArmor profile (grants userns to this
# workload only; the system-wide restriction stays on). Select it when the
# host has it; install it (one-time, sudo — same trust level dev-setup.sh
# already uses for apt) when it doesn't; print the manual command if sudo
# isn't available. An explicit OTODOCK_APPARMOR_PROFILE (shell env or
# config.env) always wins.
_userns_sysctl="/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
if [ -z "${OTODOCK_APPARMOR_PROFILE:-}" ] \
   && ! grep -qE '^\s*OTODOCK_APPARMOR_PROFILE=' "$_root/config.env" \
   && [ "$(cat "$_userns_sysctl" 2>/dev/null || echo 0)" = "1" ]; then
    if [ -f /etc/apparmor.d/otodock-userns ]; then
        export OTODOCK_APPARMOR_PROFILE=otodock_userns
    else
        echo "compose.sh: this host restricts unprivileged user namespaces"
        echo "  (kernel.apparmor_restrict_unprivileged_userns=1 — the Ubuntu 24.04+ default),"
        echo "  which blocks the agent sandbox inside the proxy container."
        echo "  Installing the scoped AppArmor profile 'otodock_userns' (one-time; the"
        echo "  system-wide hardening stays ON — this may prompt for your sudo password):"
        _sudo=(sudo)
        if [ "$(id -u)" -eq 0 ]; then _sudo=(); fi
        # No tty → sudo can't prompt; try passwordless, else fall through to
        # the manual instructions instead of hanging.
        if [ ! -t 0 ] && [ "${#_sudo[@]}" -gt 0 ]; then _sudo=(sudo -n); fi
        if "${_sudo[@]}" bash "$_here/setup-apparmor-userns.sh"; then
            export OTODOCK_APPARMOR_PROFILE=otodock_userns
        else
            echo >&2
            echo "compose.sh: could not install the profile automatically. Run once:" >&2
            echo "      sudo $_here/setup-apparmor-userns.sh" >&2
            echo "  then re-run this command." >&2
            echo "  Do NOT disable the sysctl system-wide — the profile is the supported path." >&2
            exit 1
        fi
    fi
fi

export OTODOCK_ENV_FILE="$_root/config.env"
exec docker compose --env-file "$_root/config.env" "${_files[@]}" "$@"
