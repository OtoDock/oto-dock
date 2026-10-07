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

import contextlib
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from tests.mcp.test_mcp_broker_activation import (  # noqa: E402
    _FakeManifest, _stub_assembly,
)
from core import placement  # noqa: E402

_ADMIN_PAIRED = placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m")
_USER_PAIRED = placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m")


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
    # Server + keys + prompt block exist only where key material is
    # delivered: locally and on admin-paired satellites.
    assert m.remote_policy == "admin_paired_only"
    assert m.assignment_mode == "explicit"
    assert m.instances and m.instances.delivery == "none"
    assert {f.key for f in m.instances.fields} == {
        "name", "host", "port", "username", "key_name",
    }
    assert m.data_dirs.get("keys") == "keys/"
    assert m.network_targets and m.network_targets[0].source == "instance"


# ---------------------------------------------------------------------------
# build_session_mcp_config — no server entry; excluded on remote
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


def test_context_only_mcp_excluded_on_remote(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_context_only_manifest()], env_by_mcp={}, tmp_path=tmp_path,
    )

    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=_USER_PAIRED,
    )

    # Default (no target_admin_paired) fails closed — user-paired and unknown
    # targets never get key material; the admin-paired allow case lives in
    # test_session_file_broker.py.
    assert "ssh-hosts" in excluded
    assert "admin-paired" in excluded["ssh-hosts"]


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
    assert ('`ssh -i "$OTO_SSH_KEY_DIR/prod_key" '
            f"-o StrictHostKeyChecking=accept-new {_MUX} "
            "-p 2222 root@10.0.0.5`") in text
    # No key / no port / no name → keyless line, default port, host as label.
    assert ("**backup.lan** — `ssh -o StrictHostKeyChecking=accept-new "
            f"{_MUX} -p 22 oto@backup.lan`") in text


def test_provider_mux_gated_by_target_os():
    """Remote targets: mux only where the satellite reported linux/darwin —
    Windows OpenSSH has no unix-socket ControlMaster, and an unknown OS
    (pre-capability satellite) stays conservative."""
    from services.mcp.dynamic_context import _ssh_hosts_context

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        linux = _ssh_hosts_context(
            "agent", placement=dataclasses.replace(_ADMIN_PAIRED, os="linux"),
            user_role="manager")
        windows = _ssh_hosts_context(
            "agent", placement=dataclasses.replace(_ADMIN_PAIRED, os="windows"),
            user_role="manager")
        unknown = _ssh_hosts_context("agent", placement=_ADMIN_PAIRED, user_role="manager")

    assert "ControlMaster=auto" in linux
    assert "ControlMaster" not in windows
    assert "-o StrictHostKeyChecking=accept-new -p 22 u@10.0.0.5" in windows
    assert "ControlMaster" not in unknown


def test_provider_silent_when_remote_or_unauthorized():
    from services.mcp.dynamic_context import _ssh_hosts_context

    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=[]):
        assert _ssh_hosts_context("agent", user_role="manager") is None
    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    with patch("storage.mcp.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        assert _ssh_hosts_context("agent", placement=_USER_PAIRED, user_role="manager") is None


# ---------------------------------------------------------------------------
# Per-session key materialization
# ---------------------------------------------------------------------------


def _materialize_env(tmp_path, *, assigned=True, instances=None):
    """Patch registry + store around materialize_ssh_keys_for_sandbox."""
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


def test_materializer_copies_only_authorized_keys(tmp_path):
    from core.sandbox.session_config_dir import materialize_ssh_keys_for_sandbox

    rows = _instances({"host": "h", "key_name": "good_key"})
    mcp_dir, p1, p2, p3 = _materialize_env(tmp_path, instances=rows)
    (mcp_dir / "keys" / "good_key").write_text("PRIVATE")
    (mcp_dir / "keys" / "other_key").write_text("PRIVATE2")
    cfg_dir = tmp_path / ".claude"
    cfg_dir.mkdir()
    # A stale key from a previous session must be wiped.
    (cfg_dir / "ssh").mkdir()
    (cfg_dir / "ssh" / "stale_key").write_text("OLD")

    with p1, p2, p3:
        assert materialize_ssh_keys_for_sandbox("agent", cfg_dir) is True

    dst = cfg_dir / "ssh"
    assert (dst / "good_key").read_text() == "PRIVATE"
    assert not (dst / "other_key").exists()
    assert not (dst / "stale_key").exists()
    assert (dst / "good_key").stat().st_mode & 0o777 == 0o600
    assert dst.stat().st_mode & 0o777 == 0o700


def test_materializer_refuses_traversal_key_names(tmp_path):
    from core.sandbox.session_config_dir import materialize_ssh_keys_for_sandbox

    secret = tmp_path / "outside_secret"
    secret.write_text("LEAK")
    rows = _instances({"host": "h", "key_name": "../../outside_secret"})
    _mcp_dir, p1, p2, p3 = _materialize_env(tmp_path, instances=rows)
    cfg_dir = tmp_path / ".claude"
    cfg_dir.mkdir()

    with p1, p2, p3:
        assert materialize_ssh_keys_for_sandbox("agent", cfg_dir) is False
    assert not (cfg_dir / "ssh").exists()


def test_materializer_noop_for_unassigned_agent(tmp_path):
    from core.sandbox.session_config_dir import materialize_ssh_keys_for_sandbox

    rows = _instances({"host": "h", "key_name": "k"})
    mcp_dir, p1, p2, p3 = _materialize_env(tmp_path, assigned=False, instances=rows)
    (mcp_dir / "keys" / "k").write_text("PRIVATE")
    cfg_dir = tmp_path / ".claude"
    cfg_dir.mkdir()

    with p1, p2, p3:
        assert materialize_ssh_keys_for_sandbox("agent", cfg_dir) is False
    assert not (cfg_dir / "ssh").exists()


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
# remote_policy = "admin_paired_only" — server entry follows the key material
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
        "agent", None, placement=_ADMIN_PAIRED,
    )
    assert "ssh-hosts" not in excluded


