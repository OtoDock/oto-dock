"""A compute placement at every gate that reads a placement fact.

A compute machine is the platform's own: its session's process is on the
machine, and everything else is the platform sandbox's (``core/placement.py``
``_FACTS``). Each gate here once read ``is_remote`` for its own fact; for a
compute session it must answer as the sandbox does, and an admin's machine
shows that the gate's answer does depend on the fact.
"""

from __future__ import annotations

import dataclasses

import pytest

from auth.path_policy import SecurityContext, check_tool_access
from core import placement

LOCAL = placement.PlacementCapabilities()
# A compute placement given the row facts an admin machine would carry: the
# gates must not read them, since its sessions see sandbox paths.
COMPUTE = placement.PlacementCapabilities(
    kind=placement.KIND_COMPUTE, machine_id="c-1", label="Compute one",
    home_dir="/home/sat", agents_dir="/home/sat/.oto-dock/agents", allow_full_fs=True,
    device_grants={"computer", "browser"}, has_display=True, os="linux")
ADMIN = placement.PlacementCapabilities(
    kind=placement.KIND_ADMIN_REMOTE, machine_id="m-1", label="Office",
    home_dir="/home/sat", agents_dir="/home/sat/.oto-dock/agents", allow_full_fs=True,
    device_grants={"computer", "browser"}, has_display=True, os="linux")


def _ctx(pl, role="editor", **kw) -> SecurityContext:
    return SecurityContext(role=role, username="alice", agent="personal-assistant",
                           is_admin_agent=False, placement=pl, **kw)


def _allowed(tool: str, args: dict, pl, **kw) -> bool:
    decision, _ = check_tool_access(tool, args, _ctx(pl, **kw))
    return decision.allowed


@pytest.mark.parametrize("pl, allowed", [(LOCAL, False), (COMPUTE, False), (ADMIN, True)])
def test_a_host_path_is_outside_a_sandboxed_session(pl, allowed):
    """A compute session sees sandbox paths: a path of the machine's own disk
    is outside its tree, whatever full access its row would have granted."""
    assert _allowed("Read", {"file_path": "/home/sat/notes.txt"}, pl) is allowed


@pytest.mark.parametrize("pl, allowed", [(LOCAL, False), (COMPUTE, False), (ADMIN, True)])
def test_host_commands_need_an_admin_inside_the_sandbox(pl, allowed):
    """The shell's admin tier: an editor runs ``docker`` on an admin's
    machine, never inside the platform's sandbox."""
    assert _allowed("Bash", {"command": "docker ps"}, pl) is allowed


@pytest.mark.parametrize("pl, asks", [(LOCAL, False), (COMPUTE, False), (ADMIN, True)])
def test_host_state_patterns_ask_only_on_a_machine_with_no_sandbox(pl, asks):
    from auth import path_shell
    assert path_shell._host_state_asks(_ctx(pl)) is asks


def test_an_outside_callers_web_reads_follow_the_sandbox():
    """On a machine with no sandbox an outside caller gets no WebFetch (no
    network namespace guards its private ranges); the sandbox has one."""
    from auth import path_shell
    kw = {"principal": "external", "external_channel": "phone", "external_id": "+30210",
          "external_claim": "phone:+30210"}
    remote_reason = "not available on external routes on a remote machine"
    assert remote_reason in path_shell._check_webfetch("https://example.com/", _ctx(ADMIN, role="viewer", **kw)).reason
    for pl in (LOCAL, COMPUTE):
        d = path_shell._check_webfetch("https://example.com/", _ctx(pl, role="viewer", **kw))
        assert remote_reason not in (d.reason or "")


def test_the_prompt_tells_a_sandboxed_session_its_home_and_tmp_are_temporary():
    from auth.path_prompt import build_permission_context
    text = {name: build_permission_context(_ctx(pl), execution_path="claude-code-cli")
            for name, pl in (("local", LOCAL), ("compute", COMPUTE), ("admin", ADMIN))}
    tmp_line = "`/tmp` is yours"
    assert tmp_line in text["local"] and tmp_line in text["compute"]
    assert tmp_line not in text["admin"]
    host_line = "because this is a remote satellite"
    assert host_line in text["admin"] and host_line not in text["compute"]


