"""ssh-hosts — SSH access through the agent's shell + the list_ssh_hosts tool.

ssh-hosts replaces the community ssh-server wrapper: agents run plain
``ssh``/``scp`` from bash against admin-configured instance hosts. The MCP
contributes instances (authorization + admin UI), the dynamic-context host
list, per-session key materialization, network_targets, and a minimal stdio
server whose single ``list_ssh_hosts`` tool re-fetches the host list
mid-session (``GET /v1/agents/{name}/ssh-hosts``). These tests cover each
framework seam; the context-only (transport "none") mechanism tests below
use a synthetic manifest — the mechanism outlived ssh-hosts's server flip.
"""

import asyncio
import contextlib
import dataclasses
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from core import placement
from tests.mcp.test_mcp_broker_activation import (
    _FakeManifest,
    _stub_assembly,
)

_ADMIN_PAIRED = placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m")
_USER_PAIRED = placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m")
_COMPUTE = placement.PlacementCapabilities(kind=placement.KIND_COMPUTE, machine_id="c")


def _today(where: placement.PlacementCapabilities) -> placement.PlacementCapabilities:
    """The machine on the satellite this platform ships, as every paired
    machine runs once its update lands: the version the machine rules read."""
    from ws.satellite import SATELLITE_VERSION_LATEST
    return dataclasses.replace(where, satellite_version=SATELLITE_VERSION_LATEST)


def _context_only_manifest(name="ssh-hosts"):
    fm = _FakeManifest(name)
    fm.server = SimpleNamespace(
        proxy_callbacks=False, port=0, transport="none", runtime="none",
    )
    return fm


# ---------------------------------------------------------------------------
# Manifest (the real shipped file)
# ---------------------------------------------------------------------------


def test_shipped_manifest_parses():
    from services.mcp.mcp_manifest_parse import _parse_manifest

    path = PROXY_DIR.parent / "mcps" / "custom" / "ssh-hosts" / "manifest.json"
    m = _parse_manifest(path)
    assert m is not None
    assert m.name == "ssh-hosts"
    # A minimal stdio server backs the list_ssh_hosts lookup tool; SSH itself
    # stays plain ssh/scp from bash (no exec-style tool surface).
    assert m.server.transport == "stdio" and m.server.runtime == "python"
    # The policy keeps a tool the platform hands a secret of its own off a
    # person's own machine; SSH hosts is handed none (the proxy keeps the
    # keys and signs), so the policy does not hold it back there.
    assert m.remote_policy == "admin_paired_only"
    assert m.assignment_mode == "explicit"
    assert m.instances and m.instances.delivery == "none"
    assert {f.key for f in m.instances.fields} == {
        "name", "host", "port", "username", "key_name",
    }
    assert m.data_dirs.get("keys") == "keys/"
    assert m.network_targets and m.network_targets[0].source == "instance"


# ---------------------------------------------------------------------------
# build_session_mcp_config — no server entry
# ---------------------------------------------------------------------------


def test_context_only_mcp_emits_no_server_entry_locally(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_context_only_manifest()], env_by_mcp={}, tmp_path=tmp_path,
    )

    path, _env, excluded, bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=placement.LOCAL_PLACEMENT,
    )

    assert "ssh-hosts" not in excluded  # active — just serverless
    assert "ssh-hosts" not in bundles
    if path:  # config written only when other MCPs produced entries
        import json
        written = json.loads(Path(path).read_text())
        assert "ssh-hosts" not in written.get("mcpServers", {})


def test_a_compute_machine_is_the_platforms_place_for_a_context_only_mcp(monkeypatch, tmp_path):
    """A compute machine takes the tool on the satellite this platform
    ships, as the platform's own sandbox does."""
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_context_only_manifest()], env_by_mcp={}, tmp_path=tmp_path,
    )

    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=_today(_COMPUTE),
    )
    assert "ssh-hosts" not in excluded


# ---------------------------------------------------------------------------
# Dynamic-context provider
# ---------------------------------------------------------------------------


def _instances(*rows):
    return [
        {"id": i, "field_values": dict(fv), "agents": ["agent"],
         "assigned_to_all": False}
        for i, fv in enumerate(rows, start=1)
    ]


def test_provider_renders_authorized_hosts():
    """Each line names the key by its public half, which picks the agent's
    identity from the session's agent socket."""
    from services.mcp.dynamic_context import _ssh_hosts_context

    rows = _instances(
        {"name": "prod", "host": "10.0.0.5", "port": "2222",
         "username": "root", "key_name": "prod_key"},
        {"name": "", "host": "backup.lan", "username": "oto"},
    )
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        text = _ssh_hosts_context("agent", user_role="manager")

    assert "## SSH Hosts" in text
    # accept-new on every line: the first connect in a non-interactive shell
    # must not die on ssh's TOFU check (hosts are often reachable only from
    # the machine the session runs on — no platform-side pre-scan).
    # ControlMaster mux on local sessions (bwrap = Linux): command bursts
    # reuse one authenticated connection instead of scan-shaped serial
    # connects (Suricata ET SCAN 2001219, 2026-07-06). The socket lives in
    # the OS runtime dir, NOT $OTO_SSH_KEY_DIR — the key dir's session-secrets
    # nesting overflowed the 108-byte sun_path limit (2026-07-11).
    _MUX = ("-o ControlMaster=auto "
            '-o ControlPath="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/oto-cm-%C" '
            "-o ControlPersist=60s")
    assert ('`ssh -i "$OTO_SSH_KEY_DIR/prod_key.pub" -o IdentitiesOnly=yes '
            f"-o StrictHostKeyChecking=accept-new {_MUX} "
            "-p 2222 root@10.0.0.5`") in text
    # No key / no port / no name → keyless line, default port, host as label.
    assert ("**backup.lan** — `ssh -o StrictHostKeyChecking=accept-new "
            f"{_MUX} -p 22 oto@backup.lan`") in text


