"""The platform catalog, v1 (APPS.md "Platform catalog"): the
registry behind the manifest, snapshots as the viewer's slice, methods as
the viewer, the emit sites, the sequence per feed, and the live queue's
rule for catalog frames.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from api.apps import apps as apps_api
from api.apps import catalog
from api.apps import manifest as _mf
from app import app
from auth.providers import UserContext, get_current_user
from services.notifications import notification_manager as nm
from storage import database as task_store

client = TestClient(app)

AGENT = "catalog-agent"
OWNER = "cat-owner"
VIEWER = "cat-viewer"
OTHER = "cat-other"


def _user(sub: str = OWNER, role: str = "member", agent_roles: dict[str, str] | None = None,
          agents: tuple[str, ...] = (AGENT,)) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role, agents=list(agents),
                       agent_roles={AGENT: "manager"} if agent_roles is None else agent_roles)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _world():
    for sub, name in ((OWNER, "Owner"), (VIEWER, "Viewer"), (OTHER, "Other")):
        task_store.upsert_user(sub, f"{sub}@test.com", name, "member")
    task_store.add_user_agent(OWNER, AGENT, "manager", "test")
    task_store.add_user_agent(VIEWER, AGENT, "viewer", "test")
    catalog._seq.clear()
    apps_api._fire_rate.clear()
    nm._user_connections.clear()
    _as(_user())
    yield
    nm._user_connections.clear()
    app.dependency_overrides.pop(get_current_user, None)


def _shared_app(actions: list) -> dict:
    canonical, err = _mf.validate_actions(actions, AGENT, True)
    assert err == "", err
    row = task_store.upsert_app(AGENT, "", None, "board", title="Board",
                                rel_path="workspace/apps/board.html", actions_json=canonical)
    assert task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), OWNER)
    return task_store.get_app(row["id"])


def _feed(feed: str, aid: str | None = None) -> dict:
    return {"id": aid or feed, "label": feed.title(), "type": "data_feed", "feed": feed}


def _method(method: str, **extra) -> dict:
    return {"id": method.replace(".", "_"), "label": method, "type": "platform", "method": method, **extra}


# ───────────────────────── the manifest ─────────────────────────────────────


def test_manifest_takes_every_catalog_feed_and_method_and_nothing_else():
    ok, err = _mf.validate_actions([_feed(f) for f in catalog.FEEDS], AGENT, True)
    assert err == "" and ok
    ok, err = _mf.validate_actions([_method(m) for m in catalog.METHODS], AGENT, True)
    assert err == "" and ok
    _, err = _mf.validate_actions([_feed("secrets")], AGENT, True)
    assert "unknown feed" in err
    _, err = _mf.validate_actions([_method("users.list")], AGENT, True)
    assert "unknown platform method" in err
    # A method may carry the same bounded args schema as a tool.
    ok, err = _mf.validate_actions([_method("tasks.run_result", args_schema={
        "type": "object", "properties": {"run_id": {"type": "string", "maxLength": 64}},
        "required": ["run_id"]})], AGENT, True)
    assert err == "" and '"args_schema"' in ok
    # Every entry has words for the card.
    assert all(e["description"] for e in list(catalog.FEEDS.values()) + list(catalog.METHODS.values()))


def test_the_audience_floor_is_clamped_to_editor_and_the_new_methods_are_described():
    for m in ("app.audience", "viewer.data.read", "viewer.data.write"):
        assert catalog.METHODS[m]["description"]
    assert catalog.AUDIENCE_METHOD == "app.audience"
    assert catalog.PERSON_METHODS == catalog.SETUP_METHODS | catalog.VIEWER_DATA_METHODS
    import json
    for declared, stored in ((None, "editor"), ("contributor", "editor"), ("editor", "editor"),
                             ("manager", "manager")):
        action = _method("app.audience", **({"min_role": declared} if declared else {}))
        ok, err = _mf.validate_actions([action], AGENT, True)
        assert err == "" and json.loads(ok)[0]["min_role"] == stored, (declared, err)
    ok, _ = _mf.validate_actions([_method("viewer.data.read")], AGENT, True)
    assert "min_role" not in json.loads(ok)[0]


# ───────────────────────── snapshots ────────────────────────────────────────


def test_sessions_snapshot_is_the_viewers_own_chats_of_the_agent():
    row = _shared_app([_feed("sessions")])
    mine = str(uuid.uuid4())
    task_store.create_chat(mine, OWNER, AGENT)
    task_store.update_chat(mine, title="Mine")
    theirs = str(uuid.uuid4())
    task_store.create_chat(theirs, OTHER, AGENT)
    r = client.get(f"/v1/apps/{row['id']}/catalog/sessions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["id"] for c in body["rows"]] == [mine]
    assert body["rows"][0]["title"] == "Mine" and body["rows"][0]["status"] in ("idle", "streaming", "warming", "finished", "")
    assert body["seq"] == 0
    # Undeclared and host-answered feeds are not served here.
    assert client.get(f"/v1/apps/{row['id']}/catalog/tasks").status_code == 404
    assert client.get(f"/v1/apps/{row['id']}/catalog/active_chats").status_code == 404
    assert client.get(f"/v1/apps/{row['id']}/catalog/nope").status_code == 404


def test_snapshots_need_approval_and_clear_the_floor():
    canonical, _ = _mf.validate_actions([_feed("notifications", "inbox")], AGENT, True)
    row = task_store.upsert_app(AGENT, "", None, "board", rel_path="workspace/apps/board.html",
                                actions_json=canonical)
    assert client.get(f"/v1/apps/{row['id']}/catalog/notifications").status_code == 409
    task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), OWNER)
    assert client.get(f"/v1/apps/{row['id']}/catalog/notifications").json() == {"rows": [], "seq": 0}
    # The owner's session token is a bearer: the inbox (every agent's) is a
    # person's own page, as on /v1/notifications.
    _as(UserContext(sub=OWNER, email="o@test.com", name="o", role="member", agents=[AGENT],
                    agent_roles={AGENT: "manager"}, is_api_key=True, agent=AGENT, session_id="s1"))
    assert client.get(f"/v1/apps/{row['id']}/catalog/notifications").status_code == 403
    _as(_user())
    floored, _ = _mf.validate_actions([{**_feed("tasks"), "min_role": "editor"}], AGENT, True)
    task_store.upsert_app(AGENT, "", None, "board", actions_json=floored)
    fresh = task_store.get_app(row["id"])
    task_store.approve_app_actions(row["id"], task_store.manifest_sig(fresh), OWNER)
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    assert client.get(f"/v1/apps/{row['id']}/catalog/tasks").status_code == 403


def test_tasks_and_trigger_fires_snapshots_hide_other_users_personal_rows():
    row = _shared_app([_feed("tasks"), _feed("trigger_fires")])
    shared_task = str(uuid.uuid4())
    task_store.create_dynamic_task(shared_task, AGENT, "Shared T", "do", "auto", "trigger", None,
                                   None, None, 300, OWNER, scope="agent", notification_mode="none")
    private_task = str(uuid.uuid4())
    task_store.create_dynamic_task(private_task, AGENT, "Private T", "do", "auto", "trigger", None,
                                   None, None, 300, OTHER, scope="user", notification_mode="none")
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    rows = client.get(f"/v1/apps/{row['id']}/catalog/tasks").json()["rows"]
    assert [t["name"] for t in rows] == ["Shared T"]
    assert rows[0]["last_run"] is None
    # The schedule in words, its zone and the next run ride the row, so a
    # page prints text and never humanizes a cron itself.
    assert rows[0]["schedule_text"] == "On trigger" and rows[0]["next_run_time"] is None
    assert rows[0]["effective_tz"] and rows[0]["user_tz"] == ""
    assert client.get(f"/v1/apps/{row['id']}/catalog/trigger_fires").json()["rows"] == []


# ───────────────────────── methods ──────────────────────────────────────────


def test_methods_answer_as_the_viewer_and_notifications_create_reaches_only_me(monkeypatch):
    row = _shared_app([_method("viewer.me"), _method("notifications.create"),
                       _method("tasks.run_result")])
    me = client.post(f"/v1/apps/{row['id']}/catalog/viewer.me", json={}).json()
    assert me["ok"] and me["result"]["sub"] == OWNER and me["result"]["role"] == "manager"
    delivered: list = []

    async def _fake_deliver(sub, delivery):
        delivered.append((sub, delivery["title"]))
    monkeypatch.setattr(nm, "_deliver_to_user", _fake_deliver)
    out = client.post(f"/v1/apps/{row['id']}/catalog/notifications.create",
                      json={"args": {"title": "Ping", "body": "from the app"}}).json()
    assert out["ok"] and out["result"]["delivered"] is True
    assert delivered == [(OWNER, "Ping")]
    # An empty title is a refusal, not an error (past the per-method pace).
    apps_api._fire_rate.clear()
    out = client.post(f"/v1/apps/{row['id']}/catalog/notifications.create",
                      json={"args": {"title": ""}}).json()
    assert out["ok"] is False
    # Undeclared methods and unknown ones.
    assert client.post(f"/v1/apps/{row['id']}/catalog/integrations.status", json={}).status_code == 404
    assert client.post(f"/v1/apps/{row['id']}/catalog/nope", json={}).status_code == 404
    # A run of someone else's user-scope task is not mine to read.
    tid = str(uuid.uuid4())
    task_store.create_dynamic_task(tid, AGENT, "T", "do", "auto", "trigger", None, None, None, 300,
                                   OTHER, scope="user", notification_mode="none")
    task_store.create_run("run-1", tid, AGENT, "manual", None, "do", scope="user", created_by=OTHER)
    apps_api._fire_rate.clear()
    out = client.post(f"/v1/apps/{row['id']}/catalog/tasks.run_result",
                      json={"args": {"run_id": "run-1"}}).json()
    assert out["ok"] is False and "not your run" in out["reason"]
    # The REST action routes never fire a platform entry.
    assert client.post(f"/v1/apps/{row['id']}/actions/viewer_me").status_code == 400


# ───────────────────────── emits and the queue ─────────────────────────────


def _connect(sub: str, feeds: tuple[str, ...] = ("sessions", "tasks"),
             agent: str = AGENT) -> nm.LiveQueue:
    """A connection whose app asked for ``feeds`` (the dashboard's
    ``catalog_subscribe`` frames)."""
    q = nm.LiveQueue()
    cid = f"conn-{sub}-{uuid.uuid4()}"
    nm.register_user_connection(sub, cid, asyncio.Queue(), live_queue=q)
    for feed in feeds:
        assert catalog.handle_subscription(sub, cid, {"type": "catalog_subscribe", "agent": agent, "feed": feed})
    return q


def _drain(q: nm.LiveQueue) -> list[dict]:
    out = []
    while True:
        f = q.get_nowait()
        if f is None:
            return out
        out.append(f)


def test_deltas_carry_a_sequence_per_feed_and_reach_the_right_users():
    live = _connect(OWNER)
    other = _connect(OTHER)
    chat_id = str(uuid.uuid4())
    row = task_store.create_chat(chat_id, OWNER, AGENT)
    catalog.announce_new_chat(row)
    nm.broadcast_chat_status(OWNER, chat_id, "streaming", AGENT)
    frames = [f for f in _drain(live) if f["type"] == "catalog"]
    assert [(f["feed"], f["seq"], f["delta"]["id"]) for f in frames] == [
        ("sessions", 1, chat_id), ("sessions", 2, chat_id)]
    assert frames[0]["delta"]["created"] is True and frames[1]["delta"]["status"] == "streaming"
    assert not [f for f in _drain(other) if f["type"] == "catalog"]
    # A title landing later fills the row in.
    nm.broadcast_chat_title(OWNER, chat_id, "Named", AGENT)
    titled = [f for f in _drain(live) if f["type"] == "catalog"]
    assert titled and titled[-1]["delta"] == {"id": chat_id, "title": "Named"}
    # A finished run tells the agent's members through the tasks feed.
    tid = str(uuid.uuid4())
    task_store.create_dynamic_task(tid, AGENT, "T", "do", "auto", "trigger", None, None, None, 300,
                                   OWNER, scope="agent", notification_mode="none")
    task_store.create_run("run-9", tid, AGENT, "manual", None, "do", scope="agent", created_by=OWNER)
    catalog.install(asyncio.new_event_loop())
    catalog._loop = None  # the hook emits inline in tests (no loop to hop to)
    task_store.update_run("run-9", status="completed", completed_at="2026-09-12T00:00:00+00:00")
    frames = [f for f in _drain(live) if f["type"] == "catalog"]
    assert frames and frames[-1]["feed"] == "tasks"
    # The next run rides the delta (a cron just moved a day); a trigger
    # task has none.
    assert frames[-1]["delta"] == {"id": tid, "last_run": {
        "id": "run-9", "status": "completed", "completed_at": "2026-09-12T00:00:00+00:00",
        "error_message": ""}, "next_run_time": None}
    assert catalog.current_seq(OWNER, AGENT, "tasks") == 1


def test_the_emit_sites_read_the_audience_cache_and_no_chat_row(monkeypatch):
    """The members-and-admins set comes from the notification
    manager's audience cache (no store read per emit), and a title change
    that names its owner and agent reads no chat row."""
    calls: list[str] = []
    monkeypatch.setattr(nm, "agent_audience", lambda agent, **_: calls.append(agent) or [VIEWER, OWNER])
    monkeypatch.setattr("storage.automation.notification_store.get_admin_user_subs",
                        lambda: (_ for _ in ()).throw(AssertionError("no admin read per emit")))
    assert catalog._members_and_admins(AGENT) == sorted([OWNER, VIEWER]) and calls == [AGENT]
    live = _connect(OWNER)
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    monkeypatch.setattr(task_store, "get_chat", lambda cid: (_ for _ in ()).throw(AssertionError("row read")))
    nm.broadcast_chat_title(OWNER, chat_id, "Named", AGENT)
    titled = [f for f in _drain(live) if f["type"] == "catalog"]
    assert titled and titled[-1]["delta"] == {"id": chat_id, "title": "Named"}


def test_deltas_reach_only_the_connections_that_asked_for_the_feed():
    """A tab with no app on the feed takes no frame and the sequence does
    not move for it; an agent-less feed matches under any agent; the
    subscription frame is validated and bounded."""
    silent = _connect(OWNER, feeds=())
    other_agent = _connect(OWNER, feeds=("sessions",), agent="some-other-agent")
    chat_id = str(uuid.uuid4())
    before = catalog.current_seq(OWNER, AGENT, "sessions")
    nm.broadcast_chat_status(OWNER, chat_id, "streaming", AGENT)
    assert not [f for f in _drain(silent) if f["type"] == "catalog"]
    assert not [f for f in _drain(other_agent) if f["type"] == "catalog"]
    assert catalog.current_seq(OWNER, AGENT, "sessions") == before
    live = _connect(OWNER, feeds=("sessions",))
    nm.broadcast_chat_status(OWNER, chat_id, "ready", AGENT)
    frames = [f for f in _drain(live) if f["type"] == "catalog"]
    assert len(frames) == 1 and frames[0]["seq"] == before + 1
    assert not [f for f in _drain(silent) if f["type"] == "catalog"]
    # The viewer's own inbox is agent-less on the wire: a subscription under
    # the app's agent still takes it.
    inbox = _connect(OWNER, feeds=("notifications",), agent=AGENT)
    catalog.notification_delivered(OWNER, {"id": "n1", "title": "Hi"})
    got = [f for f in _drain(inbox) if f["type"] == "catalog"]
    assert got and got[0]["agent"] == "" and got[0]["delta"]["id"] == "n1"
    # Unsubscribe stops the frames; a client feed or an unknown feed is
    # refused; the set is bounded.
    cid = nm.get_all_connections(OWNER)[-1].connection_id
    assert catalog.handle_subscription(OWNER, cid, {"type": "catalog_unsubscribe", "agent": AGENT, "feed": "notifications"})
    catalog.notification_delivered(OWNER, {"id": "n2", "title": "Hi"})
    assert not [f for f in _drain(inbox) if f["type"] == "catalog"]
    assert not catalog.handle_subscription(OWNER, cid, {"type": "catalog_subscribe", "agent": AGENT, "feed": "active_chats"})
    assert not catalog.handle_subscription(OWNER, cid, {"type": "catalog_subscribe", "agent": AGENT, "feed": "nope"})
    assert not catalog.handle_subscription(OWNER, cid, {"type": "catalog_subscribe", "agent": "a" * 65, "feed": "tasks"})
    for i in range(nm.CATALOG_SUBS_MAX):
        nm.set_catalog_subscription(OWNER, cid, f"agent-{i}", "tasks", True)
    assert not nm.set_catalog_subscription(OWNER, cid, "one-more", "tasks", True)


def test_live_queue_keeps_opens_and_lets_a_snapshot_supersede_its_deltas():
    q = nm.LiveQueue(maxsize=4)
    q.put({"type": "open_app", "app_id": "a"})
    q.put({"type": "catalog", "agent": "x", "feed": "sessions", "seq": 1, "delta": {"id": 1}})
    q.put({"type": "catalog", "agent": "x", "feed": "sessions", "seq": 2, "delta": {"id": 2}})
    q.put({"type": "app_push", "app_id": "a", "payload": 1, "ts": 1})
    # Full: a push goes first, then the oldest catalog delta; never the open.
    assert q.put({"type": "app_state", "app_id": "b", "doc": {}, "rev": 1})
    assert [f["type"] for f in q._items] == ["open_app", "catalog", "catalog", "app_state"]
    assert q.put({"type": "app_state", "app_id": "c", "doc": {}, "rev": 1})
    assert [f.get("seq", f["type"]) for f in q._items] == ["open_app", 2, "app_state", "app_state"]
    # A snapshot for the feed replaces what waits for it.
    q.put({"type": "catalog", "agent": "x", "feed": "sessions", "seq": 5, "snapshot": []})
    kinds = [(f["type"], f.get("seq")) for f in q._items]
    assert ("catalog", 2) not in kinds and ("catalog", 5) in kinds
    # Only opens left: a new push is refused, an open never evicted.
    q2 = nm.LiveQueue(maxsize=1)
    q2.put({"type": "open_app", "app_id": "a"})
    assert q2.put({"type": "app_push", "app_id": "a", "payload": 1, "ts": 1}) is False
    assert q2.put({"type": "open_app", "app_id": "b"}) is False
    assert len(q2) == 1


# ───────────────── integrations.status names whose account ──────────────────


def test_integrations_status_judges_the_identity_the_buttons_run_with(monkeypatch):
    """A shared app's buttons run with the agent's service account, so the
    method (and the card's `requires_status`) reports THAT account — not
    whether the viewer connected one (the card told the agent's own
    buttons "not connected for you" on the internal install)."""
    from services.oauth import credential_resolver
    from storage.identity import credential_store
    from services.mcp import mcp_registry
    from types import SimpleNamespace as NS

    gh = NS(name="github-mcp", manifest=NS(credentials=NS(oauth={"provider_id": "github"})))
    plain = NS(name="file-tools", manifest=NS(credentials=None))
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent, **kw: [gh, plain])

    class _Ref:
        def __init__(self, label, owner):
            self.label, self.owner_sub = label, owner
    bound = {"github-mcp": _Ref("dimitris", OWNER)}
    monkeypatch.setattr(credential_resolver, "pick_account",
                        lambda mcp, agent, *, user_sub="": bound.get(mcp) if not user_sub else None)
    monkeypatch.setattr(credential_store, "list_user_accounts",
                        lambda owner, mcp: [{"account_label": "dimitris", "display_email": "d@example.com"}])
    row = _shared_app([_method("integrations.status")])
    out = client.post(f"/v1/apps/{row['id']}/catalog/integrations.status", json={}).json()
    assert out["ok"] and out["result"]["providers"] == [
        {"mcp": "github-mcp", "provider": "github", "connected": True,
         "account": "d@example.com", "identity": "the agent"}]
    # Nothing bound to the agent: not connected, and the card says whose job it is.
    bound.clear()
    apps_api._fire_rate.clear()
    out = client.post(f"/v1/apps/{row['id']}/catalog/integrations.status", json={}).json()
    assert out["result"]["providers"][0]["connected"] is False
    assert out["result"]["providers"][0]["identity"] == "the agent"
    st = apps_api._requires_status(AGENT, {"providers": ["github", "slack"]}, _user(), {}, row)
    assert st["providers"] == [
        {"provider": "github", "mcp": "github-mcp", "connected": False, "account": "", "identity": "the agent"},
        {"provider": "slack", "mcp": "", "connected": False, "account": "", "identity": ""}]
    # A grantee who is not a member learns that the agent's account is
    # connected, never whose it is (the method and the card alike).
    bound["github-mcp"] = _Ref("dimitris", OWNER)
    outsider = _user(OTHER, agent_roles={}, agents=())
    assert catalog.provider_status(AGENT, row, outsider) == [
        {"mcp": "github-mcp", "provider": "github", "connected": True, "account": "", "identity": "the agent"}]
    assert apps_api._requires_status(AGENT, {"providers": ["github"]}, outsider, {}, row)["providers"] == [
        {"provider": "github", "mcp": "github-mcp", "connected": True, "account": "", "identity": "the agent"}]
    assert catalog.provider_status(AGENT, row, _user())[0]["account"] == "d@example.com"


# ───────────────────────── the checks feed (CHECKS.md) ──────────────────────


def _checks_world(tmp_path, monkeypatch) -> None:
    """An agent with a config tree on disk and a viewer with a username, so
    the checks documents have somewhere to live."""
    import config
    from storage.agents import agent_store
    from storage.pg import get_conn
    root = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    agent_store.create_agent(AGENT, "Catalog Agent", created_by=OWNER)
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("catviewer", VIEWER))
        conn.commit()
    # The catalog's deltas read the audience as a fan-out: fill the agent's
    # entry the way the boot fill does, so the first delta has its audience.
    from services.notifications.notification_manager import agent_audience
    agent_audience(AGENT)
    (root / AGENT / "users" / "catviewer" / "workspace").mkdir(parents=True, exist_ok=True)


def _verdict(status: str, user_sub: str, owner: str = "", name: str = "coding") -> dict:
    from storage.checks import db_checks
    return db_checks.insert_verdict(
        agent=AGENT, owner=owner, check_name=name, section="judge", status=status,
        passed=status == "pass", score=0.9 if status == "pass" else 0.4, findings=[], summary="",
        reason="", session_id="s", chat_id="chat-1", run_id="", judge_run_id="", user_sub=user_sub,
        round_no=1, ran_on="local", engine="", model="", cost_usd=0.0, duration_ms=1, script_sha256="")


def test_checks_snapshot_lists_the_checks_with_their_last_verdict_in_the_viewers_slice(tmp_path, monkeypatch):
    import json
    import time
    from services.checks import documents
    _checks_world(tmp_path, monkeypatch)
    row = _shared_app([_feed("checks")])
    documents.write_check(AGENT, "", {"name": "coding", "mandatory": True, "condition": {"kinds": ["code"]},
                                      "judge": {"rubric": "Is the code sound?"}}, None, updated_by="owner")
    # A private check of the same name: its own verdicts, none yet.
    documents.write_check(AGENT, "catviewer", {"name": "coding", "judge": {"rubric": "mine"}}, None,
                          updated_by="catviewer")
    # A check the listing reports a problem on (its script is missing).
    broken = documents.check_folder(AGENT, "", "broken")
    broken.mkdir(parents=True)
    (broken / documents.DOC_FILE).write_text(json.dumps({"name": "broken", "script": {"run": "lint.sh"}}))
    _verdict("pass", "")        # an agent-scope session, older
    time.sleep(0.01)
    _verdict("fail", OTHER)     # another person's session, newer
    # A manager sees every check, the broken one included, and the newest
    # verdict of any session; the row carries no document and no script.
    rows = client.get(f"/v1/apps/{row['id']}/catalog/checks").json()["rows"]
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"agent:broken", "agent:coding"}
    coding = by_id["agent:coding"]
    assert coding["name"] == "coding" and coding["owner"] == "" and coding["mandatory"] is True
    assert coding["condition"] == {"kinds": ["code"]} and coding["sections"] == ["judge"]
    assert coding["last_verdict"]["status"] == "fail" and coding["last_verdict"]["chat_id"] == "chat-1"
    assert "doc" not in coding and "script" not in coding and "problems" not in coding
    assert by_id["agent:broken"]["broken"] is True and by_id["agent:broken"]["last_verdict"] is None
    # A member sees the agent's sound checks and their own private one, with
    # the agent-scope verdict as the last (the other person's is not theirs).
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    rows = client.get(f"/v1/apps/{row['id']}/catalog/checks").json()["rows"]
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"agent:coding", "user:coding"}
    assert by_id["agent:coding"]["last_verdict"]["status"] == "pass"
    assert by_id["user:coding"]["owner"] == "catviewer" and by_id["user:coding"]["mandatory"] is False
    assert by_id["user:coding"]["last_verdict"] is None


def test_a_check_written_or_removed_and_a_verdict_landing_update_the_checks_feed(tmp_path, monkeypatch):
    _checks_world(tmp_path, monkeypatch)
    _shared_app([_feed("checks")])
    live = _connect(OWNER, feeds=("checks", "check_verdicts"))
    other = _connect(OTHER, feeds=("checks",), agent="someone-else")
    r = client.put(f"/v1/agents/{AGENT}/checks/coding",
                   json={"doc": {"judge": {"rubric": "sound?"}, "condition": {"always": True}}})
    assert r.status_code == 200, r.text
    frames = [f for f in _drain(live) if f["type"] == "catalog"]
    assert [f["feed"] for f in frames] == ["checks"]
    written = frames[0]["delta"]
    assert written["id"] == "agent:coding" and written["condition"] == {"always": True}
    assert "last_verdict" not in written and "doc" not in written
    # A verdict lands: the verdicts feed gets the row, the checks feed the
    # check's last verdict.
    catalog.verdict_landed({
        "id": "cv-1", "owner": "", "check_name": "coding", "status": "pass", "pass": True, "score": 1.0,
        "summary": "fine", "findings": [], "round": 1, "chat_id": "c1", "run_id": "", "ran_on": "local",
        "cost_usd": 0, "created_at": "2026-09-18T00:00:00+00:00"}, AGENT, "")
    frames = [f for f in _drain(live) if f["type"] == "catalog"]
    assert [f["feed"] for f in frames] == ["check_verdicts", "checks"]
    assert frames[1]["delta"] == {"id": "agent:coding", "last_verdict": {
        "id": "cv-1", "status": "pass", "pass": True, "score": 1.0, "summary": "fine", "round": 1,
        "chat_id": "c1", "run_id": "", "ran_on": "local", "created_at": "2026-09-18T00:00:00+00:00"}}
    assert client.delete(f"/v1/agents/{AGENT}/checks/coding").status_code == 200
    frames = [f for f in _drain(live) if f["type"] == "catalog"]
    assert frames[-1]["feed"] == "checks" and frames[-1]["delta"] == {"id": "agent:coding", "removed": True}
    # Another agent's connection hears nothing.
    assert not [f for f in _drain(other) if f["type"] == "catalog"]


def test_agent_feeds_answer_nothing_to_a_grantee_who_is_not_a_member():
    """A live internal share admits its grantee to the APP (SHARING.md), not
    to the agent: the agent's feeds are the members' slice, so a grantee
    with no row on the agent reads none of its chats, tasks or triggers —
    on a Shared-only agent too, where the chat pool is one list."""
    from core.session.visibility import shared_chat_owner
    from storage.agents import agent_store
    from storage.automation import trigger_store
    from storage.sharing import share_store
    agent_store.create_agent(AGENT, "Catalog", collaborative=False, default_scope="agent")
    row = _shared_app([_feed("sessions"), _feed("tasks"), _feed("trigger_fires")])
    pool_chat = str(uuid.uuid4())
    task_store.create_chat(pool_chat, shared_chat_owner(AGENT), AGENT)
    task_store.create_dynamic_task(str(uuid.uuid4()), AGENT, "Shared T", "do", "auto", "trigger",
                                   None, None, None, 300, OWNER, scope="agent",
                                   notification_mode="none")
    trigger_store.create_trigger(slug="hook", name="Hook", scope="agent", agent=AGENT,
                                 created_by=OWNER)
    share_store.create_internal_share(target_kind="app", target_id=row["id"], grantee_sub=OTHER,
                                      created_by=OWNER)
    _as(_user(OTHER, agents=(), agent_roles={}))
    for feed in ("sessions", "tasks", "trigger_fires"):
        r = client.get(f"/v1/apps/{row['id']}/catalog/{feed}")
        assert r.status_code == 200, r.text
        assert r.json()["rows"] == [], feed
    # A member reads the members' slice as before.
    _as(_user(VIEWER, agent_roles={AGENT: "viewer"}))
    assert [c["id"] for c in client.get(f"/v1/apps/{row['id']}/catalog/sessions").json()["rows"]] \
        == [pool_chat]
    assert [t["name"] for t in client.get(f"/v1/apps/{row['id']}/catalog/tasks").json()["rows"]] \
        == ["Shared T"]
    assert [t["name"] for t in client.get(f"/v1/apps/{row['id']}/catalog/trigger_fires").json()["rows"]] \
        == ["Hook"]
