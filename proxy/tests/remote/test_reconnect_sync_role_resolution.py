"""Machine-scope sync identity: (username, role) resolves from the PAIRING.

Pins the resolution `resolve_machine_sync_identity` must produce:
  * admin-PAIRED machine  → admin-shared (target_username=None); owned by a
    platform admin → target_role="admin" on every agent;
  * user-paired machine   → the owner's username + the owner's PER-AGENT
    role — for EVERYONE, platform admins included.

The last rule is regression coverage for the prompt-deletion incident: a
platform admin who was per-agent VIEWER used to get target_role="admin" on
their personal machine, granting config/ push + write-back — so a satellite
that lost its copy delete-attributed the agent's prompt at sync time.
`_machine_sync_role` (the session-start path's equivalent) is pinned here
too: same pairing-derived authority, session role only on admin-shared
machines.
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

from core.remote import remote_workspace_sync as rws  # noqa: E402
from core.remote.remote_execution import RemoteExecutionLayer  # noqa: E402


@pytest.fixture(autouse=True)
def _no_jitter_and_a_clean_walk_map(monkeypatch):
    """The reconnect walk sleeps a jitter and coalesces per machine; the
    tests below pin the sleep to zero and start from an empty map."""
    monkeypatch.setattr(rws, "_reconnect_jitter_s", lambda: 0.0)
    rws._reconnect_walks.clear()
    rws._idle_sync_inflight.clear()
    yield
    rws._reconnect_walks.clear()
    rws._idle_sync_inflight.clear()


def _layer_with_capture():
    """A layer whose _initial_workspace_sync just records (agent, username, role)."""
    layer = RemoteExecutionLayer(MagicMock())
    calls = []

    async def _fake_sync(machine_id, agent_slug, *, target_username=None,
                         target_role="", **kw):
        calls.append((agent_slug, target_username, target_role))

    layer._initial_workspace_sync = _fake_sync
    return layer, calls


@pytest.mark.asyncio
async def test_user_paired_resolves_owner_and_per_agent_role():
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "sub-alice", "pairing_scope": "user"}), \
         patch("storage.database.get_user", return_value={"role": "user"}), \
         patch("storage.database.get_username_by_sub", return_value="alice"), \
         patch("storage.database.get_user_agent_roles",
               return_value={"agent-1": "editor", "agent-2": "viewer"}), \
         patch("storage.files.sync_state_store.agents_for_machine",
               return_value={"agent-1", "agent-2"}):
        await layer.sync_all_agents_on_reconnect("m1")
    by_agent = {a: (u, r) for a, u, r in calls}
    assert by_agent["agent-1"] == ("alice", "editor")
    assert by_agent["agent-2"] == ("alice", "viewer")


@pytest.mark.asyncio
async def test_admin_paired_resolves_admin_shared():
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "sub-admin", "pairing_scope": "admin"}), \
         patch("storage.database.get_user", return_value={"role": "admin"}), \
         patch("storage.database.get_username_by_sub", return_value="adminuser"), \
         patch("storage.database.get_user_agent_roles", return_value={}), \
         patch("storage.files.sync_state_store.agents_for_machine", return_value={"agent-1"}):
        await layer.sync_all_agents_on_reconnect("m1")
    # admin-PAIRED → no per-user filter (None); platform-admin owner → role "admin".
    assert calls == [("agent-1", None, "admin")]


@pytest.mark.asyncio
async def test_platform_admin_owner_user_paired_uses_per_agent_role():
    # A platform admin's OWN (user-paired) machine: scoped to them by username,
    # with their PER-AGENT role — the platform role never inflates machine-scope
    # sync authority (the prompt-deletion incident: role "admin" here granted a
    # per-agent VIEWER's machine config/ write-back).
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "sub-admin", "pairing_scope": "user"}), \
         patch("storage.database.get_user", return_value={"role": "admin"}), \
         patch("storage.database.get_username_by_sub", return_value="adminuser"), \
         patch("storage.database.get_user_agent_roles",
               return_value={"agent-1": "viewer"}), \
         patch("storage.files.sync_state_store.agents_for_machine", return_value={"agent-1"}):
        await layer.sync_all_agents_on_reconnect("m1")
    assert calls == [("agent-1", "adminuser", "viewer")]


@pytest.mark.asyncio
async def test_platform_admin_owner_without_explicit_role_fails_closed():
    # No explicit per-agent role → "" (viewer-equivalent sync: personal dirs
    # only), NOT "admin".
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "sub-admin", "pairing_scope": "user"}), \
         patch("storage.database.get_user", return_value={"role": "admin"}), \
         patch("storage.database.get_username_by_sub", return_value="adminuser"), \
         patch("storage.database.get_user_agent_roles", return_value={}), \
         patch("storage.files.sync_state_store.agents_for_machine", return_value={"agent-1"}):
        await layer.sync_all_agents_on_reconnect("m1")
    assert calls == [("agent-1", "adminuser", "")]


# --- _machine_sync_role (session-start path's machine-scope role) ---


def test_session_role_kept_on_admin_paired_machine():
    from core.remote.remote_execution import _machine_sync_role
    machine = {"registered_by": "sub-x", "pairing_scope": "admin"}
    assert _machine_sync_role(machine, "agent-1", "editor") == "editor"
    assert _machine_sync_role(None, "agent-1", "admin") == "admin"


def test_session_role_replaced_by_per_agent_role_on_user_paired():
    from core.remote.remote_execution import _machine_sync_role
    machine = {"registered_by": "sub-admin", "pairing_scope": "user"}
    with patch("storage.database.get_user_agent_roles",
               return_value={"agent-1": "viewer"}):
        # The session says "admin" (platform-inflated) — the machine syncs
        # as the owner's per-agent viewer role.
        assert _machine_sync_role(machine, "agent-1", "admin") == "viewer"
        assert _machine_sync_role(machine, "other-agent", "admin") == ""


def test_user_paired_without_owner_fails_closed():
    from core.remote.remote_execution import _machine_sync_role
    machine = {"registered_by": "", "pairing_scope": "user"}
    assert _machine_sync_role(machine, "agent-1", "admin") == ""


@pytest.mark.asyncio
async def test_no_synced_agents_is_noop():
    layer, calls = _layer_with_capture()
    with patch("storage.remote_store.get_remote_machine",
               return_value={"registered_by": "s", "pairing_scope": "user"}), \
         patch("storage.database.get_user", return_value={"role": "user"}), \
         patch("storage.database.get_username_by_sub", return_value="u"), \
         patch("storage.files.sync_state_store.agents_for_machine", return_value=set()):
        await layer.sync_all_agents_on_reconnect("m1")
    assert calls == []


# --- the reconnect walk's jitter, gate and one-walk rule ------------


def _agents(agents):
    return patch("storage.files.sync_state_store.agents_for_machine",
                 return_value=set(agents))


def _identity(layer):
    layer.resolve_machine_sync_identity = AsyncMock(return_value=(None, "admin"))


@pytest.mark.asyncio
async def test_reconnect_walks_wait_behind_the_gate(monkeypatch):
    layer = RemoteExecutionLayer(MagicMock())
    _identity(layer)
    state = {"active": 0, "max": 0, "n": 0}

    async def _sync(machine_id, agent_slug, **kw):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        state["n"] += 1

    layer._initial_workspace_sync = _sync

    async def _fleet():
        await asyncio.gather(*(layer.sync_all_agents_on_reconnect(m)
                               for m in ("m1", "m2", "m3")))

    monkeypatch.setattr(rws, "_RECONNECT_SYNC_CONCURRENCY", 1)
    with _agents({"a1", "a2"}):
        await _fleet()
    assert state["max"] == 1 and state["n"] == 6
    # A wider gate lets the machines' walks overlap.
    monkeypatch.setattr(rws, "_RECONNECT_SYNC_CONCURRENCY", 4)
    state.update(active=0, max=0, n=0)
    with _agents({"a1", "a2"}):
        await _fleet()
    assert state["max"] > 1 and state["n"] == 6


@pytest.mark.asyncio
async def test_reconnect_sync_sleeps_its_jitter_after_finding_agents(monkeypatch):
    layer, calls = _layer_with_capture()
    _identity(layer)
    order = []
    monkeypatch.setattr(rws, "_reconnect_jitter_s", lambda: order.append("jitter") or 0.0)

    def _tracked(machine_id):
        order.append("agents")
        return {"a1"} if machine_id == "m1" else set()

    with patch("storage.files.sync_state_store.agents_for_machine", side_effect=_tracked):
        await layer.sync_all_agents_on_reconnect("m1")
        assert order == ["agents", "jitter"]
        await layer.sync_all_agents_on_reconnect("m0")
    # A machine with nothing tracked never sleeps.
    assert order == ["agents", "jitter", "agents"]
    assert [c[0] for c in calls] == ["a1"]


@pytest.mark.asyncio
async def test_a_walk_whose_machine_left_does_no_manifest_work():
    layer, calls = _layer_with_capture()
    _identity(layer)
    layer._cm.is_connected.return_value = False
    with _agents({"a1", "a2"}):
        await layer.sync_all_agents_on_reconnect("m1")
    assert calls == []


@pytest.mark.asyncio
async def test_kicks_within_the_jitter_coalesce_into_one_walk(monkeypatch):
    layer, calls = _layer_with_capture()
    _identity(layer)
    monkeypatch.setattr(rws, "_reconnect_jitter_s", lambda: 0.05)
    with _agents({"a1"}):
        await asyncio.gather(layer.sync_all_agents_on_reconnect("m1"),
                             layer.sync_all_agents_on_reconnect("m1"))
    assert [c[0] for c in calls] == ["a1"]
    assert "m1" not in rws._reconnect_walks

    # A kick during a running walk earns exactly one more pass.
    monkeypatch.setattr(rws, "_reconnect_jitter_s", lambda: 0.0)
    release = asyncio.Event()
    passes = []

    async def _blocking(machine_id, agent_slug, **kw):
        passes.append(agent_slug)
        await release.wait()

    layer._initial_workspace_sync = _blocking
    with _agents({"a1"}):
        first = asyncio.create_task(layer.sync_all_agents_on_reconnect("m1"))
        await asyncio.sleep(0.01)
        assert passes == ["a1"]
        await layer.sync_all_agents_on_reconnect("m1")     # returns at once
        await layer.sync_all_agents_on_reconnect("m1")     # still one rerun
        assert passes == ["a1"]
        release.set()
        await first
    assert passes == ["a1", "a1"]
    assert "m1" not in rws._reconnect_walks


@pytest.mark.asyncio
async def test_the_gate_is_rebuilt_for_a_new_loop():
    here = rws._reconnect_gate()
    assert rws._reconnect_gate() is here

    async def _elsewhere():
        return rws._reconnect_gate()

    other = await asyncio.to_thread(lambda: asyncio.run(_elsewhere()))
    assert other is not here
    assert rws._reconnect_gate() is not other


@pytest.mark.asyncio
async def test_idle_sweep_syncs_wait_behind_the_reconnect_gate(monkeypatch):
    monkeypatch.setattr(rws, "_RECONNECT_SYNC_CONCURRENCY", 1)
    gate = rws._reconnect_gate()
    await gate.acquire()
    cm = MagicMock()
    cm.get_connection.return_value = SimpleNamespace(synced_fingerprints={})
    layer = RemoteExecutionLayer(cm)
    _identity(layer)
    init = AsyncMock()
    layer._initial_workspace_sync = init
    task = asyncio.create_task(layer._idle_fingerprint_sync_one("m1", "a1", "fp"))
    await asyncio.sleep(0.02)
    init.assert_not_awaited()
    gate.release()
    await task
    init.assert_awaited_once()
    assert init.await_args.kwargs["background"] is True


@pytest.mark.asyncio
async def test_the_idle_sweep_spawns_one_task_per_pending_pair(monkeypatch):
    cm = MagicMock()
    cm.get_connected_machines.return_value = ["m1"]
    cm.get_connection.return_value = SimpleNamespace(
        agent_fingerprints={"a1": "fp2"}, synced_fingerprints={"a1": "fp1"},
    )
    layer = RemoteExecutionLayer(cm)
    monkeypatch.setattr("storage.files.sync_state_store.agents_for_machine",
                        lambda mid: {"a1"})
    monkeypatch.setattr("services.remote.workspace_fanout._active_machine_ids",
                        lambda slug: set())
    release = asyncio.Event()
    started = []

    async def _one(machine_id, slug, fp):
        started.append(slug)
        await release.wait()

    monkeypatch.setattr(layer, "_idle_fingerprint_sync_one", _one)
    await layer.run_idle_fingerprint_sweep()
    await asyncio.sleep(0)
    await layer.run_idle_fingerprint_sweep()
    await asyncio.sleep(0)
    assert started == ["a1"]
    release.set()
    await asyncio.gather(*layer._deferred_sync_tasks)
    await layer.run_idle_fingerprint_sweep()
    await asyncio.sleep(0)
    assert started == ["a1", "a1"]
