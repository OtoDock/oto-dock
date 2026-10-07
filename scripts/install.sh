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
#      profile the agent sandbox needs on such hosts. May ask for your sudo
#      password; the system-wide hardening stays enabled. Other hosts skip
#      it entirely.
#   5. Linux only: applies the recommended vm.swappiness=10 host tuning via
#      /etc/sysctl.d/ (sudo again; declining it is safe — the install
#      continues either way).
#   6. Downloads the release-pinned docker-compose.yml, starts the stack, and
#      prints the dashboard URL.
#
# Fresh installs only: it never upgrades or overwrites an existing install
# (upgrade one with scripts/upgrade.sh, see
# https://docs.otodock.io/administration/upgrading). Safe to re-run
# if a step stopped it: everything that already exists is kept. All files land
# in the current directory; nothing else on the host is touched (except the
# optional AppArmor profile in step 4 and the optional sysctl drop-in in
# step 5, each documented in its own short script).
set -euo pipefail

# Where files are fetched from. OTODOCK_REF selects a branch or tag — the
# default `main` pins the release current at the time you run this, exactly
# like downloading docker-compose.yml by hand.
_ref="${OTODOCK_REF:-main}"
_raw="https://raw.githubusercontent.com/OtoDock/oto-dock/${_ref}"

say()  { echo "install.sh: $*"; }
fail() { echo "install.sh: $*" >&2; exit 1; }

# An https public URL means a reverse proxy in front, and with TRUSTED_PROXY
# empty every visitor shares its address: one person's failed sign-ins lock
# everyone out. The server says so at every boot and shows admins a banner;
# the installer says it first. The shell environment wins over .env, as for
# docker compose itself.
env_value() { # env_value <KEY>: the shell's value, else the last .env line's, unquoted
    local v="${!1:-}"
    [ -n "$v" ] || v="$(grep -E "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    printf '%s' "$v"
}
trusted_proxy_hint() {
    case "$(env_value DASHBOARD_PUBLIC_URL)" in
        [Hh][Tt][Tt][Pp][Ss]://*) ;;
        *) return 0 ;;
    esac
    [ -z "$(env_value TRUSTED_PROXY)" ] || return 0
    say "DASHBOARD_PUBLIC_URL is https but TRUSTED_PROXY is empty: behind a reverse
  proxy every visitor shares its address. In .env add the address it connects
  from: for one on this host, PROXY_BIND_IP=127.0.0.1 and TRUSTED_PROXY set to
  the otodock network's gateway (10.200.0.1 by default:
  docker network inspect otodock -f '{{(index .IPAM.Config 0).Gateway}}');
  for one on another machine, its IP. Then run: docker compose up -d"
}

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
# AppArmor and host-tuning steps below.
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

# Architecture — the release images publish linux/amd64 + linux/arm64 manifests.
# Anything else has no matching manifest, and the failure would otherwise surface
# much later as an opaque "no match for platform" pull error.
_arch="$(uname -m)"
case "$_arch" in
    x86_64|amd64|aarch64|arm64) ;;
    *) fail "unsupported CPU architecture '$_arch'. The release images are built for
  x86-64 and arm64 only. To run on this host, build from source instead:
      https://github.com/OtoDock/oto-dock/blob/main/CONTRIBUTING.md" ;;
esac

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
    To upgrade it:            run upgrade.sh in that folder:
                                curl -fsSLO $_raw/scripts/upgrade.sh
                                bash upgrade.sh
                              (https://docs.otodock.io/administration/upgrading)
    To install fresh:         create a new, empty folder (mkdir otodock && cd otodock)
                              and re-run this script from there."
fi
say "installing into $(pwd)"

# --- 3. Configuration (.env) -------------------------------------------------
# One file is the whole configuration: Compose reads it automatically, and on
# first boot the server generates its remaining secrets (API key, signing
# keys, …) and appends them here — so back this file up with your data.
# A random hex secret (openssl if present, else /dev/urandom).
rand_hex() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 24
    else
        od -vAn -N24 -tx1 /dev/urandom | tr -d ' \n'
    fi
}

# Compose loads docker-compose.override.yml by itself only while COMPOSE_FILE
# is unset, so an override placed here goes on that line, after the base file
# and the phone overlay (the order upgrade.sh keeps too).
_compose_files="docker-compose.yml:docker-compose.phone.yml"
if [ -f docker-compose.override.yml ]; then
    _compose_files="${_compose_files}:docker-compose.override.yml"
fi

if [ -f .env ]; then
    say "keeping the existing .env"
    if [ -f docker-compose.override.yml ] \
            && ! grep -qE '^[[:space:]]*COMPOSE_FILE=.*docker-compose\.override\.yml' .env; then
        say "docker-compose.override.yml is here but the kept .env does not list it on
  COMPOSE_FILE, so compose will not load it: add it at the end of that line."
    fi
    # A set key gives the DB superuser a network password on every start (the
    # opt-in described in the template below); say so rather than keep it quietly.
    if grep -qE '^OTODOCK_DB_ADMIN_PASSWORD=.' .env; then
        say "OTODOCK_DB_ADMIN_PASSWORD is set: the DB superuser gets that network password
  on every start (opt-in). Delete the line to drop it again."
    fi
