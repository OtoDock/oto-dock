#!/usr/bin/env bash
#
# OtoDock — DEVELOPMENT bootstrap (host-process proxy + Dockerized Postgres).
#
# ┌─ DEV ONLY ────────────────────────────────────────────────────────────────┐
# │ Stands up a local development install that FOLLOWS THE PINNED TOOLCHAIN in  │
# │ VERSIONS.md (Python / Node / uv / pnpm) so the dev box matches what the app │
# │ ships — no more "host python/node is a different version" drift.            │
# │                                                                            │
# │   • Postgres runs in Docker (never native).                                │
# │   • Proxy + dashboard run as HOST PROCESSES from source (fast iteration,    │
# │     easy debugging, native bwrap sandbox).                                  │
# │                                                                            │
# │ This is NOT the production installer: no sudoers, no storage-quota tooling, │
# │ no /opt|/var/lib|/etc split, no update/rollback machinery.                  │
# │ Production = Docker Compose (docker-compose.yml). Data stays in a single │
# │ folder under the repo root, which is the code default for dev.             │
# └────────────────────────────────────────────────────────────────────────────┘
#
# Idempotent — safe to re-run. Targets Debian/Ubuntu (apt + NodeSource).
# Usage:  scripts/dev-setup.sh [--service] [--phone]
#
#   --service   also generate + enable systemd unit(s) for THIS checkout and
#               the invoking user, so the dev box doubles as a long-running
#               install. Without it the script only prints the foreground run
#               commands.
#   --phone     also set up the optional telephony daemon (phone/ component):
#               pinned-Python venv + deps; with --service, its unit too. Off by
#               default — it needs an external PBX/SIP trunk to be useful.
#
set -euo pipefail

INSTALL_SERVICE=0; WITH_PHONE=0
for _arg in "$@"; do
    case "$_arg" in
        --service) INSTALL_SERVICE=1 ;;
        --phone)   WITH_PHONE=1 ;;
        *) echo "unknown flag: $_arg (usage: scripts/dev-setup.sh [--service] [--phone])" >&2; exit 2 ;;
    esac
done

PLATFORM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PLATFORM_ROOT"
export PATH="$HOME/.local/bin:$PATH"   # uv/pipx land here on some installs

# ── colors / logging ──────────────────────────────────────────────────────
if [ -t 1 ]; then B='\033[0;34m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'; else B=; G=; Y=; R=; N=; fi
info() { echo -e "${B}[dev-setup]${N} $1"; }
ok()   { echo -e "${G}[ok]${N} $1"; }
warn() { echo -e "${Y}[warn]${N} $1"; }
die()  { echo -e "${R}[error]${N} $1" >&2; exit 1; }

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

# ── pinned versions — read straight from VERSIONS.md (single source of truth) ─
# shellcheck source=versions.sh
source "$PLATFORM_ROOT/scripts/versions.sh"
PYTHON_VERSION="$(otodock_version_get PYTHON_VERSION)"; PYTHON_VERSION="${PYTHON_VERSION:-3.13.14}"
PYVER_MINOR="${PYTHON_VERSION%.*}"                              # 3.13
NODE_VERSION="$(otodock_version_get NODE_VERSION)";   NODE_VERSION="${NODE_VERSION:-24.18.0}"
NODE_MAJOR="${NODE_VERSION%%.*}"                               # 24
UV_VERSION="$(otodock_version_get UV_VERSION)";       export UV_VERSION   # honored by install-baseline-tools.sh
PNPM_VERSION="$(otodock_version_get PNPM_VERSION)";   export PNPM_VERSION

echo
info "Pinned toolchain (from VERSIONS.md): python ${PYTHON_VERSION} · node ${NODE_VERSION} · uv ${UV_VERSION:-?} · pnpm ${PNPM_VERSION:-?}"
echo

