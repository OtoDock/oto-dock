"""The audience cache behind ``chat_status_targets`` (shared-only ``agent::``
and scheduler ``task::`` owners): served from memory on every turn edge, from
the loop or from a worker thread; a person an admin removes leaves it through
the offboarding event; other membership changes land within the TTL."""

import asyncio
import uuid
from types import SimpleNamespace

import psycopg
import pytest

import services.notifications.notification_manager as nm
from services.agents import offboarding
from services.agents.offboarding import AgentLoss, OffboardEvent
from storage.identity import db_users

pytestmark = pytest.mark.asyncio

ADMIN = "user-admin"  # the seeded platform admin: in every audience


def _agent_with(*subs: str, shared_only: bool = False) -> str:
    """An agent with ``subs`` as editors; a Shared-only one when asked (its
    ``agent::`` chats are the human pool every member sees)."""
    agent = f"aud-{uuid.uuid4().hex[:8]}"
    if shared_only:
        from storage.agents import agent_store
        agent_store.create_agent(agent, agent, collaborative=False, default_scope="agent")
    for sub in subs:
        db_users.add_user_agent(sub, agent, "editor", ADMIN)
    return agent


def _set_role(sub: str, agent: str, role: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE user_agents SET agent_role=%s WHERE sub=%s AND agent=%s",
                     (role, sub, agent))
        conn.commit()


async def _settle():
    if nm._audience_tasks:
        await asyncio.gather(*nm._audience_tasks)


async def test_second_read_within_the_ttl_makes_no_store_call(temp_db, loop_db_guard):
    agent = _agent_with("user-viewer")
    nm.reset_audience_cache()
    assert set(nm.agent_audience(agent)) == {ADMIN, "user-viewer"}
    with loop_db_guard.active():
        assert set(nm.chat_status_targets(f"agent::{agent}", agent)) == {ADMIN, "user-viewer"}
        assert set(nm.chat_status_targets(f"task::{agent}", agent)) == {ADMIN, "user-viewer"}
        assert nm.chat_status_targets("user-viewer", agent) == ["user-viewer"]
    # The list handed out is the caller's own copy.
    nm.agent_audience(agent).append("intruder")
    assert "intruder" not in nm.agent_audience(agent)
    await _settle()


async def test_offboarding_events_drop_the_person_at_once(temp_db, loop_db_guard):
    agent = _agent_with("user-viewer", "user-viewer2")
    other = _agent_with("user-viewer2")
    nm.reset_audience_cache()
    nm.agent_audience(agent)
    nm.agent_audience(other)

    # Removed from the agent: gone before the next read, no store call.
    await nm._on_offboard(OffboardEvent(
        "user-viewer", offboarding.REMOVED,
        (AgentLoss(agent, "editor", "", old_row="editor", new_row=""),), ADMIN))
    with loop_db_guard.active():
        assert set(nm.agent_audience(agent)) == {ADMIN, "user-viewer2"}

    # A lower role: out at once (a stale entry served on the loop never
    # carries the old role), back at the stored one when the refresh lands.
    await nm._on_offboard(OffboardEvent(
        "user-viewer2", offboarding.DEMOTED,
        (AgentLoss(agent, "editor", "viewer", old_row="editor", new_row="viewer"),), ADMIN))
    with loop_db_guard.active():
        assert "user-viewer2" not in nm.agent_audience(agent)
    await _settle()
    assert "user-viewer2" in nm.agent_audience(agent)

    # An admin unassigned from the agent keeps access (admins are in every
    # audience): served as is, refreshed off the loop.
    await nm._on_offboard(OffboardEvent(
        ADMIN, offboarding.REMOVED,
        (AgentLoss(agent, "admin", "admin", old_row="manager", new_row=""),), ADMIN))
    with loop_db_guard.active():
        assert ADMIN in nm.agent_audience(agent)
    await _settle()
    assert ADMIN in nm.agent_audience(agent)

    # Deleted: gone from every agent.
    await nm._on_offboard(OffboardEvent("user-viewer2", offboarding.DELETED, (), ADMIN))
    with loop_db_guard.active():
        assert "user-viewer2" not in nm.agent_audience(agent)
        assert "user-viewer2" not in nm.agent_audience(other)
    await _settle()


