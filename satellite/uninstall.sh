#!/bin/bash
# Oto Dock Satellite Uninstaller
#
# Removes the satellite daemon and all associated files. Run when:
#   - Manually decommissioning a satellite
#   - Triggered automatically by the platform when the remote machine is
#     deleted from the dashboard (via WS `uninstall` message, or via WS
#     close code 4006 = machine_deleted on next reconnect attempt).
#
# What gets removed:
#   - systemd *user* service
#     `~/.config/systemd/user/oto-dock-satellite.service` (Linux) or launchd
#     plist `~/Library/LaunchAgents/com.otodock.satellite.plist` (macOS).
#   - The entire `~/.oto-dock/` directory: satellite code, venv,
#     mcps/, agents/, satellite.conf, satellite.log.
#
# Files are only local copies — the platform retains the authoritative
# versions of agents/workspaces.
#
# Usage:
#   bash uninstall.sh             # default: prompts for confirmation
#   bash uninstall.sh --yes       # non-interactive (used by auto-uninstall)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
ok() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

ASSUME_YES="false"
if [ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ]; then
    ASSUME_YES="true"
fi

OTO_DIR="$HOME/.oto-dock"
OS="$(uname -s)"

if [ ! -d "$OTO_DIR" ] && ! systemctl --user list-units --all 2>/dev/null | grep -q oto-dock-satellite; then
    info "No satellite install detected — nothing to do."
    exit 0
fi

if [ "$ASSUME_YES" != "true" ]; then
    warn "This will permanently remove:"
    warn "  - $OTO_DIR/ (satellite code, venv, agents, mcps, config)"
    warn "  - systemd service (Linux) / launchd plist (macOS)"
    warn ""
    warn "Files on the platform (agents, workspaces, OAuth tokens) are NOT"
    warn "affected — they live on the proxy and stay intact."
    read -r -p "Continue? [y/N] " reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        info "Cancelled."
        exit 1
    fi
fi

# --- Notify platform to auto-clean the dashboard entry ---
# Best-effort: if it fails (network down, platform unreachable, secret
# already invalidated by a prior dashboard-side delete), local cleanup
# still proceeds. The conf file is read BEFORE the dir wipe below.
CONF="$OTO_DIR/satellite.conf"
if [ -f "$CONF" ]; then
    MACHINE_ID=$(grep -E '^\s*machine_id\s*=' "$CONF" | head -1 | sed 's/^[^=]*=\s*//; s/\s*$//')
    MACHINE_SECRET=$(grep -E '^\s*machine_secret\s*=' "$CONF" | head -1 | sed 's/^[^=]*=\s*//; s/\s*$//')
    PLATFORM_WSS=$(grep -E '^\s*platform_url\s*=' "$CONF" | head -1 | sed 's/^[^=]*=\s*//; s/\s*$//')
    # Strip ws:// or wss:// → http:// or https://, strip /v1/satellite suffix
    PLATFORM_HTTP=$(echo "$PLATFORM_WSS" | sed -e 's|^wss://|https://|' -e 's|^ws://|http://|' -e 's|/v1/satellite$||' -e 's|/v1/satellite/.*$||')
    if [ -n "$MACHINE_ID" ] && [ -n "$MACHINE_SECRET" ] && [ -n "$PLATFORM_HTTP" ]; then
        info "Notifying platform to remove dashboard entry..."
        # Pass the machine secret via a 0600 --config file, never `-H` on the
        # command line, so it can't be read from `ps` / `/proc/<pid>/cmdline`
        # during the request.
        secret_cfg=$(mktemp)
        chmod 600 "$secret_cfg"
        printf 'header = "X-Machine-Secret: %s"\n' "$MACHINE_SECRET" > "$secret_cfg"
        curl -X DELETE \
             --config "$secret_cfg" \
             -m 5 \
             --silent --output /dev/null --write-out "platform notify: HTTP %{http_code}\n" \
             "$PLATFORM_HTTP/v1/satellite/$MACHINE_ID/self-uninstall" \
             || warn "Platform notify failed (continuing with local uninstall)"
        rm -f "$secret_cfg"
    fi
fi

# --- Stop + remove service (per-user; no sudo) ---
if [ "$OS" = "Linux" ] && command -v systemctl &>/dev/null; then
    # `systemctl --user` needs the session bus; the daemon inherits these,
    # but set them defensively for the detached self-uninstall context.
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    SERVICE_FILE="$HOME/.config/systemd/user/oto-dock-satellite.service"
    info "Stopping oto-dock-satellite user service..."
    systemctl --user stop oto-dock-satellite 2>/dev/null || true
    systemctl --user disable oto-dock-satellite 2>/dev/null || true
    if [ -f "$SERVICE_FILE" ]; then
        info "Removing $SERVICE_FILE..."
        rm -f "$SERVICE_FILE"
        systemctl --user daemon-reload 2>/dev/null || true
    fi
    ok "systemd user service removed"
    # Linger is left in place — disabling it could stop OTHER user services.
    # Remove manually with `loginctl disable-linger $USER` if desired.
elif [ "$OS" = "Darwin" ]; then
    PLIST_FILE="$HOME/Library/LaunchAgents/com.otodock.satellite.plist"
    if [ -f "$PLIST_FILE" ]; then
        info "Unloading launchd plist..."
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
        rm -f "$PLIST_FILE"
        ok "launchd plist removed"
    fi
fi

# --- Remove install dir ---
if [ -d "$OTO_DIR" ]; then
    info "Removing $OTO_DIR/..."
    rm -rf "$OTO_DIR"
    ok "Install directory removed"
fi

DIM='\033[2m'

echo ""
echo -e "${GREEN}Satellite uninstall complete.${NC}"
echo ""
echo -e "${BLUE}Removed:${NC}"
echo "  - $([ "$OS" = "Darwin" ] && echo "launchd plist" || echo "systemd user service")"
echo "  - $OTO_DIR/ (code, venv, agents, mcps, config, sessions)"
echo ""
echo -e "${BLUE}Left in place (shared with other software on this machine):${NC}"
echo "  - git, gh, python, node, pnpm, uv, jq, ripgrep"
echo "  - poppler-utils, sqlite3"
echo "  - npm globals: @anthropic-ai/claude-code, @openai/codex"
echo ""
echo -e "${DIM}Pairing OtoDock did not record which of these were already"
echo -e "installed before, so removing them automatically would risk"
echo -e "breaking unrelated tooling. For a full cleanup, remove them"
echo -e "manually:${NC}"
echo ""
if [ "$OS" = "Darwin" ]; then
    echo "  brew uninstall git gh python@3 node pnpm uv jq ripgrep \\"
    echo "                 poppler sqlite"
else
    echo "  sudo apt remove --purge git gh python3 python3-venv python3-pip \\"
    echo "                          nodejs jq ripgrep poppler-utils sqlite3"
    echo "  # uv lives in ~/.local/bin/uv — rm it directly if installed via Astral installer"
fi
echo "  npm uninstall -g @anthropic-ai/claude-code @openai/codex pnpm"
echo ""
echo "To re-pair this machine, run the install command from the platform"
echo "dashboard (Remote Machines → Pair new machine)."
