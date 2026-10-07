"""The offboarding transfer (``services/agents/offboarding_transfer.py``):
a person's agent-scope automations follow the admin who took their
standing away, their user-scope rows and meetings on the agents they lost
go, a deleted person's trees leave the agent directories and their
username retires.

Run: cd proxy && venv/bin/pytest tests/agents/test_offboarding_transfer.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth.providers import UserContext, get_current_user
from services.agents import offboarding
from services.agents import offboarding_transfer as transfer
from services.agents.offboarding import AgentLoss, OffboardEvent
from storage import database as db
from storage.agents import agent_store
from storage.automation import notification_store, trigger_store
from storage.pg import get_conn

A, B = "tr-a", "tr-b"


def _rows(table: str, where: str, *params) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM {table} WHERE {where} ORDER BY 1", params).fetchall()]


@pytest.fixture
def world(temp_db, monkeypatch):
    """An owner, an admin, a person holding the editor tier on two agents,
    and a record of every notification fired."""
    from services.notifications import notification_manager
    agent_store.create_agent(A, "A")
    agent_store.create_agent(B, "B")
    owner = db.create_local_user("owner@t.com", "Olive Owner", "Olive", "admin", "x", is_owner=True)
    admin = db.create_local_user("admin@t.com", "Ada Admin", "Ada", "admin", "x")
    pat = db.create_local_user("pat@t.com", "Pat Person", "Pat", "member", "x")
    db.set_user_agents(pat, [A, B], admin, agent_roles={A: "editor", B: "editor"})
    fired: list[dict] = []

    async def _fire(title, body, **kw):
        fired.append({"title": title, "body": body, **kw})
        return []

    monkeypatch.setattr(notification_manager, "fire_notification", _fire)
    monkeypatch.setattr(transfer, "RESWEEP_AFTER_S", 0.0)
    monkeypatch.setattr(transfer, "ARCHIVE_AFTER_S", 0.0)
    return SimpleNamespace(owner=owner, admin=admin, pat=pat, pat_name=db.get_username_by_sub(pat),
                           fired=fired)


def _task(tid, agent, created_by, *, scope="agent", task_type="scheduled", **kw):
    schedule = "0 9 * * *" if task_type in ("scheduled",) else None
    db.create_dynamic_task(tid, agent, f"Task {tid}", "p", "cli", task_type, schedule, None, None,
                           600, created_by, scope=scope, **kw)
    return db.get_dynamic_task(tid)


def _trigger(slug, agent, created_by, *, scope="agent", target=None, **kw):
    return trigger_store.create_trigger(
        slug=slug, name=f"Trigger {slug}", scope=scope, agent=agent, created_by=created_by,
        notify_enabled=target is not None, notify_target_scope="user" if target else None,
        notify_target=target, **kw)


def _notification(agent, created_by, *, scope="agent", target=None):
    return notification_store.create_notification(
        "Digest", "b", scope=scope, target=target or agent, notification_type="scheduled",
        schedule="0 9 * * *", created_by=created_by, agent_slug=agent)


def _pats_rows(w):
    """The person's holdings, per agent: a scheduled task, a continuation,
    a trigger task with its trigger, a scheduled notification, and a
    user-scope task; plus a trigger of the admin's that notifies them."""
    for agent in (A, B):
        _task(f"sched-{agent}", agent, w.pat)
        _task(f"cont-{agent}", agent, w.pat, task_type="continuation", interval_seconds=60,
              target_chat_id=f"chat-{agent}", max_runs=3)
        _task(f"trig-{agent}", agent, w.pat, task_type="trigger")
        _trigger(f"hook-{agent}", agent, w.pat, task_id=f"trig-{agent}")
        _notification(agent, w.pat)
        _task(f"mine-{agent}", agent, w.pat, scope="user")
        _trigger(f"admins-{agent}", agent, w.admin, target=w.pat)


def _event(w, reason, *losses, actor=None):
    return OffboardEvent(sub=w.pat, reason=reason, agents=tuple(losses),
                         actor_sub=w.admin if actor is None else actor,
                         username=w.pat_name, email="pat@t.com", display_name="Pat")


