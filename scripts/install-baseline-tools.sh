#!/bin/bash
# OtoDock baseline dev tooling installer.
#
# Single source of truth for what the host must have installed for OtoDock
# agent sandboxes to function. Read by:
#
#   - the platform `Dockerfile`  (Docker deployments — baked into the image via RUN)
#
# (A bare-metal platform `setup.sh` install path is planned but not yet wired.)
#
# Tools installed: see the tier lists below for the canonical set;
# version sources: `VERSIONS.md` for pinned tools, distro default for the rest.
#
# Tier 1 — coding essentials:  git, gh, python+pipx+uv, node+npm+pnpm,
#                              curl, wget, jq, ripgrep, tree, make, gcc,
#                              build-essential, ca-certificates, gnupg
# Tier 2 — document inspection: poppler-utils, sqlite3
# CLIs:                        claude (Anthropic), codex (OpenAI) — via npm
# Git credential helper:       /usr/local/bin/oto-git-credential-helper +
#                              `/etc/gitconfig` wiring so sandboxed `git`
#                              consults `GH_TOKEN` (from manifest
#                              `env_injection`) for github.com URLs.
#
# Idempotent — re-running is a no-op for already-installed tools.
# Auto-detects EUID: if root, runs apt/cmds directly; else prefixes with sudo.
#
# Usage:
#     bash scripts/install-baseline-tools.sh           # as user (uses sudo)
#     sudo bash scripts/install-baseline-tools.sh      # as root
#     SKIP_TIER_2=true bash scripts/install-baseline-tools.sh   # skip heavy tools
#
# Optional --restart flag: when given, restarts otodock-proxy +
# otodock-phone systemd services after install (if they exist). Default:
# prints clear restart instructions but doesn't actually restart, so the
# script is safe to call during Docker build / fresh install / dev box
# where the user controls service lifecycle.
#
# Note on MCP self-containment: this script installs *base* python3 + node
# only. MCPs are fully self-contained — each MCP has its own venv (bound
# to system python OR a uv-downloaded version like ha-mcp's 3.13) and its
# own node_modules. Bumping base python/node does NOT break installed MCPs
# at the dependency level; only native bindings (rare) might need
# `npm rebuild`.

set -euo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
ok() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# --- sudo auto-detect ---
# Inside a Docker RUN (root), there's no sudo and no need for it.
# On a bare-metal host (non-root user), prefix with sudo.
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi

# --- OS detection ---
OS="$(uname -s)"
case "$OS" in
    Linux)  OS_TYPE="linux" ;;
    Darwin) OS_TYPE="macos" ;;
    *)      err "Unsupported OS: $OS"; exit 1 ;;
esac

# --- Opt-out env vars (CI / pre-baked image testing) ---
SKIP_TIER_1="${SKIP_TIER_1:-false}"
SKIP_TIER_2="${SKIP_TIER_2:-false}"
SKIP_CLAUDE_CLI="${SKIP_CLAUDE_CLI:-false}"
SKIP_CODEX_CLI="${SKIP_CODEX_CLI:-false}"
SKIP_CREDENTIAL_HELPER="${SKIP_CREDENTIAL_HELPER:-false}"

# --- Auto-restart flag ---
RESTART_PLATFORM=false
for arg in "$@"; do
    case "$arg" in
        --restart) RESTART_PLATFORM=true ;;
        --help|-h)
            sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0
            ;;
    esac
done

# Where to find the credential helper script — relative to this file's dir.
# Works for repo-local runs (scripts/install-baseline-tools.sh next to
# scripts/oto-git-credential-helper). For curl-piped runs (the installer
# fetched over HTTP) the calling script must fetch the helper separately.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREDENTIAL_HELPER_SRC="$SCRIPT_DIR/oto-git-credential-helper"
CREDENTIAL_HELPER_DST="/usr/local/bin/oto-git-credential-helper"

# ──────────────────────────────────────────────────────────────────────
# Linux (Debian/Ubuntu) — apt path
# ──────────────────────────────────────────────────────────────────────

