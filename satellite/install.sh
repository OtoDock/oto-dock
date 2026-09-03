#!/bin/bash
# Oto Dock Satellite Installer
#
# This script is a TEMPLATE. The platform's
# `GET /v1/satellite/bootstrap` endpoint generates a self-extracting
# wrapper that defines the variables marked __OTODOCK_*__ below + embeds
# the baseline-tools script and satellite tarball as base64 payloads,
# then concatenates this template at the bottom.
#
# Usage (end-user, via dashboard install command):
#   curl -sL -H "X-Pairing-Token: <token>" \
#     https://<platform>/v1/satellite/bootstrap | bash
#
# Repo-local development invocation (mostly for testing):
#   MACHINE_ID=... MACHINE_SECRET=... PLATFORM_URL=ws://localhost:8400/v1/satellite \
#     SATELLITE_REPO=/path/to/oto-dock bash install.sh

set -euo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# --- Required variables ---
# When invoked via the bootstrap endpoint, the wrapper sets these:
MACHINE_ID="${MACHINE_ID:-}"
MACHINE_SECRET="${MACHINE_SECRET:-}"
PLATFORM_URL="${PLATFORM_URL:-}"

# Optional skip-flag (useful when the host is already provisioned).
SKIP_BASELINE_TOOLS="${SKIP_BASELINE_TOOLS:-false}"

# Embedded payloads (base64). The bootstrap endpoint sets these; for
# repo-local invocation they stay empty and we read from $SATELLITE_REPO.
BASELINE_TOOLS_B64="${BASELINE_TOOLS_B64:-}"
CREDENTIAL_HELPER_B64="${CREDENTIAL_HELPER_B64:-}"
SATELLITE_TARBALL_B64="${SATELLITE_TARBALL_B64:-}"
SATELLITE_REPO="${SATELLITE_REPO:-}"

if [ -z "$MACHINE_ID" ] || [ -z "$MACHINE_SECRET" ] || [ -z "$PLATFORM_URL" ]; then
    err "Missing MACHINE_ID, MACHINE_SECRET, or PLATFORM_URL."
    err "This script is meant to be invoked via the platform's bootstrap"
    err "endpoint, which sets these automatically:"
    err ""
    err "  curl -sL -H \"X-Pairing-Token: <token>\" \\"
    err "    https://<platform>/v1/satellite/bootstrap | bash"
    exit 1
fi

# --- Detect OS ---
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux)  OS_TYPE="linux" ;;
    Darwin) OS_TYPE="macos" ;;
    *)      err "Unsupported OS: $OS"; exit 1 ;;
esac
info "Detected OS: $OS_TYPE ($ARCH)"

# --- Prime sudo cache (best-effort). The per-user service model needs NO
# root: the systemd *user* unit is written under $HOME and started with
# `systemctl --user`. Sudo is used ONLY for (1) baseline tools via apt and
# (2) the one-time `loginctl enable-linger` (boot-without-login). Both are
# non-fatal if sudo is unavailable, so we warn rather than exit.
# Skipped entirely when already root or on macOS (brew + launchd need no sudo).
if [ "$OS_TYPE" = "linux" ] && [ "$(id -u)" -ne 0 ]; then
    if command -v sudo &>/dev/null; then
        info "Priming sudo for baseline tools + boot-linger (you may be prompted)..."
        if sudo -v 2>/dev/null; then
            # Keep the sudo timestamp fresh in case the install runs long.
            ( while true; do sudo -n true 2>/dev/null; sleep 60; done ) &
            SUDO_KEEPALIVE_PID=$!
            trap 'kill $SUDO_KEEPALIVE_PID 2>/dev/null || true' EXIT
        else
            warn "sudo declined — baseline tools may not install and boot-linger"
            warn "may need manual setup (sudo loginctl enable-linger $USER). Continuing."
        fi
    else
        warn "sudo not found — assuming baseline tools are pre-installed."
        warn "Set SKIP_BASELINE_TOOLS=true to silence this warning."
    fi
fi