async def _settle():
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_lost_editor_moves_the_persons_agent_scope_rows_to_the_actor(world):
    w = world
    _pats_rows(w)
    # The person's own user-scope trigger, notifying themselves.
    _trigger(f"own-{A}", A, w.pat, scope="user", target=w.pat)
    db.set_user_agents(w.pat, [A, B], w.admin, agent_roles={A: "viewer", B: "editor"})
    await transfer.on_offboard(_event(w, offboarding.DEMOTED, AgentLoss(A, "editor", "viewer")))
    await _settle()
    # A demotion keeps the agent: their private trigger stays theirs and
    # keeps notifying them.
    [own] = _rows("triggers", "slug=%s", f"own-{A}")
    assert (own["scope"], own["created_by"], own["notify_target"]) == ("user", w.pat, w.pat)
    assert not own["transferred_from"]
    moved = _rows("dynamic_tasks", "agent=%s AND scope='agent'", A)
    assert {t["id"] for t in moved} == {f"sched-{A}", f"trig-{A}"}
    assert all(t["created_by"] == w.admin and t["transferred_from"] == w.pat
               and t["transferred_at"] and t["enabled"] for t in moved)
    # A self-continuation is the chat's own bounded wake, not a standing
    # automation: it ends instead of changing hands.
    assert _rows("dynamic_tasks", "id=%s", f"cont-{A}") == []
    [hook] = _rows("triggers", "slug=%s", f"hook-{A}")
    assert hook["created_by"] == w.admin and hook["transferred_from"] == w.pat
    [admins] = _rows("triggers", "slug=%s", f"admins-{A}")
    assert admins["created_by"] == w.admin and admins["notify_target"] == w.admin
    [digest] = _rows("notifications", "agent_slug=%s AND scope='agent'", A)
    assert digest["created_by"] == w.admin and digest["transferred_from"] == w.pat
    # The user-scope row on an agent still reachable, and everything on B, stay.
    assert _rows("dynamic_tasks", "id=%s", f"mine-{A}")[0]["created_by"] == w.pat
    assert all(t["created_by"] == w.pat and not t["transferred_from"]
               for t in _rows("dynamic_tasks", "agent=%s", B))
    assert _rows("triggers", "slug=%s", f"admins-{B}")[0]["notify_target"] == w.pat
    [note] = w.fired
    assert note["scope"] == "user" and note["target"] == w.admin
    assert note["source"] == transfer.NOTIFICATION_SOURCE
    assert note["title"] == "Automations transferred to you"
    assert "Pat (moved below the editor tier)" in note["body"]
    assert f"{A}: 2 task(s): Task sched-{A}, Task trig-{A}; 1 chat continuation(s) ended; " \
           f"1 trigger(s): Trigger hook-{A}; 1 scheduled notification(s); " \
           "1 trigger notification(s) now addressed to you" in note["body"]
    assert "without knowledge writes" in note["body"] and B not in note["body"]


@pytest.mark.asyncio
async def test_a_departed_persons_continuations_end_instead_of_moving(world):
    """A person who leaves only a self-continuation behind: it ends, nothing
    changes hands, and nobody is told."""
    w = world
    _task(f"cont-{A}", A, w.pat, task_type="continuation", interval_seconds=60,
          target_chat_id=f"chat-{A}", max_runs=3)
    db.set_user_agents(w.pat, [A], w.admin, agent_roles={A: "viewer"})
    changed = await transfer.reconcile_person(w.pat, actor=w.admin, reason=offboarding.DEMOTED)
    await _settle()
    assert _rows("dynamic_tasks", "id=%s", f"cont-{A}") == []
    assert [c["id"] for c in changed[A]["continuations"]] == [f"cont-{A}"]
    assert changed[A]["tasks"] == []
    assert w.fired == []