def test_provider_mux_gated_by_target_os():
    """Remote targets: mux only where the satellite reported linux/darwin —
    Windows OpenSSH has no unix-socket ControlMaster, and an unknown OS
    (pre-capability satellite) stays conservative. The machine runs
    today's satellite, which opens the agent endpoint."""
    from services.mcp.dynamic_context import _ssh_hosts_context
    paired = _today(_ADMIN_PAIRED)

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        linux = _ssh_hosts_context(
            "agent", placement=dataclasses.replace(paired, os="linux"),
            user_role="manager")
        windows = _ssh_hosts_context(
            "agent", placement=dataclasses.replace(paired, os="windows"),
            user_role="manager")
        unknown = _ssh_hosts_context("agent", placement=paired, user_role="manager")

    assert "ControlMaster=auto" in linux
    assert "ControlMaster" not in windows
    assert "-o StrictHostKeyChecking=accept-new -p 22 u@10.0.0.5" in windows
    assert "ControlMaster" not in unknown


def test_the_provider_speaks_on_a_compute_machine_as_on_the_platform():
    """Its block and its mux are the sandbox's: a private /tmp holds the socket."""
    from services.mcp.dynamic_context import _ssh_hosts_context

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        text = _ssh_hosts_context("agent", placement=_today(_COMPUTE), user_role="manager")
    assert text is not None and "10.0.0.5" in text and "ControlMaster" in text


def test_provider_silent_when_unauthorized():
    """No authorizing instance, no block. Every satellite at the platform's
    floor opens the agent socket, so the machine names no refusal of its own."""
    from services.mcp.dynamic_context import _ssh_hosts_context

    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=[]):
        assert _ssh_hosts_context("agent", user_role="manager") is None
    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        assert _ssh_hosts_context("agent", placement=_USER_PAIRED, user_role="manager") is not None
        assert _ssh_hosts_context("agent", placement=_ADMIN_PAIRED, user_role="manager") is not None


# ---------------------------------------------------------------------------
# Per-session key materialization
# ---------------------------------------------------------------------------


def _agent_cfg(tmp_path, monkeypatch=None, *, user="pm"):
    """A session's config folder of the agent ``agent`` under the test's own
    agents root, which ``AGENTS_DIR`` points at."""
    import config as app_config
    agents = tmp_path / "agents"
    if monkeypatch is not None:
        monkeypatch.setattr(app_config, "AGENTS_DIR", agents)
    cfg = agents / "agent" / "users" / user / ".claude"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg


def _materialize_env(tmp_path, *, assigned=True, instances=None):
    """Patch registry + store around collect_authorized_ssh_keys."""
    mcp_dir = tmp_path / "ssh-hosts"
    (mcp_dir / "keys").mkdir(parents=True)
    manifest = SimpleNamespace(name="ssh-hosts", mcp_dir=mcp_dir)
    agent_mcps = [manifest] if assigned else []
    return (
        mcp_dir,
        patch("services.mcp.mcp_registry.get_manifest", return_value=manifest),
        patch("services.mcp.mcp_registry.get_agent_mcps", return_value=agent_mcps),
        patch("storage.mcp.mcp_store.get_mcp_instances_for_agent",
              return_value=instances or []),
    )


