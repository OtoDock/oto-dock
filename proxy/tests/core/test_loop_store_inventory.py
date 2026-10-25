"""The loop-side store inventory of the session starts and the routes that
had store reads on the event loop until 1.9.0.

Each flow runs on the test thread's loop with ``loop_db_guard.recording()``
around the exercised call and must record EXACTLY its allowlist below: a new
store call on the loop fails with its ``(file, function)``, a site that left
the loop fails until its row goes. A row that stays names its reason. The
builders run through ``tests/fixtures/loop_builder_rig.py``: every read they
make themselves is real, the agents' table cache starts cold, and the agent
has two MCPs, so the MCP set costs its three reads per lookup.
"""
import asyncio
import functools
import uuid
from types import SimpleNamespace

import pytest

from tests.fixtures.loop_builder_rig import AGENT, install

# flow -> {(proxy-relative file, function): why it stays on the loop}
ALLOWED: dict[str, dict[tuple[str, str], str]] = {
    "chat builder": {},
    "task builder": {},
    "meeting builder": {},
    "delivery context": {},
    "phone builder": {},
    "warmup of a new chat": {},
    "resume of an existing chat": {},
    "mode reconcile": {},
    "offline machine": {},
    "headless resume": {},
    "implement a plan": {},
    "install enabled state": {},
    "task permission": {},
    "task routes": {},
    "abort during spawn": {},
    "usage snapshot": {},
    "push title": {},
}


def _check(flow: str, recorded: set[tuple[str, str]]) -> None:
    allowed = set(ALLOWED[flow])
    new = sorted(recorded - allowed)
    stale = sorted(allowed - recorded)
    assert not new, f"{flow}: store calls on the event loop: {new}"
    assert not stale, f"{flow}: off the loop now, drop the row: {stale}"


def _cold_agents() -> None:
    from storage.agents import agent_store
    agent_store._invalidate_cache()


def _user(sub: str) -> dict:
    from storage import database as db
    return db.get_user(sub)


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path)


def _record(loop_db_guard, make_coro) -> set[tuple[str, str]]:
    async def scenario():
        _cold_agents()
        with loop_db_guard.recording() as sites:
            await make_coro()
        return set(sites)
    return asyncio.run(scenario())


def test_the_recorder_sees_a_store_call_on_the_loop(temp_db, loop_db_guard):
    """The positive control: a read made on the loop is recorded with its
    caller and the same read in a worker thread is not, so a guard wiring
    that stopped reaching the recorder fails here instead of letting every
    empty allowlist pass."""
    from storage import database as db

    async def read_on_the_loop():
        assert db.get_user("user-admin")
        assert await asyncio.to_thread(db.get_user, "user-admin")

    recorded = _record(loop_db_guard, read_on_the_loop)
    assert recorded == {("tests/core/test_loop_store_inventory.py", "read_on_the_loop")}


def test_chat_builder(rig, loop_db_guard):
    from core.config.config_builder import build_agent_config
    from core.config.task_config_builder import TaskIdentity
    rewarm = TaskIdentity(username="vic", role="editor", scope="user", creds_user_sub="user-viewer")
    recorded: set = set()
    for sub, role, ident in (("user-admin", "admin", None), ("user-viewer", "editor", None),
                             ("user-admin", "admin", rewarm)):
        recorded |= _record(loop_db_guard, functools.partial(
            build_agent_config, AGENT, _user(sub), sub, role, client_type="dashboard",
            session_id="s-inv", task_identity=ident))
    _check("chat builder", recorded)


def test_task_builder(rig, loop_db_guard):
    from core.config.task_config_builder import build_task_agent_config
    recorded: set = set()
    for scope, creator in (("agent", "user-admin"), ("user", "user-viewer")):
        task = SimpleNamespace(scope=scope, created_by=creator, task_type="scheduled",
                               notification_mode="manual", name="t", id="dyn-inv")
        recorded |= _record(loop_db_guard, functools.partial(
            build_task_agent_config, AGENT, task, "s-inv"))
    _check("task builder", recorded)


def test_meeting_builder(rig, loop_db_guard):
    from services.meetings.meeting_context import build_meeting_agent_config
    recorded: set = set()
    for scope, creator in (("user", "user-viewer"), ("agent", "user-manager")):
        meeting = {"id": "meet-inv", "scope": scope, "created_by": creator}
        recorded |= _record(loop_db_guard, functools.partial(
            build_meeting_agent_config, AGENT, meeting, "s-inv"))
    _check("meeting builder", recorded)


