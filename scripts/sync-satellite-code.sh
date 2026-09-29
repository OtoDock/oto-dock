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
#   - proxy/core/layers/codex/app_server_client.py → satellite/_vendored/app_server_client.py
#   - proxy/core/layers/codex/codex_approvals.py   → satellite/_vendored/codex_approvals.py
#   - proxy/core/stdio_path_interceptor.py         → satellite/_vendored/stdio_path_interceptor.py
#   - proxy/core/terminal_queries.py               → satellite/_vendored/terminal_queries.py
#   - proxy/core/layout.py                         → satellite/_vendored/layout.py
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
    "proxy/core/layers/codex/app_server_client.py|satellite/_vendored/app_server_client.py|SHARED_APP_SERVER_CLIENT_HASH"
    "proxy/core/layers/codex/codex_approvals.py|satellite/_vendored/codex_approvals.py|SHARED_CODEX_APPROVALS_HASH"
    "proxy/core/stdio_path_interceptor.py|satellite/_vendored/stdio_path_interceptor.py|SHARED_STDIO_INTERCEPTOR_HASH"
    "proxy/core/terminal_queries.py|satellite/_vendored/terminal_queries.py|SHARED_TERMINAL_QUERIES_HASH"
    "proxy/core/layout.py|satellite/_vendored/layout.py|SHARED_LAYOUT_HASH"
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
