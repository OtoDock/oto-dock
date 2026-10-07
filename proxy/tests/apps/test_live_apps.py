"""Live apps (APPS.md "Live apps"): the state document store, the push
/ state / open hooks, the audience rule, the per-connection live queue and
viewer focus.

The load-bearing assertions: a state write never loses a concurrent
writer's keys (row lock + merge), the document limits hold after the merge
(not only on the patch), a hard unpin reaps the state through the FK, and
nothing about an app reaches a user who may not see the row.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from services.apps import audience, focus_context
from services.notifications import notification_manager as nm
from storage import database as task_store

client = TestClient(app)

AGENT = "live-agent"
SID = "sess-live-alice"          # alice, personal scope (manager)
SID_SHARED = "sess-live-shared"  # service session, agent scope
SID_VIEWER = "sess-live-viewer"  # bob, a viewer chatting with a shared-only agent


def _row(slug: str = "board", username: str = "", owner_sub: str | None = None,
         title: str | None = None) -> dict:
    return task_store.upsert_app(
        AGENT, username, owner_sub, slug, title=slug.title() if title is None else title,
        rel_path=f"workspace/apps/{slug}.html",
    )


def _user(sub: str = "alice-sub", role: str = "member",
          agent_roles: dict[str, str] | None = None) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role,
                       agents=[AGENT], agent_roles=agent_roles or {AGENT: "manager"})


def _set_username(sub: str, username: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh_state():
    from api.apps import apps as apps_api
    from auth import rate_limiter
    apps_api._fire_rate.clear()
    audience._cache.clear()
    focus_context._prefs_cache.clear()
    nm._user_connections.clear()
    nm._chat_turn_origin.clear()
    nm._chat_turn_user.clear()
    rate_limiter._attempts.clear()
    yield
    apps_api._fire_rate.clear()
    audience._cache.clear()
    focus_context._prefs_cache.clear()
    nm._user_connections.clear()
    nm._chat_turn_origin.clear()
    nm._chat_turn_user.clear()
    rate_limiter._attempts.clear()
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def clock(monkeypatch):
    """The limiter's monotonic clock, advanced by hand: the per-app windows
    (100 ms for a push, 500 ms for a state write) must not depend on how
    fast the runner gets from one request to the next."""
    from types import SimpleNamespace
    from api.apps import apps as apps_api
    now = [1000.0]
    monkeypatch.setattr(apps_api, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def people():
    """alice (manager) and bob (viewer) on the agent, carol outside it; the
    three sessions registered."""
    for sub, name, role in (("alice-sub", "alice", "manager"),
                            ("bob-sub", "bob", "viewer")):
        task_store.upsert_user(sub, f"{sub}@test.com", name.title(), "member")
        _set_username(sub, name)
        task_store.add_user_agent(sub, AGENT, role, "test")
    task_store.upsert_user("carol-sub", "carol@test.com", "Carol", "member")
    _set_username("carol-sub", "carol")
    session_state.set_session_security(SID, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    session_state.set_session_security(SID_SHARED, SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False))
    session_state.set_session_security(SID_VIEWER, SecurityContext(
        role="viewer", username="bob", agent=AGENT, is_admin_agent=False,
        session_scope="agent"))
    yield
    for sid in (SID, SID_SHARED, SID_VIEWER):
        session_state._session_security.pop(sid, None)


def _hook(op: str, payload: dict, sid: str = SID):
    payload.setdefault("session_id", sid)
    with patch("api.hooks.pins.verify_session_match_async"):
        return client.post(f"/v1/hooks/apps/{op}", json=payload,
                           headers={"Authorization": "Bearer dummy"})


def _connect(sub: str, *, active: bool = True, away: bool = False,
             focus: dict | None = None) -> str:
    cid = str(uuid.uuid4())
    nm.register_user_connection(sub, cid, asyncio.Queue(), live_queue=nm.LiveQueue())
    nm.set_connection_active(sub, cid, active, away=away)
    if focus is not None:
        nm.set_connection_focus(sub, cid, focus)
    return cid


def _frames(sub: str, cid: str) -> list[dict]:
    q = nm.get_connection(sub, cid).live_queue
    out = []
    while True:
        f = q.get_nowait()
        if f is None:
            return out
        out.append(f)


# ───────────────────────────── the state document ─────────────────────────


def test_state_starts_empty_and_rev_grows_per_write():
    app = _row()
    assert task_store.get_app_state(app["id"]) == ({}, 0)
    doc, rev = task_store.write_app_state(app["id"], {"a": 1}, updated_by="s1")
    assert (doc, rev) == ({"a": 1}, 1)
    doc, rev = task_store.write_app_state(app["id"], {"b": 2})
    assert (doc, rev) == ({"a": 1, "b": 2}, 2)
    assert task_store.get_app_state(app["id"]) == ({"a": 1, "b": 2}, 2)


def test_merge_patch_is_rfc_7386():
    app = _row()
    task_store.write_app_state(app["id"], {
        "projects": {"alpha": {"note": "waiting", "status": "blocked"},
                     "beta": {"status": "ok"}},
        "count": 3,
    })
    # Nested merge keeps siblings; null deletes; a scalar replaces a subtree.
    doc, _ = task_store.write_app_state(app["id"], {
        "projects": {"alpha": {"note": None, "status": "done"}, "beta": "gone"},
        "count": None,
    })
    assert doc == {"projects": {"alpha": {"status": "done"}, "beta": "gone"}}
    # A whole-document replace drops everything not in the new document.
    doc, rev = task_store.write_app_state(app["id"], {"fresh": True}, replace=True)
    assert doc == {"fresh": True} and rev == 3


def test_state_limits_apply_to_the_merged_document():
    app = _row()
    big = "x" * (40 * 1024)
    task_store.write_app_state(app["id"], {"one": big})
    # Each patch is small enough on its own; together they cross the cap.
    with pytest.raises(task_store.AppStateError, match="64 KB"):
        task_store.write_app_state(app["id"], {"two": big})
    assert task_store.get_app_state(app["id"])[1] == 1
    with pytest.raises(task_store.AppStateError, match="JSON object"):
        task_store.write_app_state(app["id"], ["not", "an", "object"], replace=True)
    with pytest.raises(task_store.AppStateError, match="characters"):
        task_store.write_app_state(app["id"], {"k" * 129: 1})
    deep: dict = {}
    node = deep
    for _ in range(17):
        node["d"] = {}
        node = node["d"]
    with pytest.raises(task_store.AppStateError, match="deeper"):
        task_store.write_app_state(app["id"], deep)


def test_state_for_a_missing_app_is_a_lookup_error():
    with pytest.raises(LookupError):
        task_store.write_app_state("no-such-app", {"a": 1})
    assert task_store.get_app_state("no-such-app") == ({}, 0)


def test_hard_unpin_reaps_the_state_soft_unpin_keeps_it():
    app = _row()
    task_store.write_app_state(app["id"], {"a": 1})
    assert task_store.set_app_hidden(app["id"], True)
    assert task_store.get_app_state(app["id"]) == ({"a": 1}, 1)
    assert task_store.delete_app(app["id"])
    assert task_store.get_app_state(app["id"]) == ({}, 0)


def test_state_rows_are_per_app():
    a, b = _row("a"), _row("b")
    task_store.write_app_state(a["id"], {"who": "a"})
    task_store.write_app_state(b["id"], {"who": "b"})
    assert task_store.get_app_state(a["id"])[0] == {"who": "a"}
    assert task_store.get_app_state(b["id"])[0] == {"who": "b"}


# ───────────────────────────── the live queue ─────────────────────────────


def test_live_queue_coalesces_state_keeps_open_and_evicts_pushes_first():
    q = nm.LiveQueue(maxsize=4)
    assert q.put({"type": "app_state", "app_id": "a", "rev": 1})
    assert q.put({"type": "app_state", "app_id": "a", "rev": 2})   # replaces in place
    assert q.put({"type": "app_state", "app_id": "a", "rev": 1})   # older rev: slot keeps 2
    assert len(q) == 1 and q.get_nowait()["rev"] == 2
    for i in range(3):
        assert q.put({"type": "app_push", "app_id": "a", "payload": i})
    assert q.put({"type": "open_app", "app_id": "a"})
    assert len(q) == 4
    assert q.put({"type": "app_push", "app_id": "a", "payload": 3})  # evicts push 0
    items = [q.get_nowait() for _ in range(4)]
    assert [i["payload"] for i in items if i["type"] == "app_push"] == [1, 2, 3]
    assert any(i["type"] == "open_app" for i in items)
    assert q.get_nowait() is None
    # Nothing evictable and the newcomer is a push: refused, open survives.
    q2 = nm.LiveQueue(maxsize=1)
    assert q2.put({"type": "open_app", "app_id": "a"})
    assert not q2.put({"type": "app_push", "app_id": "a"})
    assert q2.get_nowait()["type"] == "open_app"


@pytest.mark.asyncio
async def test_live_queue_get_parks_and_a_cancelled_reader_loses_nothing():
    q = nm.LiveQueue()
    reader = asyncio.create_task(q.get())
    await asyncio.sleep(0)
    q.put({"type": "app_push", "app_id": "a"})
    assert (await asyncio.wait_for(reader, 1))["app_id"] == "a"
    reader = asyncio.create_task(q.get())
    await asyncio.sleep(0)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    q.put({"type": "app_push", "app_id": "b"})
    assert q.get_nowait()["app_id"] == "b"


@pytest.mark.asyncio
async def test_drain_live_queue_forwards_everything_waiting():
    import ws.dashboard  # noqa: F401 — module import order
    from ws.dashboard import DashboardConnection
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.live_queue = nm.LiveQueue()
    sent: list[dict] = []

    async def _send(d: dict) -> None:
        sent.append(d)

    conn._send = _send  # type: ignore[method-assign]
    conn.live_queue.put({"type": "app_push", "app_id": "a"})
    conn.live_queue.put({"type": "app_state", "app_id": "a", "rev": 1, "doc": {}})
    await conn._drain_live_queue()
    assert [f["type"] for f in sent] == ["app_push", "app_state"]
    assert len(conn.live_queue) == 0


# ───────────────────────────── focus registry ─────────────────────────────


def test_focus_per_connection_newest_wins_hidden_clears_away_keeps():
    a = _connect("alice-sub", focus={"surface": "app", "app_id": "app-1", "chat_id": "c1"})
    b = _connect("alice-sub", focus={"surface": "chat", "chat_id": "c2"})
    assert nm.connection_focus("alice-sub", a) == {"surface": "app", "app_id": "app-1", "chat_id": "c1"}
    assert nm.user_focus("alice-sub")["surface"] == "chat"  # b is newer
    nm.set_connection_focus("alice-sub", a, {"surface": "app", "app_id": "app-2"})
    assert nm.user_focus("alice-sub")["app_id"] == "app-2"
    # Anything but a known surface with short ids is no focus.
    for bad in ({"surface": "app"}, {"surface": "nope"},
                {"surface": "app", "app_id": "x" * 65}, "garbage", None):
        nm.set_connection_focus("alice-sub", b, bad)
        assert nm.connection_focus("alice-sub", b) is None
    # A visible idle screen keeps its focus; a hidden tab loses it.
    nm.set_connection_active("alice-sub", a, False, away=True)
    assert nm.connection_focus("alice-sub", a)
    nm.set_connection_active("alice-sub", a, False, away=False)
    assert nm.connection_focus("alice-sub", a) is None
    nm.unregister_user_connection("alice-sub", b)
    assert nm.user_focus("alice-sub") is None
    assert nm.user_focus("nobody") is None
    assert nm.connection_focus("alice-sub", "unknown") is None


def test_push_live_reaches_every_connection_or_the_named_one():
    a = _connect("alice-sub")
    b = _connect("alice-sub", active=False)
    assert nm.push_live("alice-sub", {"type": "app_push", "app_id": "x"}) == 2
    assert nm.push_live("alice-sub", {"type": "open_app", "app_id": "x"}, connection_id=b) == 1
    assert len(_frames("alice-sub", a)) == 1
    assert [f["type"] for f in _frames("alice-sub", b)] == ["app_push", "open_app"]
    assert nm.push_live("nobody", {"type": "app_push"}) == 0


def test_chat_turn_origin_names_the_sender_and_dies_with_the_connection():
    cid = _connect("bob-sub")
    nm.set_chat_turn_origin("bob-sub", "chat-1", cid)
    assert nm.chat_turn_origin("chat-1") == (cid, "web", "bob-sub")
    nm.unregister_user_connection("bob-sub", cid)
    assert nm.chat_turn_origin("chat-1") is None
    assert nm.chat_turn_origin("never") is None


# ───────────────────────────── the focus line ─────────────────────────────


def test_format_focus_line_flattens_the_title():
    assert focus_context.format_focus_line("A\tB [x] ]\n c", "ab") == \
        '[The user is looking at the app "A B x c" (ab) right now.]'
    assert focus_context.format_focus_line("", "ab") == \
        '[The user is looking at the app "ab" (ab) right now.]'
    assert len(focus_context.format_focus_line("t" * 500, "s")) < 200
    assert focus_context.FOCUS_PRELUDE_RE.match(
        focus_context.format_focus_line("Ops", "ops") + "\n\nhello")


def test_focus_line_only_for_a_visible_app_and_honours_the_setting(people):
    app_row = _row("ops", title="Ops [live] \n board")
    cid = _connect("alice-sub", focus={"surface": "app", "app_id": app_row["id"]})
    line = focus_context.focus_line("alice-sub", connection_id=cid)
    assert line == '[The user is looking at the app "Ops live board" (ops) right now.]'
    # A spoken turn reads the newest screen of the user.
    assert focus_context.focus_line("alice-sub") == line
    # Chat or home on the sending screen: nothing, even with the app open elsewhere.
    other = _connect("alice-sub", focus={"surface": "chat", "chat_id": "c"})
    assert focus_context.focus_line("alice-sub", connection_id=other) == ""
    # A user who cannot see the row gets nothing however she focuses it.
    ccid = _connect("carol-sub", focus={"surface": "app", "app_id": app_row["id"]})
    assert focus_context.focus_line("carol-sub", connection_id=ccid) == ""
    # The setting turns the agent side off.
    from storage.prefs import user_ui_prefs_store
    user_ui_prefs_store.upsert_prefs("alice-sub", {"share_focus_with_agents": False})
    focus_context.invalidate_prefs("alice-sub")
    assert focus_context.focus_line("alice-sub", connection_id=cid) == ""
    user_ui_prefs_store.upsert_prefs("alice-sub", {"share_focus_with_agents": True})
    focus_context.invalidate_prefs("alice-sub")
    assert focus_context.focus_line("alice-sub", connection_id=cid) == line
    # A soft-unpinned row is nobody's screen.
    task_store.set_app_hidden(app_row["id"], True)
    assert focus_context.focus_line("alice-sub", connection_id=cid) == ""
    # An unknown connection: nothing.
    assert focus_context.focus_line("alice-sub", connection_id="gone") == ""


def test_prelude_matchers_strip_the_focus_line_for_titles():
    import ws.dashboard  # noqa: F401 — module import order
    from core.session.transcript_tailer import _title_from_prompt
    from services.title_generator import deterministic_title
    from ws.dashboard_chat_text import _TIME_PRELUDE_RE as ws_re
    text = (
        "[Current time: Monday, June 1, 2026 10:00 (10:00 AM) Europe/Athens (UTC+03:00)]\n\n"
        '[The user is looking at the app "Ops" (ops) right now.]\n\n'
        "move lane three to done"
    )
    assert _title_from_prompt(text) == "move lane three to done"
    assert deterministic_title(text) == "move lane three to done"
    assert ws_re.sub("", ws_re.sub("", text, count=1), count=1) == "move lane three to done"
    # A user quoting the shape mid-message is never touched.
    assert deterministic_title('say [The user is looking at the app "x" (x) right now.]').startswith("say")


def test_duplex_prompt_carries_the_focus_line_first():
    import ws.dashboard  # noqa: F401 — module import order
    from ws import duplex_attach
    st = duplex_attach._AttachState(execution_path="claude-code-cli", context_injected=True)
    line = '[The user is looking at the app "A" (a) right now.]'
    assert duplex_attach._build_prompt(st, "hello", None, focus=line) == f"{line}\n\nhello"
    assert duplex_attach._build_prompt(st, "hello", None) == "hello"


# ───────────────────────────── the audience ───────────────────────────────


def test_audience_shared_personal_hides_and_the_dock_chat_gate(people):
    task_store.upsert_user("root-sub", "root@test.com", "Root", "admin")
    known = {"alice-sub", "bob-sub", "carol-sub", "root-sub"}

    def _who(row: dict) -> list[str]:
        # The test database may carry other admins; they are audience too.
        return [s for s in audience.app_audience(row) if s in known]

    shared = _row("team")
    assert _who(shared) == ["alice-sub", "bob-sub", "root-sub"]
    # Cached for a moment; forget() shows the change at once.
    task_store.hide_app_for_user(shared["id"], "bob-sub")
    assert "bob-sub" in _who(shared)
    audience.forget(shared["id"])
    assert _who(shared) == ["alice-sub", "root-sub"]
    personal = _row("mine", username="alice", owner_sub="alice-sub")
    assert audience.app_audience(personal) == ["alice-sub"]
    orphan = _row("orphan", username="bob", owner_sub=None)
    assert audience.app_audience(orphan) == ["bob-sub"]
    task_store.set_app_hidden(shared["id"], True)
    audience.forget(shared["id"])
    assert audience.app_audience(task_store.get_app(shared["id"])) == []
    # A Dock pin in alice's private chat reaches alice (and admins), not bob.
    task_store.create_chat("chat-priv", "alice-sub", AGENT)
    dock = _row("dock")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE pinned_apps SET scope_chat_id=%s WHERE id=%s", ("chat-priv", dock["id"]))
        conn.commit()
    assert _who(task_store.get_app(dock["id"])) == ["alice-sub", "root-sub"]
    with get_conn() as conn:
        conn.execute("UPDATE pinned_apps SET scope_chat_id=%s WHERE id=%s", ("chat-missing", dock["id"]))
        conn.commit()
    audience.forget(dock["id"])
    assert audience.app_audience(task_store.get_app(dock["id"])) == []


# ───────────────────────────── the hooks ──────────────────────────────────


def test_push_hook_authority_size_rate_and_fan_out(people, clock):
    _row("team")
    a = _connect("alice-sub")
    b = _connect("bob-sub", active=False)   # a hidden tab still gets pushes
    c = _connect("carol-sub")
    r = _hook("push", {"slug": "team", "payload": {"lane": 3}}, sid=SID_SHARED)
    assert r.status_code == 200 and r.json()["screens"] == 2
    fa = _frames("alice-sub", a)
    assert fa[0]["type"] == "app_push" and fa[0]["payload"] == {"lane": 3} and fa[0]["ts"] > 0
    assert len(_frames("bob-sub", b)) == 1
    assert _frames("carol-sub", c) == []
    # A viewer's human session may not write the team dashboard; the service session may.
    assert _hook("push", {"slug": "team", "payload": 1}, sid=SID_VIEWER).status_code == 403
    assert _hook("push", {"slug": "team", "payload": "x" * (32 * 1024 + 1)}, sid=SID_SHARED).status_code == 400
    assert _hook("push", {"slug": "nope", "payload": 1}).status_code == 404
    assert _hook("push", {"slug": "team", "payload": 1, "session_id": "unknown"}).status_code == 400
    # Ten per second per app.
    _row("fast")
    assert _hook("push", {"slug": "fast", "payload": 1}, sid=SID_SHARED).status_code == 200
    assert _hook("push", {"slug": "fast", "payload": 2}, sid=SID_SHARED).status_code == 429
    # A personal row reaches its owner only.
    _frames("alice-sub", a)
    _frames("bob-sub", b)
    _row("mine", username="alice", owner_sub="alice-sub")
    r = _hook("push", {"slug": "mine", "payload": "hi"})
    assert r.status_code == 200 and r.json()["screens"] == 1
    assert _frames("alice-sub", a)[0]["payload"] == "hi"
    assert _frames("bob-sub", b) == []


def test_state_hook_merges_delivers_and_validates(people, clock):
    row = _row("team")
    a = _connect("alice-sub")
    r = _hook("state", {"slug": "team", "patch": {"phase": "build"}}, sid=SID_SHARED)
    assert r.status_code == 200 and r.json()["rev"] == 1
    f = _frames("alice-sub", a)[0]
    assert f["type"] == "app_state" and f["doc"] == {"phase": "build"} and f["rev"] == 1
    # Two per second per app.
    assert _hook("state", {"slug": "team", "patch": {"x": 1}}, sid=SID_SHARED).status_code == 429
    clock[0] += 0.55
    r = _hook("state", {"slug": "team", "doc": {"fresh": True}}, sid=SID_SHARED)
    assert r.status_code == 200 and r.json()["rev"] == 2
    assert task_store.get_app_state(row["id"]) == ({"fresh": True}, 2)
    clock[0] += 0.55
    assert _hook("state", {"slug": "team", "patch": ["no"]}, sid=SID_SHARED).status_code == 400
    assert _hook("state", {"slug": "team", "patch": {"k" * 129: 1}}, sid=SID_SHARED).status_code == 400
    assert _hook("state", {"slug": "team", "patch": {"a": 1}}, sid=SID_VIEWER).status_code == 403
    assert _hook("state", {"slug": "nope", "patch": {"a": 1}}).status_code == 404


def test_read_state_route_follows_app_access(people):
    row = _row("team")
    task_store.write_app_state(row["id"], {"a": 1})
    app.dependency_overrides[get_current_user] = lambda: _user("alice-sub")
    r = client.get(f"/v1/apps/{row['id']}/state")
    assert r.status_code == 200 and r.json() == {"doc": {"a": 1}, "rev": 1}
    fresh = _row("fresh")
    assert client.get(f"/v1/apps/{fresh['id']}/state").json() == {"doc": {}, "rev": 0}
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        sub="carol-sub", email="c@test.com", name="Carol", role="member", agents=[], agent_roles={})
    assert client.get(f"/v1/apps/{row['id']}/state").status_code == 404
    assert client.get("/v1/apps/nope/state").status_code == 404
    app.dependency_overrides[get_current_user] = lambda: None
    assert client.get(f"/v1/apps/{row['id']}/state").status_code == 401


def test_open_hook_targets_the_human_in_the_conversation(people):
    row = _row("team")
    task_store.create_chat("chat-live", "alice-sub", AGENT)
    with patch("api.hooks.routing.resolve_hook_chat_id", return_value="chat-live"):
        # No screen at all, then only a hidden tab: nothing to show.
        assert _hook("open", {"slug": "team"}).json()["status"] == "no_screen"
        hidden = _connect("alice-sub", active=False)
        assert _hook("open", {"slug": "team"}).json()["status"] == "no_screen"
        # A visible idle screen (a wall display) counts.
        wall = _connect("alice-sub", active=False, away=True)
        r = _hook("open", {"slug": "team"})
        assert r.json() == {"status": "opened", "app_id": row["id"], "screens": 1}
        f = _frames("alice-sub", wall)
        assert f == [{"type": "open_app", "app_id": row["id"], "title": "Team", "agent": AGENT,
                      "scope_chat_id": "", "scope_project_id": ""}]
        assert _frames("alice-sub", hidden) == []
        # The screen the user is looking at wins over the others.
        laptop = _connect("alice-sub", focus={"surface": "chat", "chat_id": "chat-live"})
        assert _hook("open", {"slug": "team"}).json()["screens"] == 1
        assert len(_frames("alice-sub", laptop)) == 1 and _frames("alice-sub", wall) == []
        # Three per minute, counted only when something is shown.
        assert _hook("open", {"slug": "team"}).status_code == 200
        assert _hook("open", {"slug": "team"}).status_code == 429


def test_open_hook_follows_the_turn_sender_and_reports_why_not(people):
    row = _row("team")
    task_store.create_chat("chat-shared", "alice-sub", AGENT)
    with patch("api.hooks.routing.resolve_hook_chat_id", return_value="chat-shared"):
        # Bob sent the last turn of the chat alice's session was warmed for:
        # the app opens on bob's screen, under bob's own access.
        bob = _connect("bob-sub")
        alice = _connect("alice-sub")
        nm.set_chat_turn_origin("bob-sub", "chat-shared", bob)
        assert _hook("open", {"slug": "team"}).json()["status"] == "opened"
        assert len(_frames("bob-sub", bob)) == 1 and _frames("alice-sub", alice) == []
        # Bob hid the app for himself: nothing opens, the tool learns why.
        task_store.hide_app_for_user(row["id"], "bob-sub")
        assert _hook("open", {"slug": "team"}).json()["status"] == "hidden"
        task_store.unhide_app_for_user(row["id"], "bob-sub")
        # Bob turned agents-open off.
        from storage.prefs import user_ui_prefs_store
        user_ui_prefs_store.upsert_prefs("bob-sub", {"agents_may_open_apps": False})
        focus_context.invalidate_prefs("bob-sub")
        assert _hook("open", {"slug": "team"}).json()["status"] == "off"
        # A personal row of alice is not bob's to see: the same 404 as missing.
        _row("mine", username="alice", owner_sub="alice-sub")
        assert _hook("open", {"slug": "mine"}).status_code == 404
    # No human on the session, a task chat, or a run chat: nothing opens.
    assert _hook("open", {"slug": "team"}, sid=SID_SHARED).status_code == 400
    with patch("api.hooks.routing.resolve_hook_chat_id", return_value="task-123"):
        assert _hook("open", {"slug": "team"}).json()["status"] == "no_screen"
    task_store.create_chat("chat-run", "task::t1", AGENT)
    with patch("api.hooks.routing.resolve_hook_chat_id", return_value="chat-run"):
        _connect("alice-sub")
        assert _hook("open", {"slug": "team"}).json()["status"] == "no_screen"
    with patch("api.hooks.routing.resolve_hook_chat_id", return_value=""):
        assert _hook("open", {"slug": "team"}).json()["status"] == "no_screen"


def test_open_hook_opens_a_placed_app_and_reads_the_hide_here(people):
    """``open_app`` finds an app a share placed in the session's agent (by
    slug, or by its home agent when a native app shares the slug), reads
    the hide in THIS agent (never the home agent's strip hides), and frames
    it with its own agent."""
    from auth import rate_limiter
    from storage.agents import agent_store
    from storage.sharing import share_store
    source = "live-source"
    for slug in (AGENT, source):
        agent_store.create_agent(slug, slug.title(), created_by="alice-sub")
    src = task_store.upsert_app(source, "", None, "register", title="Register",
                                rel_path="workspace/apps/register.html")
    task_store.create_chat("chat-live", "alice-sub", AGENT)
    with patch("api.hooks.routing.resolve_hook_chat_id", return_value="chat-live"):
        screen = _connect("alice-sub")
        assert _hook("open", {"slug": "register"}).status_code == 404
        share_store.create_internal_share(
            target_kind="app", target_id=src["id"], created_by="alice-sub",
            grantee_kind=share_store.AGENT, grantee_agent=AGENT, role_cap="viewer",
            decision=share_store.ACCEPTED, decided_by="alice-sub")
        r = _hook("open", {"slug": "register"})
        assert r.json() == {"status": "opened", "app_id": src["id"], "screens": 1}, r.text
        assert _frames("alice-sub", screen) == [{
            "type": "open_app", "app_id": src["id"], "title": "Register", "agent": source,
            "scope_chat_id": "", "scope_project_id": "", "opened_by": AGENT}]
        # Hidden in this agent: nothing opens; a hide on the home agent's
        # own strip is not this agent's.
        share_store.set_placement_hidden(src["id"], AGENT, "alice-sub", True)
        assert _hook("open", {"slug": "register"}).json()["status"] == "hidden"
        share_store.set_placement_hidden(src["id"], AGENT, "alice-sub", False)
        task_store.hide_app_for_user(src["id"], "alice-sub")
        rate_limiter._attempts.clear()
        assert _hook("open", {"slug": "register"}).json()["status"] == "opened"
        # A native app of the same slug: 400 without the home agent; the
        # home agent names the placed one, this agent the native one.
        own = _row("register")
        assert _hook("open", {"slug": "register"}).status_code == 400
        rate_limiter._attempts.clear()
        assert _hook("open", {"slug": "register", "agent": source}).json()["app_id"] == src["id"]
        rate_limiter._attempts.clear()
        assert _hook("open", {"slug": "register", "agent": AGENT}).json()["app_id"] == own["id"]
        assert _hook("open", {"slug": "nothing", "agent": source}).status_code == 404
        # The slug placed from two home agents: 400 naming both until the
        # home agent picks one.
        twin = task_store.upsert_app("live-twin", "", None, "board", title="Board",
                                     rel_path="workspace/apps/board.html")
        other_board = task_store.upsert_app(source, "", None, "board", title="Board",
                                            rel_path="workspace/apps/board.html")
        for app_id in (twin["id"], other_board["id"]):
            share_store.create_internal_share(
                target_kind="app", target_id=app_id, created_by="alice-sub",
                grantee_kind=share_store.AGENT, grantee_agent=AGENT, role_cap="viewer",
                decision=share_store.ACCEPTED, decided_by="alice-sub")
        r = _hook("open", {"slug": "board"})
        assert r.status_code == 400 and "live-source and live-twin" in r.text
        rate_limiter._attempts.clear()
        assert _hook("open", {"slug": "board", "agent": "live-twin"}).json()["app_id"] == twin["id"]
    # A placed-only slug on a task chat answers no_screen, as any app does;
    # an unknown slug stays 404.
    with patch("api.hooks.routing.resolve_hook_chat_id", return_value="task-123"):
        assert _hook("open", {"slug": "board", "agent": "live-twin"}).json()["status"] == "no_screen"
        assert _hook("open", {"slug": "nothing"}).status_code == 404