def test_delivery_context(rig, loop_db_guard):
    from core.config.task_config_builder import build_delivery_security_context
    recorded = _record(loop_db_guard, lambda: build_delivery_security_context(
        AGENT, user_sub="user-viewer", role="editor", target="local"))
    _check("delivery context", recorded)


def test_phone_builder(rig, loop_db_guard):
    from core.config.phone_config_builder import build_phone_agent_config
    from services.phone.phone_identity import EXTERNAL_ROUTE_ROLE, RouteIdentity
    route = RouteIdentity(mode="user", role=EXTERNAL_ROUTE_ROLE, external=None,
                          user=_user("user-viewer"), user_role="editor", caller_claim="phone:inv")
    recorded = _record(loop_db_guard, lambda: build_phone_agent_config(
        AGENT, call_type="outbound", session_id="s-inv", call_tools=True))
    recorded |= _record(loop_db_guard, lambda: build_phone_agent_config(
        AGENT, call_type="inbound", route_identity=route, session_id="s-inv-user"))
    _check("phone builder", recorded)


@pytest.fixture
def binding_writer():
    """The pool's binding writes on their own thread, as in production (the
    suite runs them inline, ``temp_db``): a released session's row delete
    is the writer's, not the loop's."""
    from services.engines import subscription_pool
    subscription_pool._reopen_binding_writes()
    yield
    subscription_pool.close_binding_writes(timeout=5)


def _warmup_rig(monkeypatch) -> SimpleNamespace:
    """The ws harness with its model, catalog and MCP-set stubs put back to
    the real reads (a model row seeded under the harness's name): the
    layer, a test agent and the admin's cookie."""
    import config
    from services.mcp import mcp_registry
    from storage.billing import subscription_store
    from tests.fixtures.ws_dashboard_harness import (
        TEST_MODEL,
        FakeExecutionLayer,
        make_test_agent,
        session_cookie,
        set_username,
        stub_dashboard_seams,
    )
    real = (config.get_cli_model, subscription_store.list_models, mcp_registry.get_agent_mcps)
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    monkeypatch.setattr(config, "get_cli_model", real[0])
    monkeypatch.setattr(subscription_store, "list_models", real[1])
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", real[2])
    subscription_store.add_model("claude-code-cli", TEST_MODEL, "Test", provider="anthropic", tier=2)
    set_username("user-admin", "ada")
    return SimpleNamespace(layer=layer, slug=make_test_agent(), cookie=session_cookie(),
                           model=TEST_MODEL)


def _ws_record(loop_db_guard, cookie: str, *drives) -> set[tuple[str, str]]:
    """Each ``drive(ws)`` on its own dashboard socket, the guard recording
    around it with the agents' cache cold inside the window."""
    from storage.agents import agent_store
    from tests.fixtures.ws_dashboard_harness import (
        dashboard_connection,
        drain_startup,
        run_ws_scenario,
        sync_dispatch,
    )
    recorded: set = set()
    for drive in drives:
        async def scenario(drive=drive):
            async with dashboard_connection(cookie) as ws:
                await drain_startup(ws)
                with loop_db_guard.recording() as sites:
                    agent_store._invalidate_cache()
                    await drive(ws)
                    await sync_dispatch(ws)
                recorded.update(sites)
        run_ws_scenario(scenario)
    return recorded


async def _frame(ws, kind: str) -> dict:
    while True:
        frame = await ws.next_frame(5.0)
        if frame.get("type") == kind:
            return frame


def test_warmup_of_a_new_chat(temp_db, monkeypatch, loop_db_guard, binding_writer):
    """A pre-warm the warmup reuses, then a new chat warmed fresh."""
    from tests.fixtures.ws_dashboard_harness import sync_dispatch, warm_new_chat
    w = _warmup_rig(monkeypatch)

    async def prewarmed(ws):
        ws.client_send({"type": "pre_warmup", "agent": w.slug, "permission_mode": "default"})
        await sync_dispatch(ws)
        ws.client_send({"type": "warmup", "agent": w.slug})
        await _frame(ws, "warmup_ready")
        assert len(w.layer.started) == 1  # the pre-warm, reused

    async def fresh(ws):
        await warm_new_chat(ws, w.layer, w.slug)

    _check("warmup of a new chat", _ws_record(loop_db_guard, w.cookie, prewarmed, fresh))


