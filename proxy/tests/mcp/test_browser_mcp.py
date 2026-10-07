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
from core import placement
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


def test_manifest_ships_no_origin_blocklist(manifest):
    """Restricting the Local Browser will not be built (F46, the operator,
    2026-09-30): whatever the machine reaches, the agent reaches, loopback
    included. The per-agent allow-list stays the admin's optional limit."""
    assert "PLAYWRIGHT_MCP_BLOCKED_ORIGINS" not in manifest.env


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
    return reg._device_placement_reason(m, placement=placement.PlacementCapabilities(
        kind=placement.KIND_ADMIN_REMOTE if r else placement.KIND_LOCAL,
        machine_id="m" if r else "", has_display=d, device_grants=g or set(),
    ))


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
        # the DB config reaches the process; no origin is blocked by default
        assert "PLAYWRIGHT_MCP_BLOCKED_ORIGINS" not in e["env"]
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


# ---------------------------------------------------------------------------
# Own-browser mode: the framework owns the browser env keys on the entry
# ---------------------------------------------------------------------------

def _own(token=None, *, unattended=False):
    from storage.remote_store import BrowserTargetSettings
    return BrowserTargetSettings(mode="own", extension_token=token, unattended=unattended)


def test_apply_browser_mode_dedicated_leaves_nothing_but_strips_poison():
    # Admin config values merge into the env unvalidated — every framework-
    # owned key is dropped whatever the mode, so a DB value can never flip a
    # machine into the user's real browser or repoint the profile dir.
    from storage.remote_store import BrowserTargetSettings
    poisoned = {
        "OTO_BROWSER_CHANNEL": "chrome", "OTO_BROWSER_MODE": "own",
        "PLAYWRIGHT_MCP_EXTENSION": "1", "PLAYWRIGHT_MCP_EXTENSION_TOKEN": "x",
        "PLAYWRIGHT_MCP_USER_DATA_DIR": "/home/u/.config/google-chrome",
        "PLAYWRIGHT_MCP_BROWSER": "chrome", "PLAYWRIGHT_MCP_EXECUTABLE_PATH": "/x",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT": "http://127.0.0.1:9222",
    }
    bundles = {}
    for target in (None, BrowserTargetSettings()):
        entry = {"type": "stdio", "command": "node", "env": dict(poisoned)}
        reg._apply_browser_mode(entry, bundles, target, unattended=False)
        assert entry["env"] == {"OTO_BROWSER_CHANNEL": "chrome"}
        assert bundles == {}


def test_apply_browser_mode_own_with_token_uses_the_bundle():
    # An ATTENDED session uses the saved token.
    entry = {"type": "stdio", "command": "node", "env": {"OTO_BROWSER_CHANNEL": "auto"}}
    bundles = {}
    reg._apply_browser_mode(entry, bundles, _own("T" * 43), unattended=False)
    assert entry["env"] == {
        "OTO_BROWSER_CHANNEL": "auto", "OTO_BROWSER_MODE": "own",
        "OTO_BROWSER_TOKEN_EXPECTED": "1",
    }
    # The token rides the broker bundle only — never the config env.
    assert bundles["local"].env == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "T" * 43}
    assert "PLAYWRIGHT_MCP_EXTENSION_TOKEN" not in str(entry)


def test_apply_browser_mode_unattended_needs_the_machine_optin_for_the_token():
    # F47: an unattended session (task / call / meeting) does NOT spend an
    # own-browser token left for a person unless the machine row opts in.
    entry = {"type": "stdio", "command": "node"}
    bundles = {}
    reg._apply_browser_mode(entry, bundles, _own("T" * 43, unattended=False), unattended=True)
    assert entry["env"] == {"OTO_BROWSER_MODE": "own", "OTO_BROWSER_UNATTENDED": "1"}
    assert bundles == {}
    # With the opt-in, the unattended session uses it.
    entry, bundles = {"type": "stdio", "command": "node"}, {}
    reg._apply_browser_mode(entry, bundles, _own("T" * 43, unattended=True), unattended=True)
    assert entry["env"] == {"OTO_BROWSER_MODE": "own", "OTO_BROWSER_TOKEN_EXPECTED": "1"}
    assert bundles["local"].env == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "T" * 43}


def test_apply_browser_mode_own_without_token_flags_unattended():
    entry = {"type": "stdio", "command": "node"}
    bundles = {}
    reg._apply_browser_mode(entry, bundles, _own(), unattended=False)
    assert entry["env"] == {"OTO_BROWSER_MODE": "own"}
    reg._apply_browser_mode(entry, bundles, _own(), unattended=True)
    assert entry["env"] == {"OTO_BROWSER_MODE": "own", "OTO_BROWSER_UNATTENDED": "1"}
    assert bundles == {}


def test_apply_browser_mode_extends_an_existing_bundle():
    from core.credentials.mcp_broker import SecretBundle
    bundles = {"local": SecretBundle(env={"OTHER": "1"})}
    entry = {"type": "stdio", "command": "node"}
    reg._apply_browser_mode(entry, bundles, _own("U" * 43), unattended=False)
    assert bundles["local"].env == {"OTHER": "1", "PLAYWRIGHT_MCP_EXTENSION_TOKEN": "U" * 43}


def test_build_session_config_threads_target_browser(monkeypatch, tmp_path):
    import json
    from tests.mcp.test_mcp_broker_activation import _FakeManifest, _stub_assembly
    _stub_assembly(
        monkeypatch, [_FakeManifest("browser-control", server_name="local")],
        env_by_mcp={}, tmp_path=tmp_path,
        server_entries={"browser-control": {
            "type": "stdio", "command": "node", "args": ["index.js"],
            "env": {"PLAYWRIGHT_MCP_BLOCKED_ORIGINS": "http://localhost:*",
                    "PLAYWRIGHT_MCP_EXTENSION": "1"},
        }},
    )
    path, _env, _excl, bundles, _bash = reg.build_session_mcp_config(
        "agent", None, placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m"),
        task_mode=True, target_browser=_own("V" * 43, unattended=True),
    )
    local = json.loads(path.read_text())["mcpServers"]["local"]
    assert local["env"] == {
        "PLAYWRIGHT_MCP_BLOCKED_ORIGINS": "http://localhost:*",
        "OTO_BROWSER_MODE": "own", "OTO_BROWSER_TOKEN_EXPECTED": "1",
    }
    assert bundles["local"].env == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "V" * 43}
    # No target_browser (local builders never pass one): the entry keeps only
    # what the framework allows, and no bundle is created.
    path, _env, _excl, bundles, _bash = reg.build_session_mcp_config(
        "agent", None, placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m"),
    )
    local = json.loads(path.read_text())["mcpServers"]["local"]
    assert local["env"] == {"PLAYWRIGHT_MCP_BLOCKED_ORIGINS": "http://localhost:*"}
    assert "local" not in bundles


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