def _key_file(path: Path) -> str:
    """An OpenSSH private key at ``path``, as the MCP's keys folder holds
    one; its public line without a comment."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                       serialization.NoEncryption()))
    return key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                         serialization.PublicFormat.OpenSSH).decode("ascii")


@pytest.mark.asyncio
async def test_the_dir_holds_the_public_halves_of_the_authorized_keys_alone(tmp_path, monkeypatch):
    """Only a key an authorizing instance names lands, as its public half; a
    key name that leaves the keys folder names nothing, and an agent the
    tool is not enabled for gets no dir."""
    from core.sandbox.session_config_dir import provision_ssh_keys_for_sandbox

    rows = _instances({"host": "h", "key_name": "good_key"},
                      {"host": "h2", "key_name": "../../outside_key"})
    mcp_dir, p1, p2, p3 = _materialize_env(tmp_path, instances=rows)
    good = _key_file(mcp_dir / "keys" / "good_key")
    _key_file(mcp_dir / "keys" / "other_key")
    _key_file(tmp_path / "outside_key")
    cfg = _agent_cfg(tmp_path, monkeypatch)

    with p1, p2, p3:
        out = await provision_ssh_keys_for_sandbox(_ctx("editor"), "agent", cfg, "/users/pm/.claude")
    assert out == "/users/pm/.claude/ssh"
    assert sorted(p.name for p in (cfg / "ssh").iterdir()) == ["good_key.pub"]
    assert (cfg / "ssh" / "good_key.pub").read_text() == f"{good} good_key\n"

    with p1, patch("services.mcp.mcp_registry.get_agent_mcps", return_value=[]), p3:
        out = await provision_ssh_keys_for_sandbox(_ctx("editor"), "agent", cfg, "/users/pm/.claude")
    assert out == "" and not (cfg / "ssh").exists()


# ---------------------------------------------------------------------------
# Satellite sync skips context-only MCPs
# ---------------------------------------------------------------------------


def test_mcp_sync_diff_skips_runtime_none():
    from services.mcp import mcp_sync

    manifest = SimpleNamespace(
        server=SimpleNamespace(runtime="none"), mcp_dir=Path("/nonexistent"),
    )
    with patch("services.mcp.mcp_registry.get_manifest", return_value=manifest):
        to_install, to_update, to_remove = mcp_sync._diff(
            desired={"ssh-hosts"}, installed={},
        )
    assert to_install == set() and to_update == set()



# ---------------------------------------------------------------------------
# remote_policy = "admin_paired_only" — off a person's machine for a tool
# handed a secret of the platform's own
# ---------------------------------------------------------------------------


def _admin_paired_only_manifest(name="ssh-hosts"):
    fm = _FakeManifest(name)
    fm.remote_policy = "admin_paired_only"
    return fm


def test_admin_paired_only_included_locally(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_admin_paired_only_manifest()],
        env_by_mcp={"ssh-hosts": {}}, tmp_path=tmp_path,
    )

    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=placement.LOCAL_PLACEMENT,
    )
    assert "ssh-hosts" not in excluded


def test_admin_paired_only_included_on_admin_paired_remote(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_admin_paired_only_manifest()],
        env_by_mcp={"ssh-hosts": {}}, tmp_path=tmp_path,
    )

    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=_today(_ADMIN_PAIRED),
    )
    assert "ssh-hosts" not in excluded


def test_admin_paired_only_included_on_a_compute_machine(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_admin_paired_only_manifest()],
        env_by_mcp={"ssh-hosts": {}}, tmp_path=tmp_path,
    )

    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=_today(_COMPUTE),
    )
    assert "ssh-hosts" not in excluded


def test_a_tool_handed_a_secret_keeps_the_policy(monkeypatch, tmp_path):
    """The policy is lifted for a tool the platform hands no secret of its
    own (SSH hosts: the proxy keeps the keys and signs). A tool it hands a
    key of its own stays off a person's own machine, since that key can
    reach a machine as a value."""
    from services.mcp import mcp_registry
    from services.mcp.mcp_session_config import kept_off_a_persons_machine
    keyed = _admin_paired_only_manifest("vendor-tool")
    keyed.credentials = SimpleNamespace(type="infra", fields=[{"key": "VENDOR_KEY", "input_type": "password"}],
                                        oauth=None, api_key_header=None, exit=None)
    _stub_assembly(monkeypatch, [keyed], env_by_mcp={"vendor-tool": {}}, tmp_path=tmp_path)
    _path, _env, left_out, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=_today(_USER_PAIRED))
    assert left_out["vendor-tool"] == (
        "vendor-tool is unavailable on this machine — it is delivered only to admin-paired machines")
    _path, _env, left_out, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=_today(_ADMIN_PAIRED))
    assert "vendor-tool" not in left_out
    assert kept_off_a_persons_machine(keyed) is True
    assert kept_off_a_persons_machine(_admin_paired_only_manifest()) is False
    # An instance the platform delivers reaches the tool; one it keeps for
    # the admin's screen and the authorization alone (SSH hosts') does not.
    field = SimpleNamespace(key="TOKEN", input_type="text")
    by_instance = _admin_paired_only_manifest("by-instance")
    by_instance.instances = SimpleNamespace(delivery="env", fields=[field])
    assert kept_off_a_persons_machine(by_instance) is True
    by_instance.instances = SimpleNamespace(delivery="config_file", fields=[field])
    assert kept_off_a_persons_machine(by_instance) is True
    by_instance.instances = SimpleNamespace(delivery="none", fields=[field])
    assert kept_off_a_persons_machine(by_instance) is False


# ---------------------------------------------------------------------------
# Shared command renderer (prompt block + endpoint must never drift)
# ---------------------------------------------------------------------------


def test_format_ssh_host_command():
    from services.mcp.dynamic_context import format_ssh_host_command

    fv = {"name": "prod", "host": "10.0.0.5", "port": "2222",
          "username": "root", "key_name": "prod_key"}
    with_mux = format_ssh_host_command(fv, mux=True)
    assert with_mux == (
        'ssh -i "$OTO_SSH_KEY_DIR/prod_key.pub" -o IdentitiesOnly=yes'
        " -o StrictHostKeyChecking=accept-new"
        " -o ControlMaster=auto"
        ' -o ControlPath="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/oto-cm-%C"'
        " -o ControlPersist=60s -p 2222 root@10.0.0.5"
    )
    no_mux = format_ssh_host_command(fv, mux=False)
    assert "ControlMaster" not in no_mux
    # No key / no port / no username → keyless, default port, bare host.
    assert format_ssh_host_command({"host": "backup.lan"}, mux=False) == (
        "ssh -o StrictHostKeyChecking=accept-new -p 22 backup.lan"
    )
    assert format_ssh_host_command({"name": "x"}, mux=True) is None  # no host


def test_provider_block_cross_links_the_tool():
    from services.mcp.dynamic_context import _ssh_hosts_context

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        text = _ssh_hosts_context("agent", user_role="manager")
    assert "list_ssh_hosts" in text


# ---------------------------------------------------------------------------
# GET /v1/agents/{name}/ssh-hosts — the endpoint behind list_ssh_hosts
# ---------------------------------------------------------------------------


def _ssh_hosts_app(user):
    from fastapi import FastAPI

    from api.mcp import mcps as mcps_api
    from auth.providers import get_current_user

    app = FastAPI()
    app.include_router(mcps_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return app


def _session_user(agent="agent", agent_roles=None):
    """A session-token principal: an editor of the agent unless told otherwise
    (the tool answers by tier, so the bare "member" shape is the 403 case)."""
    from auth.providers import UserContext
    if agent_roles is None:
        agent_roles = {"agent": "editor"}
    return UserContext(
        sub="user-1", email="", name="", role="member",
        agents=list(agent_roles), agent_roles=agent_roles,
        is_api_key=True, session_id="sid-1", agent=agent,
    )


@contextlib.contextmanager
def _live_session(sid="sid-1", role="editor"):
    """The session's registered SecurityContext, as every layer registers it
    before the session's process starts."""
    from auth.path_policy import SecurityContext
    from core.session import session_state
    session_state.set_session_security(sid, SecurityContext(
        role=role, username="pm", agent="agent", is_admin_agent=False))
    try:
        yield
    finally:
        session_state._session_security.pop(sid, None)


