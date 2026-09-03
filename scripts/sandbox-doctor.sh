#!/usr/bin/env bash
#
# OtoDock sandbox doctor — read-only diagnosis of the local agent sandbox.
#
# Answers "why can't sessions — or the Codex engine's own INNER sandbox —
# create their namespaces on this host?" by collecting the host facts that
# gate namespace creation and then running the exact probe ladder the
# sandbox stack depends on:
#
#   P1  unshare -Urn                      (the proxy boot preflight's gate)
#   P2  one-level bwrap, our exact flags  (what every agent session runs in)
#   P3  bwrap nested inside bwrap         (what the Codex engine's
#                                          "fs sandbox helper" needs)
#   P4  Codex's npm-vendored bwrap, one level and nested (when found) —
#       distro AppArmor profiles cover /usr/bin/bwrap by path, NOT this
#       binary, so on userns-restricted hosts (Ubuntu 23.10+) P4 is the
#       probe that reproduces "the local sandbox could not start".
#
# Where to run it:
#   bare-metal (T1) install : scripts/sandbox-doctor.sh      (as the proxy user)
#   containerised (T2/T3)   : docker exec -it otodock-proxy \
#                                 /app/scripts/sandbox-doctor.sh
#     (older images without the script: `docker cp` this file in first)
#
# Read-only: writes nothing, needs no root. When reporting a sandbox issue,
# send everything between the COPY markers.
set -u

BOLD=""; RESET=""
if [ -t 1 ]; then BOLD=$'\033[1m'; RESET=$'\033[0m'; fi

REPORT=""
say() { printf '%s\n' "$1"; REPORT="${REPORT}${1}
"; }

section() { say ""; say "${BOLD}== $1 ==${RESET}"; }

fact() { say "  $1: $2"; }

read_sysctl() { cat "$1" 2>/dev/null | tr -d '[:space:]' || true; }

PASS_N=0; FAIL_N=0; SKIP_N=0
probe() {  # probe <label> <cmd...>
  local label="$1"; shift
  local err rc
  err=$("$@" 2>&1 >/dev/null); rc=$?
  if [ "$rc" -eq 0 ]; then
    say "  [PASS] $label"
    PASS_N=$((PASS_N + 1))
  else
    say "  [FAIL] $label (exit $rc): ${err:-<no stderr>}"
    FAIL_N=$((FAIL_N + 1))
  fi
  return $rc
}
skip() { say "  [SKIP] $1"; SKIP_N=$((SKIP_N + 1)); }

say "OtoDock sandbox doctor ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
say "=== COPY FROM HERE ==="

section "Host facts"
fact "os" "$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}" || echo unknown)"
fact "kernel" "$(uname -r 2>/dev/null || echo unknown)"
fact "uid" "$(id -u) ($(id -un 2>/dev/null || true))"
if [ -f /.dockerenv ] || grep -qs 'docker\|containerd\|kubepods' /proc/1/cgroup 2>/dev/null; then
  fact "environment" "container (run this INSIDE the proxy container for T2/T3 installs)"
else
  fact "environment" "bare metal / VM"
fi
fact "apparmor_restrict_unprivileged_userns" \
  "$(read_sysctl /proc/sys/kernel/apparmor_restrict_unprivileged_userns || echo '<absent>')"
fact "unprivileged_userns_clone" \
  "$(read_sysctl /proc/sys/kernel/unprivileged_userns_clone || echo '<absent>')"
fact "max_user_namespaces" \
  "$(read_sysctl /proc/sys/user/max_user_namespaces || echo '<absent>')"
fact "current apparmor confinement" \
  "$(cat /proc/self/attr/apparmor/current 2>/dev/null || cat /proc/self/attr/current 2>/dev/null || echo '<unreadable>')"
fact "otodock_userns profile file" \
  "$([ -e /etc/apparmor.d/otodock-userns ] && echo present || echo absent)"
fact "distro bwrap/unshare profiles" \
  "$(ls /etc/apparmor.d/bwrap* /etc/apparmor.d/*unshare* 2>/dev/null | tr '\n' ' ' | sed 's/ $//' || true)"
fact "bwrap" "$(command -v bwrap >/dev/null && bwrap --version 2>/dev/null || echo '<missing>')"
fact "pasta" "$(command -v pasta >/dev/null && pasta --version 2>/dev/null | head -1 || echo '<missing>')"
fact "unshare" "$(command -v unshare >/dev/null && unshare --version 2>/dev/null || echo '<missing>')"

