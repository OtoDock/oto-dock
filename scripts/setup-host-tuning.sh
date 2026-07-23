#!/usr/bin/env bash
#
# OtoDock — one-time host tuning: keep the platform out of swap.
#
# Linux defaults to vm.swappiness=60, which lets the kernel push idle
# anonymous memory (the OtoDock server's worker processes) into swap while
# plenty of droppable page cache exists. A short memory burst from a
# sandboxed tool container can then leave the server's pages parked in swap,
# and the next request that touches them stalls the whole dashboard for as
# long as the page-in takes (observed live: one-minute freezes minutes AFTER
# the burst had already ended). vm.swappiness=10 keeps reclaim preferring
# page cache over process memory, which is the right trade for a host whose
# main job is serving the platform.
#
# What it does — nothing else on the host is touched:
#   * writes /etc/sysctl.d/90-otodock.conf with vm.swappiness=10
#   * applies it immediately (no reboot needed)
#
#   sudo scripts/setup-host-tuning.sh
#
# Self-contained (no other repo files needed), so the pull-only compose flow
# can fetch and run just this file. Idempotent, and safe on any host:
#   * non-Linux hosts (Docker Desktop on macOS/Windows) exit 0 untouched;
#   * a host already at swappiness <= 10 is left exactly as it is;
#   * to override later, add a higher-sorted drop-in (e.g.
#     /etc/sysctl.d/99-local.conf) or delete the file — standard sysctl
#     precedence applies, nothing here fights it.
set -euo pipefail

say()  { echo "setup-host-tuning: $*"; }
fail() { echo "setup-host-tuning: $*" >&2; exit 1; }

_knob="/proc/sys/vm/swappiness"
_conf="/etc/sysctl.d/90-otodock.conf"

if [ ! -r "$_knob" ]; then
    say "no $_knob on this host (not Linux?) — nothing to tune, done."
    exit 0
fi

_current="$(cat "$_knob")"
if [ "$_current" -le 10 ] && [ ! -f "$_conf" ]; then
    say "vm.swappiness is already $_current (<= 10) — leaving the host as it is."
    exit 0
fi

[ "$(id -u)" -eq 0 ] || fail "needs root for /etc/sysctl.d — run: sudo $0"

say "writing $_conf (vm.swappiness=10; was $_current)"
cat > "$_conf" <<'EOF'
# Installed by OtoDock (scripts/setup-host-tuning.sh).
#
# Prefer dropping page cache over swapping process memory: a memory burst
# from a sandboxed tool container must not park the OtoDock server's worker
# processes in swap — every later request that touches those pages stalls
# until they are read back.
#
# To override: add a higher-sorted drop-in (e.g. 99-local.conf) or delete
# this file. Deleting it does not revert the running value until reboot or
# `sysctl vm.swappiness=<n>`.
vm.swappiness = 10
EOF

say "applying now (persists across reboots via sysctl.d)"
sysctl -q -p "$_conf"
say "done — vm.swappiness=$(cat "$_knob")"