def _endpoint_patches(instances):
    manifest = SimpleNamespace(name="ssh-hosts")
    return (
        patch("services.mcp.mcp_registry.get_manifest", return_value=manifest),
        patch("services.mcp.mcp_registry.get_agent_mcps", return_value=[manifest]),
        patch("storage.mcp.mcp_store.get_mcp_instances_for_agent",
              return_value=instances),
    )


def test_endpoint_session_caller_gets_hosts_with_commands():
    from fastapi.testclient import TestClient

    rows = _instances(
        {"name": "prod", "host": "10.0.0.5", "port": "2222",
         "username": "root", "key_name": "prod_key"},
    )
    p1, p2, p3 = _endpoint_patches(rows)
    client = TestClient(_ssh_hosts_app(_session_user()))
    with p1, p2, p3, _live_session():
        resp = client.get("/v1/agents/agent/ssh-hosts")
    assert resp.status_code == 200
    hosts = resp.json()["hosts"]
    assert len(hosts) == 1
    h = hosts[0]
    assert h["name"] == "prod" and h["key_name"] == "prod_key"
    assert h["command"].startswith('ssh -i "$OTO_SSH_KEY_DIR/prod_key.pub" -o IdentitiesOnly=yes ')
    assert "ControlMaster" in h["command"]  # default target_os=linux → mux


def test_endpoint_target_os_windows_drops_mux():
    from fastapi.testclient import TestClient

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    p1, p2, p3 = _endpoint_patches(rows)
    client = TestClient(_ssh_hosts_app(_session_user()))
    with p1, p2, p3, _live_session():
        resp = client.get("/v1/agents/agent/ssh-hosts?target_os=windows")
    assert resp.status_code == 200
    assert "ControlMaster" not in resp.json()["hosts"][0]["command"]


def test_endpoint_rejects_wrong_agent_session():
    from fastapi.testclient import TestClient

    p1, p2, p3 = _endpoint_patches([])
    client = TestClient(_ssh_hosts_app(_session_user(agent="other-agent")))
    with p1, p2, p3:
        resp = client.get("/v1/agents/agent/ssh-hosts")
    assert resp.status_code == 403


def test_endpoint_403_when_not_enabled_for_agent():
    from fastapi.testclient import TestClient

    manifest = SimpleNamespace(name="ssh-hosts")
    client = TestClient(_ssh_hosts_app(_session_user()))
    with patch("services.mcp.mcp_registry.get_manifest", return_value=manifest), \
         patch("services.mcp.mcp_registry.get_agent_mcps", return_value=[]), _live_session():
        resp = client.get("/v1/agents/agent/ssh-hosts")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "ssh-hosts is not enabled for this agent"


# ---------------------------------------------------------------------------
# The editor tier: keys, the prompt block and the tool answer
# ---------------------------------------------------------------------------


def _ctx(role, username="pm", **extra):
    from auth.path_policy import SecurityContext
    return SecurityContext(role=role, username=username, agent="agent",
                           is_admin_agent=False, **extra)


@pytest.mark.parametrize("role", ["viewer", "contributor"])
def test_below_editor_never_takes_ssh_keys(role):
    from core.sandbox.session_config_dir import session_takes_ssh_keys
    assert session_takes_ssh_keys(_ctx(role)) is False


@pytest.mark.parametrize("role", ["editor", "manager", "admin"])
def test_editor_and_above_take_ssh_keys(role):
    from core.sandbox.session_config_dir import session_takes_ssh_keys
    assert session_takes_ssh_keys(_ctx(role)) is True


def test_judge_external_and_missing_context_take_no_keys():
    from core.sandbox.session_config_dir import session_takes_ssh_keys
    assert session_takes_ssh_keys(_ctx("manager", read_only=True)) is False
    assert session_takes_ssh_keys(_ctx("manager", principal="external")) is False
    assert session_takes_ssh_keys(None) is False
    assert session_takes_ssh_keys(SimpleNamespace(read_only=False)) is False


@pytest.mark.parametrize("role", ["viewer", "contributor", ""])
def test_provider_block_hidden_below_editor(role):
    from services.mcp.dynamic_context import _ssh_hosts_context
    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u", "key_name": "k"})
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        assert _ssh_hosts_context("agent", user_role=role) is None
        assert _ssh_hosts_context("agent") is None  # no role passed: no block
        assert "## SSH Hosts" in _ssh_hosts_context("agent", user_role="editor")
        assert "## SSH Hosts" in _ssh_hosts_context("agent", user_role="manager")


def test_endpoint_refuses_a_session_caller_below_editor():
    from fastapi.testclient import TestClient

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    p1, p2, p3 = _endpoint_patches(rows)
    for agent_roles in ({"agent": "contributor"}, {"agent": "viewer"}, {}):
        client = TestClient(_ssh_hosts_app(_session_user(agent_roles=agent_roles)))
        with p1, p2, p3:
            resp = client.get("/v1/agents/agent/ssh-hosts")
        assert resp.status_code == 403, agent_roles