async def test_stale_entry_is_served_and_refreshed_off_the_loop(temp_db, loop_db_guard, monkeypatch):
    agent = _agent_with("user-viewer")
    nm.reset_audience_cache()
    nm.agent_audience(agent)
    await asyncio.to_thread(db_users.add_user_agent, "user-viewer2", agent, "viewer", ADMIN)
    monkeypatch.setattr(nm, "_AUDIENCE_TTL_S", 0.0)
    with loop_db_guard.active():
        served = nm.agent_audience(agent)
    assert "user-viewer2" not in served and nm._audience_tasks
    await _settle()
    assert "user-viewer2" in {sub for sub, _role in nm._audience[agent].members}

    # A result that started before an invalidation is discarded when it lands.
    generation = nm._audience[agent].generation
    nm.invalidate_audience(agent)
    nm._store_audience(agent, generation, (("nobody", "viewer"),),
                       nm.notification_store.get_agent_members)
    assert "nobody" not in {sub for sub, _role in nm._audience[agent].members}
    assert nm._audience[agent].stale
    await _settle()


async def test_stale_entry_read_from_a_worker_thread_refreshes_there(temp_db, monkeypatch):
    agent = _agent_with("user-viewer", shared_only=True)
    nm.reset_audience_cache()
    nm.agent_audience(agent)
    await asyncio.to_thread(db_users.add_user_agent, "user-viewer2", agent, "viewer", ADMIN)
    monkeypatch.setattr(nm, "_AUDIENCE_TTL_S", 0.0)
    subs = await asyncio.to_thread(nm.chat_status_targets, f"agent::{agent}", agent)
    assert set(subs) == {ADMIN, "user-viewer", "user-viewer2"}
    assert not nm._audience_tasks


async def test_an_invalidation_during_a_worker_refresh_keeps_the_entry_stale(monkeypatch):
    """A refresh on a worker thread takes the entry's generation before its
    store read: an invalidation that lands while the read runs keeps the
    entry stale, so the list read before it is never served as fresh."""
    nm.reset_audience_cache()
    members = {"now": ["a"]}
    during: list = []

    def getter(agent):
        out = [(sub, "viewer") for sub in members["now"]]
        for step in during:
            step()
        return out
    monkeypatch.setattr(nm.notification_store, "get_agent_members", getter)
    assert await asyncio.to_thread(nm.agent_audience, "x") == ["a"]

    def add_b():  # an attach route, mid-read: the read answered before it
        members["now"] = ["a", "b"]
        nm.invalidate_audience("x")
    monkeypatch.setattr(nm, "_AUDIENCE_TTL_S", 0.0)
    during.append(add_b)
    await asyncio.to_thread(nm.agent_audience, "x")
    during.clear()
    assert nm._audience["x"].stale
    monkeypatch.setattr(nm, "_AUDIENCE_TTL_S", 30.0)
    assert await asyncio.to_thread(nm.agent_audience, "x") == ["a", "b"]


async def test_the_first_fan_out_read_on_the_loop_reads_off_the_loop(temp_db, loop_db_guard, monkeypatch):
    """An agent with no entry yet (created after the boot warm-up): its first
    turn-edge fan-out on the loop answers at once with nobody and fills the
    entry in one ``run_db`` job; the next edge reaches the agent's audience."""
    import threading

    agent = _agent_with("user-viewer")
    nm.reset_audience_cache()
    real = nm.notification_store.get_agent_members
    readers: list[int] = []

    def recording(a):
        readers.append(threading.get_ident())
        return real(a)
    monkeypatch.setattr(nm.notification_store, "get_agent_members", recording)
    with loop_db_guard.active():
        first = nm.chat_status_targets(f"agent::{agent}", agent)
    assert threading.get_ident() not in readers, "store read on the loop"
    assert first == [] and nm._audience_tasks
    await _settle()
    with loop_db_guard.active():
        assert set(nm.chat_status_targets(f"agent::{agent}", agent)) == {ADMIN, "user-viewer"}


