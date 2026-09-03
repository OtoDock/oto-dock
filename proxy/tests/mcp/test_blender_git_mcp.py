"""Tests for git-source MCP support + the official Blender MCP wrapper
(wraps the official Blender Lab MCP via a `git+` install source instead of
our own bridge).

DB-free (mirrors test_browser_mcp.py). Covers:
  1. the new `git+` source type in the SHARED installer's parse_source,
  2. the Blender manifest parsing with companion_app + BOTH high-risk exec tools,
  3. the device gate + auto-approve helpers treating it as an `app` capability,
  4. resolve_server_config emitting the `python -m blmcp` stdio entry — and in
     particular that the bare module name `blmcp` is NOT mangled into a path.
"""

import pytest

from services.mcp import mcp_registry as reg
from services.mcp import mcp_installer as inst

from tests._paths import REPO_ROOT as _ROOT
_MANIFEST = _ROOT / "mcps" / "community" / "blender" / "manifest.json"
if not _MANIFEST.is_file():
    # community/* MCPs ship from the separate community-mcps repo and are
    # gitignored here — a fresh checkout legitimately lacks them.
    pytest.skip("blender manifest not present in this checkout",
                allow_module_level=True)


@pytest.fixture()
def manifest():
    m = reg._parse_manifest(_MANIFEST)
    assert m is not None, "blender manifest failed to parse"
    return m


# ---------------------------------------------------------------------------
# git+ source type in the shared installer (proxy + vendored to the satellite)
# ---------------------------------------------------------------------------

def test_parse_source_git_with_ref():
    p = inst.parse_source("git+https://projects.blender.org/lab/blender_mcp.git@v1.0.0#subdirectory=mcp")
    assert p is not None
    assert p.registry == "git"
    assert p.package == "https://projects.blender.org/lab/blender_mcp.git"
    assert p.version == "v1.0.0"


def test_parse_source_git_without_ref():
    p = inst.parse_source("git+https://example.com/x/y.git#subdirectory=mcp")
    assert p is not None and p.registry == "git" and p.version == ""
    assert p.package == "https://example.com/x/y.git"


def test_parse_source_still_handles_pypi_npm():
    assert inst.parse_source("pypi:ha-mcp@6.6.1").registry == "pypi"
    assert inst.parse_source("npm:@scope/x@1.2.3").registry == "npm"
    assert inst.parse_source("") is None


# ---------------------------------------------------------------------------
# Manifest shape
# ---------------------------------------------------------------------------

def test_manifest_device_fields(manifest):
    assert manifest.name == "blender"
    assert manifest.server_name == "blender"  # tools -> mcp__blender__*
    assert manifest.placement == "satellite_only"
    assert manifest.device_capability == "app"
    assert manifest.requires_display is True
    assert manifest.assignment_mode == "auto"
    assert "phone" not in manifest.exclude_from  # allowed on calls; gated by device placement + grants


def test_manifest_git_source(manifest):
    assert manifest.server.runtime == "python"
    assert manifest.server.transport == "stdio"
    assert manifest.server.command == "venv/bin/python"
    assert manifest.server.args == ["-m", "blmcp"]
    assert manifest.server.source.startswith(
        "git+https://projects.blender.org/lab/blender_mcp.git@"
    )
    # the manifest's source must be recognised as a git source by the installer
    assert inst.parse_source(manifest.server.source).registry == "git"


def test_manifest_companion_app(manifest):
    ca = manifest.companion_app
    assert ca is not None
    assert ca.program == "blender"
    assert ca.connect.host == "127.0.0.1"
    assert ca.connect.port == 9876
    # the official add-on socket is OPEN (no token) — documented residual;
    # "none" is a valid companion_app.auth value
    assert ca.auth == "none"
    assert ca.plugin.supported_host_versions == ["blender>=4.2,<5.0"]


def test_manifest_high_risk_tools(manifest):
    # BOTH arbitrary-Python tools the official server exposes must be gated so
    # they still prompt even when the machine grants "app".
    assert manifest.device_high_risk_tools == [
        "execute_blender_code", "execute_blender_code_for_cli",
    ]


def test_manifest_declares_git_system_requirement(manifest):
    # git+pip needs `git` on the satellite — it must be a declared system req so
    # a missing git surfaces as a clear message, not a cryptic pip failure.
    assert "git" in repr(manifest.system_requirements).lower()


# ---------------------------------------------------------------------------
# Framework gate (predicate) + auto-approve helpers — for the "app" capability
# ---------------------------------------------------------------------------

def _reason(m, *, r=False, d=None, g=None):
    return reg._device_placement_reason(
        m, is_remote=r, target_has_display=d, target_device_grants=g or set()
    )


def test_gate_local_excluded(manifest):
    assert "remote machine" in (_reason(manifest) or "")


def test_gate_remote_requires_app_grant(manifest):
    assert "not granted" in (_reason(manifest, r=True) or "")
    assert "not granted" in (_reason(manifest, r=True, g={"computer"}) or "")


def test_gate_attaches_when_app_granted_with_display(manifest):
    assert _reason(manifest, r=True, d=True, g={"app"}) is None
    assert _reason(manifest, r=True, d=None, g={"app"}) is None


def test_gate_excluded_when_known_headless(manifest):
    assert "no interactive display" in (_reason(manifest, r=True, d=False, g={"app"}) or "")


def test_capability_and_high_risk_helpers(manifest):
    reg._manifests[manifest.name] = manifest
    try:
        assert reg.device_capability_for_server("blender") == "app"
        assert reg.is_high_risk_device_tool("blender", "execute_blender_code") is True
        assert reg.is_high_risk_device_tool("blender", "execute_blender_code_for_cli") is True
        assert reg.is_high_risk_device_tool("blender", "get_scene_info") is False
    finally:
        reg._manifests.pop(manifest.name, None)


# ---------------------------------------------------------------------------
# resolve_server_config — python -m blmcp (module name must NOT be path-mangled)
# ---------------------------------------------------------------------------

def test_resolve_server_config_python_module(manifest, monkeypatch):
    monkeypatch.setattr(reg.mcp_store, "get_mcp_config_values", lambda n: {})
    for fmt in ("json", "toml"):
        e = reg.resolve_server_config(manifest, "designer", mcp_config_format=fmt, session_ctx={})
        assert e["type"] == "stdio"
        assert "python" in e["command"]
        assert e["command"].replace("\\", "/").endswith("venv/bin/python")
        # CRITICAL: "blmcp" has no '.', so it stays a literal module name —
        # it must NOT be resolved to "<mcp_dir>/blmcp".
        assert e["args"] == ["-m", "blmcp"]