def test_endpoint_no_user_session_is_judged_on_its_live_context():
    """An agent-scope session token names no person: the answer follows the
    role its registered SecurityContext carries, and no live context is
    refused."""
    from fastapi.testclient import TestClient

    from auth import roles
    from auth.providers import UserContext
    from core.session import session_state

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    p1, p2, p3 = _endpoint_patches(rows)
    user = UserContext(sub="session:sid-task", email="", name="", role=roles.SERVICE,
                       is_api_key=True, session_id="sid-task", agent="agent")
    client = TestClient(_ssh_hosts_app(user))
    with p1, p2, p3:
        assert client.get("/v1/agents/agent/ssh-hosts").status_code == 403
    session_state.set_session_security("sid-task", _ctx("manager", username=""))
    try:
        with p1, p2, p3:
            assert client.get("/v1/agents/agent/ssh-hosts").status_code == 200
        session_state.set_session_security("sid-task", _ctx("contributor", username=""))
        with p1, p2, p3:
            assert client.get("/v1/agents/agent/ssh-hosts").status_code == 403
    finally:
        session_state._session_security.pop("sid-task", None)


def test_endpoint_judges_a_persons_session_on_its_live_context_too():
    """A person's session lists the hosts only when the session itself takes
    the keys: a live context that is neither a check's judge (read-only) nor
    an external caller's, beside the person's live editor role. An editor's
    ordinary session still lists."""
    from fastapi.testclient import TestClient

    from core.session import session_state

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    p1, p2, p3 = _endpoint_patches(rows)
    client = TestClient(_ssh_hosts_app(_session_user()))
    try:
        session_state.set_session_security("sid-1", _ctx("editor"))
        with p1, p2, p3:
            assert client.get("/v1/agents/agent/ssh-hosts").status_code == 200
        for ctx in (_ctx("editor", read_only=True), _ctx("manager", read_only=True),
                    _ctx("editor", principal="external")):
            session_state.set_session_security("sid-1", ctx)
            with p1, p2, p3:
                assert client.get("/v1/agents/agent/ssh-hosts").status_code == 403, ctx
        session_state._session_security.pop("sid-1", None)
        with p1, p2, p3:
            assert client.get("/v1/agents/agent/ssh-hosts").status_code == 403  # none live
        # The live role still rules: an editor context of a person demoted since.
        session_state.set_session_security("sid-1", _ctx("editor"))
        demoted = TestClient(_ssh_hosts_app(_session_user(agent_roles={"agent": "viewer"})))
        with p1, p2, p3:
            assert demoted.get("/v1/agents/agent/ssh-hosts").status_code == 403
    finally:
        session_state._session_security.pop("sid-1", None)


def test_endpoint_refuses_an_agent_scope_judge():
    """An agent-scope check's judge carries a manager context marked
    read-only: it is not handed the keys, so it does not list the hosts."""
    from fastapi.testclient import TestClient

    from auth import roles
    from auth.providers import UserContext
    from core.session import session_state

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    p1, p2, p3 = _endpoint_patches(rows)
    user = UserContext(sub="session:sid-judge", email="", name="", role=roles.SERVICE,
                       is_api_key=True, session_id="sid-judge", agent="agent")
    client = TestClient(_ssh_hosts_app(user))
    session_state.set_session_security("sid-judge", _ctx("manager", username="", read_only=True))
    try:
        with p1, p2, p3:
            assert client.get("/v1/agents/agent/ssh-hosts").status_code == 403
    finally:
        session_state._session_security.pop("sid-judge", None)


def test_clear_removes_a_leftover_key_dir(tmp_path, monkeypatch):
    """A person demoted below editor keeps nothing from an earlier session:
    the config dir's ``ssh`` is removed (a planted link is unlinked, never
    followed)."""
    from core.sandbox.session_config_dir import clear_ssh_keys_for_sandbox
    cfg = _agent_cfg(tmp_path, monkeypatch)
    (cfg / "ssh").mkdir(parents=True)
    (cfg / "ssh" / "k").write_text("PRIVATE")
    clear_ssh_keys_for_sandbox(cfg, agent_name="agent")
    assert not (cfg / "ssh").exists()
    clear_ssh_keys_for_sandbox(cfg, agent_name="agent")  # idempotent
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (cfg / "ssh").symlink_to(outside)
    clear_ssh_keys_for_sandbox(cfg, agent_name="agent")
    assert not (cfg / "ssh").is_symlink() and (outside / "keep").exists()


_PROD_PUB = "ssh-ed25519 AAAA prod\n"


def test_a_link_at_the_key_dir_is_replaced_and_its_target_gets_no_key(tmp_path, monkeypatch):
    import os

    from core.sandbox import session_config_dir as scd
    cfg = _agent_cfg(tmp_path, monkeypatch)
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir(mode=0o755)
    os.chmod(stand_in, 0o755)
    (cfg / "ssh").symlink_to(stand_in)
    assert scd.materialize_ssh_public_keys({"prod": _PROD_PUB}, cfg, agent_name="agent") is True
    assert (cfg / "ssh").is_dir() and not (cfg / "ssh").is_symlink()
    assert (cfg / "ssh" / "prod.pub").read_text() == _PROD_PUB
    assert list(stand_in.iterdir()) == [] and stand_in.stat().st_mode & 0o777 == 0o755


def test_a_folder_link_above_the_key_dir_refuses_the_keys(tmp_path, monkeypatch):
    import os
    import shutil

    from core.sandbox import session_config_dir as scd
    cfg = _agent_cfg(tmp_path, monkeypatch)
    stand_in = tmp_path / "stand-in"
    (stand_in / ".claude").mkdir(parents=True)
    os.chmod(stand_in / ".claude", 0o755)
    shutil.rmtree(cfg.parent)
    cfg.parent.symlink_to(stand_in)
    with pytest.raises(OSError):
        scd.materialize_ssh_public_keys({"prod": _PROD_PUB}, cfg, agent_name="agent")
    assert list((stand_in / ".claude").iterdir()) == []
    assert (stand_in / ".claude").stat().st_mode & 0o777 == 0o755