# --- Step 1: baseline tools ---
install_baseline_tools() {
    if [ "$SKIP_BASELINE_TOOLS" = "true" ]; then
        info "Skipping baseline tools (SKIP_BASELINE_TOOLS=true)"
        return 0
    fi

    if [ -n "$BASELINE_TOOLS_B64" ]; then
        info "Running embedded baseline-tools install..."
        # One private temp DIR for both payloads: the baseline script looks
        # for the credential helper NEXT TO ITSELF (repo layout), so the
        # helper must land beside it — and a mktemp dir (0700) also keeps the
        # sudo-copied helper out of reach of /tmp squatting.
        local tmpdir
        tmpdir=$(mktemp -d /tmp/oto-baseline.XXXXXX)
        echo "$BASELINE_TOOLS_B64" | base64 -d > "$tmpdir/install-baseline-tools.sh"
        chmod +x "$tmpdir/install-baseline-tools.sh"
        if [ -n "${CREDENTIAL_HELPER_B64:-}" ]; then
            echo "$CREDENTIAL_HELPER_B64" | base64 -d > "$tmpdir/oto-git-credential-helper"
            chmod +x "$tmpdir/oto-git-credential-helper"
        fi
        bash "$tmpdir/install-baseline-tools.sh"
        rm -rf "$tmpdir"
    elif [ -n "$SATELLITE_REPO" ] && [ -f "$SATELLITE_REPO/scripts/install-baseline-tools.sh" ]; then
        info "Running repo-local baseline-tools install..."
        bash "$SATELLITE_REPO/scripts/install-baseline-tools.sh"
    else
        warn "No baseline-tools payload available — skipping."
        warn "If python/git/node/claude are missing, run scripts/install-baseline-tools.sh."
    fi
}
install_baseline_tools

# --- Step 2: Python check ---
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        # Floor matches the Windows installer (3.10+) — current LTS distros
        # (Ubuntu 22.04/24.04, Debian 12) ship 3.10–3.12 and must provision.
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON_CMD="$cmd"
            ok "Python $version found ($cmd)"
            break
        fi
    fi
done
if [ -z "$PYTHON_CMD" ]; then
    err "Python 3.10+ required but not found after baseline tools install."
    exit 1
fi

# --- Step 3: directory structure ---
OTO_DIR="$HOME/.oto-dock"
SATELLITE_DIR="$OTO_DIR/satellite"
AGENTS_DIR="$OTO_DIR/agents"
MCPS_DIR="$OTO_DIR/mcps"
info "Creating directory structure at $OTO_DIR..."
mkdir -p "$SATELLITE_DIR" "$AGENTS_DIR" "$MCPS_DIR"
# Owner-only from the start: the tree holds satellite.conf (machine secret),
# logs, and synced agent workspaces — none of it is another local user's
# business, and a default 755 here would leave it all world-readable.
chmod 700 "$OTO_DIR"
ok "Directories created"

# --- Step 4: extract satellite tarball ---
if [ -n "$SATELLITE_TARBALL_B64" ]; then
    info "Extracting embedded satellite package..."
    echo "$SATELLITE_TARBALL_B64" | base64 -d | tar -xz -C "$SATELLITE_DIR"
    ok "Satellite package extracted"
elif [ -n "$SATELLITE_REPO" ] && [ -d "$SATELLITE_REPO/satellite" ]; then
    info "Copying satellite package from $SATELLITE_REPO (repo-local install)..."
    cp -r "$SATELLITE_REPO/satellite" "$SATELLITE_DIR/"
    cp "$SATELLITE_REPO/satellite/requirements.txt" "$SATELLITE_DIR/"
    cp "$SATELLITE_REPO/satellite/uninstall.sh" "$SATELLITE_DIR/"
    ok "Satellite package copied"
else
    err "No satellite payload available."
    exit 1
fi

# --- Step 5: venv + Python deps ---
if [ ! -d "$SATELLITE_DIR/venv" ]; then
    "$PYTHON_CMD" -m venv "$SATELLITE_DIR/venv"
fi
"$SATELLITE_DIR/venv/bin/pip" install -q --upgrade pip
"$SATELLITE_DIR/venv/bin/pip" install -q -r "$SATELLITE_DIR/requirements.txt"
ok "Python dependencies installed"

# --- Step 5b: bootstrap uv (needed for MCPs that pin a non-system Python) ---
# Some community MCPs (e.g. unifi-network) declare `python>=3.13` while
# the platform default is 3.13. The satellite's mcp_installer auto-fetches
# the right interpreter via uv. Without uv, those MCPs silently soft-fail
# at install time. uv is small (~50MB) and installs into ~/.local/bin.
if ! [ -x "${HOME}/.local/bin/uv" ] && ! [ -x "/usr/local/bin/uv" ]; then
    info "Installing uv (needed for Python-version-pinned MCPs)..."
    if command -v curl &>/dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        ok "uv installed at ~/.local/bin/uv"
    else
        warn "curl not available; skipping uv install. Python-3.13-pinned MCPs"
        warn "(e.g. unifi-network, ha-mcp) will not install on this satellite."
    fi
else
    ok "uv already present"
fi