@pytest.mark.asyncio
async def test_a_callers_own_continuations_on_a_phone_chat_are_left_alone(world):
    """A contributor tied to a phone route schedules a continuation from
    the call: the row is agent-scope (the chat is the phone's) but it is
    their own wake at their own role, not an automation of the editor
    tier. Neither the boot pass nor an event for them ends it, and nobody
    is told; a continuation on a chat that runs as the agent still ends."""
    from core.session.visibility import PHONE_CHAT_OWNER, shared_chat_owner
    w = world
    cleo = db.create_local_user("cleo@t.com", "Cleo C", "Cleo", "member", "x")
    db.set_user_agents(cleo, [A], w.admin, agent_roles={A: "contributor"})
    db.create_chat("phone-chat", PHONE_CHAT_OWNER, A)
    db.create_chat("shared-chat", shared_chat_owner(A), A)
    _task("cont-phone", A, cleo, task_type="continuation", interval_seconds=60,
          target_chat_id="phone-chat", max_runs=3)
    # Their own user-scope trigger, notifying themselves.
    _trigger("cleo-own", A, cleo, scope="user", target=cleo)
    assert await transfer.reconcile_at_boot() == {}
    await _settle()
    assert [t["id"] for t in _rows("dynamic_tasks", "created_by=%s", cleo)] == ["cont-phone"]
    assert _rows("triggers", "slug='cleo-own'")[0]["notify_target"] == cleo
    assert w.fired == []
    # A demotion to viewer loses no editor tier they held.
    db.set_user_agents(cleo, [A], w.admin, agent_roles={A: "viewer"})
    assert await transfer.reconcile_person(cleo, actor=w.admin, reason=offboarding.DEMOTED) == {}
    await _settle()
    assert [t["id"] for t in _rows("dynamic_tasks", "created_by=%s", cleo)] == ["cont-phone"]
    assert w.fired == []
    # A wake on the agent's shared chat took the editor tier to schedule.
    _task("cont-shared", A, cleo, task_type="continuation", interval_seconds=60,
          target_chat_id="shared-chat", max_runs=3)
    changed = await transfer.reconcile_at_boot()
    await _settle()
    assert [c["id"] for c in changed[A]["continuations"]] == ["cont-shared"]
    assert [t["id"] for t in _rows("dynamic_tasks", "created_by=%s", cleo)] == ["cont-phone"]
    assert w.fired == []


@pytest.mark.asyncio
async def test_a_deletion_moves_every_agent_and_falls_back_to_the_owner(world):
    w = world
    _pats_rows(w)
    assert db.delete_user(w.pat)
    await transfer.on_offboard(_event(w, offboarding.DELETED, actor=""))
    await _settle()
    for agent in (A, B):
        moved = _rows("dynamic_tasks", "agent=%s AND scope='agent'", agent)
        assert {t["created_by"] for t in moved} == {w.owner}
        assert {t["transferred_from"] for t in moved} == {w.pat}
        assert _rows("dynamic_tasks", "task_type='continuation' AND agent=%s", agent) == []
        assert _rows("triggers", "slug=%s", f"admins-{agent}")[0]["notify_target"] == w.owner
    [note] = w.fired
    assert note["target"] == w.owner and "(account deleted)" in note["body"]
    assert A in note["body"] and B in note["body"]


@pytest.mark.asyncio
async def test_a_person_re_added_before_the_chain_keeps_their_rows(world):
    w = world
    _pats_rows(w)
    # The event says they lost A; by the time the chain runs they are an
    # editor there again.
    await transfer.on_offboard(_event(w, offboarding.REMOVED, AgentLoss(A, "editor", "")))
    await _settle()
    assert all(t["created_by"] == w.pat and not t["transferred_from"]
               for t in _rows("dynamic_tasks", "agent=%s", A))
    assert _rows("triggers", "slug=%s", f"admins-{A}")[0]["notify_target"] == w.pat
    assert w.fired == []


@pytest.mark.asyncio
async def test_delegate_check_and_slug_created_rows_are_not_moved(world):
    w = world
    _task("worker", A, w.pat, task_type="delegate", use_persistent=True)
    _task("handler", A, w.pat, task_type="app", app_id="app-1", app_handler="tick")
    _task("by-agent", A, "tr-b")
    _trigger("app-hook", A, w.pat, app_id="app-1", handler="tick")
    db.set_user_agents(w.pat, [A, B], w.admin, agent_roles={A: "viewer", B: "viewer"})
    moved = await transfer.reconcile_person(w.pat, actor=w.admin, reason=offboarding.DEMOTED)
    await _settle()
    assert moved == {}
    assert {t["created_by"] for t in _rows("dynamic_tasks", "agent=%s", A)} == {w.pat, "tr-b"}
    assert _rows("triggers", "slug='app-hook'")[0]["created_by"] == w.pat
    assert w.fired == []