install_linux_tier_1() {
    info "Tier 1 — coding essentials (apt)..."

    if ! command -v apt-get &>/dev/null; then
        err "apt-get not found — this script supports Debian/Ubuntu only."
        err "See the tier lists at the top of this script for the packages to install manually."
        exit 1
    fi

    # gh apt repo — only add if missing
    if [ ! -f /etc/apt/sources.list.d/github-cli.list ]; then
        info "Adding GitHub CLI apt repo..."
        $SUDO mkdir -p -m 755 /etc/apt/keyrings
        wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
            | $SUDO tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
        $SUDO chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
            | $SUDO tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    fi

    info "apt-get update + install Tier 1..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y --no-install-recommends \
        git gh \
        python3 python3-pip python3-venv pipx \
        curl wget jq ripgrep tree \
        make gcc build-essential \
        ca-certificates gnupg \
        openssh-client \
        bubblewrap

    # Node.js — NodeSource repo pinned to the platform's major (NODE_VERSION,
    # keep in sync with VERSIONS.md; apt floats within the major). Ubuntu/Debian's
    # own
    # `nodejs` package is far too old for the CLIs (min Node 22). Without
    # this, a FRESH machine has no npm and every npm-dependent step below
    # (pnpm + the claude/codex CLI installs) falls over with "npm not
    # present" — first hit on a fresh dev-VM satellite pairing. Same keyring
    # pattern as the gh repo above; idempotent.
    local node_ver="${NODE_VERSION:-24.18.0}"
    local node_major="${node_ver%%.*}"
    if ! command -v npm &>/dev/null; then
        info "Adding NodeSource apt repo (node ${node_major}.x)..."
        $SUDO mkdir -p -m 755 /etc/apt/keyrings
        wget -qO- https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
            | $SUDO gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
        $SUDO chmod go+r /etc/apt/keyrings/nodesource.gpg
        echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${node_major}.x nodistro main" \
            | $SUDO tee /etc/apt/sources.list.d/nodesource.list > /dev/null
        $SUDO apt-get update -qq
        $SUDO apt-get install -y nodejs
    fi

    # uv — system-wide so sandboxed agents see it (HOME isn't mounted in
    # bwrap, so ~/.local/bin/uv would be invisible to the agent). PINNED to an
    # exact version (uv is 0.x and moves fast — an unpinned `latest` would make
    # installs/images non-reproducible). Keep UV_VERSION in sync with VERSIONS.md;
    # the Docker build runs this BEFORE VERSIONS.md is copied in, so the default
    # is carried here (same as the claude/codex pins below).
    local uv_ver="${UV_VERSION:-0.11.24}"
    if ! command -v uv &>/dev/null; then
        info "Installing uv ${uv_ver} to /usr/local/bin..."
        # `pipx install --global` (drops binaries straight into /usr/local/bin)
        # needs pipx >= 1.5; Debian/Ubuntu ship an older pipx, so fall back to a
        # plain pipx install and COPY the resolved binaries into /usr/local/bin.
        # A copy — NOT a symlink into $HOME — because the runtime user routinely
        # differs from the installing user (the Docker image builds as root but
        # runs as uid 1000; the bwrap sandbox mounts /usr but not $HOME), and
        # $HOME (/root) is mode 0700, so a $HOME-bound symlink is unreachable to
        # both. uv/uvx are self-contained static binaries → a copy runs anywhere.
        if ! $SUDO pipx install --global "uv==${uv_ver}" 2>/dev/null; then
            pipx install "uv==${uv_ver}"
            for _b in uv uvx; do
                src="$(command -v "$_b" 2>/dev/null || echo "$HOME/.local/bin/$_b")"
                if [ -e "$src" ] && [ ! -e "/usr/local/bin/$_b" ]; then
                    $SUDO cp -Lf "$src" "/usr/local/bin/$_b"
                fi
            done
        fi
    fi

    # pnpm — needs npm to bootstrap. PINNED to an exact version (keep
    # PNPM_VERSION in sync with VERSIONS.md; v11 is the current major).
    local pnpm_ver="${PNPM_VERSION:-11.9.0}"
    if command -v npm &>/dev/null && ! command -v pnpm &>/dev/null; then
        info "Installing pnpm ${pnpm_ver} via npm..."
        $SUDO npm install -g "pnpm@${pnpm_ver}"
    elif ! command -v npm &>/dev/null; then
        warn "npm not present — install Node 24 LTS first, then re-run for pnpm."
    fi

    ok "Tier 1 installed"
}