# ── 1. Node (pinned major, via NodeSource) ────────────────────────────────
# install-baseline-tools.sh does NOT install Node, and the distro's node is the
# wrong major — so put the pinned major under /usr FIRST (the sandbox mounts
# /usr, not $HOME, so a version-manager node in ~/ is invisible to agents).
install_node() {
    local cur=""
    command -v node >/dev/null 2>&1 && cur="$(node -v 2>/dev/null | sed 's/^v//;s/\..*//')"
    if [ "$cur" = "$NODE_MAJOR" ]; then
        ok "Node ${NODE_MAJOR}.x present ($(node -v))"
        return
    fi
    info "Installing Node ${NODE_MAJOR}.x via NodeSource (found: ${cur:-none})..."
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | $SUDO -E bash -
    $SUDO apt-get install -y nodejs
    case "$(readlink -f "$(command -v node)")" in
        /usr/*) ok "Node $(node -v) at $(command -v node)" ;;
        *) die "node is not under /usr — sandboxed MCPs won't see it. Remove the version-manager node and re-run." ;;
    esac
}

# ── 2. Baseline toolchain (git, gh, python3, uv@pinned, pnpm@pinned, bwrap, passt, CLIs) ─
install_baseline() {
    info "Running install-baseline-tools.sh (git/gh/uv/pnpm/bubblewrap/passt/CLIs)..."
    bash "$PLATFORM_ROOT/scripts/install-baseline-tools.sh"
    command -v uv >/dev/null 2>&1 || die "uv not on PATH after baseline install"
    ok "Baseline toolchain installed"
}

# ── 3. Docker (needed for Postgres + Docker MCPs — required on every install) ─
install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        ok "Docker + Compose present ($(docker --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1))"
        return
    fi
    info "Installing Docker Engine + Compose (get.docker.com)..."
    curl -fsSL https://get.docker.com | $SUDO sh
    if [ -n "$SUDO" ]; then
        $SUDO usermod -aG docker "$USER" || true
        warn "Added $USER to the 'docker' group — run 'newgrp docker' or re-login BEFORE starting the proxy from this shell, or it will boot WITHOUT its Docker MCPs (file-tools etc.)."
    fi
}

# ── 4. Pinned Python + proxy venv ─────────────────────────────────────────
# uv fetches the EXACT pinned CPython (distro-independent); the venv is built on
# it, then proxy/setup.sh reuses that venv (it skips creation when venv/ exists)
# to install deps + generate config.env — one source of truth for both.
setup_proxy() {
    info "Provisioning Python ${PYTHON_VERSION} via uv..."
    uv python install "$PYTHON_VERSION"

    local venv="$PLATFORM_ROOT/proxy/venv" have=""
    [ -x "$venv/bin/python" ] && have="$("$venv/bin/python" -V 2>&1 | awk '{print $2}')"
    case "$have" in
        "$PYVER_MINOR".*)
            if [ -x "$venv/bin/pip" ]; then
                ok "proxy/venv already on Python $have"
            else
                # Right Python but no pip — a venv created without --seed (plain
                # `uv venv` from an earlier session, rsync'd tree) breaks
                # setup.sh's `venv/bin/pip`; rebuild seeded.
                info "Rebuilding proxy/venv (Python $have but no pip — not seeded)"
                rm -rf "$venv"
                uv venv "$venv" --python "$PYTHON_VERSION" --seed
            fi
            ;;
        *)
            [ -n "$have" ] && info "Rebuilding proxy/venv ($have → ${PYVER_MINOR}.x)"
            rm -rf "$venv"
            uv venv "$venv" --python "$PYTHON_VERSION" --seed   # --seed → pip in the venv, so setup.sh's `venv/bin/pip` works
            ;;
    esac

    info "Running proxy/setup.sh (deps + config.env, on the pinned venv)..."
    ( cd "$PLATFORM_ROOT/proxy" && ./setup.sh )
    ok "Proxy venv on Python $("$venv/bin/python" -V 2>&1 | awk '{print $2}')"
}

# ── 5. Postgres in Docker ─────────────────────────────────────────────────
start_postgres() {
    [ -f "$PLATFORM_ROOT/config.env" ] || die "config.env missing — proxy/setup.sh should have created it"
    local DC="docker"; docker info >/dev/null 2>&1 || DC="sudo docker"
    info "Starting Postgres (${POSTGRES_IMAGE:-postgres:16.14-alpine}) in Docker..."
    $DC compose --env-file "$PLATFORM_ROOT/config.env" -f "$PLATFORM_ROOT/docker-compose.t1.yml" up -d postgres
    ok "Postgres up on 127.0.0.1:5432 (container otodock-postgres)"
}

# ── 6. Dashboard build (on the pinned Node) ───────────────────────────────
build_dashboard() {
    info "Building the dashboard (npm ci && npm run build) on Node $(node -v)..."
    ( cd "$PLATFORM_ROOT/dashboard" && npm ci && npm run build )
    ok "Dashboard built (dist/)"
}

# ── 6b. Optional telephony daemon (--phone) ───────────────────────────────
# Same pinned-venv treatment as the proxy (phone/setup.sh alone would build on
# the system python). Phone-specific knobs live in phone/.env; runtime config
# (routes, providers) is pushed by the proxy over /ws/phone-management.
setup_phone() {
    [ "$WITH_PHONE" = 1 ] || return 0
    [ -d "$PLATFORM_ROOT/phone" ] || die "--phone: this checkout has no phone/ component"
    local venv="$PLATFORM_ROOT/phone/venv" have=""
    [ -x "$venv/bin/python" ] && have="$("$venv/bin/python" -V 2>&1 | awk '{print $2}')"
    case "$have" in
        "$PYVER_MINOR".*)
            if [ -x "$venv/bin/pip" ]; then
                ok "phone/venv already on Python $have"
            else
                info "Rebuilding phone/venv (Python $have but no pip — not seeded)"
                rm -rf "$venv"
                uv venv "$venv" --python "$PYTHON_VERSION" --seed
            fi
            ;;
        *)
            [ -n "$have" ] && info "Rebuilding phone/venv ($have → ${PYVER_MINOR}.x)"
            rm -rf "$venv"
            uv venv "$venv" --python "$PYTHON_VERSION" --seed
            ;;
    esac
    info "Installing phone deps (includes the on-box audio models — this can take a while)..."
    "$venv/bin/pip" install --quiet -r "$PLATFORM_ROOT/phone/requirements.txt"
    # Editable local audio package with the on-box [localmodels] stack — not a
    # requirements.txt pin; skipping it breaks the daemon at import time.
    "$venv/bin/pip" install --quiet -e "$PLATFORM_ROOT/audio[localmodels]"
    ok "Phone daemon ready"
}

# ── 7. Optional systemd service (--service) ───────────────────────────────
# Generates the unit FROM this checkout (absolute paths + invoking user), so it
# works out of the box wherever the repo lives. proxy/proxy.service stays in
# the repo as the hand-edit template for anyone managing the unit themselves.
install_service() {
    [ "$INSTALL_SERVICE" = 1 ] || return 0
    info "Installing systemd unit otodock-proxy.service (user $USER, root $PLATFORM_ROOT)..."
    $SUDO tee /etc/systemd/system/otodock-proxy.service >/dev/null <<EOF
[Unit]
Description=OtoDock — multi-agent AI orchestration platform
# Postgres runs in Docker (docker-compose.t1.yml), so wait for the daemon.
After=network.target docker.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$PLATFORM_ROOT/proxy
EnvironmentFile=$PLATFORM_ROOT/config.env
ExecStart=$PLATFORM_ROOT/proxy/venv/bin/python app.py
Restart=on-failure
RestartSec=10
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
Environment="NODE_PATH=/usr/lib/node_modules"
StandardOutput=journal
StandardError=journal
SyslogIdentifier=otodock-proxy

[Install]
WantedBy=multi-user.target
EOF
    if [ "$WITH_PHONE" = 1 ]; then
        info "Installing systemd unit otodock-phone.service..."
        $SUDO tee /etc/systemd/system/otodock-phone.service >/dev/null <<EOF
[Unit]
Description=OtoDock phone server — PBX AudioSocket to STT/LLM/TTS pipeline
After=network.target otodock-proxy.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$PLATFORM_ROOT/phone
EnvironmentFile=$PLATFORM_ROOT/config.env
ExecStart=$PLATFORM_ROOT/phone/venv/bin/python main.py
Restart=on-failure
RestartSec=10
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
StandardOutput=journal
StandardError=journal
SyslogIdentifier=otodock-phone

[Install]
WantedBy=multi-user.target
EOF
    fi
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now otodock-proxy
    ok "otodock-proxy.service enabled + started"
    if [ "$WITH_PHONE" = 1 ]; then
        $SUDO systemctl enable --now otodock-phone
        ok "otodock-phone.service enabled + started"
    fi
}

install_node
install_baseline
install_docker
setup_proxy
start_postgres
build_dashboard
setup_phone
install_service

echo
ok "Development environment ready."
echo
info "Versions in use:"
echo "    python (venv): $("$PLATFORM_ROOT/proxy/venv/bin/python" -V 2>&1 | awk '{print $2}')   (pinned ${PYTHON_VERSION})"
echo "    node:          $(node -v)   (pinned major ${NODE_MAJOR})"
echo "    uv:            $(uv --version 2>/dev/null | awk '{print $2}')"
echo "    pnpm:          $(pnpm --version 2>/dev/null || echo '—')"
echo
if [ "$INSTALL_SERVICE" = 1 ]; then
    info "Running as systemd service(s):"
    echo "    status:   systemctl status otodock-proxy"
    echo "    logs:     journalctl -u otodock-proxy -f"
    echo "    restart:  sudo systemctl restart otodock-proxy   # after git pull / code changes"
    [ "$WITH_PHONE" = 1 ] && echo "    phone:    systemctl status otodock-phone · journalctl -u otodock-phone -f"
else
    info "Run it (two terminals — foreground, so you get reload + logs):"
    echo "    1) proxy:      cd proxy && venv/bin/python app.py"
    echo "    2) dashboard:  cd dashboard && npm run dev        # hot-reload; or use the built dist/ served by the proxy"
    [ "$WITH_PHONE" = 1 ] && echo "    3) phone:      cd phone && venv/bin/python main.py"
    echo
    info "Long-running dev box? Re-run with --service to register systemd unit(s) for this checkout."
fi
echo
info "Health check:   curl http://localhost:8400/health"
echo
warn "First proxy boot runs the sandbox preflight — it needs unprivileged user namespaces enabled + passt/bubblewrap (installed above)."