def test_resume_of_an_existing_chat(temp_db, monkeypatch, loop_db_guard, binding_writer):
    """A chat whose session died: resumed when the engine can, else a fresh
    session with the history seed flagged (the swap and the re-claim are
    one lane job), on a chat that keeps no model, so the warmup fills it."""
    from storage import database as db
    w = _warmup_rig(monkeypatch)
    chats = {}
    for name in ("resumed", "fresh"):
        cid = str(uuid.uuid4())
        db.create_chat(cid, "user-admin", w.slug, "default", execution_path="claude-code-cli")
        db.update_chat(cid, session_id=f"dead-{name}")
        chats[name] = cid
    w.layer.resumable.add("dead-resumed")

    def drive(name):
        async def _drive(ws):
            ws.client_send({"type": "warmup", "agent": w.slug, "chat_id": chats[name]})
            return await _frame(ws, "warmup_ready")
        return _drive

    recorded = _ws_record(loop_db_guard, w.cookie, drive("resumed"), drive("fresh"))
    _check("resume of an existing chat", recorded)
    assert [cfg.resume for _sid, cfg in w.layer.started] == [True, False]
    row = db.get_chat(chats["fresh"])
    assert row["session_id"] != "dead-fresh" and row["pending_history_seed"] == "resume_failed"


def test_mode_reconcile(temp_db, monkeypatch, loop_db_guard, binding_writer):
    """The session runs another mode than the warmup asked for (a pick that
    raced the spawn): the chat row takes the session's."""
    from storage import database as db
    from ws import dashboard_warmup_tail
    w = _warmup_rig(monkeypatch)
    monkeypatch.setattr(dashboard_warmup_tail, "get_session_mode", lambda sid: "plan")
    seen = {}

    async def drive(ws):
        ws.client_send({"type": "warmup", "agent": w.slug})
        started = await _frame(ws, "warmup_started")
        seen["chat_id"] = started["chat_id"]
        seen["ready"] = await _frame(ws, "warmup_ready")

    _check("mode reconcile", _ws_record(loop_db_guard, w.cookie, drive))
    assert seen["ready"]["mode"] == "plan"
    assert db.get_chat(seen["chat_id"])["permission_mode"] == "plan"


def test_offline_machine(temp_db, monkeypatch, loop_db_guard, binding_writer):
    """The person's own machine is offline and the session fell back (the
    banner names the machine), and a chat pinned to a machine that is
    offline is refused with the machine's name."""
    from services.remote import remote_status
    from storage import database as db, remote_store
    w = _warmup_rig(monkeypatch)
    remote_store.create_remote_machine("m-own", "Desk", "user-admin", pairing_scope="user")
    remote_store.set_user_remote_target("user-admin", "m-own", w.slug)
    remote_store.create_remote_machine("m-pin", "Rack", "user-admin")
    pinned = str(uuid.uuid4())
    db.create_chat(pinned, "user-admin", w.slug, "default", execution_path="claude-code-cli")
    db.update_chat(pinned, session_id="dead-pinned", execution_target="m-pin")
    monkeypatch.setattr(remote_status, "resolve_execution_target",
                        lambda *a, **k: ("local", "user-override-offline"))
    monkeypatch.setattr(remote_status, "is_reachable", lambda machine_id: False)
    # The pinned chat's machine answers through the remote layer, the harness's layer here.
    from core.session import session_manager
    monkeypatch.setattr(session_manager, "_remote_layer", w.layer)
    seen = {}

    async def fell_back(ws):
        ws.client_send({"type": "warmup", "agent": w.slug})
        seen["ready"] = await _frame(ws, "warmup_ready")

    async def pinned_offline(ws):
        ws.client_send({"type": "warmup", "agent": w.slug, "chat_id": pinned})
        seen["failed"] = await _frame(ws, "warmup_failed")

    _check("offline machine", _ws_record(loop_db_guard, w.cookie, fell_back, pinned_offline))
    assert seen["ready"]["offline_machine_name"] == "Desk"
    assert "Rack" in seen["failed"]["error"]


