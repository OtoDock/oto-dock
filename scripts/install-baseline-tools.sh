#!/bin/bash
# OtoDock baseline dev tooling installer.
#
# Single source of truth for what the host must have installed for OtoDock
# agent sandboxes to function. Read by:
#
#   - `satellite/install.sh`     (satellite hosts — calls this before pairing)
#   - the platform `Dockerfile`  (Docker deployments — baked into the image via RUN)
#   - `scripts/dev-setup.sh`     (the bare-metal dev bootstrap)
#
# (A bare-metal platform `setup.sh` install path is planned but not yet wired.)
#
# Tools installed: see the tier lists below for the canonical set;
# version sources: `VERSIONS.md` for pinned tools, distro default for the rest.
#
# Tier 1 — coding essentials:  git, gh, python+pipx+uv, node+npm+pnpm,
#                              curl, wget, jq, ripgrep, tree, make, gcc,
#                              build-essential, ca-certificates, gnupg,
#                              openssh-client, bubblewrap, Bun (Linux)
# Tier 2 — document inspection: poppler-utils, sqlite3
# Required (Linux):            passt (pasta) + iproute2 (network isolation)
# Optional (Linux):            xfsprogs, hard storage quotas
# Every OS:                    sympy in the agents' python
# CLIs:                        claude (Anthropic), codex (OpenAI) — via npm
# Git credential helper:       /usr/local/bin/oto-git-credential-helper +
#                              `/etc/gitconfig` wiring so sandboxed `git`
#                              consults `GH_TOKEN` (from manifest
#                              `env_injection`) for github.com URLs.
#
# Idempotent — re-running installs what is missing, upgrades an older uv or pnpm
# (a newer one stays), builds the pinned pasta where the host's is older than the
# floor, moves Bun and the two CLIs to their exact pins, and lets
# apt bring its packages to the distro's current version; Node and sympy are
# left once present. On macOS `brew install` brings each formula to brew's
# current version.
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
# Where pasta comes from when the host's is absent or below the floor:
# auto (build the pinned release, else fall back to what the distro offers),
# required (build it or fail: the Docker image), distro (no build or download).
OTODOCK_PASST_BUILD="${OTODOCK_PASST_BUILD:-auto}"

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