@pytest.mark.asyncio
async def test_the_boot_reconcile_moves_orphaned_rows_to_the_owner_and_skips_unknown_creators(
        world, monkeypatch):
    from storage.identity import api_key_store
    # A key row with no minter must not be pinned on "several people".
    monkeypatch.setattr(api_key_store, "list_agent_api_keys",
                        lambda: [{"created_by": "", "agent": A, "name": "seeded"}])
    w = world
    quinn = db.create_local_user("quinn@t.com", "Quinn Q", "Quinn", "member", "x")
    db.set_user_agents(quinn, [A], w.admin, agent_roles={A: "manager"})
    _task("pat-a", A, w.pat)
    _task("pat-b", B, w.pat)
    _trigger("pat-hook", A, w.pat)
    _notification(A, w.pat)
    _task("quinn-a", A, quinn)
    _task("api-a", A, "api")
    _task("slug-a", A, "tr-b")
    # Deleted before retired_usernames existed: a local subject no user row
    # carries any more moves like a retired one; an unknown IdP subject is
    # named in the notice and left in place.
    _task("gone-a", A, "local:0f6c2a1e-3b4d-4c5e-8f90-1a2b3c4d5e6f")
    _task("idp-a", A, "00u1abcdEFGHijkl2345")
    _task("key-a", A, "api-key")
    # Pat: demoted on A without an event (a lost one), deleted from nothing.
    db.set_user_agents(w.pat, [A, B], w.admin, agent_roles={A: "viewer", B: "editor"})
    # Rae: deleted without an event.
    rae = db.create_local_user("rae@t.com", "Rae R", "Rae", "member", "x")
    db.set_user_agents(rae, [B], w.admin, agent_roles={B: "editor"})
    _task("rae-b", B, rae)
    assert db.delete_user(rae)
    moved = await transfer.reconcile_at_boot()
    await _settle()
    assert set(moved) == {A, B}
    by_id = {t["id"]: t for t in _rows("dynamic_tasks", "TRUE")}
    assert by_id["pat-a"]["created_by"] == w.owner and by_id["pat-a"]["transferred_from"] == w.pat
    assert by_id["rae-b"]["created_by"] == w.owner and by_id["rae-b"]["transferred_from"] == rae
    gone = "local:0f6c2a1e-3b4d-4c5e-8f90-1a2b3c4d5e6f"
    assert by_id["gone-a"]["created_by"] == w.owner and by_id["gone-a"]["transferred_from"] == gone
    for untouched, creator in (("pat-b", w.pat), ("quinn-a", quinn), ("api-a", "api"),
                               ("slug-a", "tr-b"), ("idp-a", "00u1abcdEFGHijkl2345"),
                               ("key-a", "api-key")):
        assert by_id[untouched]["created_by"] == creator and not by_id[untouched]["transferred_from"]
    assert _rows("triggers", "slug='pat-hook'")[0]["created_by"] == w.owner
    [note] = w.fired
    assert note["target"] == w.owner and "pat-a" in note["body"] and "rae-b" in note["body"]
    assert "gone-a" in note["body"] and "00u1abcdEFGHijkl2345" in note["body"]
    assert "api-key" not in note["body"] and "(tr-b" not in note["body"]
    assert "They minted" not in note["body"]
    # A second boot finds nothing to do.
    assert await transfer.reconcile_at_boot() == {}
    await _settle()
    assert len(w.fired) == 1


@pytest.mark.asyncio
async def test_a_deletion_sweeps_user_scope_rows_on_every_agent(world):
    w = world
    _pats_rows(w)
    # A row on an agent they never had a membership row on (an admin's).
    agent_store.create_agent("tr-c", "C")
    _task("mine-c", "tr-c", w.pat, scope="user")
    _trigger("mine-hook-c", "tr-c", w.pat, scope="user")
    _notification("tr-c", w.pat, scope="user", target=w.pat)
    assert db.delete_user(w.pat)
    assert _rows("dynamic_tasks", "scope='user' AND created_by=%s", w.pat) == []
    assert _rows("triggers", "scope='user' AND created_by=%s", w.pat) == []
    assert _rows("notifications", "scope='user' AND target=%s", w.pat) == []
    # The agent-scope rows stay for the transfer.
    assert len(_rows("dynamic_tasks", "scope='agent' AND created_by=%s", w.pat)) == 6
    [retired] = db.list_retired_usernames()
    assert (retired["username"], retired["sub"], retired["archived_at"]) == (w.pat_name, w.pat, "")


@pytest.mark.asyncio
async def test_a_removal_sweeps_user_scope_rows_where_no_membership_row_cascaded(world):
    """A platform admin holds user-scope rows on agents without a row; a
    demotion to member takes those agents away with no cascade."""
    w = world
    agent_store.create_agent("tr-c", "C")
    _task("mine-c", "tr-c", w.pat, scope="user")
    _task("mine-a", A, w.pat, scope="user")
    # Pat is a member: C is out of reach, A is still theirs.
    await transfer.reconcile_person(w.pat, actor=w.admin, reason=offboarding.REMOVED)
    assert [t["id"] for t in _rows("dynamic_tasks", "scope='user' AND created_by=%s", w.pat)] \
        == ["mine-a"]