install_linux_tier_2() {
    info "Tier 2 — document inspection utilities (apt)..."
    # poppler-utils for PDF bash inspection, sqlite3 for embedded DB
    # work. Office / PDF / image editing live in file-tools-mcp; video
    # work lives in video-mcp.
    $SUDO apt-get install -y --no-install-recommends \
        poppler-utils sqlite3
    ok "Tier 2 installed"
}

install_netns_tools() {
    # REQUIRED — sandbox network isolation is always on. `passt` provides
    # `pasta`, which wraps every local agent sandbox in an isolated network
    # namespace; `iproute2` provides `ip` for the in-netns route blackholes.
    # The proxy's startup preflight (core/sandbox.netns_preflight) hard-fails
    # if either is missing, so a missing tool here would only defer the failure
    # to first boot — install it now (auto-fetching the upstream static pasta
    # on distros without it, e.g. Ubuntu 22.04/jammy). See VERSIONS.md
    # (PASST_VERSION) + docs/architecture/SANDBOX.md.
    info "Required — network-isolation tools (passt + iproute2)..."
    $SUDO apt-get install -y --no-install-recommends iproute2 || {
        err "iproute2 (ip) is required for sandbox network isolation."; exit 1; }

    if command -v pasta >/dev/null 2>&1; then
        ok "pasta already present ($(pasta --version 2>&1 | head -1))"
        return
    fi
    if $SUDO apt-get install -y --no-install-recommends passt 2>/dev/null && \
       command -v pasta >/dev/null 2>&1; then
        ok "passt installed — pasta available"
        return
    fi

    # Not in this distro's apt (jammy) — fetch the upstream static build.
    warn "passt not in apt — fetching the upstream static pasta build..."
    local arch dl
    case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in
        amd64|x86_64)  arch="x86_64" ;;
        arm64|aarch64) arch="aarch64" ;;
        *) err "no apt passt and no static build for this architecture — install \
'pasta' manually (https://passt.top/builds/); isolation is mandatory."; exit 1 ;;
    esac
    for dl in \
        "https://passt.top/builds/latest/${arch}/pasta" \
        "https://passt.top/builds/latest/${arch}/passt" ; do
        if $SUDO curl -fsSL "$dl" -o /usr/local/bin/pasta 2>/dev/null; then
            $SUDO chmod 0755 /usr/local/bin/pasta
            # pasta is passt invoked as argv0 'pasta' — keep a passt alias too.
            $SUDO ln -sf /usr/local/bin/pasta /usr/local/bin/passt
            if pasta --version >/dev/null 2>&1; then
                ok "pasta installed (upstream static) — $(pasta --version 2>&1 | head -1)"
                return
            fi
        fi
    done
    err "Could not obtain 'pasta' (sandbox network isolation is mandatory)."
    err "  Install the upstream static build from https://passt.top/builds/"
    err "  (pin: PASST_VERSION in VERSIONS.md) into /usr/local/bin, then re-run."
    exit 1
}

install_quota_tools() {
    # OPTIONAL — for HARD storage-quota enforcement. `xfsprogs` provides
    # `xfs_quota`, which assigns project IDs + limits on a project-quota XFS
    # mount. Hard enforcement auto-activates only when the data dir is on such a
    # mount (we never create an image or reconfigure a host fs); otherwise the
    # soft tier (measure + warn) runs. Best-effort: a distro that ships nothing
    # must NOT fail the baseline — the proxy degrades to soft + logs one line.
    info "Optional — storage-quota tools (xfsprogs)..."
    if $SUDO apt-get install -y --no-install-recommends xfsprogs 2>/dev/null; then
        ok "xfsprogs installed — xfs_quota available for hard storage-quota enforcement"
    else
        warn "xfsprogs not installed — hard storage quotas need it (xfs_quota)."
        warn "  Skipped — the soft (measurement + warnings) tier still works everywhere."
    fi
}

# ──────────────────────────────────────────────────────────────────────
# macOS — brew path (best-effort, CI matrix not fully exercised)
# ──────────────────────────────────────────────────────────────────────