# True when version $1 is older than $2 (an equal version is not older).
_older() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]; }
# A version for a message: "0" marks a copy whose --version said nothing.
_shown() { if [ "$1" = 0 ]; then echo "(no version)"; else echo "$1"; fi; }

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
    local node_ver="${NODE_VERSION:-26.12.1}"
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
    # An installed uv older than the pin is upgraded (as the CLIs are below); a
    # newer one is left alone. The version that decides is /usr/local/bin/uv's,
    # the copy this script owns and the sandboxes see, else the one on PATH,
    # which is then copied in when it is at or above the pin: the sandboxes
    # mount /usr and never $HOME, so /usr/local/bin/uv must exist either way.
    # A copy that does not run without $HOME is removed and the pin installed.
    # The install itself also replaces the installing user's ~/.local/bin/uv:
    # pipx --force takes that path whatever sits there.
    local uv_ver="${UV_VERSION:-0.13.5}" uv_have="" uv_bin="/usr/local/bin/uv"
    local uv_install=true uv_clean=""
    [ -x "$uv_bin" ] || uv_bin="$(command -v uv 2>/dev/null || true)"
    if [ -n "$uv_bin" ]; then uv_have="$(_cli_ver "$uv_bin")"; uv_have="${uv_have:-0}"; fi
    if [ -n "$uv_have" ] && ! _older "$uv_have" "$uv_ver"; then
        uv_install=false
        [ "$uv_have" = "$uv_ver" ] || warn "uv ${uv_have} is newer than the pin ${uv_ver} — left as it is"
        if [ "$uv_bin" != /usr/local/bin/uv ]; then
            # A real file, as below; uvx is taken from beside the PATH uv.
            info "Copying uv ${uv_have} from ${uv_bin} to /usr/local/bin..."
            for _b in uv uvx; do
                src="${uv_bin%/*}/$_b"
                [ -e "$src" ] || continue
                $SUDO install -m 0755 "$(readlink -f "$src")" "/usr/local/bin/$_b" \
                    || warn "could not copy $_b into /usr/local/bin"
            done
            # The copy must run as a sandbox runs it, with no $HOME and a
            # system PATH. A version manager's shim (mise's resolves to the
            # mise binary, asdf's is a script that needs ASDF_DIR) passes the
            # check above on the host and fails here; it is removed so that a
            # re-run judges the PATH uv again, and the pin goes in instead.
            uv_clean="$({ env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent \
                /usr/local/bin/uv --version </dev/null 2>/dev/null \
                | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1; } || true)"
            if [ "$uv_clean" != "$uv_have" ]; then
                warn "uv copied from ${uv_bin} does not run without \$HOME (a version manager's shim?) — installing the pin instead"
                $SUDO rm -f /usr/local/bin/uv /usr/local/bin/uvx
                uv_have=""; uv_install=true
            fi
        fi
    fi
    if [ "$uv_install" = true ]; then
        info "Installing uv ${uv_ver}${uv_have:+ over $(_shown "$uv_have")} to /usr/local/bin..."
        # A plain pipx install, then a COPY of the binaries into /usr/local/bin
        # — never a link: the runtime user routinely differs from the
        # installing one (the Docker image builds as root but runs as uid 1000;
        # the bwrap sandbox mounts /usr but not $HOME or /opt), so a link into
        # $HOME or into `pipx --global`'s /opt/pipx is unreachable to agents.
        # uv/uvx are self-contained static binaries → a copy runs anywhere.
        # `install` replaces the destination, where `cp` would write through a
        # link left there by an earlier `pipx --global`.
        if pipx install --force "uv==${uv_ver}"; then
            for _b in uv uvx; do
                src="${PIPX_BIN_DIR:-$HOME/.local/bin}/$_b"
                [ -e "$src" ] || continue
                $SUDO install -m 0755 "$(readlink -f "$src")" "/usr/local/bin/$_b" \
                    || warn "could not copy $_b into /usr/local/bin"
            done
            hash -r
            _verify_resolved_cli "uv" "uv" "$uv_ver"
        elif [ -n "$uv_have" ]; then
            warn "uv ${uv_ver} did not install — $(_shown "$uv_have") stays"
        else
            err "uv ${uv_ver} did not install"; exit 1
        fi
    fi

    # pnpm — needs npm to bootstrap. PINNED to an exact version (keep
    # PNPM_VERSION in sync with VERSIONS.md; a new major is a deliberate bump),
    # with the uv rule above: absent or older is installed, newer is left alone.
    # --allow-scripts: pnpm 12's install step puts its native binary at the bin
    # path; npm 12 skips it by default and leaves a shim that needs Node.
    local pnpm_ver="${PNPM_VERSION:-12.15.0}" pnpm_have=""
    if command -v pnpm &>/dev/null; then pnpm_have="$(_cli_ver pnpm)"; pnpm_have="${pnpm_have:-0}"; fi
    if ! command -v npm &>/dev/null; then
        warn "npm not present — install Node ${node_major} LTS first, then re-run for pnpm."
    elif [ -n "$pnpm_have" ] && ! _older "$pnpm_have" "$pnpm_ver"; then
        [ "$pnpm_have" = "$pnpm_ver" ] || warn "pnpm ${pnpm_have} is newer than the pin ${pnpm_ver} — left as it is"
    elif [ -z "$pnpm_have" ]; then
        info "Installing pnpm ${pnpm_ver} via npm..."
        $SUDO npm install -g --allow-scripts=pnpm "pnpm@${pnpm_ver}"
    elif info "Upgrading pnpm $(_shown "$pnpm_have") → ${pnpm_ver} via npm..." \
            && $SUDO npm install -g --allow-scripts=pnpm "pnpm@${pnpm_ver}"; then
        hash -r
        _verify_resolved_cli "pnpm" "pnpm" "$pnpm_ver"
    else
        warn "pnpm ${pnpm_ver} did not install — $(_shown "$pnpm_have") stays"
    fi

    ok "Tier 1 installed"
}

