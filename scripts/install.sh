#!/usr/bin/env bash
#
# OtoDock — one-command install for the Docker quick start.
#
#   mkdir otodock && cd otodock
#   curl -fsSLO https://raw.githubusercontent.com/OtoDock/oto-dock/main/scripts/install.sh
#   bash install.sh
#
# What it does (each step announces itself before it runs):
#   1. Checks that Docker and the Compose plugin are installed and reachable.
#   2. Installs into the directory it is run from (give it a folder of its
#      own) — and refuses to touch a directory that already holds an install.
#      $OTODOCK_DIR is still honored as an override for older one-liners.
#   3. Writes a minimal .env with a generated PostgreSQL password (kept as-is
#      if one already exists; the server appends its own generated secrets to
#      the same file on first boot).
#   4. Ubuntu 24.04+ only: installs the scoped `otodock_userns` AppArmor
#      profile the agent sandbox needs on such hosts. This is the one step
#      that may ask for your sudo password; the system-wide hardening stays
#      enabled. Other hosts skip it entirely.
#   5. Downloads the release-pinned docker-compose.yml, starts the stack, and
#      prints the dashboard URL.
#
# Fresh installs only — it never upgrades or overwrites an existing install
# (upgrades: https://docs.otodock.io/administration/upgrading). Safe to re-run
# if a step stopped it: everything that already exists is kept. All files land
# in the current directory; nothing else on the host is touched (except the
# optional AppArmor profile in step 4, which its own script documents).
set -euo pipefail

# Where files are fetched from. OTODOCK_REF selects a branch or tag — the
# default `main` pins the release current at the time you run this, exactly
# like downloading docker-compose.yml by hand.
_ref="${OTODOCK_REF:-main}"
_raw="https://raw.githubusercontent.com/OtoDock/oto-dock/${_ref}"

say()  { echo "install.sh: $*"; }
fail() { echo "install.sh: $*" >&2; exit 1; }

# Fetch a file atomically: a failed download must not leave a partial file
# behind (a half-written docker-compose.yml would look like an existing
# install on the next run).
fetch() { # fetch <url> <dest>
    curl -fsSL "$1" -o "$2.tmp" || fail "could not download $1 — check your network and retry."
    mv "$2.tmp" "$2"
}

# --- 1. Preflight ------------------------------------------------------------
# Run as a regular user: the stack itself needs no root, and the server's own
# files should not end up root-owned. Sudo is used only for the optional
# AppArmor step below.
if [ "$(id -u)" -eq 0 ]; then
    fail "please run as a regular user, not root — sudo is used only where needed.
  (On a fresh server: adduser <name> && usermod -aG docker,sudo <name>, then re-run as them.)"
fi

command -v curl >/dev/null 2>&1 || fail "curl is required — install it first (apt-get install curl)."

if ! command -v docker >/dev/null 2>&1; then
    fail "Docker is not installed. Install Docker Engine first:
      https://docs.docker.com/engine/install/
  (Debian/Ubuntu short version:
      curl -fsSL https://get.docker.com | sh
      sudo usermod -aG docker \$USER    # then log out and back in
  ) — then re-run this script."
fi
if ! docker compose version >/dev/null 2>&1; then
    fail "the Docker Compose plugin is missing (\`docker compose version\` failed).
  Install it: https://docs.docker.com/compose/install/linux/ — then re-run this script."
fi
if ! docker info >/dev/null 2>&1; then
    fail "cannot talk to the Docker daemon. Is it running, and is your user in the
  'docker' group?  (sudo usermod -aG docker \$USER — then log out and back in.)"
fi

# --- 2. Install directory ----------------------------------------------------
# The install lands right here, in the directory the script is run from —
# give it a folder of its own first (mkdir otodock && cd otodock). OTODOCK_DIR
# is honored as an override so older one-liners keep installing where they
# always did.
if [ -n "${OTODOCK_DIR:-}" ]; then
    mkdir -p "$OTODOCK_DIR"
    cd "$OTODOCK_DIR"
fi
if [ -n "${HOME:-}" ] && [ "$(pwd -P)" = "$(cd "$HOME" && pwd -P)" ]; then
    fail "this is your home directory — give the install a folder of its own:
      mkdir otodock && cd otodock
  then re-run this script from there."
fi
if [ -f docker-compose.yml ]; then
    fail "this directory already contains a docker-compose.yml — this script performs
  fresh installs only and never touches an existing one.
    To upgrade it:            https://docs.otodock.io/administration/upgrading
    To install fresh:         create a new, empty folder (mkdir otodock && cd otodock)
                              and re-run this script from there."
fi
say "installing into $(pwd)"