def test_implement_a_plan(temp_db, monkeypatch, loop_db_guard, binding_writer):
    """A plan implemented in a new chat: the session closed, a new one started
    in the chosen mode with the chat's model, the row rebound."""
    from storage import database as db
    from tests.fixtures.ws_dashboard_harness import warm_new_chat
    w = _warmup_rig(monkeypatch)
    seen = {}

    async def implement(ws):
        seen["chat_id"], _sid = await warm_new_chat(ws, w.layer, w.slug)
        ws.client_send({"type": "implement_plan", "plan_path": "plans/p.md", "mode": "autoReview"})
        seen["ready"] = await _frame(ws, "warmup_ready")

    _check("implement a plan", _ws_record(loop_db_guard, w.cookie, implement))
    assert seen["ready"]["mode"] == "autoReview"
    row = db.get_chat(seen["chat_id"])
    assert (row["session_id"], row["permission_mode"]) == (seen["ready"]["session_id"], "autoReview")


def test_headless_resume(temp_db, monkeypatch, loop_db_guard, binding_writer):
    """The connection-free heal of a dead session (the duplex heal): a
    plain chat resumed, a task chat rebuilt fresh in its run's identity."""
    from storage import database as db
    from ws import headless_resume
    w = _warmup_rig(monkeypatch)
    plain = str(uuid.uuid4())
    db.create_chat(plain, "user-admin", w.slug, "default", model=w.model,
                   execution_path="claude-code-cli")
    db.update_chat(plain, session_id="dead-plain")
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    db.create_run(run_id, "dyn-heal", w.slug, "manual", None, "heal",
                  task_type="scheduled", scope="agent", created_by="user-admin")
    task_chat = f"task-{run_id}"
    db.create_chat(task_chat, "user-admin", w.slug, "default", model=w.model,
                   execution_path="claude-code-cli")
    db.update_chat(task_chat, session_id="dead-task")
    w.layer.resumable.add("dead-plain")
    spawns = []
    recorded: set = set()
    for cid, dead in ((plain, "dead-plain"), (task_chat, "dead-task")):
        recorded |= _record(loop_db_guard, functools.partial(
            _heal, headless_resume, cid, dead, w.layer, spawns))
    _check("headless resume", recorded)
    assert [s.resumed for s in spawns] == [True, False]
    assert db.get_chat(task_chat)["pending_history_seed"] == "resume_failed"


async def _heal(headless_resume, chat_id, dead_sid, layer, spawns):
    spawns.append(await headless_resume.resume_dead_session_headless(
        chat_id, dead_sid, layer, user_sub="user-admin"))


def test_install_enabled_state(temp_db, loop_db_guard, tmp_path):
    from services.community import community_installer as ci
    existing = SimpleNamespace(version="1.0.0", mcp_dir=tmp_path / "inv-mcp", category="community",
                               server=SimpleNamespace(runtime="python", transport="stdio"))
    recorded = _record(loop_db_guard, lambda: ci.ensure_enabled_and_running("inv-mcp"))
    recorded |= _record(loop_db_guard, lambda: ci._place_folder(
        "inv-mcp", existing, True, False, None, tmp_path))
    _check("install enabled state", recorded)


def test_task_permission(temp_db, loop_db_guard):
    from api.tasks import tasks as tasks_api
    from auth.providers import UserContext
    from storage import database as db
    from storage.agents import agent_store
    from storage.identity import db_users
    agent_store.create_agent(AGENT, "Char Agent")
    db_users.add_user_agent("user-viewer", AGENT, "editor", "user-admin")
    tid = f"dyn-{uuid.uuid4().hex[:8]}"
    db.create_dynamic_task(tid, AGENT, "T", "p", "auto", "one_time", None, None, 600,
                           600, "user-viewer", scope="agent")
    editor = UserContext(sub="user-viewer", email="viewer@test.com", name="Viewer User",
                         role="member", agents=[AGENT], agent_roles={AGENT: "editor"})
    recorded = _record(loop_db_guard, lambda: tasks_api._resolve_pause_resume_target(tid, editor, None))
    _check("task permission", recorded)