@pytest.mark.asyncio
async def test_a_second_event_moves_nothing_and_sends_nothing(world):
    w = world
    _pats_rows(w)
    db.set_user_agents(w.pat, [B], w.admin, agent_roles={B: "editor"})
    first = await transfer.reconcile_person(w.pat, actor=w.admin, reason=offboarding.REMOVED)
    second = await transfer.reconcile_person(w.pat, actor=w.admin, reason=offboarding.REMOVED)
    await _settle()
    assert set(first) == {A} and second == {}
    assert len(w.fired) == 1


@pytest.mark.asyncio
async def test_a_removed_persons_meetings_end(world, monkeypatch):
    from services.meetings import meeting_orchestrator
    from storage.chat import meeting_status
    w = world
    ended, failed = [], []

    async def _end(mid, agent_slug=None):
        ended.append((mid, agent_slug))
        return {"status": "concluding"}

    async def _fail(mid, reason):
        failed.append((mid, reason))

    monkeypatch.setattr(meeting_orchestrator, "end_meeting", _end)
    monkeypatch.setattr(meeting_orchestrator, "fail_meeting", _fail)
    for mid, moderator, others, status in (
            ("m-live-a", A, [B], meeting_status.ACTIVE),
            ("m-pending-a", A, [], meeting_status.PENDING),
            ("m-paused-b", B, [A], meeting_status.PAUSED),
            ("m-live-b", B, [], meeting_status.ACTIVE),
            ("m-over-a", A, [], meeting_status.CONCLUDED)):
        db.create_meeting(mid, "t", json.dumps([moderator, *others]), moderator, "round_robin",
                          10, "chat-1", None, None, "agent", w.pat)
        db.update_meeting(mid, status=status)
    db.create_meeting("m-quinn", "t", json.dumps([A]), A, "round_robin", 10, "chat-2", None,
                      None, "agent", w.admin)
    db.update_meeting("m-quinn", status=meeting_status.ACTIVE)
    db.set_user_agents(w.pat, [B], w.admin, agent_roles={B: "editor"})
    await transfer.reconcile_person(w.pat, actor=w.admin, reason=offboarding.REMOVED)
    # Every meeting touching A ends; the one on B alone and the admin's run on.
    assert sorted(ended) == [("m-live-a", None), ("m-paused-b", None)]
    assert failed == [("m-pending-a", transfer.MEETING_END_REASON)]


@pytest.mark.asyncio
async def test_a_deletion_archives_the_folders_outside_the_agent_trees(world):
    w = world
    root = Path(config.AGENTS_DIR)
    home_a = root / A / "users" / w.pat_name
    (home_a / "workspace").mkdir(parents=True, exist_ok=True)
    (home_a / "workspace" / "notes.md").write_text("mine")
    # The membership made B's tree; a link stands in its place here.
    shutil.rmtree(root / B / "users" / w.pat_name)
    (root / B / "users" / w.pat_name).symlink_to(root / A)
    (root / A / "users" / "someone-else").mkdir()
    assert db.delete_user(w.pat)
    await transfer.on_offboard(_event(w, offboarding.DELETED))
    for task in list(transfer._background):
        await task
    archive = root / transfer.ARCHIVE_DIRNAME
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    assert stat.S_IMODE((archive / w.pat_name).stat().st_mode) == 0o700
    assert (archive / w.pat_name / A / "workspace" / "notes.md").read_text() == "mine"
    assert os.path.islink(archive / w.pat_name / B)
    assert not home_a.exists() and not os.path.lexists(root / B / "users" / w.pat_name)
    assert (root / A / "users" / "someone-else").is_dir()
    [retired] = db.list_retired_usernames()
    assert retired["archived_at"]


@pytest.mark.asyncio
async def test_a_restored_person_is_not_archived(world):
    w = world
    root = Path(config.AGENTS_DIR)
    home = root / A / "users" / w.pat_name / "workspace"
    home.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute("INSERT INTO retired_usernames (username, sub, retired_at) VALUES (%s,%s,'t')",
                     (w.pat_name, w.pat))
        conn.commit()
    assert await transfer.archive_person(w.pat_name) is False
    assert home.is_dir() and not (root / transfer.ARCHIVE_DIRNAME).exists()
    assert db.list_retired_usernames() == []


