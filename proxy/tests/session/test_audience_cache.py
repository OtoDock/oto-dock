"""The audience cache behind ``chat_status_targets`` (shared-only ``agent::``
and scheduler ``task::`` owners): served from memory on every turn edge, from
the loop or from a worker thread; a person an admin removes leaves it through
the offboarding event; other membership changes land within the TTL."""

import asyncio
import uuid

import pytest

import services.notifications.notification_manager as nm
from services.agents import offboarding
from services.agents.offboarding import AgentLoss, OffboardEvent
from storage.identity import db_users

pytestmark = pytest.mark.asyncio

ADMIN = "user-admin"  # the seeded platform admin: in every audience


def _agent_with(*subs: str) -> str:
    agent = f"aud-{uuid.uuid4().hex[:8]}"
    for sub in subs:
        db_users.add_user_agent(sub, agent, "editor", ADMIN)
    return agent


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

    # A lower role, still a member: nothing changes.
    await nm._on_offboard(OffboardEvent(
        "user-viewer2", offboarding.DEMOTED,
        (AgentLoss(agent, "editor", "viewer", old_row="editor", new_row="viewer"),), ADMIN))
    with loop_db_guard.active():
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
    assert "user-viewer2" in nm._audience[agent].subs

    # A result that started before an invalidation is discarded when it lands.
    generation = nm._audience[agent].generation
    nm.invalidate_audience(agent)
    nm._store_audience(agent, generation, ("nobody",), nm.notification_store.get_agent_user_subs)
    assert "nobody" not in nm._audience[agent].subs and nm._audience[agent].stale
    await _settle()


async def test_stale_entry_read_from_a_worker_thread_refreshes_there(temp_db, monkeypatch):
    agent = _agent_with("user-viewer")
    nm.reset_audience_cache()
    nm.agent_audience(agent)
    await asyncio.to_thread(db_users.add_user_agent, "user-viewer2", agent, "viewer", ADMIN)
    monkeypatch.setattr(nm, "_AUDIENCE_TTL_S", 0.0)
    subs = await asyncio.to_thread(nm.chat_status_targets, f"agent::{agent}", agent)
    assert set(subs) == {ADMIN, "user-viewer", "user-viewer2"}
    assert not nm._audience_tasks


async def test_a_failed_cold_read_caches_nothing_and_the_getter_is_pinned(temp_db, monkeypatch):
    nm.reset_audience_cache()

    def boom(agent):
        raise RuntimeError("db down")
    monkeypatch.setattr(nm.notification_store, "get_agent_user_subs", boom)
    assert nm.chat_status_targets("agent::x", "x") == []
    assert "x" not in nm._audience
    # A test-style replacement of the getter misses the cache (the entry
    # remembers which function filled it).
    monkeypatch.setattr(nm.notification_store, "get_agent_user_subs", lambda agent: ["a"])
    assert nm.agent_audience("x") == ["a"]
    monkeypatch.setattr(nm.notification_store, "get_agent_user_subs", lambda agent: ["b"])
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