def test_no_device_tool_is_offered_on_a_compute_machine():
    from services.mcp.mcp_manifest_store import _device_placement_reason

    class _Manifest:
        placement = ""
        device_capability = "computer"
        requires_display = True

    assert _device_placement_reason(_Manifest(), placement=COMPUTE) == "Requires a remote machine (satellite)"
    assert _device_placement_reason(_Manifest(), placement=LOCAL) == "Requires a remote machine (satellite)"
    assert _device_placement_reason(_Manifest(), placement=ADMIN) is None


def test_a_compute_session_reads_no_browser_settings_of_its_row(monkeypatch):
    from storage import remote_store

    def _no_read(*_a, **_k):
        raise AssertionError("a compute placement must not read the machine's browser columns")

    monkeypatch.setattr(remote_store, "get_conn", _no_read)
    got = remote_store.get_target_browser_settings(COMPUTE)
    assert got == remote_store.BrowserTargetSettings()


def test_a_compute_sessions_files_are_never_served_from_a_machine(monkeypatch):
    """A session with no registry entry is served from its machine only when
    that machine holds a synced copy; a compute machine works on the
    platform's folder."""
    from core.remote import remote_file_flow
    from core.session import session_state
    monkeypatch.setitem(session_state._session_security, "s-compute", _ctx(COMPUTE))
    monkeypatch.setitem(session_state._session_security, "s-admin", _ctx(ADMIN))
    assert remote_file_flow._get_remote_session_info("s-compute") is None
    assert remote_file_flow._get_remote_session_info("s-admin") is not None


def test_the_attached_ssh_hosts_open_inside_the_sandbox_only():
    from core.sandbox import session_config_dir

    def _wants(pl) -> bool:
        return session_config_dir.wants_attached_ssh_hosts(_ctx(pl), "ssh box")

    # The opening needs hosts attached to the agent: with none every place
    # answers no, so read the placement's half of the rule.
    import auth.path_shell_hosts as hosts
    orig = hosts.takes_attached_hosts
    try:
        hosts.takes_attached_hosts = lambda _ctx: True
        assert _wants(LOCAL) and _wants(COMPUTE) and not _wants(ADMIN)
    finally:
        hosts.takes_attached_hosts = orig


@pytest.mark.parametrize("refresh, value, read", [
    ("refresh_target_allow_full_fs", True, lambda pl: pl.allow_full_fs),
    ("refresh_target_device_grants", {"computer"}, lambda pl: pl.device_grants),
])
def test_a_live_compute_session_takes_no_machine_setting(monkeypatch, refresh, value, read):
    """A row's flags are pushed into every live session placed on that machine.
    A compute placement never carries them, even if its row's ever change."""
    import dataclasses

    from core.session import session_state
    plain = dataclasses.replace(COMPUTE, allow_full_fs=False, device_grants=set())
    machine = dataclasses.replace(ADMIN, allow_full_fs=False, device_grants=set(), machine_id="c-1")
    monkeypatch.setattr(session_state, "_session_security", {"s-c": _ctx(plain), "s-m": _ctx(machine)})
    monkeypatch.setattr(session_state, "_save_session_security", lambda: None)
    assert getattr(session_state, refresh)("c-1", value) == 1
    assert not read(session_state.get_session_security("s-c").placement)
    assert read(session_state.get_session_security("s-m").placement)