@pytest.mark.asyncio
async def test_an_archive_that_cannot_move_stays_and_is_retried(world, monkeypatch):
    from services.infra import safe_fs
    w = world
    root = Path(config.AGENTS_DIR)
    (root / A / "users" / w.pat_name).mkdir(parents=True, exist_ok=True)
    assert db.delete_user(w.pat)

    def _exdev(*a, **k):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(safe_fs, "rename_beneath", _exdev)
    assert await transfer.archive_person(w.pat_name) is False
    assert (root / A / "users" / w.pat_name).is_dir()
    assert db.list_retired_usernames(unarchived_only=True)[0]["username"] == w.pat_name
    monkeypatch.undo()
    assert await transfer.archive_person(w.pat_name) is True
    assert (root / transfer.ARCHIVE_DIRNAME / w.pat_name / A).is_dir()


def test_the_archive_layout_is_inert_to_the_agents_dir_enumerators(temp_db):
    from services.apps import releases
    from services.infra import external_retention, retention
    from services.sharing import chat_snapshot
    root = Path(config.AGENTS_DIR)
    agent_store.create_agent(A, "A")
    (root / A / "users" / "live-one" / "workspace").mkdir(parents=True)
    archived = root / transfer.ARCHIVE_DIRNAME / "gone-one" / A
    for sub in ("workspace", "context", "users/x", "externals/telegram/123",
                f"{chat_snapshot.SNAPSHOT_DIRNAME}/users/b/chat-1", ".claude"):
        (archived / sub).mkdir(parents=True)
    (archived / chat_snapshot.SNAPSHOT_DIRNAME / "users" / "b" / "chat-1" / "f").write_text("x")
    # A departed person whose username spells a folder the sweeps look for
    # (`users`, `app-releases`) must not make the archive read as an agent.
    named_users = root / transfer.ARCHIVE_DIRNAME / "users" / A
    for sub in ("workspace", "users/y", "externals/telegram/456"):
        (named_users / sub).mkdir(parents=True)
    named_releases = root / transfer.ARCHIVE_DIRNAME / "app-releases" / "shared" / "orphan-app"
    named_releases.mkdir(parents=True)
    # Other tests leave agent trees of their own on this disk: the checks
    # are containment and absence, never equality.
    homes = list(retention.iter_local_homes())
    assert (A, "live-one") in {(a, u) for a, u, _h in homes}
    assert not any(transfer.ARCHIVE_DIRNAME in h.parts for _a, _u, h in homes)
    assert not any(transfer.ARCHIVE_DIRNAME in h.parts
                   for _a, h in external_retention.iter_external_homes())
    chat_snapshot.sweep_orphans(set())
    assert (archived / chat_snapshot.SNAPSHOT_DIRNAME / "users" / "b" / "chat-1" / "f").exists()
    releases.reconcile()
    assert named_releases.is_dir()
    on_disk = db.usernames_on_disk()
    assert "live-one" in on_disk and not on_disk & {"x", "b", "y", "gone-one"}


def test_a_retired_username_is_never_minted_again(temp_db):
    agent_store.create_agent(A, "A")
    first = db.create_local_user("pat1@t.com", "Pat Person", "Pat", "member", "x")
    assert db.get_username_by_sub(first) == "pat-person"
    assert db.delete_user(first)
    second = db.create_local_user("pat2@t.com", "Pat Person", "Pat", "member", "x")
    assert db.get_username_by_sub(second) == "pat-person-2"
    db.upsert_user("oidc:pat3", "pat3@t.com", "Pat Person", "member")
    assert db.get_username_by_sub("oidc:pat3") == "pat-person-3"
    # A tree on disk with no row (a deletion before the archive existed).
    (Path(config.AGENTS_DIR) / A / "users" / "mia-mo").mkdir(parents=True)
    mia = db.create_local_user("mia@t.com", "Mia Mo", "Mia", "member", "x")
    assert db.get_username_by_sub(mia) == "mia-mo-2"
    db.upsert_user("oidc:mia2", "mia2@t.com", "Mia Mo", "member")
    assert db.get_username_by_sub("oidc:mia2") == "mia-mo-3"
    # A later login of an existing user keeps their name.
    db.upsert_user("oidc:mia2", "mia2@t.com", "Mia Mo", "member")
    assert db.get_username_by_sub("oidc:mia2") == "mia-mo-3"


