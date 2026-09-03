"""Unit tests for the authenticated browser-control MCP.

DB-free by design (mirrors test_device_local_mcps.py): the manifest is parsed
straight off disk, the framework gate is exercised against it, and the per-agent
allowed-origins injection helper is pure. The proxy needs ZERO new framework
code to attach this MCP — only the origin-injection plumbing is new, and that's
what we assert here.
"""

import pytest

from services.mcp import mcp_registry as reg

from tests._paths import REPO_ROOT as _ROOT
_MANIFEST = _ROOT / "mcps" / "custom" / "browser-mcp" / "manifest.json"


@pytest.fixture()
def manifest():
    m = reg._parse_manifest(_MANIFEST)
    assert m is not None, "browser-control manifest failed to parse"
    return m


# ---------------------------------------------------------------------------
# Manifest shape
# ---------------------------------------------------------------------------

def test_manifest_device_fields(manifest):
    assert manifest.name == "browser-control"
    assert manifest.server_name == "local"  # tools -> mcp__local__*
    assert manifest.placement == "satellite_only"
    assert manifest.device_capability == "browser"
    assert manifest.requires_display is True
    assert manifest.assignment_mode == "auto"  # device MCPs are auto, not explicit
    assert "phone" not in manifest.exclude_from  # allowed on calls; gated by device placement + grants


def test_manifest_default_blocklist_is_valid_loopback(manifest):
    blocked = manifest.env.get("PLAYWRIGHT_MCP_BLOCKED_ORIGINS", "")
    assert blocked, "a default blocked-origins must ship"
    entries = blocked.split(";")
    assert len(entries) >= 2
    # Every entry is a well-formed scheme://host[:port] (NOT a scheme-only
    # 'chrome://*' which @playwright/mcp would not treat as a network origin).
    for e in entries:
        assert "://" in e, f"malformed origin {e!r}"
        assert "localhost" in e or "127.0.0.1" in e, f"default blocklist is loopback-only, got {e!r}"


def test_manifest_node_stdio_source(manifest):
    assert manifest.server.runtime == "node"
    assert manifest.server.transport == "stdio"
    assert manifest.server.command == "node"
    assert manifest.server.args == ["index.js"]
    assert manifest.server.source.startswith("npm:@playwright/mcp@")


# ---------------------------------------------------------------------------
# Framework gate (the predicate) — unchanged, exercised for "browser"
# ---------------------------------------------------------------------------

def _reason(m, *, r=False, d=None, g=None):
    return reg._device_placement_reason(
        m, is_remote=r, target_has_display=d, target_device_grants=g or set()
    )


def test_gate_local_excluded(manifest):
    assert "remote machine" in (_reason(manifest) or "")


def test_gate_remote_requires_browser_grant(manifest):
    assert "not granted" in (_reason(manifest, r=True) or "")            # no grants
    assert "not granted" in (_reason(manifest, r=True, g={"computer"}) or "")  # wrong cap


def test_gate_attaches_when_granted_with_display(manifest):
    assert _reason(manifest, r=True, d=True, g={"browser"}) is None
    assert _reason(manifest, r=True, d=None, g={"browser"}) is None  # unknown display != exclude


def test_gate_excluded_when_known_headless(manifest):
    assert "no interactive display" in (_reason(manifest, r=True, d=False, g={"browser"}) or "")


def test_device_capability_for_server(manifest):
    reg._manifests[manifest.name] = manifest
    try:
        assert reg.device_capability_for_server("local") == "browser"
    finally:
        reg._manifests.pop(manifest.name, None)


# ---------------------------------------------------------------------------
# resolve_server_config — node stdio entry + env merge
# ---------------------------------------------------------------------------

def test_resolve_server_config_node_stdio(manifest, monkeypatch):
    monkeypatch.setattr(reg.mcp_store, "get_mcp_config_values", lambda n: {"OTO_BROWSER_CHANNEL": "chrome"})
    for fmt in ("json", "toml"):
        e = reg.resolve_server_config(manifest, "sales", mcp_config_format=fmt, session_ctx={})
        assert e["type"] == "stdio"
        assert e["command"] == "node"
        assert e["args"] == [str(_MANIFEST.parent / "index.js")]  # bare index.js -> mcp_dir
        # manifest env (default blocklist) + DB config both reach the process
        assert e["env"]["PLAYWRIGHT_MCP_BLOCKED_ORIGINS"].startswith("http://localhost")
        assert e["env"]["OTO_BROWSER_CHANNEL"] == "chrome"