install_macos_all() {
    info "macOS — installing via Homebrew (best-effort)..."

    if ! command -v brew &>/dev/null; then
        err "Homebrew not found — install from https://brew.sh and re-run,"
        err "or install the tools listed at the top of this script manually."
        exit 1
    fi

    brew install \
        git gh \
        python uv \
        node pnpm \
        jq ripgrep tree make \
        || warn "Some Tier 1 brew installs failed — check output."

    if [ "$SKIP_TIER_2" != "true" ]; then
        brew install poppler sqlite \
            || warn "Some Tier 2 brew installs failed — check output."
    fi

    ok "macOS install complete (best-effort)"
}

# ──────────────────────────────────────────────────────────────────────
# sympy — symbolic maths in the agents' python (the file-tools maths
# workflow: transcribe LaTeX → compute → write equations back). The agent
# sandbox binds the system python and its $HOME is a per-session tmpfs, so
# an on-demand `pip install --user sympy` evaporates every session — bake
# it into the environment instead. Keep SYMPY_VERSION in sync with
# VERSIONS.md.
# ──────────────────────────────────────────────────────────────────────

install_sympy() {
    local sympy_ver="${SYMPY_VERSION:-1.14.0}"
    if ! command -v python3 &>/dev/null; then
        warn "python3 not present — skipping sympy."
        return 0
    fi
    if python3 -c "import sympy" &>/dev/null; then
        ok "sympy already importable"
        return 0
    fi
    info "Installing sympy ${sympy_ver}..."
    # pip first, WITH $SUDO: lands in the system site of whatever `python3`
    # resolves to (notably the Docker image's /usr/local python). Without
    # sudo, pre-PEP-668 pips silently fall back to the installing user's
    # ~/.local — invisible to the bwrap sandbox (HOME isn't mounted).
    # Externally-managed pythons (PEP 668: Debian/Ubuntu apt python,
    # Homebrew python) refuse even under sudo — fall back to the distro
    # package there (its version floats with the distro; presence beats an
    # exact pin for a maths library).
    if $SUDO python3 -m pip install --quiet "sympy==${sympy_ver}" 2>/dev/null; then
        ok "sympy ${sympy_ver} installed via pip"
    elif [ "$OS_TYPE" = "linux" ] \
        && $SUDO apt-get install -y --no-install-recommends python3-sympy; then
        ok "sympy installed via apt (python3-sympy)"
    elif [ "$OS_TYPE" = "macos" ] && brew install sympy; then
        ok "sympy installed via brew"
    else
        warn "Could not install sympy — agents can still 'pip install sympy' per session."
    fi
}


# ──────────────────────────────────────────────────────────────────────
# CLIs — claude + codex (via npm globals on both Linux and macOS)
# ──────────────────────────────────────────────────────────────────────

# Pinned CLI versions — keep in sync with VERSIONS.md (CLAUDE_CODE_VERSION /
# CODEX_VERSION). We pin exact versions because the platform runs against a
# verified CLI (auto-update is disabled in-app). These defaults are overridable
# via the matching env vars (compose.sh passes the VERSIONS.md values in);
# _install_pinned_cli upgrades an existing install to the exact pin rather than
# skipping it.
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.206}"
CODEX_VERSION="${CODEX_VERSION:-0.144.1}"

# Extract the bare x.y.z from a CLI's --version output ("2.1.177 (Claude Code)",
# "codex-cli 0.139.0").
_cli_ver() { "$1" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1; }

# Install OR upgrade an npm-global CLI to the EXACT pinned version. The platform
# runs against a verified CLI (auto-update disabled in-app), so a mismatched
# install is upgraded — not skipped. This is what keeps the version the proxy
# actually spawns (resolved from PATH) in lockstep with VERSIONS.md; a plain
# "already installed → skip" let the system /usr install drift stale while a
# newer user-prefix copy sat unused.
_install_pinned_cli() {
    local label="$1" pkg="$2" bin="$3" want="$4"
    if command -v "$bin" &>/dev/null; then
        local have; have="$(_cli_ver "$bin")"
        if [ "$have" = "$want" ]; then
            ok "${label} at pinned ${want}"
            return
        fi
        if command -v npm &>/dev/null; then
            info "Upgrading ${label} ${have:-unknown} → ${want}..."
            $SUDO npm install -g "${pkg}@${want}"
        else
            warn "${label} is ${have:-unknown}, want ${want}, but npm not present to upgrade."
        fi
    elif command -v npm &>/dev/null; then
        info "Installing ${label} ${want}..."
        $SUDO npm install -g "${pkg}@${want}"
    else
        warn "Cannot install ${label}: npm not present."
    fi
}