# --- 3. Configuration (.env) -------------------------------------------------
# One file is the whole configuration: Compose reads it automatically, and on
# first boot the server generates its remaining secrets (API key, signing
# keys, …) and appends them here — so back this file up with your data.
if [ -f .env ]; then
    say "keeping the existing .env"
else
    say "writing .env with a generated PostgreSQL password"
    if command -v openssl >/dev/null 2>&1; then
        _pw="$(openssl rand -hex 24)"
    else
        _pw="$(od -vAn -N24 -tx1 /dev/urandom | tr -d ' \n')"
    fi
    cat > .env <<EOF
# OtoDock configuration — docker compose reads this file automatically, and
# the server appends its own generated secrets here on first boot.
# Every knob: https://github.com/OtoDock/oto-dock/blob/main/config.env.example

# The bundled PostgreSQL initialises with this password on first run.
POSTGRES_PASSWORD=${_pw}

# Uncomment if users browse to this server by name/IP rather than localhost —
# it drives login cookies, OAuth redirects, and links in notifications:
#DASHBOARD_PUBLIC_URL=http://your-server:8400

# Uncomment to publish the dashboard on a different port:
#PROXY_PORT=8400

# Container timezone — scheduled tasks and notification times use this:
#TZ=Europe/Athens
EOF
    chmod 600 .env
fi

# --- 4. Ubuntu 24.04+ user-namespace restriction -----------------------------
# Ubuntu 24.04+ ships kernel.apparmor_restrict_unprivileged_userns=1: only
# processes whose AppArmor profile grants `userns` may create the unprivileged
# user namespaces the OtoDock agent sandbox is built on. The supported fix is
# a SCOPED profile for the OtoDock container only — the system-wide hardening
# stays enabled (never disable that sysctl). scripts/setup-apparmor-userns.sh
# installs it; it is short and readable, and this is the only step that needs
# sudo. Hosts without the restriction skip all of this.
_userns_sysctl="/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
if grep -qE '^\s*OTODOCK_APPARMOR_PROFILE=' .env; then
    : # explicitly configured — always wins, same as scripts/compose.sh
elif [ "$(cat "$_userns_sysctl" 2>/dev/null || echo 0)" = "1" ]; then
    if [ ! -f /etc/apparmor.d/otodock-userns ]; then
        say "this host restricts unprivileged user namespaces (the Ubuntu 24.04+
  default), which blocks the agent sandbox. Installing the scoped AppArmor
  profile 'otodock_userns' — one time, and the only sudo step:"
        fetch "$_raw/scripts/setup-apparmor-userns.sh" setup-apparmor-userns.sh
        _sudo=(sudo)
        # No tty → sudo can't prompt; try passwordless, else stop with the
        # manual instructions instead of hanging.
        if [ ! -t 0 ]; then _sudo=(sudo -n); fi
        if ! command -v sudo >/dev/null 2>&1 || ! "${_sudo[@]}" bash setup-apparmor-userns.sh; then
            # Starting anyway would boot-loop the server against the kernel
            # restriction — a clear stop beats a broken start.
            fail "could not install the profile (sudo unavailable or declined). Run once,
  as an administrator:
      sudo bash $(pwd)/setup-apparmor-userns.sh
  then re-run this installer — it picks up where it left off.
  Do NOT disable the sysctl system-wide — the scoped profile is the supported path."
        fi
    else
        say "the scoped AppArmor profile 'otodock_userns' is already installed — reusing it"
    fi
    say "selecting the profile in .env (OTODOCK_APPARMOR_PROFILE=otodock_userns)"
    echo "OTODOCK_APPARMOR_PROFILE=otodock_userns" >> .env
fi

# --- 5. Fetch the compose file and start ------------------------------------
# The compose file pins the OtoDock release it shipped with, so the install is
# reproducible; upgrading later is a one-line version bump (see the upgrade
# docs). Fetched last on purpose: its presence is what marks this directory as
# an install.
say "downloading docker-compose.yml (release-pinned)"
fetch "$_raw/docker-compose.yml" docker-compose.yml

say "starting OtoDock — the first run pulls the release images, which can take
  a few minutes on a fresh host"
docker compose up -d

# PROXY_PORT moves the published port; the shell environment wins over .env,
# matching how docker compose itself resolves it.
_port="${PROXY_PORT:-$(grep -E '^PROXY_PORT=' .env | tail -n1 | cut -d= -f2- || true)}"
_port="${_port:-8400}"
echo
say "done. OtoDock is starting at:
      http://localhost:${_port}   (from this machine)
      http://<this-server>:${_port}   (from your network)
  A fresh install greets you with the setup wizard — create your admin account
  there, then connect your AI subscription or API key.
      First run:  https://docs.otodock.io/getting-started/first-run
      Logs:       docker compose logs -f otodock-proxy   (in $(pwd))"