install_bun() {
    # Bun — the runtime of app servers (apps with a server/ folder). The
    # proxy bind-mounts the binary into each app's sandbox, so it must be a
    # real file under /usr/local/bin (HOME is never mounted). PINNED exactly
    # (keep BUN_VERSION in sync with VERSIONS.md; the Docker build runs this
    # before VERSIONS.md is copied in, so the default is carried here). The
    # release zip is fetched straight from GitHub: no curl-pipe-bash, no rc
    # edits, the same bytes on every host.
    local bun_ver="${BUN_VERSION:-1.4.4}"
    if command -v bun &>/dev/null && [ "$(bun --version 2>/dev/null)" = "$bun_ver" ]; then
        ok "bun ${bun_ver} already installed"
        return 0
    fi
    local arch
    case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in
        amd64|x86_64)  arch="x64" ;;
        arm64|aarch64) arch="aarch64" ;;
        *) warn "no Bun build for this architecture — app servers will not start"; return 0 ;;
    esac
    # The default x64 build needs AVX2; older CPUs take the -baseline build.
    if [ "$arch" = "x64" ] && ! grep -q avx2 /proc/cpuinfo 2>/dev/null; then
        arch="x64-baseline"
    fi
    local url="https://github.com/oven-sh/bun/releases/download/bun-v${bun_ver}/bun-linux-${arch}.zip"
    local tmp
    tmp="$(mktemp -d)"
    info "Installing bun ${bun_ver} (${arch}) to /usr/local/bin..."
    if curl -fsSL "$url" -o "$tmp/bun.zip" \
        && python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$tmp/bun.zip" "$tmp"; then
        $SUDO install -m 0755 "$tmp/bun-linux-${arch}/bun" /usr/local/bin/bun
        ok "bun $(bun --version 2>/dev/null || echo "$bun_ver") installed"
    else
        warn "bun download failed (${url}) — app servers will not start until it is installed"
    fi
    rm -rf "$tmp"
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
    # The proxy's startup preflight (core/sandbox/sandbox.py::netns_preflight)
    # hard-fails if either is missing and warns on a pasta older than
    # PASST_MIN_VERSION, so install it now. See VERSIONS.md (PASST_VERSION).
    # A pasta at or above the floor stays; else the distro's passt when its
    # candidate is at or above it (Ubuntu 24.04, Debian 13); else the pinned
    # release is BUILT from its git tag (Debian 12, Ubuntu 22.04, the image):
    # passt.top keeps no versioned static build and none for aarch64. The
    # defaults follow VERSIONS.md (the Docker build runs this before it).
    local passt_ver="${PASST_VERSION:-2026_10_02.cba3570}"
    local passt_min="${PASST_MIN_VERSION:-2023_12_30}"
    local passt_commit="cba357068dc586e7540971eb40bb56c5e62c4de1"
    info "Required — network-isolation tools (passt + iproute2)..."
    $SUDO apt-get install -y --no-install-recommends iproute2 || {
        err "iproute2 (ip) is required for sandbox network isolation."; exit 1; }

    local have candidate
    have="$(_passt_date "$(pasta --version 2>/dev/null | head -1)")"
    if [ -n "$have" ] && ! _older "$have" "$passt_min"; then
        ok "pasta already present ($(pasta --version 2>&1 | head -1))"
        return
    fi
    candidate="$(apt-cache policy passt 2>/dev/null | sed -n 's/^ *Candidate: *//p')"
    if [ "$OTODOCK_PASST_BUILD" != required ] && [ -n "$(_passt_date "$candidate")" ] \
            && ! _older "$(_passt_date "$candidate")" "$passt_min" \
            && $SUDO apt-get install -y --no-install-recommends passt && command -v pasta >/dev/null 2>&1; then
        ok "passt installed — $(pasta --version 2>&1 | head -1)"
        return
    fi
    if [ "$OTODOCK_PASST_BUILD" != distro ]; then
        info "Building pasta ${passt_ver} from its release tag (the distro offers ${candidate:-no passt}, the floor is ${passt_min})..."
        if _build_pinned_passt "$passt_ver" "$passt_commit"; then
            ok "pasta installed — $(pasta --version 2>&1 | head -1)"; return
        fi
        [ "$OTODOCK_PASST_BUILD" = required ] && {
            err "pasta ${passt_ver} did not build, and this install takes no other (OTODOCK_PASST_BUILD=required)."; exit 1; }
    fi

    # The fallbacks a host had before the pinned build: what the distro
    # offers, then upstream's moving static build (x86_64 only).
    if [ -n "$have" ] || command -v pasta >/dev/null 2>&1; then
        warn "pasta $(pasta --version 2>&1 | head -1) stays, below the floor ${passt_min}"; return
    fi
    if $SUDO apt-get install -y --no-install-recommends passt 2>/dev/null && command -v pasta >/dev/null 2>&1; then
        warn "passt installed from the distro — $(pasta --version 2>&1 | head -1), below the floor ${passt_min}"; return
    fi
    # distro (a paired machine's bootstrap): such a host runs no agent
    # sandbox, so a missing pasta is no reason to fetch a moving build or fail.
    if [ "$OTODOCK_PASST_BUILD" = distro ]; then
        warn "no pasta on this host (OTODOCK_PASST_BUILD=distro): an agent sandbox here cannot start"; return
    fi
    local tmp
    tmp="$(mktemp -d)"
    if [ "$(uname -m)" = x86_64 ] && warn "fetching upstream's latest static pasta build (not the pin)..." \
            && curl -fsSL https://passt.top/builds/latest/x86_64/pasta -o "$tmp/passt" \
            && _install_passt "$tmp/passt" && pasta --version >/dev/null 2>&1; then
        rm -rf "$tmp"; ok "pasta installed (upstream static) — $(pasta --version 2>&1 | head -1)"; return
    fi
    rm -rf "$tmp"
    err "Could not obtain 'pasta' (sandbox network isolation is mandatory). Build passt ${passt_ver}"
    err "  from https://passt.top/passt (make passt) into /usr/local/bin, pasta linked to it, and re-run."
    exit 1
}