# ---------------------------------------------------------------------------
# Per-agent allowed-origins injection (the only net-new framework plumbing)
# ---------------------------------------------------------------------------

def test_apply_allowed_origins_sets_semicolon_joined_env():
    entry = {"type": "stdio", "command": "node", "env": {"OTO_BROWSER_CHANNEL": "chrome"}}
    reg._apply_browser_allowed_origins(entry, ["https://a.com", "https://b.com:8443"])
    assert entry["env"]["PLAYWRIGHT_MCP_ALLOWED_ORIGINS"] == "https://a.com;https://b.com:8443"
    # existing env preserved
    assert entry["env"]["OTO_BROWSER_CHANNEL"] == "chrome"


def test_apply_allowed_origins_empty_is_noop():
    entry = {"type": "stdio"}
    reg._apply_browser_allowed_origins(entry, [])
    assert "env" not in entry  # empty list = permissive default (blocklist only)


def test_apply_allowed_origins_creates_env_when_absent():
    entry = {"type": "stdio"}
    reg._apply_browser_allowed_origins(entry, ["https://only.com"])
    assert entry["env"] == {"PLAYWRIGHT_MCP_ALLOWED_ORIGINS": "https://only.com"}


_LOOPBACK_BLOCKLIST = (
    "http://localhost:*;https://localhost:*;http://127.0.0.1:*;https://127.0.0.1:*"
)


def test_allow_listed_loopback_host_unblocks_it():
    """@playwright/mcp deny-wins across the two lists, so an explicit allow for
    localhost must SUBTRACT the manifest's localhost block patterns — while the
    allowlist mode keeps everything outside the allow-list unreachable."""
    entry = {"type": "stdio",
             "env": {"PLAYWRIGHT_MCP_BLOCKED_ORIGINS": _LOOPBACK_BLOCKLIST}}
    reg._apply_browser_allowed_origins(entry, ["http://localhost:8400"])
    assert entry["env"]["PLAYWRIGHT_MCP_ALLOWED_ORIGINS"] == "http://localhost:8400"
    # localhost patterns dropped (both schemes — host-keyed), 127.0.0.1 kept.
    assert entry["env"]["PLAYWRIGHT_MCP_BLOCKED_ORIGINS"] == (
        "http://127.0.0.1:*;https://127.0.0.1:*"
    )


def test_non_loopback_allow_list_keeps_blocklist_intact():
    entry = {"type": "stdio",
             "env": {"PLAYWRIGHT_MCP_BLOCKED_ORIGINS": _LOOPBACK_BLOCKLIST}}
    reg._apply_browser_allowed_origins(entry, ["https://example.com"])
    assert entry["env"]["PLAYWRIGHT_MCP_BLOCKED_ORIGINS"] == _LOOPBACK_BLOCKLIST


def test_empty_allow_list_never_touches_blocklist():
    entry = {"type": "stdio",
             "env": {"PLAYWRIGHT_MCP_BLOCKED_ORIGINS": _LOOPBACK_BLOCKLIST}}
    reg._apply_browser_allowed_origins(entry, [])
    assert entry["env"]["PLAYWRIGHT_MCP_BLOCKED_ORIGINS"] == _LOOPBACK_BLOCKLIST
    assert "PLAYWRIGHT_MCP_ALLOWED_ORIGINS" not in entry["env"]


def test_origin_host_parsing():
    assert reg._origin_host("http://localhost:*") == "localhost"
    assert reg._origin_host("https://127.0.0.1:8443/path") == "127.0.0.1"
    assert reg._origin_host("HTTP://LocalHost:8400") == "localhost"
    assert reg._origin_host("http://[::1]:8400") == "[::1]"
    assert reg._origin_host("example.com:443") == "example.com"
