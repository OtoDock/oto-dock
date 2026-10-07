"""The offboarding hook (``services/agents/offboarding.py``): one event when
a person loses an agent or the editor tier on it, dispatched after the
change commits, on every admin path that takes standing away. The fillers
(D1 transfers automations, C1 closes sessions, D2 stops app servers) are
tested where they live; this module tests the event, the dispatcher and
that every path fires it.

Run: cd proxy && venv/bin/pytest tests/agents/test_offboarding.py -v
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from services.agents import offboarding
from services.agents.offboarding import AgentLoss
from storage import database as task_store
from storage.agents import agent_store
from tests._paths import PROXY_DIR

client = TestClient(app)

ADMIN = "local:offb-admin"
PERSON = "local:offb-person"


# ── losses ─────────────────────────────────────────────────────────────


def test_a_removed_agent_is_a_loss_of_access():
    [loss] = offboarding.losses("member", {"a": "editor"}, "member", {})
    assert loss == AgentLoss("a", "editor", "", "editor", "")
    assert loss.lost_access and loss.lost_editor and loss.lost_workspace and loss.lost_row


def test_a_lower_role_is_a_demotion():
    losses = offboarding.losses(
        "member", {"a": "manager", "b": "editor", "c": "contributor", "d": "viewer"},
        "member", {"a": "editor", "b": "contributor", "c": "viewer", "d": "viewer"})
    assert [(x.agent, x.old_role, x.new_role) for x in losses] == [
        ("a", "manager", "editor"), ("b", "editor", "contributor"), ("c", "contributor", "viewer")]
    assert [x.lost_editor for x in losses] == [False, True, False]
    assert [x.lost_workspace for x in losses] == [False, False, True]
    assert not any(x.lost_access or x.lost_row for x in losses)


def test_a_viewer_removed_still_loses_access():
    # NO_ACCESS and viewer share rank 0: the vanished role is its own test.
    [loss] = offboarding.losses("member", {"a": "viewer"}, "member", {})
    assert loss.lost_access and not loss.lost_editor


def test_a_promotion_or_an_unchanged_row_is_no_loss():
    assert offboarding.losses("member", {"a": "viewer"}, "member", {"a": "manager", "b": "viewer"}) == []
    assert offboarding.losses("member", {"a": "editor"}, "member", {"a": "editor"}) == []


def test_an_admin_keeps_access_but_a_removed_row_is_reported():
    # Apps, a user-paired machine and task delivery judge the row alone.
    [loss] = offboarding.losses("admin", {"a": "manager"}, "admin", {}, ["a", "b"])
    assert (loss.old_role, loss.new_role, loss.old_row, loss.new_row) == ("admin", "admin", "manager", "")
    assert loss.lost_row and not (loss.lost_access or loss.lost_editor)
    assert offboarding.losses("admin", {}, "admin", {"a": "viewer"}, ["a"]) == []


def test_an_admin_demoted_loses_every_agent_without_an_editor_row():
    losses = offboarding.losses("admin", {"a": "manager", "b": "viewer"}, "member",
                                {"a": "manager", "b": "viewer"}, ["a", "b", "c"])
    assert [(x.agent, x.old_role, x.new_role) for x in losses] == [
        ("a", "admin", "manager"), ("b", "admin", "viewer"), ("c", "admin", "")]
    assert [x.lost_editor for x in losses] == [False, True, True]


# ── the dispatcher ─────────────────────────────────────────────────────


@pytest.fixture
def recorded():
    """A clean registry with one recording subscriber."""
    saved = dict(offboarding._subscribers)
    offboarding._subscribers.clear()
    events: list[offboarding.OffboardEvent] = []

    async def record(event):
        events.append(event)

    offboarding.subscribe("record", record)
    yield events
    offboarding._subscribers.clear()
    offboarding._subscribers.update(saved)


@pytest.mark.asyncio
async def test_subscribers_run_in_order_and_one_failure_does_not_stop_the_next(recorded, caplog):
    order = []

    async def first(event):
        order.append("first")
        raise RuntimeError("boom")

    def second(event):  # a plain function runs in a worker thread
        order.append(("second", threading.current_thread() is threading.main_thread()))

    offboarding.subscribe("first", first)
    offboarding.subscribe("second", second)
    offboarding.subscribe("record", offboarding._subscribers["record"][1], priority=200)
    with caplog.at_level(logging.INFO, logger="claude-proxy.offboarding"):
        await offboarding.on_user_offboarded(offboarding.OffboardEvent(
            PERSON, offboarding.REMOVED, (AgentLoss("a", "editor", ""),), ADMIN))
    assert order == ["first", ("second", False)]
    assert len(recorded) == 1 and recorded[0].actor_sub == ADMIN
    assert "offboarding subscriber first failed" in caplog.text
    assert f"offboarding: {PERSON} removed on 1 agent(s) [a (editor->-)] by {ADMIN}" in caplog.text


@pytest.mark.asyncio
async def test_priority_orders_the_chain(recorded):
    order = []
    offboarding.subscribe("late", lambda e: order.append("late"), priority=200)
    offboarding.subscribe("early", lambda e: order.append("early"), priority=10)
    await offboarding.dispatch_losses(PERSON, [], ADMIN, deleted=True)
    assert order == ["early", "late"]


@pytest.mark.asyncio
async def test_a_slow_subscriber_is_left_running_past_its_budget(recorded, monkeypatch):
    monkeypatch.setattr(offboarding, "SUBSCRIBER_BUDGET_S", 0.05)
    finished = asyncio.Event()

    async def slow(event):
        await asyncio.sleep(0.2)
        finished.set()

    offboarding._subscribers.clear()
    offboarding.subscribe("slow", slow, priority=1)
    offboarding.subscribe("record", recorded.append, priority=2)
    await offboarding.on_user_offboarded(offboarding.OffboardEvent(PERSON, offboarding.DELETED, (), ADMIN))
    assert len(recorded) == 1 and not finished.is_set()
    await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.asyncio
async def test_the_route_waits_for_the_chain_only_briefly(recorded, monkeypatch):
    monkeypatch.setattr(offboarding, "ROUTE_WAIT_S", 0.05)
    done = asyncio.Event()

    async def slow(event):
        await asyncio.sleep(0.2)
        done.set()

    offboarding.subscribe("slow", slow)
    await offboarding.dispatch_losses(PERSON, [], ADMIN, deleted=True)
    assert not done.is_set()
    await asyncio.wait_for(done.wait(), 2)


@pytest.mark.asyncio
async def test_dispatch_splits_removed_and_demoted_and_skips_no_loss(recorded):
    await offboarding.dispatch_losses(PERSON, [], ADMIN)
    assert recorded == []
    await offboarding.dispatch_losses(
        PERSON, [AgentLoss("a", "editor", ""), AgentLoss("b", "manager", "viewer", "manager", "viewer"),
                 AgentLoss("c", "admin", "admin", "editor", "")], ADMIN,
        person={"username": "person", "email": "p@t.com", "display_name": "Per Son"})
    assert [(e.reason, [x.agent for x in e.agents], e.username, e.email, e.display_name)
            for e in recorded] == [
        ("removed", ["a", "c"], "person", "p@t.com", "Per Son"),
        ("demoted", ["b"], "person", "p@t.com", "Per Son")]


@pytest.mark.asyncio
async def test_a_deletion_is_dispatched_even_with_no_agent(recorded):
    await offboarding.dispatch_losses(PERSON, [], ADMIN, deleted=True)
    assert [(e.reason, e.agents) for e in recorded] == [("deleted", ())]


@pytest.mark.asyncio
async def test_an_unknown_reason_is_refused(recorded):
    with pytest.raises(ValueError):
        await offboarding.on_user_offboarded(offboarding.OffboardEvent(PERSON, "paused", (), ADMIN))


# ── the store ──────────────────────────────────────────────────────────


def test_set_user_agents_returns_the_rows_it_replaced_and_wrote():
    for slug in ("oa", "ob"):
        agent_store.create_agent(slug, slug)
    task_store.upsert_user(PERSON, "p@t.com", "Person", "member")
    first = task_store.set_user_agents(PERSON, ["oa"], ADMIN, agent_roles={"oa": "editor"})
    assert first == ("member", {}, {"oa": "editor"})
    second = task_store.set_user_agents(PERSON, ["ob"], ADMIN)
    assert second.before == {"oa": "editor"} and second.after == {"ob": "viewer"}


def test_an_edit_waits_for_another_writer_of_the_same_person():
    from storage.pg import get_conn
    for slug in ("oa", "ob"):
        agent_store.create_agent(slug, slug)
    task_store.upsert_user(PERSON, "p@t.com", "Person", "member")
    task_store.set_user_agents(PERSON, ["oa"], ADMIN)
    results = []
    with get_conn() as conn:
        # Another writer (an add, a role change) holds the person's row...
        conn.execute("SELECT 1 FROM users WHERE sub=%s FOR UPDATE", (PERSON,))
        conn.execute(
            "INSERT INTO user_agents (sub, agent, assigned_at, assigned_by, agent_role) "
            "VALUES (%s, 'ob', 'now', %s, 'editor')", (PERSON, ADMIN))
        editor = threading.Thread(
            target=lambda: results.append(task_store.set_user_agents(PERSON, ["oa"], ADMIN)))
        editor.start()
        editor.join(0.5)
        assert editor.is_alive(), "the edit did not wait for the row lock"
        conn.commit()
    editor.join(10)
    # ...and the edit saw that writer's committed row in its diff.
    assert results[0].before == {"oa": "viewer", "ob": "editor"}
    assert results[0].after == {"oa": "viewer"}


def test_upsert_user_returns_the_previous_platform_role():
    assert task_store.upsert_user(PERSON, "p@t.com", "Person", "admin") is None
    assert task_store.upsert_user(PERSON, "p@t.com", "Person", "member") == "admin"


def test_removal_deletes_the_memory_folder_without_following_a_link(tmp_path, monkeypatch):
    import config
    agent_store.create_agent("om", "om")
    task_store.upsert_user(PERSON, "p@t.com", "Person", "member")
    uname = task_store.get_username_by_sub(PERSON)
    task_store.set_user_agents(PERSON, ["om"], ADMIN)
    agent_dir = config.get_agent_dir("om")
    mem = agent_dir / "users" / uname / "context" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "topic.md").write_text("x")
    task_store.set_user_agents(PERSON, [], ADMIN)
    assert not mem.exists()
    # A session that swaps its context folder for a link reaches nothing.
    victim = tmp_path / "victim"
    (victim / "memory").mkdir(parents=True)
    (victim / "memory" / "keep.md").write_text("keep")
    task_store.set_user_agents(PERSON, ["om"], ADMIN)
    ctx = agent_dir / "users" / uname / "context"
    import shutil
    shutil.rmtree(ctx)
    ctx.symlink_to(victim)
    task_store.set_user_agents(PERSON, [], ADMIN)
    assert (victim / "memory" / "keep.md").read_text() == "keep"



def test_removal_refuses_an_agent_folder_swapped_for_a_link(tmp_path):
    """The agent folder itself is not a root: a process that can write the
    agents tree (a sidecar's /agents mount) swaps it for a link, and the
    removal must not follow it."""
    import shutil

    import config
    agent_store.create_agent("ol", "ol")
    task_store.upsert_user(PERSON, "p@t.com", "Person", "member")
    uname = task_store.get_username_by_sub(PERSON)
    task_store.set_user_agents(PERSON, ["ol"], ADMIN)
    agent_dir = config.get_agent_dir("ol")
    victim = tmp_path / "victim"
    keep = victim / "users" / uname / "context" / "memory" / "keep.md"
    keep.parent.mkdir(parents=True)
    keep.write_text("keep")
    shutil.rmtree(agent_dir)
    agent_dir.symlink_to(victim)
    task_store.set_user_agents(PERSON, [], ADMIN)
    assert keep.read_text() == "keep"

# ── the routes ─────────────────────────────────────────────────────────


@pytest.fixture
def as_admin():
    task_store.upsert_user(ADMIN, "admin@t.com", "Admin", "admin")
    task_store.upsert_user(PERSON, "person@t.com", "Person", "member")
    for slug in ("oa", "ob", "oc"):
        agent_store.create_agent(slug, slug)

    async def _user():
        return UserContext(sub=ADMIN, email="admin@t.com", name="Admin", role="admin", agent_roles={})

    app.dependency_overrides[get_current_user] = _user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _put_agents(roles: dict[str, str]):
    return client.put(f"/v1/admin/users/{PERSON}/agents",
                      json={"agents": list(roles), "agent_roles": roles})


def test_the_agents_route_dispatches_a_removal_and_a_demotion(as_admin, recorded):
    assert _put_agents({"oa": "editor", "ob": "manager"}).status_code == 200
    assert recorded == []  # an addition takes nothing away
    seen_rows = []
    offboarding.subscribe("rows", lambda e: seen_rows.append(task_store.get_user_agent_roles(e.sub)))
    assert _put_agents({"ob": "contributor"}).status_code == 200
    assert [(e.reason, [(x.agent, x.old_role, x.new_role) for x in e.agents], e.actor_sub)
            for e in recorded] == [
        ("removed", [("oa", "editor", "")], ADMIN),
        ("demoted", [("ob", "manager", "contributor")], ADMIN),
    ]
    # The subscriber ran after the commit: it reads the new rows.
    assert seen_rows[0] == {"ob": "contributor"}
    assert recorded[0].username == task_store.get_username_by_sub(PERSON)


def test_re_adding_and_raising_dispatch_nothing(as_admin, recorded):
    _put_agents({"oa": "viewer"})
    _put_agents({"oa": "viewer", "ob": "viewer"})
    _put_agents({"oa": "manager", "ob": "viewer"})
    assert recorded == []


def test_the_role_route_dispatches_an_admin_demotion(as_admin, recorded):
    task_store.upsert_user(PERSON, "person@t.com", "Person", "admin")
    task_store.set_user_agents(PERSON, ["oa"], ADMIN, agent_roles={"oa": "manager"})
    resp = client.put(f"/v1/admin/users/{PERSON}/role", json={"role": "member"})
    assert resp.status_code == 200, resp.text
    assert [(e.reason, sorted(x.agent for x in e.agents)) for e in recorded] == [
        ("removed", sorted(set(agent_store.get_agent_slugs()) - {"oa"})),
        ("demoted", ["oa"]),
    ]
    assert recorded[1].agents[0].old_role == "admin"
    assert recorded[1].agents[0].new_role == "manager"
    assert (recorded[1].platform_before, recorded[1].platform_after) == ("admin", "member")


def test_an_admin_demotion_keeps_the_roles_of_the_agents_that_stay(as_admin, recorded):
    agent_store.update_agent("oc", admin_only=True)
    task_store.upsert_user(PERSON, "person@t.com", "Person", "admin")
    task_store.set_user_agents(PERSON, ["oa", "oc"], ADMIN, agent_roles={"oa": "manager", "oc": "manager"})
    resp = client.put(f"/v1/admin/users/{PERSON}/role", json={"role": "member"})
    assert resp.status_code == 200, resp.text
    assert task_store.get_user_agent_roles(PERSON) == {"oa": "manager"}
    demoted = {x.agent: x for e in recorded for x in e.agents}
    assert demoted["oa"].new_role == "manager" and not demoted["oa"].lost_editor
    assert demoted["oc"].lost_access


def test_the_delete_route_dispatches_one_deletion_with_every_agent(as_admin, recorded):
    _put_agents({"oa": "editor", "ob": "viewer"})
    uname = task_store.get_username_by_sub(PERSON)
    resp = client.delete(f"/v1/admin/users/{PERSON}")
    assert resp.status_code == 200, resp.text
    [event] = recorded
    assert event.reason == "deleted" and event.actor_sub == ADMIN and event.username == uname
    assert event.email == "person@t.com" and event.platform_before == "member"
    assert sorted(x.agent for x in event.agents) == ["oa", "ob"]
    assert task_store.get_user(PERSON) is None


def test_deleting_a_person_removes_their_user_scope_automations():
    from storage.pg import get_conn
    agent_store.create_agent("oa", "oa")
    task_store.upsert_user(PERSON, "p@t.com", "Person", "member")
    task_store.set_user_agents(PERSON, ["oa"], ADMIN)
    for scope in ("user", "agent"):
        task_store.create_dynamic_task(
            f"offb-{scope}", "oa", "t", "p", "cli", "scheduled", "0 9 * * *", None, None, 600,
            PERSON, scope=scope)
    assert task_store.delete_user(PERSON)
    with get_conn() as conn:
        left = [r["id"] for r in conn.execute(
            "SELECT id FROM dynamic_tasks WHERE created_by=%s ORDER BY id", (PERSON,)).fetchall()]
    # The agent-scope one stays for the offboarding transfer.
    assert left == ["offb-agent"]


def test_a_failing_subscriber_never_blocks_the_change(as_admin, recorded):
    async def broken(event):
        raise RuntimeError("down")

    offboarding.subscribe("broken", broken)
    _put_agents({"oa": "editor"})
    assert _put_agents({}).status_code == 200
    assert task_store.get_user_agent_roles(PERSON) == {}


# ── the guard: no membership path can skip the hook silently ───────────

_MUTATORS = {"set_user_agents", "set_user_agent_role", "delete_user", "update_user_role", "upsert_user"}

# {module: reason}. A module that calls a mutator, hands one to a thread
# or imports one is listed here; a new one fails until it dispatches the
# hook after the change or says why it takes no standing away.
KNOWN_CALLERS = {
    "proxy/api/auth/admin_users.py": "dispatches offboarding after every change",
    "proxy/api/auth/identity.py": "OIDC login refreshes the platform role and dispatches offboarding on a lowered one",
    "proxy/services/agents/shared_only_members.py": "the Shared-only switch's removals dispatch offboarding",
}

# Raw SQL that rewrites memberships or the platform role outside the store.
_RAW_SQL = re.compile(r"(DELETE\s+FROM|UPDATE|INSERT\s+INTO)\s+user_agents\b|UPDATE\s+users\s+SET[^;]*\brole\s*=",
                      re.IGNORECASE)
KNOWN_RAW_SQL = {
    "proxy/storage/identity/db_users.py": "the store the hook's callers use",
    "proxy/storage/agents/agent_store.py": "deleting an agent deletes its rows: the agent is gone, no one is offboarded",
}


def _mutator_uses(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _MUTATORS:
            found.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in _MUTATORS:
            found.add(node.id)
        elif isinstance(node, ast.alias) and node.name in _MUTATORS:
            found.add(node.name)
    return found


def _proxy_sources():
    repo = PROXY_DIR.parent
    for path in PROXY_DIR.rglob("*.py"):
        rel = path.relative_to(repo).as_posix()
        if "/tests/" in rel or "/venv/" in rel:
            continue
        yield rel, path.read_text(encoding="utf-8")


def test_every_membership_mutator_caller_is_known():
    callers = {}
    for rel, text in _proxy_sources():
        if rel.startswith("proxy/storage/"):
            continue
        if uses := _mutator_uses(ast.parse(text)):
            callers[rel] = uses
    unknown = sorted(set(callers) - set(KNOWN_CALLERS))
    assert not unknown, (f"{unknown} change membership or a role: dispatch "
                         "services/agents/offboarding after the change, or list it here with why not")
    gone = sorted(set(KNOWN_CALLERS) - set(callers))
    assert not gone, f"{gone} no longer call a mutator: drop them"


def test_membership_sql_lives_in_the_known_stores():
    hits = sorted(rel for rel, text in _proxy_sources() if _RAW_SQL.search(text))
    assert hits == sorted(KNOWN_RAW_SQL), hits


def test_the_guard_sees_a_mutator_handed_to_a_thread_or_imported():
    handed = ast.parse("await asyncio.to_thread(task_store.set_user_agents, sub, [], by)")
    imported = ast.parse("from storage.identity.db_users import delete_user as drop")
    assert _mutator_uses(handed) == {"set_user_agents"}
    assert _mutator_uses(imported) == {"delete_user"}


def test_an_identity_provider_role_drop_dispatches_like_the_role_route(as_admin, recorded, monkeypatch):
    """0a-to-C2 item 1: a login whose identity provider lowered the platform
    role runs the rule an admin's change does, with no person as its actor."""
    from unittest.mock import AsyncMock
    from urllib.parse import parse_qs, urlparse

    import config
    from api.auth import identity
    from auth.providers.base import AuthResult
    for key, value in (("OIDC_ENABLED", True), ("OIDC_DISCOVERY_URL", ""),
                       ("OIDC_AUTHORIZE_URL", "https://idp.example.com/authorize"),
                       ("OIDC_TOKEN_URL", "https://idp.example.com/token"),
                       ("OIDC_CLIENT_ID", "test-client"),
                       ("OIDC_REDIRECT_URI", "https://dash.example.com/auth/callback")):
        monkeypatch.setattr(config, key, value)
    task_store.upsert_user(PERSON, "person@t.com", "Person", "admin")
    task_store.update_user_auth_fields(PERSON, auth_provider="oidc:mock-sso")
    task_store.set_user_agents(PERSON, ["oa"], ADMIN, agent_roles={"oa": "manager"})
    monkeypatch.setattr(identity._oidc_provider, "authenticate", AsyncMock(return_value=AuthResult(
        success=True, sub=PERSON, email="person@t.com", name="Person", role="member",
        auth_provider="oidc:mock-sso")))
    app.dependency_overrides.pop(get_current_user, None)
    browser = TestClient(app)
    url = browser.get("/auth/oidc-url").json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    resp = browser.post("/auth/callback", json={"code": "c", "state": state})
    assert resp.status_code == 200, resp.text
    assert [(e.reason, sorted(x.agent for x in e.agents)) for e in recorded] == [
        ("removed", sorted(set(agent_store.get_agent_slugs()) - {"oa"})),
        ("demoted", ["oa"]),
    ]
    assert {e.actor_sub for e in recorded} == {""}
    assert task_store.get_user_agent_roles(PERSON) == {"oa": "manager"}
