"""App handlers (APPS.md "Handlers").

Load-bearing: a delivery is a row before it is a wake, with a lease, a
retry schedule and a dead end; an on_schedule handler is a task row that
never opens a session and cannot be edited into one; the rows follow the
manifest at go-live and go with the app; a handler's forwarded claim lets
the app identity write only while its delivery is in flight; platform
events wake only the approved rows that asked for them, in their slice;
the fire carries the signed claim and the delivery id and reads the
handler's verdict; a draining app is never idle-stopped and nothing wakes
during shutdown.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from api.apps import app_proxy, catalog
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from services.apps import app_deploy, app_handlers, app_lifecycle, app_sandbox, app_supervisor, app_tokens, releases
from services.scheduler import runner, scheduler, shared
from storage import database as task_store
from storage import db_app_deliveries as deliveries
from storage.automation import trigger_store
from storage.pg import get_conn

client = TestClient(app)

AGENT = "handlers-agent"
SID_SHARED = "sess-handlers-shared"

_needs_runtime = pytest.mark.skipif(
    not (shutil.which("pasta") and shutil.which("bwrap") and app_sandbox.bun_binary()),
    reason="requires pasta + bwrap + Bun on this host",
)

# A server whose handlers record what the fire brought and answer as told.
HANDLER_SERVER = """
const port = Number(process.env.PORT || 3000);
let flaky = 0;
Bun.serve({ port, hostname: "0.0.0.0", async fetch(req) {
  const u = new URL(req.url);
  if (u.pathname === "/_health") return new Response("ok");
  if (u.pathname.startsWith("/_handler/")) {
    const body = await req.text();
    const seen = { path: u.pathname, claim: req.headers.get("x-otodock-viewer"),
                   basis: req.headers.get("x-otodock-basis"),
                   delivery: req.headers.get("x-otodock-delivery-id"), body };
    await Bun.write("/app/data/last.json", JSON.stringify(seen));
    if (u.pathname === "/_handler/bad") return new Response("no such thing", { status: 400 });
    if (u.pathname === "/_handler/flaky") {
      flaky += 1;
      if (flaky === 1) return new Response("not yet", { status: 500 });
    }
    return new Response("pong");
  }
  return new Response("hello");
}});
"""


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None) -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=list(roles.keys()), agent_roles=roles)


ALICE = _user()


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from api.apps import apps as apps_api
    _as(ALICE)
    apps_api._fire_rate.clear()
    app_proxy._buckets.clear()
    app_proxy._client = None
    catalog._loop = None
    catalog._app_subs.clear()
    app_handlers._drains.clear()
    app_handlers._drain_locks.clear()
    app_handlers.mark_dirty()
    monkeypatch.setattr(app_proxy, "PLATFORM_RATE", 1000.0)
    yield
    app.dependency_overrides.pop(get_current_user, None)
    shared._shutting_down = False
    import contextlib
    import os
    import signal
    for inst in list(app_supervisor._instances.values()):
        proc = inst.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    app_handlers._drains.clear()
    app_proxy._client = None


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace" / "notes").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    for sub, name in (("alice-sub", "alice"), ("bob-sub", "bob")):
        task_store.upsert_user(sub, f"{sub}@test.com", name.title(), "member")
        with get_conn() as conn:
            conn.execute("UPDATE users SET username=%s WHERE sub=%s", (name, sub))
            conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    task_store.add_user_agent("bob-sub", AGENT, "viewer", "test")
    session_state.set_session_security(SID_SHARED, SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False))
    yield agents_root / AGENT
    session_state._session_security.pop(SID_SHARED, None)


def _hook(op: str, payload: dict, sid: str = SID_SHARED):
    payload.setdefault("session_id", sid)
    with patch("api.hooks.app_deploy.verify_session_match_async"), \
            patch("api.hooks.pins.verify_session_match_async"):
        return client.post(f"/v1/hooks/apps/{op}", json=payload,
                           headers={"Authorization": "Bearer dummy"})


def _folder(agent_tree: Path, slug: str, manifest: dict, server: str | None = None) -> Path:
    root = agent_tree / "workspace" / "apps" / slug
    (root / "client").mkdir(parents=True, exist_ok=True)
    (root / "app.json").write_text(json.dumps(manifest))
    (root / "client" / "index.html").write_text("<p>app</p>")
    if server is not None:
        (root / "server").mkdir(exist_ok=True)
        (root / "server" / "index.ts").write_text(server)
    return root


def _row(slug: str, actions: list[dict], *, files: dict | None = None, handlers: dict | None = None,
         username: str = "", owner: str | None = None, approve: bool = True) -> dict:
    from api.apps import manifest as _mf
    root = f"users/{username}/workspace" if username else "workspace"
    blocks: dict[str, str] = {}
    if files:
        blocks["files"] = task_store.canonical_actions_json(files)
    if handlers:
        blocks["handlers"] = task_store.canonical_actions_json(handlers)
    actions_json, err = _mf.validate_actions(actions, AGENT, shared=not username)
    assert actions_json is not None, err
    row = task_store.upsert_app(AGENT, username, owner, slug, title=slug.title(),
                                rel_path=f"{root}/apps/{slug}", kind="folder",
                                actions_json=actions_json, blocks=blocks or None)
    if approve:
        task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "alice-sub")
    return task_store.get_app(row["id"])


def _install(row: dict, **over) -> app_supervisor.Instance:
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=config.get_agent_dir(AGENT),
                                   data_dir=config.get_agent_dir(AGENT), host_port=1, state="up")
    for k, v in over.items():
        setattr(inst, k, v)
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    return inst


def _deploy_approved(agent_tree: Path, slug: str, manifest: dict, server: str | None = None) -> dict:
    """A folder with a manifest that needs approval, deployed and approved
    through the routes (the handler rows are created at go-live)."""
    _folder(agent_tree, slug, manifest, server)
    body = _hook("deploy", {"slug": slug, "visibility": "agent"}).json()
    assert body["status"] == "pending approval", body
    r = client.post(f"/v1/apps/{body['app_id']}/deploy/approve")
    assert r.status_code == 200, r.text
    return task_store.get_app(body["app_id"])


# ── the store ───────────────────────────────────────────────────────────────


def test_deliveries_are_rows_first_with_a_lease_and_a_dead_end(agent_tree):
    row = _row("store", [])
    d = deliveries.enqueue(row["id"], "ping", "schedule", {"a": 1})
    assert d["status"] == "pending" and d["attempts"] == 0 and d["payload"] == {"a": 1}
    # A replayed event id is a no-op that names the first row.
    first = deliveries.enqueue(row["id"], "ping", "trigger:gh", {}, event_id="run-1:7")
    assert deliveries.enqueue(row["id"], "ping", "trigger:gh", {}, event_id="run-1:7") is None
    assert deliveries.find_event(row["id"], "ping", "run-1:7")["id"] == first["id"]
    # A payload over the cap is trimmed to its small fields and still wakes
    # the app (a big GitHub push carried hundreds of file names and the
    # wake was lost — the VM pass, 2026-09-18); one that stays over the cap
    # even so is a dead row with the reason.
    big = deliveries.enqueue(row["id"], "ping", "schedule",
                             {"ref": "refs/heads/main", "commits": "y" * (300 * 1024)})
    assert big["status"] == "pending"
    assert big["payload"]["ref"] == "refs/heads/main" and "commits" not in big["payload"]
    assert big["payload"]["payload_truncated"] is True and big["payload"]["payload_dropped"] == ["commits"]
    assert big["payload"]["payload_bytes"] > 300 * 1024
    huge = deliveries.enqueue(row["id"], "ping", "schedule", {f"k{i}": "y" * 3000 for i in range(100)})
    assert huge["status"] == "dead" and "256 KB" in huge["last_error"]
    # The trimmed row is a wake like any other; out of the way of the claims below.
    deliveries.finish(big["id"], "done")
    assert row["id"] in deliveries.due_app_ids()
    # The claim takes the oldest due row with a lease.
    c = deliveries.claim_next(row["id"])
    assert c["id"] == d["id"] and c["status"] == "inflight" and c["next_at"] > c["claimed_at"]
    # A counted retry re-arms on the next delay of the ladder and the fifth
    # fire is the last: every delay is reached, none sits unreachable past
    # the dead end.
    for i, delay in enumerate(deliveries.RETRY_DELAYS_S):
        before = datetime.now(timezone.utc)
        assert deliveries.retry(c["id"], error="500") == "pending"
        again = deliveries.get(c["id"])
        assert again["attempts"] == i + 1
        waited = (datetime.fromisoformat(again["next_at"]) - before).total_seconds()
        assert delay <= waited <= delay + 5, (i, waited)
    assert deliveries.retry(c["id"], error="500") == "dead"
    assert deliveries.get(c["id"])["attempts"] == deliveries.MAX_ATTEMPTS == 5
    # An uncounted re-arm keeps the count; a day-old row dies instead.
    c2 = deliveries.claim_next(row["id"])
    assert c2["id"] == first["id"]
    assert deliveries.retry(c2["id"], error="waiting", count=False, delay_s=5) == "pending"
    assert deliveries.get(c2["id"])["attempts"] == 0
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE app_handler_deliveries SET created_at=%s, next_at=%s WHERE id=%s",
                     (old, now, c2["id"]))
        conn.commit()
    assert deliveries.claim_next(row["id"])["id"] == c2["id"]
    assert deliveries.retry(c2["id"], error="waiting", count=False, delay_s=5) == "dead"
    assert deliveries.get(c2["id"])["last_error"].startswith("the app never came up")
    # A verdict is final: a retry after it (an error raised once the verdict
    # was written) changes nothing, and a NUL in what was printed is no error.
    d4 = deliveries.enqueue(row["id"], "ping", "schedule", {})
    deliveries.claim_next(row["id"])
    deliveries.finish_step(d4["id"], "done", exit_code=0, output="x\x00y", error="e\x00")
    assert deliveries.get(d4["id"])["output"] == "xy"
    assert deliveries.retry(d4["id"], error="internal error") == "done"
    assert deliveries.get(d4["id"])["status"] == "done" and deliveries.get(d4["id"])["attempts"] == 0
    # Boot puts a row left in flight back; retention removes the old.
    d3 = deliveries.enqueue(row["id"], "ping", "schedule", {})
    deliveries.claim_next(row["id"])
    assert deliveries.reset_inflight() == 1 and deliveries.get(d3["id"])["status"] == "pending"
    deliveries.finish(d3["id"], "done")
    older = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE app_handler_deliveries SET done_at=%s WHERE id=%s", (older, d3["id"]))
        conn.commit()
    assert deliveries.purge_old() >= 1 and deliveries.get(d3["id"]) is None
    # What is left: the retried row and the day-old one (dead), the trimmed
    # one (done) and the one over the cap (dead).
    left = deliveries.recent(row["id"])
    assert [w["handler"] for w in left] == ["ping"] * 5
    assert sorted(w["status"] for w in left) == ["dead", "dead", "dead", "done", "done"]


# ── schedules as task rows ─────────────────────────────────────────────────


def test_a_schedule_handler_is_a_task_row_that_never_opens_a_session(agent_tree, monkeypatch):
    manifest = {"title": "Flow", "handlers": {"on_schedule": {"digest": {"cron": "0 7 * * 1-5"}}}}
    _folder(agent_tree, "flow", manifest)
    body = _hook("deploy", {"slug": "flow", "visibility": "agent"}).json()
    assert body["status"] == "pending approval"
    row = task_store.get_app(body["app_id"])
    assert task_store.list_app_handler_tasks(row["id"]) == []
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 200
    rows = task_store.list_app_handler_tasks(row["id"])
    assert len(rows) == 1 and rows[0]["task_type"] == "app" and rows[0]["prompt"] == ""
    assert rows[0]["schedule"] == "0 7 * * 1-5" and rows[0]["scope"] == "agent"
    assert rows[0]["created_by"] == AGENT and rows[0]["app_handler"] == "digest"
    tid = rows[0]["id"]
    # The editor refuses the row; pause and resume work.
    ok, err = asyncio.run(scheduler.update_dynamic_task(tid, {"schedule": "* * * * *"}))
    assert ok is False and "manifest" in err
    assert asyncio.run(scheduler.pause_dynamic_task(tid)) and asyncio.run(scheduler.resume_dynamic_task(tid))
    # A fire is a run row and a delivery, never a session.
    sessions: list = []
    monkeypatch.setattr(runner, "_run_task", lambda *a, **k: sessions.append(a))
    monkeypatch.setattr(app_handlers, "schedule_drain", lambda app_id: None)
    task_def = scheduler._row_to_task(task_store.get_dynamic_task(tid))
    assert task_def.app_id == row["id"] and task_def.app_handler == "digest"
    run_id = asyncio.run(runner._execute_task(task_def, trigger_type="manual"))
    assert run_id and not sessions
    run = task_store.get_run(run_id)
    assert run["task_type"] == "app" and run["trigger_source"] == f"app:{row['id']}:digest"
    assert run["trigger_type"] == "manual"
    pend = deliveries.recent(row["id"])
    assert pend[0]["handler"] == "digest" and pend[0]["status"] == "pending"
    # Boot recovery never blind-fails an app run.
    task_store.mark_orphaned_runs_failed()
    assert task_store.get_run(run_id)["status"] == "pending"
    # A new cron re-crons the same row; a departed handler removes it.
    manifest["handlers"]["on_schedule"]["digest"]["cron"] = "30 8 * * *"
    (agent_tree / "workspace/apps/flow/app.json").write_text(json.dumps(manifest))
    assert _hook("deploy", {"slug": "flow", "visibility": "agent"}).json()["status"] == "pending approval"
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 200
    rows = task_store.list_app_handler_tasks(row["id"])
    assert [r["id"] for r in rows] == [tid] and rows[0]["schedule"] == "30 8 * * *"
    manifest["handlers"] = {"on_trigger": ["gh"]}
    (agent_tree / "workspace/apps/flow/app.json").write_text(json.dumps(manifest))
    assert _hook("deploy", {"slug": "flow", "visibility": "agent"}).json()["status"] == "pending approval"
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 200
    assert task_store.list_app_handler_tasks(row["id"]) == []
    # A row whose app is gone removes itself on its next fire.
    manifest["handlers"] = {"on_schedule": {"digest": {"cron": "0 7 * * *"}}}
    (agent_tree / "workspace/apps/flow/app.json").write_text(json.dumps(manifest))
    assert _hook("deploy", {"slug": "flow", "visibility": "agent"}).json()["status"] == "pending approval"
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 200
    (tid2,) = [r["id"] for r in task_store.list_app_handler_tasks(row["id"])]
    task_def = scheduler._row_to_task(task_store.get_dynamic_task(tid2))
    asyncio.run(app_lifecycle.purge(task_store.get_app(row["id"])))
    assert task_store.get_dynamic_task(tid2) is None      # the purge removed it
    assert asyncio.run(runner._execute_task(task_def, trigger_type="scheduled")) == ""


def test_the_plain_approval_card_puts_the_handler_rows_back(agent_tree):
    """The deploy card is not the only approval. An app whose manifest lost
    its approval (an import over it, an approver who lost the authority)
    keeps serving its live release with its schedules gone; approving the
    same manifest on the plain card is what brings the rows back, with no
    redeploy in between."""
    manifest = {"title": "Flow", "handlers": {"on_schedule": {"digest": {"cron": "0 7 * * 1-5"}}}}
    _folder(agent_tree, "flow3", manifest)
    row = task_store.get_app(_hook("deploy", {"slug": "flow3", "visibility": "agent"}).json()["app_id"])
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 200
    assert len(task_store.list_app_handler_tasks(row["id"])) == 1
    task_store.clear_app_approval(row["id"])
    dropped = task_store.get_app(row["id"])
    asyncio.run(app_handlers.sync_rows(dropped))
    assert task_store.list_app_handler_tasks(row["id"]) == []
    r = client.post(f"/v1/apps/{row['id']}/approve",
                    json={"sig": task_store.manifest_sig(dropped)})
    assert r.status_code == 200, r.text
    rows = task_store.list_app_handler_tasks(row["id"])
    assert len(rows) == 1 and rows[0]["app_handler"] == "digest"
    assert rows[0]["schedule"] == "0 7 * * 1-5" and rows[0]["task_type"] == "app"


# ── the platform basis ─────────────────────────────────────────────────────


def test_the_platform_basis_writes_while_its_delivery_is_in_flight(agent_tree, monkeypatch):
    row = _row("writer", [
        {"id": "w", "label": "Write", "type": "platform", "method": "files.write"},
        {"id": "n", "label": "Notify", "type": "platform", "method": "notifications.create"},
    ], files={"read": [], "write": ["workspace/notes"]})
    inst = _install(row)
    deliveries.enqueue(row["id"], "sync", "schedule", {})
    d = deliveries.claim_next(row["id"])
    claim = app_handlers.claim_for(d, row["id"])
    hdr = {"Authorization": f"Bearer {inst.token}"}
    args = {"args": {"path": "workspace/notes/a.md", "content": "hi"}}
    # The launch token alone never writes; the in-flight claim writes as the app.
    assert client.post(f"/v1/apps/{row['id']}/platform/files.write", json=args, headers=hdr).status_code == 403
    r = client.post(f"/v1/apps/{row['id']}/platform/files.write", json=args,
                    headers={**hdr, "X-OtoDock-Viewer": claim})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert (agent_tree / "workspace/notes/a.md").read_text() == "hi"
    # A notification on that basis goes to the members, never a named user.
    sent: list = []

    async def fake_fire(title, body, **kw):
        sent.append((title, kw))
        return [{"id": "x"}, {"id": "y"}]

    monkeypatch.setattr("services.notifications.notification_manager.fire_notification", fake_fire)
    r = client.post(f"/v1/apps/{row['id']}/platform/notifications.create",
                    json={"args": {"title": "Done", "body": "b", "severity": "success"}},
                    headers={**hdr, "X-OtoDock-Viewer": claim})
    assert r.status_code == 200 and r.json()["result"] == {"delivered": 2, "to": "members"}, r.text
    assert sent[0][1]["scope"] == "agent" and sent[0][1]["target"] == AGENT and sent[0][1]["source"] == "app"
    # The claim of a delivery that is no longer in flight is refused; so is
    # a claim minted for another app.
    deliveries.finish(d["id"], "done")
    r = client.post(f"/v1/apps/{row['id']}/platform/files.write", json=args,
                    headers={**hdr, "X-OtoDock-Viewer": claim})
    assert r.status_code == 403 and "in flight" in r.text
    other = _row("other", [])
    foreign = app_handlers.claim_for(d, other["id"])
    r = client.post(f"/v1/apps/{row['id']}/platform/files.write", json=args,
                    headers={**hdr, "X-OtoDock-Viewer": foreign})
    assert r.status_code == 400


def test_a_handler_presses_the_buttons_unattended_on_the_approvers_authority(agent_tree, monkeypatch):
    task_store.create_dynamic_task("task-h1", AGENT, "Report", "write the report", "cli", "trigger",
                                   None, None, None, 600, "alice-sub", scope="agent")
    row = _row("presser", [{"id": "run", "label": "Run", "type": "fire_task", "task_id": "task-h1",
                            "min_role": "editor"}])
    inst = _install(row)
    deliveries.enqueue(row["id"], "sync", "trigger:gh", {})
    d = deliveries.claim_next(row["id"])
    claim = app_handlers.claim_for(d, row["id"])
    fired: list = []

    async def fake_now(task_def, **kw):
        fired.append(kw)
        return "run-x"

    monkeypatch.setattr("services.scheduler.scheduler.trigger_task_now", fake_now)
    _as(None)
    hdr = {"Authorization": f"Bearer {inst.token}"}
    # Without the claim: no caller. With it: the run carries the handler.
    assert client.post(f"/v1/apps/{row['id']}/actions/run", json={}, headers=hdr).status_code == 400
    r = client.post(f"/v1/apps/{row['id']}/actions/run", json={},
                    headers={**hdr, "X-OtoDock-Viewer": claim})
    assert r.status_code == 200 and r.json()["run_id"] == "run-x", r.text
    assert fired[0]["trigger_type"] == "app_handler"
    assert fired[0]["trigger_source"] == f"app:{row['id']}:sync"
    # The batch route takes the same caller.
    from api.apps import apps as apps_api
    apps_api._fire_rate.clear()
    r = client.post(f"/v1/apps/{row['id']}/actions/batch",
                    json={"calls": [{"call_id": "c1", "action_id": "run"}]},
                    headers={**hdr, "X-OtoDock-Viewer": claim})
    assert r.status_code == 200 and '"run_id":"run-x"' in r.text, r.text
    # A stranger's bearer is nobody.
    assert client.post(f"/v1/apps/{row['id']}/actions/run", json={},
                       headers={"Authorization": "Bearer nope"}).status_code == 401


# ── platform events ─────────────────────────────────────────────────────────


def test_platform_events_wake_the_rows_that_asked_in_their_slice(agent_tree, monkeypatch):
    watcher = _row("watcher", [], handlers={"on_event": {"refresh": ["task_finished", "chat_created"]}})
    mine = _row("mine", [], handlers={"on_event": {"inbox": ["notification_created"]}},
                username="alice", owner="alice-sub")
    unapproved = _row("later", [], handlers={"on_event": {"r": ["task_finished"]}}, approve=False)
    monkeypatch.setattr(app_handlers, "schedule_drain", lambda app_id: None)
    app_handlers.mark_dirty()

    async def go():
        catalog.emit(["alice-sub"], AGENT, "tasks",
                     {"id": "t1", "task_type": "scheduled",
                      "last_run": {"id": "run-1", "status": "completed"}}, shared=True)
        catalog.emit(["alice-sub"], AGENT, "tasks",
                     {"id": "t2", "task_type": "app",
                      "last_run": {"id": "run-2", "status": "completed"}}, shared=True)
        catalog.emit(["alice-sub"], AGENT, "sessions", {"id": "c1", "created": True, "title": "x"},
                     shared=False)
        catalog.emit(["alice-sub"], AGENT, "sessions", {"id": "c2", "status": "idle"}, shared=True)
        catalog.emit(["alice-sub"], "", "notifications",
                     {"id": "n1", "title": "hi", "agent_slug": AGENT}, shared=False)
        catalog.emit(["bob-sub"], "", "notifications",
                     {"id": "n2", "title": "yo", "agent_slug": AGENT}, shared=False)
        await asyncio.sleep(0.6)

    asyncio.run(go())
    got = deliveries.recent(watcher["id"])
    assert [g["event"] for g in got] == ["task_finished"], got
    assert deliveries.get(got[0]["id"])["payload"]["last_run"]["id"] == "run-1"
    assert [g["event"] for g in deliveries.recent(mine["id"])] == ["notification_created"]
    assert deliveries.recent(unapproved["id"]) == []
    assert app_handlers.has_subscribers(AGENT) and not app_handlers.has_subscribers("nobody")


# ── the fire ────────────────────────────────────────────────────────────────


@_needs_runtime
def test_the_fire_carries_the_claim_and_reads_the_verdict(agent_tree, monkeypatch):
    monkeypatch.setattr(deliveries, "RETRY_DELAYS_S", (0.2, 0.2, 0.2, 0.2, 0.2))
    manifest = {"title": "Flow", "handlers": {"on_trigger": ["ping", "flaky", "bad"]}}
    source = _folder(agent_tree, "flow", manifest, HANDLER_SERVER)

    async def go():
        out = await app_deploy.deploy_folder(AGENT, "", None, "flow", source, "workspace/apps/flow")
        assert out["status"] == "pending approval"
        row = task_store.get_app(out["app_id"])
        await app_deploy.approve_pending(row, "alice-sub")
        row = task_store.get_app(row["id"])
        last = releases.app_data_dir(row) / "last.json"

        # A good handler: the claim, the basis, the delivery id, the body.
        d = await app_handlers.enqueue(row, "ping", "trigger:t", {"n": 1})
        await app_handlers._drains[row["id"]]
        got = deliveries.get(d["id"])
        assert got["status"] == "done", got
        seen = json.loads(last.read_text())
        assert seen["path"] == "/_handler/ping" and seen["basis"] == "platform"
        assert seen["delivery"] == d["id"]
        claims = app_tokens.verify(seen["claim"], row["id"], app_tokens.PURPOSE_CALLER)
        assert claims["principal"] == "platform" and claims["handler"] == "ping"
        assert claims["delivery"] == d["id"]
        body = json.loads(seen["body"])
        assert body["event"] == "trigger:t" and body["payload"] == {"n": 1} and body["attempt"] == 1

        # A 5xx is retried and then done; a 4xx is dead.
        d2 = await app_handlers.enqueue(row, "flaky", "trigger:t", {})
        await app_handlers._drains[row["id"]]
        mid = deliveries.get(d2["id"])
        assert mid["status"] == "pending" and mid["attempts"] == 1 and "500" in mid["last_error"]
        assert mid["next_at"] > datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute("UPDATE app_handler_deliveries SET next_at=%s WHERE id=%s",
                         (datetime.now(timezone.utc).isoformat(), d2["id"]))
            conn.commit()
        await app_handlers.drain(row["id"])
        assert deliveries.get(d2["id"])["status"] == "done"
        assert json.loads(json.loads(last.read_text())["body"])["attempt"] == 2
        d3 = await app_handlers.enqueue(row, "bad", "trigger:t", {})
        await app_handlers._drains[row["id"]]
        dead = deliveries.get(d3["id"])
        assert dead["status"] == "dead" and "400" in dead["last_error"]

        # An unapproved app waits, uncounted; approval resumes it.
        task_store.clear_app_approval(row["id"])
        d4 = await app_handlers.enqueue(row, "ping", "trigger:t", {})
        await app_handlers._drains[row["id"]]
        waiting = deliveries.get(d4["id"])
        assert waiting["status"] == "pending" and waiting["attempts"] == 0
        assert "approval" in waiting["last_error"]
        fresh = task_store.get_app(row["id"])
        task_store.approve_app_actions(fresh["id"], task_store.manifest_sig(fresh), "alice-sub")
        with get_conn() as conn:
            conn.execute("UPDATE app_handler_deliveries SET next_at=%s WHERE id=%s",
                         (datetime.now(timezone.utc).isoformat(), d4["id"]))
            conn.commit()
        await app_handlers.drain(row["id"])
        assert deliveries.get(d4["id"])["status"] == "done"
        st = app_deploy.status(task_store.get_app(row["id"]))
        assert [w["status"] for w in st["wakes"]] == ["done", "dead", "done", "done"]
        await app_supervisor.stop(row["id"])

    asyncio.run(go())


# ── triggers with an app action ────────────────────────────────────────────


def test_a_trigger_wakes_a_handler_and_a_replay_is_a_no_op(agent_tree, monkeypatch):
    from services.scheduler import trigger_manager
    monkeypatch.setattr(app_handlers, "schedule_drain", lambda app_id: None)
    hooked = _row("hooked", [], handlers={"on_trigger": ["github"]})
    personal = _row("mine2", [], handlers={"on_trigger": ["ping"]}, username="alice", owner="alice-sub")
    _row("later2", [], handlers={"on_trigger": ["x"]}, approve=False)
    # The route resolves the slug in the trigger's scope; the linkage rule
    # refuses the rest; an unapproved app is a 409.
    r = client.post("/v1/triggers", json={"name": "GH", "scope": "agent", "agent": AGENT,
                                          "app_slug": "hooked", "handler": "github"})
    assert r.status_code == 200, r.text
    trig = r.json()["trigger"]
    assert trig["app_id"] == hooked["id"] and trig["handler"] == "github"
    assert trig["app_slug"] == "hooked" and trig["app_title"] == "Hooked" and trig["task_id"] is None
    for i, (extra, code) in enumerate((
        ({"scope": "agent", "app_slug": "hooked", "handler": "nope"}, 400),
        ({"scope": "user", "app_slug": "hooked", "handler": "github"}, 404),
        ({"scope": "agent", "app_slug": "mine2", "handler": "ping"}, 404),
        ({"scope": "agent", "app_slug": "ghost", "handler": "x"}, 404),
        ({"scope": "agent", "app_slug": "later2", "handler": "x"}, 409),
        ({"scope": "agent", "app_slug": "hooked", "handler": "github", "debounce_seconds": 30}, 400),
        ({"scope": "agent", "app_slug": "hooked", "handler": "github", "task_id": "t-none"}, 400),
    )):
        r = client.post("/v1/triggers", json={"name": f"bad {i}", "agent": AGENT, **extra})
        assert r.status_code == code, (extra, r.status_code, r.text)
    r = client.post("/v1/triggers", json={"name": "Mine", "scope": "user", "agent": AGENT,
                                          "app_slug": "mine2", "handler": "ping"})
    assert r.status_code == 200, r.text
    assert r.json()["trigger"]["app_id"] == personal["id"]
    # A fire is a delivery with the trigger's payload; the event id makes a
    # replay a no-op that names the first delivery; the fire still counts.
    row = trigger_store.get_trigger(trig["id"])

    async def fire(eid: str = ""):
        return await trigger_manager.fire_trigger(row, {"ref": "main"}, trigger_source="agent:x/gh",
                                                  event_id=eid)

    out = asyncio.run(fire("run-1:7"))
    assert out["status"] == "ok" and out["actions"] == ["app"] and out["delivery_id"], out
    d = deliveries.get(out["delivery_id"])
    assert d["handler"] == "github" and d["event"] == "trigger:gh" and d["trigger_id"] == trig["id"]
    assert d["payload"]["body"] == {"ref": "main"} and d["payload"]["trigger"]["slug"] == "gh"
    again = asyncio.run(fire("run-1:7"))
    assert again["duplicate"] is True and again["delivery_id"] == out["delivery_id"]
    assert again["actions"] == ["duplicate"]
    assert trigger_store.get_trigger(trig["id"])["fired_count"] == 2
    # The list says the target in words; an edit clears or re-aims it; the
    # webhook route reads the idempotency header and refuses a bad one.
    listed = next(t for t in client.get(f"/v1/triggers?agent={AGENT}").json()["triggers"]
                  if t["id"] == trig["id"])
    assert listed["app_title"] == "Hooked" and listed["handler"] == "github"
    r = client.post(f"/v1/triggers/{trig['id']}/edit",
                    json={"app_slug": "", "notify_enabled": True, "notify_title": "t", "notify_body": "b"})
    assert r.status_code == 200, r.text
    row = trigger_store.get_trigger(trig["id"])
    assert row["app_id"] is None and row["handler"] is None and row["notify_enabled"]
    r = client.post(f"/v1/triggers/{trig['id']}/edit", json={"app_slug": "hooked", "handler": "github"})
    assert r.status_code == 200, r.text
    assert trigger_store.get_trigger(trig["id"])["app_id"] == hooked["id"]
    from api.events import triggers as triggers_api
    from fastapi import Request as _Req
    scope_ = {"type": "http", "headers": [(b"x-otodock-event-id", b"bad id!")], "method": "POST",
              "path": "/", "query_string": b""}
    with pytest.raises(Exception):
        triggers_api._event_id(_Req(scope_))
    # A dead delivery reports on the trigger; purging the app pauses and
    # detaches its triggers.
    asyncio.run(app_handlers._dead(deliveries.get(out["delivery_id"]), "the handler answered 400"))
    assert "github: the handler answered 400" in trigger_store.get_trigger(trig["id"])["last_error"]
    asyncio.run(app_lifecycle.purge(task_store.get_app(hooked["id"])))
    row = trigger_store.get_trigger(trig["id"])
    assert row["app_id"] is None and not row["enabled"] and "removed" in row["last_error"]


def test_unattended_spend_is_grouped_by_app(agent_tree):
    from services.billing import usage_service
    from storage.billing import db_usage
    row = _row("spender", [])
    task_store.create_dynamic_task("task-u1", AGENT, "Report", "p", "cli", "trigger",
                                   None, None, None, 600, AGENT, scope="agent")
    # A run the handler fired bills to the app; a button press stays with the person.
    task_store.create_run("run-u1", "task-u1", AGENT, "app_handler", f"app:{row['id']}:sync",
                          "p", "trigger", "agent", AGENT)
    task_store.update_run("run-u1", chat_id="task-run-u1")
    task_store.create_run("run-u2", "task-u1", AGENT, "app_action", "spender:btn",
                          "p", "trigger", "agent", "alice-sub")
    task_store.update_run("run-u2", chat_id="task-run-u2")
    db_usage.insert_usage_record(None, AGENT, "agent", "task", "task-run-u1", 0.25, message_count=3)
    db_usage.insert_usage_record(None, AGENT, "agent", "task", "task-run-u1", 0.05, message_count=1)
    db_usage.insert_usage_record("alice-sub", AGENT, "user", "task", "task-run-u2", 9.0, message_count=2)
    start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    got = [a for a in db_usage.get_usage_by_app(start, end) if a["app_id"] == row["id"]]
    assert len(got) == 1 and abs(float(got[0]["total_cost"]) - 0.30) < 1e-6
    assert got[0]["run_count"] == 1 and got[0]["message_count"] == 4
    over = usage_service.get_admin_overview(days=7)
    mine = next(a for a in over["apps"] if a["app_id"] == row["id"])
    assert mine["title"] == "Spender" and mine["agent"] == AGENT and mine["scope"] == "shared"
    asyncio.run(app_lifecycle.purge(task_store.get_app(row["id"])))
    over = usage_service.get_admin_overview(days=7)
    assert next(a for a in over["apps"] if a["app_id"] == row["id"])["scope"] == "gone"


# ── the sweep and the shutdown ─────────────────────────────────────────────


def test_the_sweep_holds_a_draining_app_and_shutdown_wakes_nothing(agent_tree, monkeypatch):
    import time
    row = _row("quiet", [])
    inst = _install(row, last_activity=time.monotonic() - 10_000)

    async def go():
        ev = asyncio.Event()
        app_handlers._drains[row["id"]] = asyncio.get_running_loop().create_task(ev.wait())
        await app_supervisor.sweep_once()
        assert app_supervisor.get(row["id"]) is inst
        ev.set()
        await asyncio.sleep(0)
        await app_supervisor.sweep_once()
        assert app_supervisor.get(row["id"]) is None

    asyncio.run(go())
    d = deliveries.enqueue(row["id"], "h", "schedule", {})
    called: list = []

    async def never(row_):
        called.append(row_)

    monkeypatch.setattr(app_supervisor, "ensure_up", never)
    shared._shutting_down = True
    try:
        asyncio.run(app_handlers.drain_due())
        asyncio.run(app_handlers.drain(row["id"]))
    finally:
        shared._shutting_down = False
    assert not called and deliveries.get(d["id"])["status"] == "pending"


def test_a_dormant_personal_app_presses_nothing_and_its_server_stays_down(agent_tree):
    """Once a personal app's owner lost the agent, a handler's
    claim in flight presses none of its buttons, the sweep stops a server
    that is still up, and nothing starts it again."""
    import time
    from storage.pg import get_conn
    row = _row("mine", [{"id": "note", "label": "Note", "type": "platform",
                         "method": "notifications.create"}], username="alice", owner="alice-sub")
    inst = _install(row, last_activity=time.monotonic())
    deliveries.enqueue(row["id"], "sync", "trigger:gh", {})
    claim = app_handlers.claim_for(deliveries.claim_next(row["id"]), row["id"])
    with get_conn() as conn:
        conn.execute("DELETE FROM user_agents WHERE sub=%s AND agent=%s", ("alice-sub", AGENT))
        conn.commit()
    _as(None)
    r = client.post(f"/v1/apps/{row['id']}/actions/note", json={},
                    headers={"Authorization": f"Bearer {inst.token}", "X-OtoDock-Viewer": claim})
    assert r.status_code == 404, r.text
    asyncio.run(app_supervisor.sweep_once())
    assert app_supervisor.get(row["id"]) is None
    with pytest.raises(app_sandbox.AppStartError):
        asyncio.run(app_supervisor.start(task_store.get_app(row["id"])))
    assert app_supervisor.get(row["id"]) is None


def test_an_anonymous_action_call_learns_nothing_of_a_dormant_personal_app(monkeypatch):
    """The dormancy answer follows a verified step claim; without one the
    caller falls to the cookie path and its 401."""
    import asyncio
    from unittest.mock import AsyncMock
    from api.apps import app_actions, app_proxy
    monkeypatch.setattr(task_store, "get_app", lambda app_id: {"id": app_id, "username": "bob"})
    monkeypatch.setattr(task_store, "personal_row_dormant", lambda row: True)
    monkeypatch.setattr(app_proxy, "step_caller", AsyncMock(return_value=None))

    class _Req:
        headers = {}

    assert asyncio.run(app_actions._step_caller(_Req(), "app-1")) is None
