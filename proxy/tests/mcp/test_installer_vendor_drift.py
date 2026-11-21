"""Test that every vendored satellite module stays in sync with proxy.

If a developer edits a proxy-side shared module without running
scripts/sync-satellite-code.sh, these tests fail — preventing silently
divergent behavior between platform and satellite. The satellite only
checks its own copy against its own baked hash at startup, so this test
is the ONLY guard that catches a forgotten sync on the proxy side.

The pair list mirrors scripts/sync-satellite-code.sh (keep both in sync).
"""

import hashlib
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT as _ROOT

_SAT_CONFIG = _ROOT / "satellite" / "config.py"

# (proxy source, vendored copy, hash constant in satellite/config.py)
_VENDORED = [
    ("proxy/services/mcp/mcp_installer.py",
     "satellite/_vendored/mcp_installer.py", "SHARED_MCP_INSTALLER_HASH"),
    ("proxy/services/mcp/mcp_system_deps.py",
     "satellite/_vendored/mcp_system_deps.py", "SHARED_MCP_SYSTEM_DEPS_HASH"),
    ("proxy/services/mcp/mcp_pin.py",
     "satellite/_vendored/mcp_pin.py", "SHARED_MCP_PIN_HASH"),
    ("proxy/core/layers/codex/app_server_client.py",
     "satellite/_vendored/app_server_client.py", "SHARED_APP_SERVER_CLIENT_HASH"),
    ("proxy/core/layers/codex/codex_approvals.py",
     "satellite/_vendored/codex_approvals.py", "SHARED_CODEX_APPROVALS_HASH"),
    ("proxy/core/layers/codex/session_text.py",
     "satellite/_vendored/session_text.py", "SHARED_SESSION_TEXT_HASH"),
    ("proxy/core/stdio_path_interceptor.py",
     "satellite/_vendored/stdio_path_interceptor.py", "SHARED_STDIO_INTERCEPTOR_HASH"),
    ("proxy/core/terminal_queries.py",
     "satellite/_vendored/terminal_queries.py", "SHARED_TERMINAL_QUERIES_HASH"),
    ("proxy/core/layout.py",
     "satellite/_vendored/layout.py", "SHARED_LAYOUT_HASH"),
    ("proxy/core/loop_watchdog.py",
     "satellite/_vendored/loop_watchdog.py", "SHARED_LOOP_WATCHDOG_HASH"),
    ("proxy/core/sandbox/sandbox_render.py",
     "satellite/_vendored/sandbox_render.py", "SHARED_SANDBOX_RENDER_HASH"),
    ("proxy/core/sandbox/session_home.py",
     "satellite/_vendored/session_home.py", "SHARED_SESSION_HOME_HASH"),
    ("proxy/core/sandbox/host_resources.py",
     "satellite/_vendored/host_resources.py", "SHARED_HOST_RESOURCES_HASH"),
    ("proxy/scripts/oto-sandbox-net",
     "satellite/_vendored/oto-sandbox-net", "SHARED_SANDBOX_NET_HASH"),
    ("proxy/core/credentials/exit_relay.py",
     "satellite/_vendored/exit_relay.py", "SHARED_EXIT_RELAY_HASH"),
    ("proxy/core/credentials/exit_authority.py",
     "satellite/_vendored/exit_authority.py", "SHARED_EXIT_AUTHORITY_HASH"),
    ("proxy/services/infra/outbound_url.py",
     "satellite/_vendored/outbound_url.py", "SHARED_OUTBOUND_URL_HASH"),
]

_IDS = [src.rsplit("/", 1)[-1] for src, _, _ in _VENDORED]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("src,vendored,hash_const", _VENDORED, ids=_IDS)
def test_vendored_satellite_copy_is_identical(src, vendored, hash_const):
    """Byte-for-byte identical vendored copy."""
    proxy_file = _ROOT / src
    sat_file = _ROOT / vendored
    assert proxy_file.is_file(), f"missing: {proxy_file}"
    assert sat_file.is_file(), f"missing: {sat_file}"
    assert _sha256(proxy_file) == _sha256(sat_file), (
        f"{vendored} differs from {src} — run scripts/sync-satellite-code.sh"
    )


def test_the_vendored_launcher_keeps_its_execute_bit():
    """The satellite runs the launcher by its path, as the proxy does."""
    import os
    assert os.access(_ROOT / "satellite/_vendored/oto-sandbox-net", os.X_OK)


@pytest.mark.parametrize("src,vendored,hash_const", _VENDORED, ids=_IDS)
def test_satellite_config_hash_matches(src, vendored, hash_const):
    """The hash constant in satellite/config.py equals the sha256 of the
    proxy-side source (authoritative)."""
    proxy_hash = _sha256(_ROOT / src)
    cfg_src = _SAT_CONFIG.read_text()
    assert f'{hash_const} = "{proxy_hash}"' in cfg_src, (
        f"satellite/config.py::{hash_const} is stale — "
        "run scripts/sync-satellite-code.sh"
    )