install_clis() {
    # if-statements, NOT `[ … ] && …` one-liners: under `set -e` a skipped
    # CLI would make the && list (and the function) return 1 and abort the
    # whole installer before the credential helper / summary / --restart.
    if [ "$SKIP_CLAUDE_CLI" != "true" ]; then
        _install_pinned_cli "Claude Code CLI" "@anthropic-ai/claude-code" "claude" "${CLAUDE_CODE_VERSION}"
    fi
    if [ "$SKIP_CODEX_CLI" != "true" ]; then
        _install_pinned_cli "Codex CLI" "@openai/codex" "codex" "${CODEX_VERSION}"
    fi
}

# ──────────────────────────────────────────────────────────────────────
# Git credential helper for github-mcp (`env_injection` → `git push`)
# ──────────────────────────────────────────────────────────────────────

install_credential_helper() {
    if [ "$SKIP_CREDENTIAL_HELPER" = "true" ]; then
        info "Skipping git credential helper install (SKIP_CREDENTIAL_HELPER=true)"
        return 0
    fi

    if [ ! -f "$CREDENTIAL_HELPER_SRC" ]; then
        warn "Credential helper source not found at: $CREDENTIAL_HELPER_SRC"
        warn "(Expected next to this script. Skipping helper install — git push from"
        warn " sandboxed agents won't auto-use GH_TOKEN until this is set up.)"
        return 0
    fi

    info "Installing git credential helper to $CREDENTIAL_HELPER_DST..."
    $SUDO install -m 0755 "$CREDENTIAL_HELPER_SRC" "$CREDENTIAL_HELPER_DST"

    # Wire it into /etc/gitconfig. `git config --system` writes to
    # /etc/gitconfig (creating the file if absent) and overwrites the
    # key if already present — idempotent across re-runs.
    info "Wiring credential helper into /etc/gitconfig..."
    $SUDO git config --system \
        credential.https://github.com.helper "$CREDENTIAL_HELPER_DST"

    ok "Credential helper installed; \`git push\` to github.com now auto-uses GH_TOKEN"
}

# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

main() {
    info "OtoDock baseline dev tooling installer ($OS_TYPE)"
    info "Sudo prefix: ${SUDO:-<none, running as root>}"

    if [ "$OS_TYPE" = "linux" ]; then
        if [ "$SKIP_TIER_1" != "true" ]; then
            install_linux_tier_1
        fi
        if [ "$SKIP_TIER_2" != "true" ]; then
            install_linux_tier_2
        fi
        install_netns_tools
        install_quota_tools
    elif [ "$OS_TYPE" = "macos" ]; then
        install_macos_all
    fi

    install_sympy
    install_clis
    install_credential_helper

    echo ""
    ok "Baseline tooling install complete."
    echo ""
    echo "Verify with:"
    echo "    git --version && gh --version && python3 --version && uv --version"
    echo "    node --version && npm --version && pnpm --version"
    echo "    jq --version && rg --version && tree --version"
    echo "    pdftotext -v 2>&1 | head -1 && sqlite3 --version"
    if [ "$SKIP_CREDENTIAL_HELPER" != "true" ]; then
        echo ""
        echo "Credential helper check:"
        echo "    cat /etc/gitconfig | grep -A1 'credential \"https://github.com\"'"
    fi

    # --- Restart platform services (opt-in) ---
    if [ "$RESTART_PLATFORM" = "true" ] && command -v systemctl &>/dev/null; then
        echo ""
        info "Restarting platform services (--restart given)..."
        for svc in otodock-proxy otodock-phone; do
            if $SUDO systemctl is-enabled "$svc" >/dev/null 2>&1; then
                $SUDO systemctl restart "$svc" && \
                    ok "Restarted $svc" || \
                    warn "Failed to restart $svc — check: systemctl status $svc"
            fi
        done
    elif command -v systemctl &>/dev/null && \
         systemctl is-enabled otodock-proxy >/dev/null 2>&1; then
        echo ""
        warn "Platform services are running. Restart them so bwrap remounts pick"
        warn "up new tools and /etc/gitconfig:"
        echo "    sudo systemctl restart otodock-proxy otodock-phone"
        echo ""
        echo "Or re-run this script with --restart to do it automatically."
    fi
}

main "$@"