# --- Step 6: write satellite.conf ---
# Bare CLI names on purpose: the daemon resolves the actual binary at runtime
# (pin-verified, npm dirs scanned explicitly — host/cli_versions.py), so the
# conf must not freeze whatever this installer shell happens to resolve. A
# capture here once pinned a stale shadowed copy for good.
info "Writing satellite.conf..."
# Create 0600 BEFORE the secret lands in it — a plain redirect would leave a
# umask-default (0644) window between write and the chmod below.
rm -f "$OTO_DIR/satellite.conf"
touch "$OTO_DIR/satellite.conf"
chmod 600 "$OTO_DIR/satellite.conf"
cat > "$OTO_DIR/satellite.conf" <<EOF
[satellite]
machine_id = $MACHINE_ID
machine_secret = $MACHINE_SECRET
platform_url = $PLATFORM_URL
agents_dir = $AGENTS_DIR
mcps_dir = $MCPS_DIR

[cli]
claude_bin = claude

[codex]
codex_bin = codex
EOF
chmod 600 "$OTO_DIR/satellite.conf"
ok "Configuration written"

# --- Step 7: install + start per-user service ---
# Per-user model: the daemon runs in the installing user's own session
# (no root, no SYSTEM). A compromise can't reach root; uninstall/restart
# need no elevation. Boot-without-login is handled by `loginctl enable-linger`.
info "Installing per-user service..."
if [ "$OS_TYPE" = "linux" ] && command -v systemctl &>/dev/null; then
    # `systemctl --user` needs a session bus; ensure XDG_RUNTIME_DIR is set
    # (it is in an interactive shell, but be defensive for SSH/non-login).
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    USER_UNIT_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$USER_UNIT_DIR/oto-dock-satellite.service"
    mkdir -p "$USER_UNIT_DIR"
    # A user unit always runs as the user (no User=); WantedBy the user's
    # default.target (NOT the system multi-user.target). No After=network
    # (a system target) — the WS client retries until the network is up.
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Oto Dock Satellite Daemon

[Service]
Type=simple
ExecStart=$SATELLITE_DIR/venv/bin/python -m satellite
WorkingDirectory=$SATELLITE_DIR
Restart=always
RestartSec=5
Environment=HOME=$HOME

[Install]
WantedBy=default.target
EOF
    # Enable linger FIRST. On a headless box with no active login session,
    # `systemctl --user` has no runtime dir / user manager to talk to until
    # linger creates /run/user/<uid> and starts a persistent user manager.
    # (On a normal interactive login the dir already exists; linger is then
    # just what makes the satellite survive logout + start on boot.)
    # Best-effort: try unprivileged first (polkit allows self-linger on many
    # distros), fall back to sudo.
    LINGER_OK=false
    if loginctl enable-linger "$USER" 2>/dev/null \
        || { command -v sudo &>/dev/null && sudo loginctl enable-linger "$USER" 2>/dev/null; }; then
        LINGER_OK=true
    fi
    # Wait (up to ~5s) for the user runtime dir + manager to be reachable —
    # newly-enabled linger starts `user@<uid>.service` asynchronously.
    for _ in $(seq 1 10); do
        [ -d "$XDG_RUNTIME_DIR" ] && systemctl --user >/dev/null 2>&1 && break
        sleep 0.5
    done

    systemctl --user daemon-reload
    systemctl --user enable --now oto-dock-satellite
    ok "systemd user service installed and started (runs as $USER, no root)"

    if [ "$LINGER_OK" = true ] \
        && [ "$(loginctl show-user "$USER" --property=Linger 2>/dev/null)" = "Linger=yes" ]; then
        ok "Boot-linger enabled (starts at boot without login)"
    else
        warn "Could not enable linger. The satellite runs now and after each"
        warn "login, but NOT on a login-less reboot until you run:"
        warn "  sudo loginctl enable-linger $USER"
    fi
elif [ "$OS_TYPE" = "macos" ]; then
    PLIST_FILE="$HOME/Library/LaunchAgents/com.otodock.satellite.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.otodock.satellite</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SATELLITE_DIR/venv/bin/python</string>
        <string>-m</string>
        <string>satellite</string>
    </array>
    <key>WorkingDirectory</key><string>$SATELLITE_DIR</string>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$OTO_DIR/satellite.log</string>
    <key>StandardErrorPath</key><string>$OTO_DIR/satellite.err</string>
</dict>
</plist>
EOF
    launchctl unload "$PLIST_FILE" 2>/dev/null || true
    launchctl load "$PLIST_FILE"
    ok "launchd service installed and started"
else
    warn "Could not install system service. Run manually:"
    warn "  cd $SATELLITE_DIR && ./venv/bin/python -m satellite"
fi

echo ""
echo -e "${GREEN}Satellite installation complete!${NC}"
echo ""
echo "Machine ID: $MACHINE_ID"
echo ""
if [ "$OS_TYPE" = "linux" ]; then
    echo "Status: systemctl --user status oto-dock-satellite"
    echo "Logs:   journalctl --user -u oto-dock-satellite -f"
elif [ "$OS_TYPE" = "macos" ]; then
    echo "Status: launchctl list | grep otodock"
    echo "Logs:   tail -f $OTO_DIR/satellite.log"
fi