def test_admin_paired_only_excluded_on_user_paired_remote(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_admin_paired_only_manifest()],
        env_by_mcp={"ssh-hosts": {}}, tmp_path=tmp_path,
    )

    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, placement=_USER_PAIRED,
    )
    assert "ssh-hosts" in excluded
    assert "admin-paired" in excluded["ssh-hosts"]


# ---------------------------------------------------------------------------
# Shared command renderer (prompt block + endpoint must never drift)
# ---------------------------------------------------------------------------


def test_format_ssh_host_command():
    from services.mcp.dynamic_context import format_ssh_host_command

    fv = {"name": "prod", "host": "10.0.0.5", "port": "2222",
          "username": "root", "key_name": "prod_key"}
    with_mux = format_ssh_host_command(fv, mux=True)
    assert with_mux == (
        'ssh -i "$OTO_SSH_KEY_DIR/prod_key" -o StrictHostKeyChecking=accept-new'
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
    assert h["command"].startswith('ssh -i "$OTO_SSH_KEY_DIR/prod_key"')
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

import pytest  # noqa: E402


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


def test_clear_removes_a_leftover_key_dir(tmp_path):
    """A person demoted below editor keeps nothing from an earlier session:
    the config dir's ``ssh`` is removed (a planted link is unlinked, never
    followed)."""
    from core.sandbox.session_config_dir import clear_ssh_keys_for_sandbox
    cfg = tmp_path / ".claude"
    (cfg / "ssh").mkdir(parents=True)
    (cfg / "ssh" / "k").write_text("PRIVATE")
    clear_ssh_keys_for_sandbox(cfg)
    assert not (cfg / "ssh").exists()
    clear_ssh_keys_for_sandbox(cfg)  # idempotent
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (cfg / "ssh").symlink_to(outside)
    clear_ssh_keys_for_sandbox(cfg)
    assert not (cfg / "ssh").is_symlink() and (outside / "keep").exists()


# ---------------------------------------------------------------------------
# One rule at spawn: keys for the editor tier, a cleared dir below it, an
# untouched dir for a judge and an external caller
# ---------------------------------------------------------------------------

def _keyed_config_dir(tmp_path):
    cfg = tmp_path / ".claude"
    (cfg / "ssh").mkdir(parents=True)
    (cfg / "ssh" / "k").write_text("PRIVATE")
    return cfg


@pytest.mark.parametrize("role", ["viewer", "contributor"])
def test_below_editor_spawn_clears_the_leftover_key_dir(tmp_path, role):
    from core.sandbox.session_config_dir import provision_ssh_keys_for_sandbox
    cfg = _keyed_config_dir(tmp_path)
    assert provision_ssh_keys_for_sandbox(_ctx(role), "agent", cfg, "/users/pm/.claude") == ""
    assert not (cfg / "ssh").exists()


def test_editor_spawn_materialises_the_keys(tmp_path, monkeypatch):
    from core.sandbox import session_config_dir as scd
    cfg = _keyed_config_dir(tmp_path)
    src = tmp_path / "keys" / "prod"
    src.parent.mkdir()
    src.write_text("NEW")
    monkeypatch.setattr(scd, "collect_authorized_ssh_keys", lambda agent: {"prod": src})
    out = scd.provision_ssh_keys_for_sandbox(_ctx("editor"), "agent", cfg, "/users/pm/.claude")
    assert out == "/users/pm/.claude/ssh"
    assert (cfg / "ssh" / "prod").read_text() == "NEW" and not (cfg / "ssh" / "k").exists()
    # No key authorised: nothing to point at, and the stale dir is gone.
    monkeypatch.setattr(scd, "collect_authorized_ssh_keys", lambda agent: {})
    assert scd.provision_ssh_keys_for_sandbox(_ctx("manager"), "agent", cfg, "/users/pm/.claude") == ""
    assert not (cfg / "ssh").exists()


def test_a_judge_and_an_external_caller_leave_the_dir_alone(tmp_path):
    from core.sandbox.session_config_dir import provision_ssh_keys_for_sandbox
    cfg = _keyed_config_dir(tmp_path)
    judge = _ctx("manager", read_only=True)
    assert provision_ssh_keys_for_sandbox(judge, "agent", cfg, "/users/pm/.claude") == ""
    external = _ctx("viewer", username="", principal="external", external_claim="phone:+30210")
    assert provision_ssh_keys_for_sandbox(external, "agent", cfg, "/workspace/.claude") == ""
    assert provision_ssh_keys_for_sandbox(None, "agent", cfg, "/workspace/.claude") == ""
    assert (cfg / "ssh" / "k").read_text() == "PRIVATE"


def test_both_local_layers_route_through_the_one_rule():
    from pathlib import Path
    from tests._paths import PROXY_DIR
    for rel in ("core/layers/cli/layer.py", "core/layers/codex/layer.py"):
        src = (Path(PROXY_DIR) / rel).read_text()
        assert "provision_ssh_keys_for_sandbox(" in src, rel
        assert "materialize_ssh_keys_for_sandbox" not in src, rel
