"""Unit tests for the device-local MCP framework.

Covers the three security-critical pieces:
  * ``_device_placement_reason`` — the placement / consent / display gate.
  * ``get_agent_mcps`` fail-closed default + ``get_agent_mcps_all_placements``
    (the configuration view that bypasses the gate).
  * ``remote_store`` helpers (grant parsing, target display/grant lookups) and
    the endpoint grant validator.

DB-free by design: everything is monkeypatched, so these run fast and never
touch the conftest Postgres pool.
"""
import pytest

from services.mcp import mcp_registry as reg
from services.mcp.mcp_registry import McpManifest, ServerConfig, CredentialConfig
from storage import remote_store


def _mk(name, *, placement="any", requires_display=False, device_capability=None,
        server_name="", device_high_risk_tools=None):
    return McpManifest(
        name=name, label=name.title(), description=f"{name} does things.",
        version="1.0.0", category="custom",
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(), config=[], env={}, agent_env={},
        exclude_from=[], skills=[], server_name=server_name,
        placement=placement, requires_display=requires_display,
        device_capability=device_capability,
        device_high_risk_tools=device_high_risk_tools or [],
    )


# ---------------------------------------------------------------------------
# _device_placement_reason — the 3-gate truth table
# ---------------------------------------------------------------------------

def _reason(m, *, is_remote=False, target_has_display=None, target_device_grants=None):
    return reg._device_placement_reason(
        m, is_remote=is_remote, target_has_display=target_has_display,
        target_device_grants=target_device_grants or set(),
    )


def test_normal_mcp_never_gated():
    assert _reason(_mk("n")) is None
    assert _reason(_mk("n"), is_remote=True) is None


def test_satellite_only_blocked_on_local():
    assert "remote machine" in (_reason(_mk("c", placement="satellite_only")) or "")


def test_device_capability_implies_remote_only():
    # device_capability set but placement left "any" must STILL be remote-only:
    # running device control on the proxy would drive the server's screen.
    assert "remote machine" in (_reason(_mk("c", device_capability="computer")) or "")


def test_consent_gate():
    m = _mk("c", placement="satellite_only", device_capability="computer")
    assert "not granted" in (_reason(m, is_remote=True) or "")  # no grant
    assert "not granted" in (_reason(m, is_remote=True, target_device_grants={"browser"}) or "")  # wrong cap
    assert _reason(m, is_remote=True, target_device_grants={"computer"}) is None  # granted


def test_display_gate():
    m = _mk("c", placement="satellite_only", requires_display=True, device_capability="computer")
    g = {"computer"}
    assert "no interactive display" in (
        _reason(m, is_remote=True, target_has_display=False, target_device_grants=g) or ""
    )
    assert _reason(m, is_remote=True, target_has_display=True, target_device_grants=g) is None
    # display unknown (None) must NOT exclude — the tool reports it at call time
    assert _reason(m, is_remote=True, target_has_display=None, target_device_grants=g) is None


# ---------------------------------------------------------------------------
# get_agent_mcps (fail-closed) + get_agent_mcps_all_placements (config view)
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(monkeypatch):
    manifests = {
        "normal": _mk("normal"),
        "computer-control": _mk(
            "computer-control", placement="satellite_only",
            requires_display=True, device_capability="computer",
        ),
    }
    monkeypatch.setattr(reg, "_manifests", manifests)
    monkeypatch.setattr(reg.mcp_store, "get_manager_enabled_mcps", lambda a: list(manifests))
    monkeypatch.setattr(reg.mcp_store, "get_all_mcp_states", lambda: {k: True for k in manifests})
    monkeypatch.setattr(reg.mcp_store, "get_visible_explicit_mcps", lambda a: set(manifests))
    return manifests


def test_get_agent_mcps_fail_closed_on_local(registry):
    names = {m.name for m in reg.get_agent_mcps("a")}  # default kwargs = local
    assert "normal" in names
    assert "computer-control" not in names


def test_get_agent_mcps_remote_granted_with_display(registry):
    names = {m.name for m in reg.get_agent_mcps(
        "a", is_remote=True, target_has_display=True, target_device_grants={"computer"},
    )}
    assert "computer-control" in names


def test_get_agent_mcps_remote_ungranted_excluded(registry):
    names = {m.name for m in reg.get_agent_mcps("a", is_remote=True, target_has_display=True)}
    assert "computer-control" not in names  # no machine grant → consent gate


def test_all_placements_includes_device_regardless(registry):
    names = {m.name for m in reg.get_agent_mcps_all_placements("a")}
    assert names == {"normal", "computer-control"}  # config view bypasses the gate


# ---------------------------------------------------------------------------
# remote_store helpers
# ---------------------------------------------------------------------------