async def test_a_new_agents_first_invalidation_fills_its_entry(temp_db, loop_db_guard):
    """Agent creation and a community install invalidate the new agent on
    the loop before any chat exists: with no entry to mark, that fills one
    in a ``run_db`` job, so the agent's first turn edge finds its audience."""
    agent = _agent_with("user-viewer")
    nm.reset_audience_cache()
    with loop_db_guard.active():
        nm.invalidate_audience(agent)
    await _settle()
    with loop_db_guard.active():
        assert set(nm.chat_status_targets(f"agent::{agent}", agent)) == {ADMIN, "user-viewer"}


async def test_an_authorization_read_never_answers_from_a_placeholder(temp_db):
    """The trigger notify-reach check decides a refusal: right after a
    fan-out found no entry on the loop, it still sees the agent's members
    (the placeholder serves fan-outs only)."""
    from core.session import visibility as _vis
    from services.scheduler import trigger_manager as tm

    agent = _agent_with("user-viewer")
    nm.reset_audience_cache()
    nm.chat_status_targets(f"agent::{agent}", agent)
    tm._check_notify_reach(agent=agent, created_by="user-alice", target_scope=_vis.SCOPE_USER,
                           target="user-viewer", caller_is_admin=False)
    await _settle()


async def test_a_failed_cold_read_caches_nothing_and_the_getter_is_pinned(temp_db, monkeypatch):
    nm.reset_audience_cache()

    def boom(agent):
        raise psycopg.OperationalError("db down")
    monkeypatch.setattr(nm.notification_store, "get_agent_members", boom)
    assert await asyncio.to_thread(nm.chat_status_targets, "agent::x", "x") == []
    assert "x" not in nm._audience
    # On the loop the fan-out's miss answers nobody; the failed fill leaves
    # only the placeholder, which the next read tries again.
    assert nm.chat_status_targets("agent::x", "x") == []
    await _settle()
    entry = nm._audience["x"]
    assert entry.placeholder and entry.stale and not entry.refreshing
    with pytest.raises(psycopg.OperationalError, match="db down"):
        nm.agent_audience("x")
    assert "x" not in nm._audience
    # A test-style replacement of the getter misses the cache (the entry
    # remembers which function filled it).
    monkeypatch.setattr(nm.notification_store, "get_agent_members", lambda agent: [("a", "viewer")])
    assert nm.agent_audience("x") == ["a"]
    monkeypatch.setattr(nm.notification_store, "get_agent_members", lambda agent: [("b", "viewer")])
    assert nm.agent_audience("x") == ["b"]
    nm.reset_audience_cache()
    assert not nm._audience


def test_start_registers_the_invalidation_subscriber():
    nm._register_audience_invalidation()
    priority, fn = offboarding._subscribers["audience-cache"]
    assert priority == 5 and fn is nm._on_offboard
    offboarding.unsubscribe("audience-cache")


async def test_an_admin_add_is_seen_on_the_next_refresh(temp_db, loop_db_guard):
    """A person an admin adds to an agent joins its audience on the read
    after the add, not one TTL later: the add route invalidates the entry."""
    from api.auth import admin_users
    from auth.providers import UserContext
    from storage.agents import agent_store

    agent = _agent_with("user-viewer")
    agent_store.create_agent(agent, "Audience")
    nm.reset_audience_cache()
    assert "user-viewer2" not in nm.agent_audience(agent)
    admin = UserContext(sub=ADMIN, email="admin@t.com", name="Admin", role="admin")
    await admin_users.admin_add_user_agent(
        "user-viewer2", agent, admin_users.AddAgentRequest(role="editor"), user=admin)
    with loop_db_guard.active():
        nm.agent_audience(agent)
    await _settle()
    with loop_db_guard.active():
        assert "user-viewer2" in nm.agent_audience(agent)


