#!/usr/bin/env bash
# Vendor shared proxy modules into the satellite tree.
#
# The satellite must run standalone (Python 3.10+ with no proxy imports),
# so we copy-in rather than symlink. Each destination file is byte-identical
# to its source. After copying, we compute a sha256 of the shared module and
# write it into the matching `satellite/config.py::SHARED_*_HASH` constant so
# the satellite detects drift at import time (see satellite/__main__.py).
#
# Shared modules (edit the source, then run this script):
#   - proxy/services/mcp/mcp_installer.py          → satellite/_vendored/mcp_installer.py
#   - proxy/services/mcp/mcp_system_deps.py        → satellite/_vendored/mcp_system_deps.py
#     (the installer's system requirements, imported beside it)
#   - proxy/services/mcp/mcp_pin.py                → satellite/_vendored/mcp_pin.py
#     (what a build leaves in a tool's hashed files: the platform and the
#     machine that built both write it, so both hash the same folder)
#   - proxy/core/layers/codex/app_server_client.py → satellite/_vendored/app_server_client.py
#   - proxy/core/layers/codex/session_text.py      → satellite/_vendored/session_text.py
#   - proxy/core/layers/codex/codex_approvals.py   → satellite/_vendored/codex_approvals.py
#   - proxy/core/stdio_path_interceptor.py         → satellite/_vendored/stdio_path_interceptor.py
#   - proxy/core/terminal_queries.py               → satellite/_vendored/terminal_queries.py
#   - proxy/core/layout.py                         → satellite/_vendored/layout.py
#   - proxy/core/loop_watchdog.py                  → satellite/_vendored/loop_watchdog.py
#   - proxy/core/sandbox/sandbox_render.py         → satellite/_vendored/sandbox_render.py
#   - proxy/core/sandbox/host_resources.py         → satellite/_vendored/host_resources.py
#   - proxy/core/sandbox/session_home.py           → satellite/_vendored/session_home.py
#     (a session's own engine folder: a paired machine makes it as the platform does)
#   - proxy/scripts/oto-sandbox-net                → satellite/_vendored/oto-sandbox-net
#     (the sandbox render and the launcher it names: a compute machine renders
#     a session's sandbox with them; the launcher keeps its execute bit)
#   - proxy/core/credentials/exit_relay.py         → satellite/_vendored/exit_relay.py
#   - proxy/core/credentials/exit_authority.py     → satellite/_vendored/exit_authority.py
#   - proxy/services/infra/outbound_url.py         → satellite/_vendored/outbound_url.py
#     (the credential exit a machine's satellite runs: the relay, its authority
#     and the sandbox shim's address rule; hashed by file at boot, since the
#     relay imports packages a venv may lack)
#
# NOT vendored: the host OS table (proxy/core/host_os.py). satellite/config.py is
# the boot guard's leaf — __main__ imports it before the rollback runs, so it may
# import nothing but the standard library and the stdlib-only sibling leaf
# engines.py, and a broken vendored copy would raise first. The table is defined
# in both files as a TWIN (the gate's twin rule pins
# _table / family_of / of; proxy/tests/core/test_host_os.py compares every row).
# Edit the proxy module first, then copy the block into satellite/config.py verbatim.
#
# Usage: ./scripts/sync-satellite-code.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cfg="$ROOT/satellite/config.py"

# Each entry: "<src-rel>|<dst-rel>|<hash-const-name>"
ENTRIES=(
    "proxy/services/mcp/mcp_installer.py|satellite/_vendored/mcp_installer.py|SHARED_MCP_INSTALLER_HASH"
    "proxy/services/mcp/mcp_system_deps.py|satellite/_vendored/mcp_system_deps.py|SHARED_MCP_SYSTEM_DEPS_HASH"
    "proxy/services/mcp/mcp_pin.py|satellite/_vendored/mcp_pin.py|SHARED_MCP_PIN_HASH"
    "proxy/core/layers/codex/app_server_client.py|satellite/_vendored/app_server_client.py|SHARED_APP_SERVER_CLIENT_HASH"
    "proxy/core/layers/codex/codex_approvals.py|satellite/_vendored/codex_approvals.py|SHARED_CODEX_APPROVALS_HASH"
    "proxy/core/layers/codex/session_text.py|satellite/_vendored/session_text.py|SHARED_SESSION_TEXT_HASH"
    "proxy/core/stdio_path_interceptor.py|satellite/_vendored/stdio_path_interceptor.py|SHARED_STDIO_INTERCEPTOR_HASH"
    "proxy/core/terminal_queries.py|satellite/_vendored/terminal_queries.py|SHARED_TERMINAL_QUERIES_HASH"
    "proxy/core/layout.py|satellite/_vendored/layout.py|SHARED_LAYOUT_HASH"
    "proxy/core/loop_watchdog.py|satellite/_vendored/loop_watchdog.py|SHARED_LOOP_WATCHDOG_HASH"
    "proxy/core/sandbox/sandbox_render.py|satellite/_vendored/sandbox_render.py|SHARED_SANDBOX_RENDER_HASH"
    "proxy/core/sandbox/host_resources.py|satellite/_vendored/host_resources.py|SHARED_HOST_RESOURCES_HASH"
    "proxy/core/sandbox/session_home.py|satellite/_vendored/session_home.py|SHARED_SESSION_HOME_HASH"
    "proxy/scripts/oto-sandbox-net|satellite/_vendored/oto-sandbox-net|SHARED_SANDBOX_NET_HASH"
    "proxy/core/credentials/exit_relay.py|satellite/_vendored/exit_relay.py|SHARED_EXIT_RELAY_HASH"
    "proxy/core/credentials/exit_authority.py|satellite/_vendored/exit_authority.py|SHARED_EXIT_AUTHORITY_HASH"
    "proxy/services/infra/outbound_url.py|satellite/_vendored/outbound_url.py|SHARED_OUTBOUND_URL_HASH"
)

for entry in "${ENTRIES[@]}"; do
    IFS='|' read -r src_rel dst_rel hash_var <<< "$entry"
    src="$ROOT/$src_rel"
    dst="$ROOT/$dst_rel"
    if [[ ! -f "$src" ]]; then
        echo "error: source not found: $src" >&2
        exit 1
    fi
    # Copy verbatim — the satellite-side file must be a bit-for-bit copy so the
    # hash check passes. Any divergence must go through this script.
    cp "$src" "$dst"
    hash="$(sha256sum "$src" | cut -d' ' -f1)"
    echo "$hash_var: $hash  ($dst_rel)"

    HASH_VAR="$hash_var" HASH_VAL="$hash" CFG="$cfg" python3 - <<'PY'
import os, re
from pathlib import Path

cfg = Path(os.environ["CFG"])
var = os.environ["HASH_VAR"]
val = os.environ["HASH_VAL"]
src = cfg.read_text()
new_line = f'{var} = "{val}"'
if re.search(rf'^{var}\s*=', src, flags=re.M):
    out = re.sub(rf'^{var}\s*=.*$', new_line, src, count=1, flags=re.M)
else:
    # Append after the SATELLITE_VERSION line.
    out = re.sub(
        r'^(SATELLITE_VERSION\s*=.*$)',
        lambda m: m.group(1) + '\n' + new_line,
        src, count=1, flags=re.M,
    )
cfg.write_text(out)
print(f"  updated {cfg.name}: {var}")
PY
done

echo "done — restart the proxy so satellites auto-update to the new vendored code"