@pytest.mark.asyncio
@pytest.mark.parametrize("pl", [LOCAL, COMPUTE, ADMIN])
async def test_a_wake_resolves_the_platforms_engine_folder_on_every_machine(monkeypatch, pl):
    """The scope's folder names the session's key wherever it runs, as the
    chat builder's does: a paired computer's satellite owns its own copy of
    the config, and its sessions share one account by that folder."""
    from types import SimpleNamespace

    from core.config import task_config_builder
    from core.sandbox import sandbox
    from services.engines import subscription_pool
    from services.scheduler import wake_session
    from storage import remote_store
    from storage.agents import agent_store
    folders: list[str] = []

    def _folder(agent, **kw):
        folders.append(agent)
        return "/tmp/engine-folder"

    async def _security(*_a, **_k):
        return None

    async def _contexts(*_a, **_k):
        return []

    def _no_seat(*_a, **_k):
        raise subscription_pool.NoSubscriptionError("the walk ends here")

    monkeypatch.setattr(remote_store, "placement_of", lambda *_a: pl)
    monkeypatch.setattr(remote_store, "get_target_browser_settings",
                        lambda _p: remote_store.BrowserTargetSettings())
    monkeypatch.setattr(wake_session, "_wake_visibility",
                        lambda *_a: SimpleNamespace(mount_username="alice", mount_scope="user"))
    monkeypatch.setattr(task_config_builder, "build_delivery_security_context", _security)
    monkeypatch.setattr(wake_session, "_wake_delegation_targets", lambda *_a: [])
    monkeypatch.setattr(wake_session, "_wake_mcp_build", lambda *_a, **_k: (None, {}, {}, {}, set()))
    monkeypatch.setattr(wake_session, "_wake_dynamic_contexts", _contexts)
    monkeypatch.setattr(wake_session, "_wake_prompt", lambda *_a, **_k: "")
    monkeypatch.setattr(wake_session, "_wake_credential_env", lambda *_a, **_k: (None, {}, {}, 0))
    monkeypatch.setattr(sandbox, "ensure_persistent_agent_dir", _folder)
    monkeypatch.setattr(agent_store, "get_agent", lambda _a: {})
    monkeypatch.setattr(subscription_pool, "resolve_subscription_env", _no_seat)
    built = await wake_session._wake_built_config(
        "s-1", "agent-a", user_sub="sub-a", role="manager", chat_id="", chat_row=None,
        exec_path="claude-code-cli", target="c-1", username="alice", fresh=False,
        permission_mode="ask", client_type="task", woken=True)
    assert built is None
    assert folders == ["agent-a"]


@pytest.mark.asyncio
async def test_a_wake_on_a_paired_computer_that_makes_homes_names_its_home(monkeypatch):
    """A paired computer keeps its own copy, so the wake resolves no folder
    to spawn from, yet its session runs from a home there: the builder finds
    the scope's folder for it, acquires with the home's key and hands the
    start that home."""
    from types import SimpleNamespace

    from core.config import task_config_builder
    from core.sandbox import sandbox
    from services.engines import subscription_pool
    from services.scheduler import wake_session
    from storage import remote_store
    from storage.agents import agent_store
    sid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    pl = dataclasses.replace(ADMIN, session_homes=frozenset({".claude", ".codex"}))
    acquired: list[str] = []

    async def _security(*_a, **_k):
        return _ctx(pl)

    async def _contexts(*_a, **_k):
        return []

    def _seat(*_a, sticky_scope="", **_k):
        acquired.append(sticky_scope)
        return "sub-1", {}

    monkeypatch.setattr(remote_store, "placement_of", lambda *_a: pl)
    monkeypatch.setattr(remote_store, "get_target_browser_settings",
                        lambda _p: remote_store.BrowserTargetSettings())
    monkeypatch.setattr(wake_session, "_wake_visibility",
                        lambda *_a: SimpleNamespace(mount_username="alice", mount_scope="user"))
    monkeypatch.setattr(task_config_builder, "build_delivery_security_context", _security)
    monkeypatch.setattr(wake_session, "_wake_delegation_targets", lambda *_a: [])
    monkeypatch.setattr(wake_session, "_wake_mcp_build", lambda *_a, **_k: (None, {}, {}, {}, set()))
    monkeypatch.setattr(wake_session, "_wake_dynamic_contexts", _contexts)
    monkeypatch.setattr(wake_session, "_wake_prompt", lambda *_a, **_k: "")
    monkeypatch.setattr(wake_session, "_wake_credential_env", lambda *_a, **_k: (None, {}, {}, 0))
    monkeypatch.setattr(sandbox, "ensure_persistent_agent_dir", lambda agent, **kw: "/tmp/engine-folder")
    monkeypatch.setattr(agent_store, "get_agent", lambda _a: {})
    monkeypatch.setattr(subscription_pool, "resolve_subscription_env", _seat)
    built = await wake_session._wake_built_config(
        sid, "agent-a", user_sub="sub-a", role="manager", chat_id="", chat_row=None,
        exec_path="claude-code-cli", target="m-1", username="alice", fresh=False,
        permission_mode="ask", client_type="task", woken=True)
    assert built is not None and built.sandbox_host_claude_dir == "/tmp/engine-folder"
    assert built.session_home == f"/tmp/engine-folder/own/{sid}"
    assert acquired == [f"m-1:/tmp/engine-folder/own/{sid}"]
    # On a satellite that makes no homes the wake keys by the scope's folder,
    # as the chat's own start does, never by no folder at all.
    monkeypatch.setattr(remote_store, "placement_of", lambda *_a: ADMIN)

    async def _plain_security(*_a, **_k):
        return _ctx(ADMIN)
    monkeypatch.setattr(task_config_builder, "build_delivery_security_context", _plain_security)
    built = await wake_session._wake_built_config(
        sid, "agent-a", user_sub="sub-a", role="manager", chat_id="", chat_row=None,
        exec_path="claude-code-cli", target="m-1", username="alice", fresh=False,
        permission_mode="ask", client_type="task", woken=True)
    assert built.session_home == "" and acquired[-1] == "m-1:/tmp/engine-folder"