# The build date of a passt version: YYYY_MM_DD of an upstream tag, a git
# build (2026_06_11.a9c61ff-31-g...) or a Debian version (0.0~git20230309...).
_passt_date() {
    if [[ "$1" =~ ([0-9]{4})_([0-9]{2})_([0-9]{2}) ]] || [[ "$1" =~ git([0-9]{4})([0-9]{2})([0-9]{2}) ]]; then
        echo "${BASH_REMATCH[1]}_${BASH_REMATCH[2]}_${BASH_REMATCH[3]}"
    fi
}

# passt into /usr/local/bin with pasta linked to it (argv0 picks the mode):
# copied beside first, so a failed copy keeps the old one, then the old
# static layout (pasta the file, passt a link to it) is replaced.
_install_passt() {
    $SUDO install -m 0755 "$1" /usr/local/bin/passt.new && $SUDO rm -f /usr/local/bin/pasta /usr/local/bin/passt \
        && $SUDO mv /usr/local/bin/passt.new /usr/local/bin/passt && $SUDO ln -sf passt /usr/local/bin/pasta && hash -r
}

# Clone the release tag (the pinned commit, or for another tag one its short
# sha prefixes) and build it with Tier 1's C toolchain; the licence texts and
# the source tarball (GPL-2.0) go to /usr/local/share/doc/passt.
_build_pinned_passt() {
    local ver="$1" commit="$2" tmp head built=1
    tmp="$(mktemp -d)"
    if git -c advice.detachedHead=false clone -q --depth 1 --branch "$ver" https://passt.top/passt "$tmp/passt"; then
        head="$(git -C "$tmp/passt" rev-parse HEAD)"
        if [[ "$commit" == "${ver##*.}"* && "$head" != "$commit" ]] || [[ "$head" != "${ver##*.}"* ]]; then
            err "passt ${ver} resolves to commit ${head}, not the pinned one"
        elif make -s -C "$tmp/passt" passt >/dev/null && _install_passt "$tmp/passt/passt"; then
            $SUDO install -d /usr/local/share/doc/passt
            git -C "$tmp/passt" archive --format=tar.gz --prefix="passt-${ver}/" -o "$tmp/passt-${ver}.tar.gz" HEAD
            $SUDO install -m 0644 "$tmp/passt/LICENSES/"*.txt "$tmp/passt-${ver}.tar.gz" /usr/local/share/doc/passt/
            printf 'passt %s, built from https://passt.top/passt at commit %s. Its source is passt-%s.tar.gz.\nLicensed GPL-2.0-or-later, some files BSD-3-Clause (the texts beside this file).\n' \
                "$ver" "$head" "$ver" | $SUDO tee /usr/local/share/doc/passt/SOURCE > /dev/null
            built=0
        fi
    fi
    rm -rf "$tmp"
    return "$built"
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
# verified CLI (auto-update is disabled in-app). These defaults are what every
# Docker image installs: the proxy image runs this script before VERSIONS.md is
# copied in, and nothing passes the VERSIONS.md values down (a pairing or a
# bare-metal run may override them through the matching env vars);
# proxy/tests/execution/test_engine_descriptor.py holds them equal to the rows.
# _install_pinned_cli upgrades an existing install to the exact pin rather than
# skipping it.
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.296}"
CODEX_VERSION="${CODEX_VERSION:-0.163.0}"