def test_parse_device_grants():
    assert remote_store._parse_device_grants(None) == set()
    assert remote_store._parse_device_grants("[]") == set()
    assert remote_store._parse_device_grants('["computer","browser"]') == {"computer", "browser"}
    assert remote_store._parse_device_grants(["app"]) == {"app"}
    assert remote_store._parse_device_grants("not-json") == set()
    assert remote_store._parse_device_grants('{"a": 1}') == set()  # non-list


def test_get_target_has_display(monkeypatch):
    monkeypatch.setattr(remote_store, "get_remote_machine",
                        lambda m: {"capabilities": '{"display": {"has_display": true}}'})
    assert remote_store.get_target_has_display("admin_remote", "m") is True
    monkeypatch.setattr(remote_store, "get_remote_machine",
                        lambda m: {"capabilities": '{"display": {"has_display": false}}'})
    assert remote_store.get_target_has_display("user_remote", "m") is False
    monkeypatch.setattr(remote_store, "get_remote_machine",
                        lambda m: {"capabilities": '{"os": "linux"}'})  # no display key
    assert remote_store.get_target_has_display("admin_remote", "m") is None
    # local target never reads the DB
    def _boom(_):
        raise AssertionError("should not read DB for local target")
    monkeypatch.setattr(remote_store, "get_remote_machine", _boom)
    assert remote_store.get_target_has_display("local", "") is None


def test_get_target_device_grants(monkeypatch):
    monkeypatch.setattr(remote_store, "get_remote_machine",
                        lambda m: {"device_grants": '["computer"]'})
    assert remote_store.get_target_device_grants("admin_remote", "m") == {"computer"}
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda m: None)
    assert remote_store.get_target_device_grants("user_remote", "m") == set()  # machine gone
    def _boom(_):
        raise AssertionError("should not read DB for local target")
    monkeypatch.setattr(remote_store, "get_remote_machine", _boom)
    assert remote_store.get_target_device_grants("local", "m") == set()


# ---------------------------------------------------------------------------
# Endpoint grant validator
# ---------------------------------------------------------------------------

def test_validate_device_grants():
    import fastapi
    from api.remote import remote_machines
    # de-dup + sort
    assert remote_machines._validate_device_grants(["computer", "computer"]) == ["computer"]
    assert remote_machines._validate_device_grants(["browser", "app"]) == ["app", "browser"]
    assert remote_machines._validate_device_grants([]) == []
    # unknown capability → 422
    with pytest.raises(fastapi.HTTPException) as ei:
        remote_machines._validate_device_grants(["bogus"])
    assert ei.value.status_code == 422


# ---------------------------------------------------------------------------
# Registry: device_capability_for_server (maps a tool's server → capability)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Manifest validation: the device-class fields parse + reject bad values
# ---------------------------------------------------------------------------

def _parse_manifest_with(tmp_path, extra):
    import json
    data = {
        "name": "test-device-mcp",
        "server": {"runtime": "python", "transport": "stdio"},
        **extra,
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(data))
    return reg._parse_manifest(p)


def test_manifest_device_fields_parse(tmp_path):
    m = _parse_manifest_with(tmp_path, {
        "placement": "satellite_only",
        "requires_display": True,
        "device_capability": "computer",
    })
    assert m is not None
    assert m.placement == "satellite_only"
    assert m.requires_display is True
    assert m.device_capability == "computer"


def test_manifest_device_field_defaults(tmp_path):
    m = _parse_manifest_with(tmp_path, {})
    assert m.placement == "any"
    assert m.requires_display is False
    assert m.device_capability is None
    assert m.companion_app is None


def test_manifest_rejects_bad_placement(tmp_path):
    with pytest.raises(ValueError, match="placement"):
        _parse_manifest_with(tmp_path, {"placement": "bogus"})


def test_manifest_rejects_bad_device_capability(tmp_path):
    with pytest.raises(ValueError, match="device_capability"):
        _parse_manifest_with(tmp_path, {"device_capability": "teleport"})


def test_device_capability_for_server(monkeypatch):
    manifests = {
        "computer-control": _mk("computer-control", server_name="computer",
                                device_capability="computer"),
        "file-tools": _mk("file-tools"),  # no device_capability
    }
    monkeypatch.setattr(reg, "_manifests", manifests)
    assert reg.device_capability_for_server("computer") == "computer"
    assert reg.device_capability_for_server("file-tools") is None
    assert reg.device_capability_for_server("nope") is None
    assert reg.device_capability_for_server("") is None


# ---------------------------------------------------------------------------
# Hook: device-tool auto-approve in /v1/hooks/permission
# ---------------------------------------------------------------------------