def test_a_transferred_run_keeps_the_prompts_author(world):
    from services.scheduler import runner, scheduler
    w = world
    row = _task("moved", A, w.pat)
    db.set_user_agents(w.pat, [A], w.admin, agent_roles={A: "viewer"})
    assert db.transfer_agent_scope_automations(A, w.pat, w.admin, list(transfer.KINDS))
    task = scheduler._row_to_task(db.get_dynamic_task(row["id"]))
    assert task.created_by == w.admin and task.transferred_from == w.pat
    assert runner._run_creator(task) == w.pat


# --- the adoption ---------------------------------------------------------------


@pytest.fixture
def as_user(world, monkeypatch):
    from api.events import triggers as triggers_api
    from api.notifications import notifications as notifications_api
    from api.tasks import tasks as tasks_api
    from services.scheduler import scheduler
    monkeypatch.setattr(scheduler, "_register_task", lambda task: None)
    app = FastAPI()
    app.include_router(tasks_api.router)
    app.include_router(triggers_api.router)
    app.include_router(notifications_api.router)
    current = {}

    async def _current_user():
        return current["u"]

    app.dependency_overrides[get_current_user] = _current_user
    client = TestClient(app)

    def _as(sub, role, agent_roles):
        current["u"] = UserContext(sub=sub, email=f"{sub}@t", name=sub, role=role,
                                   agents=list(agent_roles), agent_roles=agent_roles)
        return client

    return _as


def test_an_adopting_prompt_edit_clears_the_transfer(world, as_user):
    w = world
    _task("moved", A, w.pat)
    _task("moved-trig", A, w.pat, task_type="trigger")
    _trigger("moved-hook", A, w.pat, task_id="moved-trig")
    db.set_user_agents(w.pat, [A], w.admin, agent_roles={A: "viewer"})
    manager = db.create_local_user("max@t.com", "Max M", "Max", "member", "x")
    db.set_user_agents(manager, [A], w.admin, agent_roles={A: "manager"})
    assert db.transfer_agent_scope_automations(A, w.pat, w.admin, list(transfer.KINDS))
    client = as_user(manager, "member", {A: "manager"})
    shown = client.get("/v1/tasks/moved").json()
    assert shown["transferred_from"] == w.pat and shown["transferred_at"]
    # A name edit keeps the record; a prompt edit by the manager adopts.
    assert client.patch("/v1/tasks/moved", json={"name": "Renamed"}).status_code == 200
    row = db.get_dynamic_task("moved")
    assert (row["transferred_from"], row["created_by"]) == (w.pat, w.admin)
    assert client.patch("/v1/tasks/moved", json={"prompt": "now mine"}).status_code == 200
    row = db.get_dynamic_task("moved")
    # The adopter owns it now: their provenance decides the grant, and
    # their own departure moves it on.
    assert row["transferred_from"] == "" and row["transferred_at"]
    assert row["created_by"] == manager
    assert client.get("/v1/tasks/moved").json()["transferred_from"] == ""
    # A trigger keeps one creator with the task it fires.
    assert client.patch("/v1/tasks/moved-trig", json={"prompt": "mine too"}).status_code == 200
    assert db.get_dynamic_task("moved-trig")["created_by"] == manager
    [hook] = _rows("triggers", "slug='moved-hook'")
    assert hook["created_by"] == manager and hook["transferred_from"] == w.pat
    db.set_user_agents(manager, [A], w.admin, agent_roles={A: "viewer"})
    asyncio.run(transfer.reconcile_person(manager, actor=w.admin, reason=offboarding.DEMOTED))
    for tid in ("moved", "moved-trig"):
        row = db.get_dynamic_task(tid)
        assert (row["created_by"], row["transferred_from"]) == (w.admin, manager)
    assert _rows("triggers", "slug='moved-hook'")[0]["created_by"] == w.admin