async def test_the_trigger_notify_reach_reads_the_cache(temp_db, loop_db_guard):
    from core.session import visibility as _vis
    from services.scheduler import trigger_manager as tm

    agent = _agent_with("user-viewer")
    nm.reset_audience_cache()
    nm.agent_audience(agent)
    with loop_db_guard.active():
        tm._check_notify_reach(agent=agent, created_by="user-alice", target_scope=_vis.SCOPE_USER,
                               target="user-viewer", caller_is_admin=False)
        with pytest.raises(tm.TriggerValidationError):
            tm._check_notify_reach(agent=agent, created_by="user-alice", target_scope=_vis.SCOPE_USER,
                                   target="user-viewer2", caller_is_admin=False)


async def test_a_platform_role_change_refreshes_every_audience(temp_db, loop_db_guard):
    """Every admin is in every audience, so a promotion or a demotion marks
    every entry for a refresh on its next read."""
    from api.auth import admin_users
    from auth.providers import UserContext

    agent = _agent_with("user-viewer")
    nm.reset_audience_cache()
    assert "user-viewer2" not in nm.agent_audience(agent)
    admin = UserContext(sub=ADMIN, email="admin@t.com", name="Admin", role="admin")
    await admin_users.admin_update_role(
        "user-viewer2", admin_users.UpdateRoleRequest(role="admin"), user=admin)
    with loop_db_guard.active():
        nm.agent_audience(agent)
    await _settle()
    with loop_db_guard.active():
        assert "user-viewer2" in nm.agent_audience(agent)
    await admin_users.admin_update_role(
        "user-viewer2", admin_users.UpdateRoleRequest(role="member"), user=admin)
    with loop_db_guard.active():
        nm.agent_audience(agent)
    await _settle()
    with loop_db_guard.active():
        assert "user-viewer2" not in nm.agent_audience(agent)


async def test_the_agents_own_runs_reach_the_editor_tier(temp_db, loop_db_guard):
    """A scheduled run's chat (``task::``) is what the agent does on its own
    behalf: its live frames reach the editors, managers and admins, never a
    contributor or a viewer; the shared pool (``agent::``) reaches every
    member; a demotion refreshes the entry."""
    agent = _agent_with("user-viewer", shared_only=True)
    await asyncio.to_thread(db_users.add_user_agent, "user-viewer2", agent, "viewer", ADMIN)
    await asyncio.to_thread(db_users.add_user_agent, "user-manager", agent, "contributor", ADMIN)
    nm.reset_audience_cache()
    assert set(nm.agent_audience(agent)) == {ADMIN, "user-viewer", "user-viewer2", "user-manager"}
    with loop_db_guard.active():
        assert set(nm.chat_status_targets(f"task::{agent}", agent)) == {ADMIN, "user-viewer"}
        assert set(nm.chat_status_targets(f"agent::{agent}", agent)) == {
            ADMIN, "user-viewer", "user-viewer2", "user-manager"}
        assert nm.agent_audience(agent, floor="contributor") == [
            s for s in nm.agent_audience(agent) if s != "user-viewer2"]
    await nm._on_offboard(OffboardEvent(
        "user-viewer", offboarding.DEMOTED,
        (AgentLoss(agent, "editor", "viewer", old_row="editor", new_row="viewer"),), ADMIN))
    assert nm._audience[agent].stale
    await asyncio.to_thread(_set_role, "user-viewer", agent, "viewer")
    assert set(await asyncio.to_thread(nm.agent_audience, agent, floor="editor")) == {ADMIN}
    await _settle()


# --- an agent:: owner off a Shared-only agent (a delegated worker) ---------

_MEMBERS = [("u-view", "viewer"), ("u-contrib", "contributor"), ("u-ed", "editor"),
            ("u-adm", "admin")]