@pytest.mark.parametrize("pl, allowed", [(LOCAL, False), (COMPUTE, False), (ADMIN, True)])
def test_a_shell_path_argument_is_read_as_the_sandboxs_or_the_machines(pl, allowed):
    """A path of the machine's own disk in a command: on a compute machine as
    outside the tree as a Read of it, whatever full access its row would hold."""
    assert _allowed("Bash", {"command": "cat /home/sat/notes.txt"}, pl) is allowed


@pytest.mark.parametrize("pl", [LOCAL, COMPUTE])
@pytest.mark.parametrize("tool, args", [("Bash", {"command": "cat /tmp/notes.txt"}),
                                        ("Read", {"file_path": "/tmp/notes.txt"})])
def test_the_private_tmp_is_a_sandboxed_sessions_own(pl, tool, args):
    """A machine's path resolver refuses /tmp as a host path: a sandboxed session
    has a private one, so its shell and its file tools read it as the sandbox's."""
    assert _allowed(tool, args, pl)


@pytest.mark.parametrize("pl, allowed", [(LOCAL, False), (COMPUTE, False), (ADMIN, True)])
def test_powershells_host_commands_need_an_admin_inside_the_sandbox(pl, allowed):
    assert _allowed("PowerShell", {"command": "Restart-Service spooler"}, pl) is allowed


def test_the_prompt_promises_no_host_reach_inside_the_sandbox():
    from auth.path_prompt import build_permission_context
    reach = "satellite host filesystem"
    assert reach in build_permission_context(_ctx(ADMIN), execution_path="claude-code-cli")
    assert reach not in build_permission_context(_ctx(COMPUTE), execution_path="claude-code-cli")


def test_a_docker_tools_output_shows_the_sandbox_path_on_a_compute_machine():
    from services.mcp import mcp_output_relocation
    shown = mcp_output_relocation.session_workspace_shown
    assert shown(_ctx(ADMIN)).startswith("/home/sat/")
    assert shown(_ctx(COMPUTE)) == shown(_ctx(LOCAL)) and not shown(_ctx(COMPUTE)).startswith("/home/sat/")


def test_a_check_reads_a_compute_sessions_files_on_the_platform():
    assert COMPUTE.site == placement.SITE_LOCAL
    assert ADMIN.site == placement.SITE_MACHINE