@pytest.mark.asyncio
async def test_register_subscribes_after_the_closer_and_the_lifespan_calls_it(world, monkeypatch):
    import ast
    from tests._paths import PROXY_DIR
    monkeypatch.setattr(transfer, "BOOT_RECONCILE_AFTER_S", 3600)
    transfer.register()
    try:
        priority, fn = offboarding._subscribers[transfer.SUBSCRIBER]
        assert priority == 20 and fn is transfer.on_offboard
    finally:
        offboarding.unsubscribe(transfer.SUBSCRIBER)
        for task in list(transfer._background):
            task.cancel()
    tree = ast.parse((PROXY_DIR / "startup.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "register"
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "offboarding_transfer"]
    assert calls, "startup.py must register the offboarding transfer"


def test_the_rows_name_who_they_were_transferred_from(world, as_user):
    """Task, trigger and notification rows moved at offboarding carry the
    first creator and the time, and the name to show: a live person's
    display name, a deleted person's retired username."""
    w = world
    _task("sched-x", A, w.pat)
    _task("trig-x", A, w.pat, task_type="trigger")
    _trigger("hook-x", A, w.pat, task_id="trig-x")
    _notification(A, w.pat)
    db.set_user_agents(w.pat, [A], w.admin, agent_roles={A: "viewer"})
    assert db.transfer_agent_scope_automations(A, w.pat, w.admin, list(transfer.KINDS))
    client = as_user(w.admin, "admin", {})

    def _views():
        tasks = {t["id"]: t for t in client.get(f"/v1/tasks?agent={A}").json()["tasks"]}
        trig = next(t for t in client.get(f"/v1/triggers?agent={A}").json()["triggers"]
                    if t["slug"] == "hook-x")
        notes = client.get(f"/v1/notifications?view=definitions&agent={A}").json()
        note = next(n for n in (notes.get("notifications") or notes) if n.get("created_by") == w.admin
                    and n.get("transferred_from") == w.pat)
        return tasks["sched-x"], client.get("/v1/tasks/sched-x").json(), trig, note

    for row in _views():
        assert row["transferred_from"] == w.pat and row["transferred_at"]
        assert row["transferred_from_name"] == "Pat"
    # Deleted: the retired username names them.
    assert db.delete_user(w.pat)
    for row in _views():
        assert row["transferred_from_name"] == w.pat_name


@pytest.mark.asyncio
async def test_an_unknown_creator_alone_is_named_once_and_moves_nothing(world):
    """A creator the install cannot place (an IdP subject with no user row)
    is listed for the owner to review; nothing moves, and a second boot says
    it again only in its own notice."""
    w = world
    _task("idp-only", A, "00u9zzzzYYYYxxxx0000")
    moved = await transfer.reconcile_at_boot()
    await _settle()
    assert moved == {}
    assert _rows("dynamic_tasks", "id='idp-only'")[0]["created_by"] == "00u9zzzzYYYYxxxx0000"
    [note] = w.fired
    assert note["target"] == w.owner and note["title"] == "Automations to review"
    assert "00u9zzzzYYYYxxxx0000" in note["body"] and A in note["body"]
    # Named once: the next boot is silent about it.
    assert await transfer.reconcile_at_boot() == {}
    await _settle()
    assert len(w.fired) == 1


@pytest.mark.asyncio
async def test_a_departed_persons_stored_wakes_go_with_their_standing(world):
    """A wake stored for the person (or scheduled by them) on an agent they
    lost is dropped; one on a Shared-only agent goes when they fall below
    the editor tier; everyone else's wakes and their wakes elsewhere stay."""
    w = world
    agent_store.create_agent("tr-so", "SO", collaborative=False, default_scope="agent")
    db.set_user_agents(w.pat, [A, B, "tr-so"], w.admin,
                       agent_roles={A: "editor", B: "editor", "tr-so": "editor"})
    for chat, agent in (("w-a", A), ("w-b", B), ("w-so", "tr-so")):
        db.create_chat(chat, f"agent::{agent}", agent)
        db.append_pending_delegate_wake(chat, "pat's", person=w.pat, role="editor")
        db.append_pending_delegate_wake(chat, "admin's", person=w.admin, role="admin")
    db.append_pending_delegate_wake("w-a", "scheduled by pat", role="editor", by=w.pat)
    # Pat leaves A, falls to contributor on the Shared-only agent, keeps B.
    db.set_user_agents(w.pat, [B, "tr-so"], w.admin,
                       agent_roles={B: "editor", "tr-so": "contributor"})
    await transfer.reconcile_person(w.pat, actor=w.admin, reason=offboarding.REMOVED)
    assert db.claim_pending_delegate_wake("w-a") == ["admin's"]
    assert db.claim_pending_delegate_wake("w-so") == ["admin's"]
    assert db.claim_pending_delegate_wake("w-b") == ["pat's", "admin's"]


@pytest.mark.asyncio
async def test_a_deletion_drops_every_stored_wake_of_the_person(world):
    w = world
    db.create_chat("w-del", f"agent::{B}", B)
    db.append_pending_delegate_wake("w-del", "pat's", person=w.pat, role="editor")
    assert db.delete_user(w.pat)
    await transfer.reconcile_person(w.pat, actor=w.admin, reason=offboarding.DELETED)
    assert db.claim_pending_delegate_wake("w-del") == []