def test_task_routes(temp_db, monkeypatch, loop_db_guard):
    """A peer agent's session creates a task on the agent (the scope check,
    the agent-to-agent check through the peer's delegation targets), the
    person lists the agent's tasks (each row's model and the platform zone)
    and the session pauses the task (the agent-to-agent check and the
    permission check), on a cold agents cache."""
    import config
    from api.tasks import tasks as tasks_api
    from auth.providers import UserContext
    from storage import database as db
    from storage.agents import agent_store
    from storage.billing import subscription_store
    from storage.identity import db_users
    monkeypatch.setattr(config, "SCHEDULER_MODE", "standalone")
    agent_store.create_agent(AGENT, "Char Agent")
    agent_store.create_agent("char-peer", "Char Peer")
    agent_store.set_delegation_targets("char-peer", [AGENT])
    db_users.add_user_agent("user-viewer", AGENT, "editor", "user-admin")
    subscription_store.add_model("claude-code-cli", "claude-inv-1", "Inv", provider="anthropic", tier=2)
    roles = {"agents": [AGENT], "agent_roles": {AGENT: "editor"}}
    session = UserContext(sub="user-viewer", email="viewer@test.com", name="Viewer User",
                          role="member", is_api_key=True, session_id="s-inv", agent="char-peer",
                          **roles)
    person = UserContext(sub="user-viewer", email="viewer@test.com", name="Viewer User",
                         role="member", **roles)
    req = tasks_api.CreateScheduledTaskRequest(
        name="inv", agent=AGENT, prompt="p", interval_seconds=3600, scope="agent",
        notification_mode="none")
    made: dict = {}

    async def create():
        made.update(await tasks_api.create_scheduled_task(req, user=session, x_agent_name=None))

    recorded = _record(loop_db_guard, create)
    recorded |= _record(loop_db_guard, functools.partial(
        tasks_api.list_tasks, agent=AGENT, audit=False, user=person))
    recorded |= _record(loop_db_guard, lambda: tasks_api._resolve_pause_resume_target(
        made["task_id"], session, None))
    _check("task routes", recorded)
    assert db.get_dynamic_task(made["task_id"])["scope"] == "agent"


def test_abort_during_spawn(temp_db, loop_db_guard):
    from storage import database as db
    from ws.dashboard_warmup_tail import WarmupTail, _TailRun
    cid = str(uuid.uuid4())
    db.create_chat(cid, "user-admin", AGENT, "default")
    aborted: list = []

    class _Layer:
        async def abort(self, sid):
            aborted.append(sid)

    host = SimpleNamespace(_warmup_abort_chat=cid, _drop_warmup_mark=lambda c: None)
    run = _TailRun(res=SimpleNamespace(layer=_Layer(), session_id="s-abort"))
    recorded = _record(loop_db_guard, lambda: WarmupTail._tail_abort_check(
        host, SimpleNamespace(wcid=cid), run))
    _check("abort during spawn", recorded)
    assert run.aborted and aborted == ["s-abort"]
    assert db.get_chat(cid)["last_turn_aborted"]


def test_usage_snapshot(temp_db, loop_db_guard):
    from core.events import chat_writer
    from core.events.stream_pump import ChatStreamPump
    from storage import database as db
    cid = str(uuid.uuid4())
    db.create_chat(cid, "user-admin", AGENT, "default")

    async def scenario():
        producer = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        pump = ChatStreamPump(chat_id=cid, session_id=f"sess-{cid}", producer=producer,
                              event_queue=asyncio.Queue(), perm_queue=None)
        pump._input_tokens, pump._output_tokens, pump._total_cost_delta = 10, 5, 0.01
        with loop_db_guard.recording() as sites:
            fut = pump._submit_cost_job()
        await fut
        await chat_writer.drain(cid)
        producer.cancel()
        return set(sites)
    recorded = asyncio.run(scenario())
    _check("usage snapshot", recorded)


def test_push_title(temp_db, monkeypatch, loop_db_guard):
    import services.notifications.notification_manager as nm
    import services.notifications.push_sender as ps
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Char Agent")
    sent: list = []

    async def _send(user_sub, payload):
        sent.append(payload)
    monkeypatch.setattr(ps, "send_to_user", _send)
    monkeypatch.setattr(nm, "get_active_connections", lambda u: [])
    monkeypatch.setattr(nm, "get_all_connections", lambda u: [])
    delivery = {"id": "d1", "notification_id": "n1", "title": "Done", "body": "B",
                "severity": "info", "scope": "user", "source": "",
                "delivered_at": "2026-10-18T00:00:00Z", "agent_slug": AGENT, "chat_id": ""}
    recorded = _record(loop_db_guard, functools.partial(nm._deliver_to_user, "user-admin", delivery))
    _check("push title", recorded)
    assert sent and sent[0]["title"] == "Char Agent · Done"