@pytest.fixture
def two_modes(monkeypatch):
    """Two agents with the same four people: ``collab`` (collaborative) and
    ``pool`` (Shared-only), and a socket per person."""
    from core.session import visibility

    nm.reset_audience_cache()
    monkeypatch.setattr(nm.notification_store, "get_agent_members", lambda agent: list(_MEMBERS))
    monkeypatch.setattr(visibility, "is_shared_only", lambda agent: agent == "pool")
    conns = {sub: SimpleNamespace(queue=asyncio.Queue(), focus=None, live_queue=None)
             for sub, _ in _MEMBERS}
    monkeypatch.setattr(nm, "_user_connections", {s: [c] for s, c in conns.items()})
    return conns


def _told(conns, frame_type: str) -> set[str]:
    out = set()
    for sub, c in conns.items():
        while not c.queue.empty():
            if c.queue.get_nowait().get("type") == frame_type:
                out.add(sub)
    return out


_EDITOR_TIER = {"u-ed", "u-adm"}
_EVERYONE = {"u-view", "u-contrib", "u-ed", "u-adm"}


@pytest.mark.parametrize("agent,expected", [("collab", _EDITOR_TIER), ("pool", _EVERYONE)])
async def test_every_frame_class_of_an_agent_owned_chat_follows_the_rule(two_modes, agent,
                                                                         expected):
    """A delegated worker's chat (an ``agent::`` owner on a collaborative
    agent) is the agent's own work: its status, title, read marker, engine
    switch and move frames reach the editor tier only. A Shared-only pool
    chat's reach every member. Read from a worker thread (the cache fills
    there; on the loop a cold read answers nobody)."""
    from ws import wire_events as wire
    conns, owner = two_modes, f"agent::{agent}"
    await asyncio.to_thread(nm.agent_audience, agent)
    cases = [
        (wire.CHAT_STATUS, lambda: nm.broadcast_chat_status(owner, "w-1", "streaming", agent=agent)),
        (wire.TITLE_UPDATED, lambda: nm.broadcast_chat_title(owner, "w-1", "Lane: report", agent=agent)),
        (wire.CHAT_READ, lambda: nm.broadcast_chat_read(owner, "w-1", agent=agent)),
        (wire.ENGINE_SWITCHED, lambda: nm.broadcast_engine_switched(owner, "w-1", "codex-cli", "m",
                                                                    agent=agent)),
        ("chat_moved", lambda: nm._send_move_frame(owner, agent, "", {"type": "chat_moved",
                                                                      "chat_id": "w-1"}, set())),
    ]
    for frame_type, send in cases:
        send()
        assert _told(conns, frame_type) == expected, frame_type


@pytest.mark.parametrize("agent,expected", [("collab", _EDITOR_TIER), ("pool", _EVERYONE)])
async def test_the_queue_frames_of_an_agent_owned_chat_follow_the_rule(two_modes, monkeypatch,
                                                                       agent, expected):
    """The input queue's ``queued``, ``queue_snapshot`` and ``steered`` frames
    carry a queued message's text: on a collaborative agent's worker they
    reach the editor tier (and the message's own author) only."""
    from core.events import input_queue
    from ws import wire_events as wire
    await asyncio.to_thread(nm.agent_audience, agent)
    pushed: list[tuple[str, str]] = []
    monkeypatch.setattr(nm, "push_live", lambda sub, frame: pushed.append((sub, frame["type"])) or 1)
    q = input_queue.get(f"q-{agent}")
    q.owner_sub, q.agent = f"agent::{agent}", agent
    for frame_type in (wire.QUEUED, wire.QUEUE_SNAPSHOT, wire.STEERED):
        pushed.clear()
        input_queue.fan_out(q.chat_id, {"type": frame_type, "text": "a late steer"})
        assert {sub for sub, _ in pushed} == expected, frame_type
    input_queue._registry.pop(q.chat_id, None)
