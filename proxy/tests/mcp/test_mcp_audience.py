"""A manifest's ``audience`` (the skills' tier vocabulary): the session
config leaves the MCP out for a person below the tier, the sandbox egress
carves nothing for it, the prompt's catalog stops listing it, and an
agent-scope session is the agent itself and keeps it. ssh-hosts declares
the editor tier.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._paths import PROXY_DIR, REPO_ROOT
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from services.mcp import mcp_registry  # noqa: E402
from services.mcp.mcp_manifest_types import SKILL_AUDIENCES  # noqa: E402


def test_ssh_hosts_declares_the_editor_tier():
    data = json.loads((Path(REPO_ROOT) / "mcps" / "custom" / "ssh-hosts" / "manifest.json").read_text())
    assert data["audience"] in SKILL_AUDIENCES
    from auth import roles
    gate = {"owner": roles.can_manage, "editor": roles.can_edit,
            "workspace": roles.can_write_workspace}[data["audience"]]
    assert gate is roles.can_edit


@pytest.mark.parametrize("audience, role, admitted", [
    ("", "viewer", True),
    ("editor", None, True),
    ("editor", "viewer", False),
    ("editor", "contributor", False),
    ("editor", "editor", True),
    ("editor", "manager", True),
    ("editor", "admin", True),
    ("owner", "editor", False),
    ("owner", "manager", True),
    ("workspace", "viewer", False),
    ("workspace", "contributor", True),
])
def test_audience_admits_follows_the_role_tiers(audience, role, admitted):
    assert mcp_registry.audience_admits(audience, role) is admitted


def _m(audience):
    return SimpleNamespace(name="ssh-hosts", label="SSH", audience=audience)


def test_the_refusal_names_the_tier_and_spares_agent_scope_and_unknown_people():
    assert mcp_registry.audience_refusal(_m(""), user_role="viewer", task_scope="user") is None
    assert mcp_registry.audience_refusal(_m("editor"), user_role="viewer", task_scope="agent") is None
    assert mcp_registry.audience_refusal(_m("editor"), user_role="", task_scope="user") is None
    assert mcp_registry.audience_refusal(_m("editor"), user_role="editor", task_scope="user") is None
    out = mcp_registry.audience_refusal(_m("editor"), user_role="viewer", task_scope="user")
    assert out == "SSH is for the editor tier of this agent and above"


def test_the_session_config_leaves_the_mcp_out_for_a_person_below_the_tier(monkeypatch, tmp_path):
    from tests.mcp.test_mcp_broker_activation import _FakeManifest, _stub_assembly
    ssh = _FakeManifest("ssh-hosts")
    ssh.audience = "editor"
    tool = _FakeManifest("memory")
    _stub_assembly(monkeypatch, [ssh, tool], env_by_mcp={}, tmp_path=tmp_path)
    _p, _e, excluded, _b, _ = mcp_registry.build_session_mcp_config(
        "agent", "u-1", user_role="viewer", task_scope="user",
    )
    assert "ssh-hosts" in excluded and "editor tier" in excluded["ssh-hosts"]
    assert "memory" not in excluded
    _p, _e, excluded, _b, _ = mcp_registry.build_session_mcp_config(
        "agent", "u-1", user_role="editor", task_scope="user",
    )
    assert "ssh-hosts" not in excluded
    _p, _e, excluded, _b, _ = mcp_registry.build_session_mcp_config(
        "agent", None, user_role="", task_scope="agent",
    )
    assert "ssh-hosts" not in excluded


def test_the_prompt_catalog_stops_listing_it_for_a_viewer(monkeypatch):
    ssh = SimpleNamespace(name="ssh-hosts", label="SSH", description="hosts", audience="editor",
                          category="custom", exclude_from=[], server=SimpleNamespace(transport="stdio"))
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [ssh])
    assert "SSH" in mcp_registry.build_available_mcps_section("agent", user_role="editor")
    assert "SSH" in mcp_registry.build_available_mcps_section("agent")
    assert mcp_registry.build_available_mcps_section("agent", user_role="viewer") == ""


def test_build_agent_prompt_drops_it_from_a_viewers_catalog(temp_db, monkeypatch):
    """The catalog the prompt builder renders, not only the section helper:
    ``build_agent_prompt`` hands the person's role to it."""
    import config as app_config
    from storage.agents import agent_store
    if not agent_store.agent_exists("sshbot"):
        agent_store.create_agent("sshbot", "Sshbot")
    agent_dir = app_config.AGENTS_DIR / "sshbot"
    (agent_dir / "config").mkdir(parents=True, exist_ok=True)
    (agent_dir / "config" / "agent.md").write_text("You run commands.")
    ssh = SimpleNamespace(name="ssh-hosts", label="SSH Hosts", description="Reach hosts.",
                          audience="editor", category="custom", exclude_from=[],
                          server=SimpleNamespace(transport="stdio"))
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [ssh])
    monkeypatch.setattr(mcp_registry, "get_skills_for_agent", lambda *a, **k: [])
    monkeypatch.setattr(mcp_registry, "get_skill_catalog_for_agent", lambda *a, **k: [])
    viewer = app_config.build_agent_prompt("sshbot", role="viewer", user_role="viewer") or ""
    editor = app_config.build_agent_prompt("sshbot", role="editor", user_role="editor") or ""
    assert "SSH Hosts" not in viewer
    assert "SSH Hosts" in editor


def test_the_egress_carve_skips_it_for_a_viewer(monkeypatch):
    from auth import providers
    from services.mcp.mcp_manifest_types import NetworkTargetDecl
    ssh = SimpleNamespace(
        name="ssh-hosts", label="SSH", audience="editor", placement="any",
        network_targets=[NetworkTargetDecl(source="instance", host_key="host", port_key="port",
                                           port_default=22)],
        server=SimpleNamespace(runtime="python", transport="stdio", port=0, url_template=""),
    )
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda *a, **k: [ssh])
    monkeypatch.setattr(mcp_registry, "manifest_capability_available", lambda m: True)
    monkeypatch.setattr(mcp_registry, "network_access_enabled", lambda m: True)
    monkeypatch.setattr(mcp_registry, "enumerate_mcp_network_targets",
                        lambda m, a, **k: [("203.0.113.9", 22)])
    monkeypatch.setattr(mcp_registry, "_resolve_to_ips", lambda h: [h])
    monkeypatch.setattr(mcp_registry, "_is_local_host_ip", lambda ip: False)
    roles = {"u-editor": "editor", "u-viewer": "viewer"}
    monkeypatch.setattr(providers, "effective_role_of", lambda sub, agent: roles[sub])
    _fw, allow = mcp_registry.resolve_sandbox_egress("agent", user_sub="u-editor")
    assert "203.0.113.9" in allow
    _fw, allow = mcp_registry.resolve_sandbox_egress("agent", user_sub="u-viewer")
    assert "203.0.113.9" not in allow