def test_a_file_at_the_key_dir_through_a_folder_link_above_is_kept(tmp_path, monkeypatch):
    import shutil

    from core.sandbox import session_config_dir as scd
    cfg = _agent_cfg(tmp_path, monkeypatch)
    stand_in = tmp_path / "stand-in"
    (stand_in / ".claude").mkdir(parents=True)
    (stand_in / ".claude" / "ssh").write_text("stand-in")
    shutil.rmtree(cfg.parent)
    cfg.parent.symlink_to(stand_in)
    with pytest.raises(OSError):
        scd.clear_ssh_keys_for_sandbox(cfg, agent_name="agent")
    assert (stand_in / ".claude" / "ssh").read_text() == "stand-in"


def test_the_key_dir_swapped_for_a_link_above_between_the_two_removals(tmp_path, monkeypatch):
    """The name held a folder at the first removal and a link sits above it at
    the second: the folder the link names keeps its files."""
    import shutil

    from core.sandbox import session_config_dir as scd
    from services.infra import safe_fs
    cfg = _agent_cfg(tmp_path, monkeypatch)
    (cfg / "ssh").mkdir()
    stand_in = tmp_path / "stand-in"
    (stand_in / ".claude" / "ssh").mkdir(parents=True)
    (stand_in / ".claude" / "ssh" / "old").write_text("stand-in")
    real_unlink = safe_fs.unlink_beneath

    def unlink(*a, **kw):
        try:
            return real_unlink(*a, **kw)
        except IsADirectoryError:
            shutil.rmtree(cfg.parent)
            cfg.parent.symlink_to(stand_in)
            raise

    monkeypatch.setattr(safe_fs, "unlink_beneath", unlink)
    with pytest.raises(OSError):
        scd.clear_ssh_keys_for_sandbox(cfg, agent_name="agent")
    assert (stand_in / ".claude" / "ssh" / "old").read_text() == "stand-in"


def test_the_key_dir_swapped_for_a_link_after_the_make_gets_no_key(tmp_path, monkeypatch):
    """The folder is made, then made a link before the first key: every key
    is written beneath the root, so none lands where the link points."""
    import shutil

    from core.sandbox import session_config_dir as scd
    from services.infra import safe_fs
    cfg = _agent_cfg(tmp_path, monkeypatch)
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    real_open = safe_fs.open_dir_beneath

    def open_then_swap(*a, **kw):
        fd = real_open(*a, **kw)
        shutil.rmtree(cfg / "ssh")
        (cfg / "ssh").symlink_to(stand_in)
        return fd

    monkeypatch.setattr(safe_fs, "open_dir_beneath", open_then_swap)
    with pytest.raises(OSError):
        scd.materialize_ssh_public_keys({"prod": _PROD_PUB}, cfg, agent_name="agent")
    assert list(stand_in.iterdir()) == []


def test_a_key_dir_made_meanwhile_with_another_mode_is_made_owner_only(tmp_path, monkeypatch):
    import os

    from core.sandbox import session_config_dir as scd
    from services.infra import safe_fs
    cfg = _agent_cfg(tmp_path, monkeypatch)
    real_make = safe_fs.mkdirs_beneath

    def make(*a, **kw):
        (cfg / "ssh").mkdir()
        os.chmod(cfg / "ssh", 0o755)
        return real_make(*a, **kw)

    monkeypatch.setattr(safe_fs, "mkdirs_beneath", make)
    assert scd.materialize_ssh_public_keys({"prod": _PROD_PUB}, cfg, agent_name="agent") is True
    assert (cfg / "ssh").stat().st_mode & 0o777 == 0o700


def test_two_starts_of_one_scope_interleaved_at_the_make_both_pass(tmp_path, monkeypatch):
    """Start A removed the folder and is about to make it when start B runs
    whole: A's make finds B's folder and goes on."""
    from core.sandbox import session_config_dir as scd
    from services.infra import safe_fs
    cfg = _agent_cfg(tmp_path, monkeypatch)
    real_make = safe_fs.mkdirs_beneath
    answers = []

    def make(*a, **kw):
        monkeypatch.setattr(safe_fs, "mkdirs_beneath", real_make)
        answers.append(scd.materialize_ssh_public_keys({"b": "ssh-ed25519 AAAA b\n"}, cfg, agent_name="agent"))
        return real_make(*a, **kw)

    monkeypatch.setattr(safe_fs, "mkdirs_beneath", make)
    answers.append(scd.materialize_ssh_public_keys({"a": "ssh-ed25519 AAAA a\n"}, cfg, agent_name="agent"))
    assert answers == [True, True]
    assert (cfg / "ssh" / "a.pub").read_text() == "ssh-ed25519 AAAA a\n"


# ---------------------------------------------------------------------------
# One rule at spawn: keys for the editor tier, a cleared dir below it, an
# untouched dir for a judge and an external caller
# ---------------------------------------------------------------------------

def _keyed_config_dir(tmp_path, monkeypatch):
    """A session config dir whose ``ssh`` an earlier start left holding the
    public half of a key ``k``."""
    cfg = _agent_cfg(tmp_path, monkeypatch)
    (cfg / "ssh").mkdir(parents=True)
    (cfg / "ssh" / "k.pub").write_text("ssh-ed25519 AAAA k\n")
    return cfg


@pytest.mark.parametrize("role", ["viewer", "contributor"])
@pytest.mark.asyncio
async def test_below_editor_spawn_clears_the_leftover_key_dir(tmp_path, monkeypatch, role):
    from core.sandbox.session_config_dir import provision_ssh_keys_for_sandbox
    cfg = _keyed_config_dir(tmp_path, monkeypatch)
    assert await provision_ssh_keys_for_sandbox(_ctx(role), "agent", cfg, "/users/pm/.claude") == ""
    assert not (cfg / "ssh").exists()