# Codex's vendored bubblewrap: shipped inside the npm package, next to the
# native binary (…/@openai/codex-linux-*/vendor/*/codex-resources/bwrap).
VENDORED=""
CODEX_BIN="$(command -v codex 2>/dev/null || true)"
_ROOTS=""
[ -n "$CODEX_BIN" ] && _ROOTS="$(dirname "$(readlink -f "$CODEX_BIN")")/../node_modules"
_NPM_ROOT="$(npm root -g 2>/dev/null || true)"
[ -n "$_NPM_ROOT" ] && _ROOTS="$_ROOTS $_NPM_ROOT"
for root in $_ROOTS; do
  [ -d "$root/@openai" ] || continue
  VENDORED="$(find "$root/@openai" -maxdepth 6 -type f -name bwrap \
                -path '*codex-resources*' 2>/dev/null | head -1)"
  [ -n "$VENDORED" ] && break
done
fact "codex" "${CODEX_BIN:-<missing>}"
fact "codex vendored bwrap" "${VENDORED:-<not found>}"

section "Probes"
UIDFLAGS=""
if [ "$(id -u)" != "0" ]; then
  UIDFLAGS="--unshare-user --uid $(id -u) --gid $(id -g)"
fi

P1=1; P2=1; P3=1; P4A=1; P4B=1
if command -v unshare >/dev/null; then
  probe "P1 unshare -Urn (user+net namespace)" unshare -Urn true; P1=$?
else
  skip "P1 unshare missing (util-linux not installed)"
fi

if command -v bwrap >/dev/null; then
  # shellcheck disable=SC2086  # UIDFLAGS is a deliberate word-split flag list
  probe "P2 one-level bwrap (session sandbox flags)" \
    bwrap --unshare-pid --die-with-parent $UIDFLAGS \
          --ro-bind / / --dev /dev --proc /proc -- /bin/true; P2=$?
  # shellcheck disable=SC2086
  probe "P3 nested bwrap (system bwrap inside system bwrap)" \
    bwrap --unshare-pid --die-with-parent $UIDFLAGS \
          --ro-bind / / --dev /dev --proc /proc -- \
          bwrap --ro-bind / / -- /bin/true; P3=$?
else
  skip "P2/P3 bwrap missing"
fi

if [ -n "$VENDORED" ]; then
  probe "P4a codex vendored bwrap, one level" \
    "$VENDORED" --ro-bind / / -- /bin/true; P4A=$?
  # shellcheck disable=SC2086
  probe "P4b codex vendored bwrap nested inside system bwrap" \
    bwrap --unshare-pid --die-with-parent $UIDFLAGS \
          --ro-bind / / --dev /dev --proc /proc -- \
          "$VENDORED" --ro-bind / / -- /bin/true; P4B=$?
else
  skip "P4 codex vendored bwrap not found (codex not installed here?)"
fi

section "Reading"
APPARMOR_ON="$(read_sysctl /proc/sys/kernel/apparmor_restrict_unprivileged_userns)"
if [ "$P2" -ne 0 ]; then
  say "  The SESSION sandbox itself cannot start here — the proxy boot"
  say "  preflight should already be failing loudly. Fix that first"
  say "  (see the preflight error; Ubuntu 24.04+: setup-apparmor-userns.sh)."
elif [ "$P3" -ne 0 ] || { [ -n "$VENDORED" ] && [ "$P4B" -ne 0 ]; }; then
  say "  Sessions run, but a bwrap NESTED inside the sandbox fails — exactly"
  say "  what the Codex engine's inner sandbox ('fs sandbox helper') needs."
  say "  Codex commands will degrade to unsandboxed retries behind permission"
  say "  prompts; Claude sessions are unaffected."
  if [ "$APPARMOR_ON" = "1" ]; then
    say "  Likely cause on this host: kernel.apparmor_restrict_unprivileged_userns=1"
    say "  (Ubuntu 23.10+) — profiles cover the outer level but not the inner"
    say "  binary/level. Send this report to the OtoDock maintainers."
  fi
elif [ -n "$VENDORED" ] && [ "$P4A" -ne 0 ]; then
  say "  The codex vendored bwrap is denied even at one level (system bwrap"
  say "  works) — a per-binary AppArmor/profile asymmetry on this host."
else
  say "  All probes pass — this host's namespace stack is healthy. If an"
  say "  agent still reports sandbox failures, capture the proxy log lines"
  say "  tagged [stderr][sandbox] and send them with this report."
fi

say ""
say "Summary: ${PASS_N} pass, ${FAIL_N} fail, ${SKIP_N} skipped"
say "=== COPY TO HERE ==="
[ "$FAIL_N" -eq 0 ]
