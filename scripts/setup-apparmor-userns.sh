#!/usr/bin/env bash
#
# OtoDock — one-time host setup for Ubuntu's unprivileged-userns restriction.
#
# Ubuntu 24.04+ ships kernel.apparmor_restrict_unprivileged_userns=1: an
# unprivileged process may only create user namespaces when its AppArmor
# profile explicitly grants `userns`. The containerised OtoDock proxy needs
# that capability for the agent sandbox (every session runs in a rootless
# bwrap+pasta namespace), so on such hosts the proxy boot preflight
# hard-fails until the host allows it.
#
# This script installs the SCOPED fix: the `otodock_userns` AppArmor profile
# (embedded below — the same flags=(unconfined) + `userns,` shape Docker's own
# docs prescribe for rootlesskit on Ubuntu 24.04) into /etc/apparmor.d/ and
# loads it. The proxy container then runs under
# `security_opt: apparmor=otodock_userns` — scripts/compose.sh selects it
# automatically once installed; standalone `docker compose` users set
# OTODOCK_APPARMOR_PROFILE=otodock_userns in .env. The system-wide restriction
# stays ON — unlike the commonly-suggested
# `sysctl kernel.apparmor_restrict_unprivileged_userns=0`, which disables the
# hardening for every process on the host.
#
#   sudo scripts/setup-apparmor-userns.sh
#
# Self-contained (no other repo files needed), so the pull-only compose flow
# can fetch and run just this file. Idempotent; safe on any host — it exits 0
# with a message on hosts that don't have the restriction (older Ubuntu,
# Debian, non-AppArmor distros), whose pre-4.x AppArmor parsers couldn't
# compile the `userns,` rule anyway.
set -euo pipefail

PROFILE_DST="/etc/apparmor.d/otodock-userns"
PROFILE_NAME="otodock_userns"
SYSCTL_FILE="/proc/sys/kernel/apparmor_restrict_unprivileged_userns"

if [ ! -e "$SYSCTL_FILE" ]; then
    echo "setup-apparmor-userns: this host does not restrict unprivileged user"
    echo "  namespaces ($SYSCTL_FILE not present) — nothing to do."
    exit 0
fi
if [ "$(cat "$SYSCTL_FILE")" != "1" ]; then
    echo "setup-apparmor-userns: kernel.apparmor_restrict_unprivileged_userns is"
    echo "  currently 0 — the restriction is off. Installing the profile anyway so"
    echo "  you can re-enable the system hardening:"
    echo "      sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=1"
    echo "  (and remove any /etc/sysctl.d override that sets it to 0)."
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "setup-apparmor-userns: must run as root:  sudo $0" >&2
    exit 1
fi
if ! command -v apparmor_parser >/dev/null 2>&1; then
    echo "setup-apparmor-userns: apparmor_parser not found — install the 'apparmor'" >&2
    echo "  package first (it is preinstalled on every Ubuntu release that has the" >&2
    echo "  userns restriction, so this is unexpected):  sudo apt-get install apparmor" >&2
    exit 1
fi

# The scoped profile: named (no attachment path — it is only ever attached to
# the proxy container via security_opt), unconfined semantics (the stack
# already requires that for bwrap's mount propagation) plus the userns grant.
cat > "$PROFILE_DST" <<'EOF'
# OtoDock — scoped AppArmor userns grant for the containerised proxy.
#
# Grants unprivileged user-namespace creation (the capability the agent
# sandbox is built on) to the OtoDock proxy container ONLY, attached via
# `security_opt: apparmor=otodock_userns`. Keeps the container's AppArmor
# mediation unconfined — the same semantics the stack already requires —
# while the system-wide kernel.apparmor_restrict_unprivileged_userns=1
# hardening stays enabled for everything else on the host.
#
# Installed by scripts/setup-apparmor-userns.sh (OtoDock). Loaded at boot by
# apparmor.service. Requires AppArmor 4.x (the `userns,` rule).

abi <abi/4.0>,

include <tunables/global>

profile otodock_userns flags=(unconfined) {
  userns,

  # Site-specific additions and overrides. See local/README for details.
  include if exists <local/otodock_userns>
}
EOF
chmod 0644 "$PROFILE_DST"
apparmor_parser -r "$PROFILE_DST"

# Verify the kernel actually has it (root can read the securityfs profile list).
if ! grep -q "^${PROFILE_NAME} " /sys/kernel/security/apparmor/profiles 2>/dev/null; then
    echo "setup-apparmor-userns: profile installed but '${PROFILE_NAME}' is not in" >&2
    echo "  /sys/kernel/security/apparmor/profiles — is AppArmor enabled on this kernel?" >&2
    exit 1
fi

echo "setup-apparmor-userns: profile '${PROFILE_NAME}' installed and loaded."
echo "  • persists across reboots ($PROFILE_DST is loaded by apparmor.service)"
echo "  • the system-wide userns restriction remains ENABLED (as it should be)"
echo "  • scripts/compose.sh now selects it automatically; for a standalone"
echo "    'docker compose' run, set OTODOCK_APPARMOR_PROFILE=otodock_userns in .env"