@pytest.mark.asyncio
async def test_an_editor_spawn_gets_the_public_halves_alone(tmp_path, monkeypatch):
    """The proxy signs: the dir holds the public halves alone, the key an
    earlier start left and a private file an earlier release wrote both gone."""
    from core.credentials import ssh_signer
    from core.sandbox import session_config_dir as scd
    monkeypatch.setattr(ssh_signer, "public_lines", lambda agent: {"prod": _PROD_PUB})
    cfg = _keyed_config_dir(tmp_path, monkeypatch)
    (cfg / "ssh" / "old_key").write_text("PRIVATE")
    out = await scd.provision_ssh_keys_for_sandbox(_ctx("editor"), "agent", cfg, "/users/pm/.claude")
    assert out == "/users/pm/.claude/ssh"
    assert sorted(p.name for p in (cfg / "ssh").iterdir()) == ["prod.pub"]
    assert (cfg / "ssh" / "prod.pub").read_text() == _PROD_PUB
    # Below the editor tier the leftover dir goes.
    assert await scd.provision_ssh_keys_for_sandbox(_ctx("viewer"), "agent", cfg, "/users/pm/.claude") == ""
    assert not (cfg / "ssh").exists()
    # No key authorised: nothing to point at, and the stale dir is gone.
    cfg = _keyed_config_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(ssh_signer, "public_lines", lambda agent: {})
    assert await scd.provision_ssh_keys_for_sandbox(_ctx("manager"), "agent", cfg, "/users/pm/.claude") == ""
    assert not (cfg / "ssh").exists()


@pytest.mark.asyncio
async def test_the_starts_of_one_scope_share_the_dir_and_none_fails_on_another(tmp_path, monkeypatch):
    """Each wipes and rebuilds the one dir. In threads, one would remove what
    another is writing, so the rebuild of the public halves stays on the loop."""
    from core.credentials import ssh_signer
    from core.sandbox import session_config_dir as scd
    cfg = _agent_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(ssh_signer, "public_lines",
                        lambda agent: {name: f"ssh-ed25519 AAAA {name}\n" for name in ("k1", "k2", "k3")})
    real_write = scd.write_beneath_agent
    writers = set()

    def write(*a, **kw):
        writers.add(threading.current_thread())
        return real_write(*a, **kw)

    monkeypatch.setattr(scd, "write_beneath_agent", write)
    answers = await asyncio.gather(*(
        scd.provision_ssh_keys_for_sandbox(_ctx("editor"), "agent", cfg, "/users/pm/.claude")
        for _ in range(6)))
    assert answers == ["/users/pm/.claude/ssh"] * 6
    assert sorted(p.name for p in (cfg / "ssh").iterdir()) == ["k1.pub", "k2.pub", "k3.pub"]
    assert writers == {threading.current_thread()}


@pytest.mark.asyncio
async def test_a_judge_and_an_external_caller_leave_the_dir_alone(tmp_path, monkeypatch):
    from core.sandbox.session_config_dir import provision_ssh_keys_for_sandbox
    cfg = _keyed_config_dir(tmp_path, monkeypatch)
    judge = _ctx("manager", read_only=True)
    assert await provision_ssh_keys_for_sandbox(judge, "agent", cfg, "/users/pm/.claude") == ""
    external = _ctx("viewer", username="", principal="external", external_claim="phone:+30210")
    assert await provision_ssh_keys_for_sandbox(external, "agent", cfg, "/workspace/.claude") == ""
    assert await provision_ssh_keys_for_sandbox(None, "agent", cfg, "/workspace/.claude") == ""
    assert (cfg / "ssh" / "k.pub").read_text() == "ssh-ed25519 AAAA k\n"


def test_both_local_layers_route_through_the_one_rule():
    from pathlib import Path

    from tests._paths import PROXY_DIR
    for rel in ("core/layers/cli/layer.py", "core/layers/codex/spawn.py"):
        src = (Path(PROXY_DIR) / rel).read_text()
        assert "provision_ssh_keys_for_sandbox(" in src, rel
        assert "materialize_ssh_public_keys" not in src, rel


# ---------------------------------------------------------------------------
# The proxy signs: the line names the public half, the machine forwards
# ---------------------------------------------------------------------------


def test_the_command_names_the_public_file_and_the_block_says_who_signs():
    from services.mcp.dynamic_context import _ssh_hosts_context, format_ssh_host_command
    fv = {"host": "10.0.0.5", "username": "root", "key_name": "prod_key"}
    assert format_ssh_host_command(fv, mux=False) == (
        'ssh -i "$OTO_SSH_KEY_DIR/prod_key.pub" -o IdentitiesOnly=yes '
        "-o StrictHostKeyChecking=accept-new -p 22 root@10.0.0.5")
    # A host without a key names none.
    assert format_ssh_host_command({"host": "h"}, mux=False) == "ssh -o StrictHostKeyChecking=accept-new -p 22 h"
    rows = _instances({"name": "prod", "host": "10.0.0.5", "username": "root", "key_name": "prod_key"})
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        text = _ssh_hosts_context("agent", user_role="manager")
    assert "The platform holds the keys and signs for you" in text
    assert "$SSH_AUTH_SOCK" in text and "mode 0600" not in text
    assert '-i "$OTO_SSH_KEY_DIR/prod_key.pub" -o IdentitiesOnly=yes' in text


def _paired(version: str, os: str = ""):
    return placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m",
                                           satellite_version=version, os=os)