else
    say "writing .env with a generated database password"
    _pw="$(rand_hex)"
    # Created private (umask 077 in a subshell): the file holds the database
    # password now and every generated secret after first boot.
    ( umask 077; cat > .env ) <<EOF
# OtoDock configuration — docker compose reads this file automatically, and
# the server appends its own generated secrets here on first boot.
# Every knob: https://github.com/OtoDock/oto-dock/blob/main/config.env.example

# The bundled PostgreSQL initialises with this APP-role password on first run;
# the platform connects as the non-superuser role 'otodock' with it.
POSTGRES_PASSWORD=${_pw}

# The DB superuser 'otodock_admin' has NO network password: reach it over the
# container's local socket with
#   docker compose exec otodock-postgres psql -U otodock_admin -d otodock
# Optional: a value here gives it a password for TCP admin tools (pgAdmin); it
# is applied on every start and removed again when you unset it.
#OTODOCK_DB_ADMIN_PASSWORD=

# The phone/voice service ships enabled by default (it idles until you
# configure telephony in the dashboard). To run without it, remove the
# phone overlay from this line:
COMPOSE_FILE=${_compose_files}

# FreePBX/Asterisk telephony only (Twilio needs nothing here): the LAN IP your
# PBX reaches this server on, for the raw audio socket. The phone ports listen
# on it too, so give a literal IP this host owns, never a hostname:
#OTO_AUDIOSOCKET_PUBLIC_HOST=192.168.1.10

# The phone daemon's AudioSocket (9092) and register API (9093) listen on
# OTO_AUDIOSOCKET_PUBLIC_HOST above, else on 127.0.0.1. Set this only when the
# PBX dials a hostname or an address this host does not own: this host's LAN
# IP, a literal IP. Never 0.0.0.0 on an internet-facing host: AudioSocket has
# no authentication of its own.
#OTO_PHONE_BIND=192.168.1.10

# Uncomment if users browse to this server by name/IP rather than localhost —
# it drives login cookies, OAuth redirects, and links in notifications:
#DASHBOARD_PUBLIC_URL=http://your-server:8400

# Uncomment to publish the dashboard on a different port:
#PROXY_PORT=8400

# Behind a reverse proxy: bind the published port to loopback and name the
# address the edge connects from, so X-Forwarded-For cannot be forged. See
# https://docs.otodock.io/getting-started/installation#put-it-behind-https
#PROXY_BIND_IP=127.0.0.1
#TRUSTED_PROXY=

# Container timezone — scheduled tasks and notification times use this:
#TZ=Europe/Athens
EOF
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

# --- 5. Host tuning: keep the platform out of swap ---------------------------
# Linux's default vm.swappiness=60 lets a memory burst from a sandboxed tool
# container push the OtoDock server's own processes into swap — and the next
# request that touches those pages stalls the dashboard while they page back
# in. scripts/setup-host-tuning.sh writes /etc/sysctl.d/90-otodock.conf with
# vm.swappiness=10 (standard sysctl precedence — easy to override or remove).
# Best-effort on purpose: this is a tuning, not a requirement, so an install
# never fails over it. Hosts without the knob (macOS/Windows) or already at
# <= 10 skip it silently inside the script.
if [ "$(cat /proc/sys/vm/swappiness 2>/dev/null || echo 0)" -gt 10 ]; then
    say "tuning the host to keep OtoDock out of swap (vm.swappiness=10 via
  /etc/sysctl.d/90-otodock.conf — one time, needs sudo; skipping is safe):"
    # Best-effort throughout — a tuning step must never abort the install,
    # so even its download failing only skips it (unlike the fatal fetch()).
    if curl -fsSL "$_raw/scripts/setup-host-tuning.sh" -o setup-host-tuning.sh.tmp \
            && mv setup-host-tuning.sh.tmp setup-host-tuning.sh; then
        _sudo=(sudo)
        if [ ! -t 0 ]; then _sudo=(sudo -n); fi
        if ! command -v sudo >/dev/null 2>&1 || ! "${_sudo[@]}" bash setup-host-tuning.sh; then
            say "skipped (sudo unavailable or declined) — OtoDock runs fine without it.
  For smoother behaviour under memory pressure, run once when convenient:
      sudo bash $(pwd)/setup-host-tuning.sh"
        fi
    else
        rm -f setup-host-tuning.sh.tmp
        say "skipped (could not download the tuning script) — OtoDock runs fine
  without it; see scripts/setup-host-tuning.sh in the repo to apply it later."
    fi
fi

# --- 6. Fetch the compose files and start ------------------------------------
# The compose files pin the OtoDock release they shipped with, so the install
# is reproducible; scripts/upgrade.sh moves it to a newer release later, new
# compose files included. docker-compose.yml is fetched LAST on purpose: its
# presence is what marks this directory as an install, so the overlay must
# already be in place by then (a failed overlay fetch must not leave a
# half-marked install).
say "downloading the phone service overlay (docker-compose.phone.yml)"
fetch "$_raw/docker-compose.phone.yml" docker-compose.phone.yml

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
trusted_proxy_hint