# Extract the bare x.y.z from a CLI's --version output ("2.1.177 (Claude Code)",
# "codex-cli 0.139.0").
# Never fails (a tool whose --version breaks reads as empty under set -e) and
# never waits on stdin (a corepack shim may ask before downloading).
_cli_ver() { { "$1" --version </dev/null 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1; } || true; }

# After an install/upgrade, verify what the shell NOW resolves. The install
# places the pinned copy in its own location (npm's global prefix,
# /usr/local/bin for uv), but PATH may still resolve another
# install (e.g. the vendor's standalone installer under ~/.local/bin) — the
# upgrade then silently changes nothing for anything that spawns "$bin".
# Warn with every resolvable copy and its version so the shadow is obvious.
_verify_resolved_cli() {
    local label="$1" bin="$2" want="$3"
    local now; now="$(_cli_ver "$bin")"
    if [ "$now" = "$want" ]; then
        ok "${label} resolves at pinned ${want}"
        return 0
    fi
    warn "${label}: '${bin}' still resolves ${now:-nothing} — want ${want}."
    warn "Another install shadows the pinned copy on PATH:"
    local p v
    while read -r p; do
        [ -n "$p" ] || continue
        v="$(_cli_ver "$p")"; warn "  ${p} ($(_shown "${v:-0}"))"
    done < <(type -aP "$bin" 2>/dev/null | awk '!seen[$0]++')
    warn "Remove or upgrade the shadowing copy so '${bin}' resolves ${want}."
    return 0
}

# Install OR upgrade an npm-global CLI to the EXACT pinned version. The platform
# runs against a verified CLI (auto-update disabled in-app), so a mismatched
# install is upgraded — not skipped. This is what keeps the version the proxy
# actually spawns (resolved from PATH) in lockstep with VERSIONS.md; a plain
# "already installed → skip" let the system /usr install drift stale while a
# newer user-prefix copy sat unused. --allow-scripts lets the CLI's own install
# step run under npm 12, which blocks it by default (Claude Code's puts its
# native binary in place; without it `claude` is a stub that exits 1).
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
            $SUDO npm install -g --allow-scripts="${pkg}" "${pkg}@${want}"
            _verify_resolved_cli "$label" "$bin" "$want"
        else
            warn "${label} is ${have:-unknown}, want ${want}, but npm not present to upgrade."
        fi
    elif command -v npm &>/dev/null; then
        info "Installing ${label} ${want}..."
        $SUDO npm install -g --allow-scripts="${pkg}" "${pkg}@${want}"
        _verify_resolved_cli "$label" "$bin" "$want"
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
            install_bun
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
    # Only a unit that is running needs the restart: dev-setup.sh runs this
    # script with otodock-proxy stopped (it refuses while the unit is
    # active), so keying on is-enabled would call a stopped unit running.
    elif command -v systemctl &>/dev/null && \
         systemctl is-active --quiet otodock-proxy; then
        echo ""
        warn "Platform services are running. Restart them so bwrap remounts pick"
        warn "up new tools and /etc/gitconfig:"
        echo "    sudo systemctl restart otodock-proxy otodock-phone"
        echo ""
        echo "Or re-run this script with --restart to do it automatically."
    fi
}

main "$@"