# ---------------------------------------------------------------------------
# The rule is the editor tier everywhere: a person's own machine included
# (its satellite forwards the signatures the same way)
# ---------------------------------------------------------------------------


def test_a_users_own_machine_gets_the_tool(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    user_paired = placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m",
                                                  satellite_version="0.5.173")
    _stub_assembly(monkeypatch, [_admin_paired_only_manifest(), _context_only_manifest("notes")],
                   env_by_mcp={"ssh-hosts": {}, "notes": {}}, tmp_path=tmp_path)
    _path, _env, left_out, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=user_paired)
    assert "ssh-hosts" not in left_out and "notes" not in left_out


def test_the_block_renders_on_a_users_own_machine():
    from services.mcp.dynamic_context import _ssh_hosts_context
    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u", "key_name": "k"})
    own = placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m",
                                          satellite_version="0.5.173")
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        text = _ssh_hosts_context("agent", placement=own, user_role="editor")
        assert text and '-i "$OTO_SSH_KEY_DIR/k.pub" -o IdentitiesOnly=yes' in text
        # The tier holds everywhere: a viewer on their own machine gets no block.
        assert _ssh_hosts_context("agent", placement=own, user_role="viewer") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("version, sent", [("0.5.196", True)])
async def test_the_public_keys_ride_to_a_windows_satellite_that_opens_the_pipe(monkeypatch, version, sent):
    """The start payload gives a Windows machine the public halves: every
    satellite at the platform's floor opens the pipe."""
    from core.credentials import ssh_signer
    from core.execution_layer import AgentConfig
    from core.remote.remote_start_payload import ssh_agent_payload
    monkeypatch.setattr(ssh_signer, "public_lines", lambda agent: {"prod": "ssh-ed25519 AAAA prod\n"})
    cfg = AgentConfig(agent_name="agent", user_sub="sub-1", security_context=SimpleNamespace(
        session_scope="user", username="alice", role="manager", placement=_paired(version, os="windows")))
    expected = {"public_keys": {"prod": "ssh-ed25519 AAAA prod\n"}} if sent else None
    assert await ssh_agent_payload(cfg) == expected


def test_a_windows_line_runs_windows_own_ssh():
    """A Windows machine's agent is a named pipe that only Windows' own ssh
    reads, and Git Bash's ``ssh`` is Git's own: the line runs ``$OTO_SSH``
    (the satellite's), in the block and through the endpoint's
    ``target_os=windows``."""
    from services.mcp.dynamic_context import _ssh_hosts_context, format_ssh_host_command
    fv = {"host": "10.0.0.5", "username": "root", "port": "22", "key_name": "prod_key"}
    assert format_ssh_host_command(fv, mux=False, pipe_agent=True) == (
        '"${OTO_SSH:-ssh}" -i "$OTO_SSH_KEY_DIR/prod_key.pub" -o IdentitiesOnly=yes '
        "-o StrictHostKeyChecking=accept-new -p 22 root@10.0.0.5")
    assert format_ssh_host_command(fv, mux=False).startswith("ssh -i ")
    rows = _instances({"name": "prod", "host": "10.0.0.5", "username": "root", "key_name": "prod_key"})
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        windows = _ssh_hosts_context("agent", placement=_paired("0.5.194", os="windows"), user_role="manager")
        linux = _ssh_hosts_context("agent", placement=_paired("0.5.194", os="linux"), user_role="manager")
    assert '`"${OTO_SSH:-ssh}" -i "$OTO_SSH_KEY_DIR/prod_key.pub"' in windows
    assert 'scp -S "$OTO_SSH"' in windows
    assert "OTO_SSH" not in linux.replace("OTO_SSH_KEY_DIR", "")


def test_endpoint_target_os_windows_runs_windows_own_ssh():
    from fastapi.testclient import TestClient

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u", "key_name": "k"})
    p1, p2, p3 = _endpoint_patches(rows)
    client = TestClient(_ssh_hosts_app(_session_user()))
    with p1, p2, p3, _live_session():
        windows = client.get("/v1/agents/agent/ssh-hosts?target_os=windows")
        linux = client.get("/v1/agents/agent/ssh-hosts?target_os=linux")
    assert windows.json()["hosts"][0]["command"].startswith('"${OTO_SSH:-ssh}" -i ')
    assert 'scp -S "$OTO_SSH"' in windows.json()["note"]
    assert linux.json()["hosts"][0]["command"].startswith("ssh -i ") and "note" not in linux.json()


@pytest.mark.parametrize("note", ["", "On this Windows computer copy files with `scp -S \"$OTO_SSH\"`."])
def test_the_list_ssh_hosts_answer_carries_the_platforms_note(monkeypatch, note):
    """The tool prints the platform's note for a Windows machine (one source,
    ``dynamic_providers.PIPE_AGENT_NOTE``) and its plain words without one."""
    from tests._paths import CUSTOM_MCPS, load_mcp_server
    monkeypatch.setenv("OTO_AGENT_NAME", "agent")
    mod = load_mcp_server(CUSTOM_MCPS / "ssh-hosts")
    data = {"hosts": [{"name": "x", "host": "h", "port": "22", "username": "u", "key_name": "k",
                       "command": '"${OTO_SSH:-ssh}" -i "$OTO_SSH_KEY_DIR/k.pub"'}]}
    if note:
        data["note"] = note

    async def fake_get(path, params):
        return data

    monkeypatch.setattr(mod, "_get", fake_get)
    text = asyncio.run(mod._handle_list_ssh_hosts({}))
    if note:
        assert f"Run it as given. {note}" in text and "plain `ssh`" not in text
    else:
        assert "use it as given with plain `ssh`/`scp`/`rsync`" in text