def _hook_env(monkeypatch, *, grants):
    """Wire up api.hooks.permission for a default-mode dashboard session whose target
    machine grants ``grants``. Returns the configured SecurityContext."""
    import asyncio  # noqa: F401
    from api.hooks import permission, routing
    from auth.path_policy import SecurityContext, PathDecision
    from services import path_policy_v2

    manifests = {"computer-control": _mk(
        "computer-control", placement="satellite_only",
        device_capability="computer", server_name="computer",
    )}
    monkeypatch.setattr(reg, "_manifests", manifests)

    ctx = SecurityContext(
        role="manager", username="u", agent="a", is_admin_agent=False,
        target_kind="user_remote", target_machine_id="m1deadbeef",
        target_device_grants=set(grants),
    )
    monkeypatch.setattr(permission, "verify_session_match", lambda *a, **k: None)
    monkeypatch.setattr(permission, "get_session_mode", lambda sid: "default")
    monkeypatch.setattr(permission, "get_session_client_type", lambda sid: "dashboard")
    monkeypatch.setattr(routing, "get_meeting_session_info", lambda sid: None)
    monkeypatch.setattr(permission, "record_hook_activity", lambda sid: None)
    monkeypatch.setattr(permission, "get_session_security", lambda sid: ctx)
    monkeypatch.setattr(permission, "check_tool_access",
                        lambda *a, **k: (PathDecision(allowed=True), None))
    monkeypatch.setattr(path_policy_v2, "check_target_still_valid", lambda c: None)
    return permission, ctx


def test_hook_auto_approves_granted_device_tool(monkeypatch):
    import asyncio
    permission, _ = _hook_env(monkeypatch, grants={"computer"})
    req = permission.HookPermissionRequest(
        session_id="s1", tool_name="mcp__computer__computer", tool_input={},
    )
    out = asyncio.run(permission.hook_permission(req, authorization="Bearer x"))
    assert out == {"decision": "allow"}


def test_hook_prompts_ungranted_device_tool(monkeypatch):
    import asyncio
    permission, _ = _hook_env(monkeypatch, grants=set())  # capability NOT granted

    # The ungranted device tool must fall through to the dashboard prompt
    # (NOT auto-approve). Stub the prompt machinery so the deny resolves
    # immediately instead of blocking on a real user.
    class _Q:
        async def put(self, _):
            return None
    async def _wait(_request_id, _session_id="", timeout=0):
        return False
    monkeypatch.setattr(permission, "get_permission_queue", lambda sid: _Q())
    monkeypatch.setattr(permission, "wait_for_permission", _wait)

    req = permission.HookPermissionRequest(
        session_id="s1", tool_name="mcp__computer__computer", tool_input={},
    )
    out = asyncio.run(permission.hook_permission(req, authorization="Bearer x"))
    assert out["decision"] == "deny"  # prompted (then our stub denied) — never auto-approved


def test_hook_does_not_auto_approve_nondevice_mcp(monkeypatch):
    import asyncio
    # A non-device MCP (no device_capability) must still prompt even though a
    # device capability is granted — the grant is per-capability, not blanket.
    permission, _ = _hook_env(monkeypatch, grants={"computer"})
    hooks_reg = reg._manifests
    hooks_reg["slack"] = _mk("slack", server_name="slack")  # no device_capability

    class _Q:
        async def put(self, _):
            return None
    async def _wait(_request_id, _session_id="", timeout=0):
        return False
    monkeypatch.setattr(permission, "get_permission_queue", lambda sid: _Q())
    monkeypatch.setattr(permission, "wait_for_permission", _wait)

    req = permission.HookPermissionRequest(
        session_id="s1", tool_name="mcp__slack__post_message", tool_input={},
    )
    out = asyncio.run(permission.hook_permission(req, authorization="Bearer x"))
    assert out["decision"] == "deny"  # non-device MCP still prompts


def test_hook_high_risk_device_tool_prompts_even_when_granted(monkeypatch):
    import asyncio
    # A granted device capability auto-approves the connector's normal tools,
    # but a tool listed in device_high_risk_tools (e.g. execute_blender_code =
    # RCE inside the app) must STILL prompt.
    permission, _ = _hook_env(monkeypatch, grants={"app"})
    reg._manifests["blender-bridge"] = _mk(
        "blender-bridge", placement="satellite_only", device_capability="app",
        server_name="blender", device_high_risk_tools=["execute_blender_code"],
    )

    class _Q:
        async def put(self, _):
            return None
    async def _wait(_request_id, _session_id="", timeout=0):
        return False
    monkeypatch.setattr(permission, "get_permission_queue", lambda sid: _Q())
    monkeypatch.setattr(permission, "wait_for_permission", _wait)

    # high-risk tool → prompts (our stub denies), NOT auto-approved
    hr = permission.HookPermissionRequest(
        session_id="s1", tool_name="mcp__blender__execute_blender_code",
        tool_input={"code": "import bpy"},
    )
    assert asyncio.run(permission.hook_permission(hr, authorization="Bearer x"))["decision"] == "deny"

    # a non-high-risk tool on the SAME granted connector still auto-approves
    ok = permission.HookPermissionRequest(
        session_id="s1", tool_name="mcp__blender__get_scene_info", tool_input={},
    )
    assert asyncio.run(permission.hook_permission(ok, authorization="Bearer x")) == {"decision": "allow"}
