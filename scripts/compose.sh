#!/usr/bin/env bash
#
# OtoDock self-host Compose wrapper — the build-from-source / full-featured
# flow. Sources the pinned image versions from VERSIONS.md (scripts/versions.sh)
# and runs `docker compose` with the overlay stack:
#
#   docker-compose.yml        the pull-only base (what end users run standalone)
#   docker-compose.build.yml  build contexts + VERSIONS.md-pinned base images
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
    echo "  Container-only flow: printf 'POSTGRES_PASSWORD=%s\\n' \"\$(openssl rand -hex 24)\" > config.env" >&2
    echo "  Native/dev flow:     cd proxy && ./setup.sh   (or scripts/dev-setup.sh for the full bootstrap)" >&2
    exit 1
fi

_files=(-f "$_root/docker-compose.yml" -f "$_root/docker-compose.build.yml")
if [ -f "$_root/docker-compose.phone.yml" ] && [ "${OTODOCK_PHONE:-1}" != "0" ]; then
    _files+=(-f "$_root/docker-compose.phone.yml")
fi

export OTODOCK_ENV_FILE="$_root/config.env"
exec docker compose --env-file "$_root/config.env" "${_files[@]}" "$@"
