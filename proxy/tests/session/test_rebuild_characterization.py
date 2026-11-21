"""Characterization of the closes a rebuild of a chat's session starts with:
the switch between a terminal and a headless chat, the plan's fresh start and
a lane's model change. What was closed and through what, what the person was
sent, what the chat's row holds after.

Rows are real. Faked at the boundaries: the execution layer and the
collaborators ``stub_dashboard_seams`` stubs, a terminal that records its
close, the warm-up the switch ends in (recorded, not run) and the admission
ledger. A drift is a behaviour change, and the commit that records it says why.
"""
import asyncio
import re
import uuid

from tests.fixtures.characterization.golden_master import check_golden
from tests.fixtures.ws_dashboard_harness import (
    TEST_MODEL,
    FakeExecutionLayer,
    make_test_agent,
    stub_dashboard_seams,
)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
OWNER = "user-admin"


class _Terminal:
    """A live terminal of a chat: who may drive it and how it was closed."""

    def __init__(self, chat_id, session_id, *, user_sub=OWNER):
        self.chat_id, self.session_id, self.user_sub = chat_id, session_id, user_sub
        self.username, self.agent_name, self.target = "admin", "agent", "local"
        self.is_remote = False
        self.alive, self.turn_open, self.has_viewer = True, False, False
        self.created_at = 0.0
        self.closed: list[str] = []

    async def close(self, *, reason: str = "closed") -> None:
        self.alive = False
        self.closed.append(reason)

    def may_drive(self, sender_sub):
        return not self.user_sub or not sender_sub or sender_sub == self.user_sub


def _chat(agent, *, session_id=None, user_sub=OWNER, execution_mode=""):
    from storage import database as task_store
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, user_sub, agent, "ask", model=TEST_MODEL,
                           execution_path="claude-code-cli")
    fields = {k: v for k, v in (("session_id", session_id),
                                ("execution_mode", execution_mode)) if v}
    if fields:
        task_store.update_chat(cid, **fields)
    return cid


def _connection(chat_id, session_id, *, agent, layer, user_sub=OWNER):
    """A dashboard connection with no socket: the handlers run against the
    test database and a recorded outbox, the warm-up recorded in place."""
    from ws.dashboard import DashboardConnection
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.user_sub = user_sub
    conn.user = {"role": "admin" if user_sub == OWNER else "member",
                 "username": "admin" if user_sub == OWNER else "viewer"}
    conn.user_role = conn.user["role"]
    conn.user_agents, conn.agent_roles = [agent], {}
    conn.chat_id, conn.agent_name, conn.session_id, conn.layer = chat_id, agent, session_id, layer
    conn.streaming = False
    conn.pending_control_requests, conn.message_queue, conn.artifact_queue = [], [], []
    conn.implementing_plan = ""
    conn.deferred_mode = conn.deferred_model = conn.deferred_for = ""
    conn._pre_warmed_sid = conn._warmup_task = None
    conn._history_floors = None
    conn.session_execution_target = conn.session_fallback_reason = None
    conn.notify_connection_id = "conn-test"
    conn.sent, conn.warmups, conn.detached = [], [], 0

    async def _send(frame):
        conn.sent.append(frame)

    async def _send_error(text):
        conn.sent.append({"type": "error", "message": text})

    async def _handle_warmup(msg, *, background=True):
        conn.warmups.append({**msg, "background": background})

    def _detach():
        conn.detached += 1

    conn._send, conn._send_error = _send, _send_error
    conn._handle_warmup, conn._detach_pty_viewer = _handle_warmup, _detach
    return conn


def _norm(value, known: dict[str, str]):
    """Ids as their names: the scenario's own, and ``<uuid>`` for a minted one."""
    import json
    text = json.dumps(value, sort_keys=True, default=repr)
    for real, name in known.items():
        text = text.replace(real, name)
    return json.loads(_UUID.sub("<uuid>", text))


def _frames(sent: list[dict]) -> list[dict]:
    keep = ("type", "execution_mode", "mode", "model", "session_id", "interactive", "message")
    return [{k: f[k] for k in keep if k in f} for f in sent]


def _row(chat_id: str) -> dict:
    from storage import database as task_store
    row = task_store.get_chat(chat_id) or {}
    return {k: row.get(k) for k in ("session_id", "execution_mode", "permission_mode")}


def _switch(temp_db, monkeypatch, *, to, live="", terminal_of="", pump=False, viewer=""):
    from core.events import stream_pump
    from core.session import interactive_session
    layer = FakeExecutionLayer()
    slots = stub_dashboard_seams(monkeypatch, layer)
    agent = make_test_agent()
    cid = _chat(agent, session_id="sid-old")
    term = None
    if live == "headless":
        layer.alive.add("sid-old")
    if terminal_of:
        term = _Terminal(cid, "sid-old", user_sub=terminal_of)
        monkeypatch.setitem(interactive_session._sessions, "sid-old", term)
    if pump:
        monkeypatch.setitem(stream_pump._active_pumps, cid,
                            type("P", (), {"is_done": False, "session_id": "sid-old"})())
    if viewer:
        from storage import database as task_store
        task_store.add_user_agent(viewer, agent, "editor", "test")
    conn = _connection(cid, "sid-old", agent=agent, layer=layer, user_sub=viewer or OWNER)
    asyncio.run(conn._handle_switch_execution_mode({"execution_mode": to, "chat_id": cid}))
    return _norm({
        "closed_on_layer": layer.closed_sessions,
        "terminal_closed": term.closed if term else None,
        "frames": _frames(conn.sent),
        "warmup": [{k: w[k] for k in ("chat_id", "execution_mode", "background")}
                   for w in conn.warmups],
        "viewer_detached": conn.detached,
        "bound_session": conn.session_id,
        "row": _row(cid),
        "slots_released": slots.released,
    }, {cid: "<chat>", agent: "<agent>"})


def test_the_switch_closes_what_is_live(temp_db, monkeypatch):
    check_golden("rebuild/switch-close", {
        "a live headless session, to a terminal": _switch(
            temp_db, monkeypatch, to="interactive", live="headless"),
        "a headless turn still streaming": _switch(
            temp_db, monkeypatch, to="interactive", live="headless", pump=True),
        "a live terminal, to headless": _switch(
            temp_db, monkeypatch, to="-p", terminal_of=OWNER),
        "a dead session": _switch(temp_db, monkeypatch, to="interactive"),
        "a person who may not open the chat": _switch(
            temp_db, monkeypatch, to="-p", terminal_of=OWNER, viewer="user-viewer"),
    })


def test_the_switch_history_names_the_delegations_still_working(temp_db, monkeypatch):
    # The switch closes the session, never a delegation it started: the page
    # rebuilt from this history keeps the badge of one older than the page.
    from storage import database as task_store
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    agent = make_test_agent()
    cid = _chat(agent, session_id="sid-old")
    task_store.create_dynamic_task(
        "dyn-switch1", agent, "long lane", "p", "cli", "delegate", None, None, None, 3600,
        OWNER, on_complete_chat_id=cid)
    task_store.create_run("run-switch1", "dyn-switch1", agent, "manual", None, "p",
                          task_type="delegate", scope="user", created_by=OWNER)
    conn = _connection(cid, "sid-old", agent=agent, layer=layer)
    asyncio.run(conn._handle_switch_execution_mode({"execution_mode": "interactive", "chat_id": cid}))
    history = [f for f in conn.sent if f["type"] == "chat_history"]
    assert [f["running"]["delegates"] for f in history] == [
        [{"id": "dyn-switch1", "description": "long lane", "agent": agent}]]


def _plan(temp_db, monkeypatch, *, session="sid-old", live="", terminal=False):
    from core.session import interactive_session
    layer = FakeExecutionLayer()
    slots = stub_dashboard_seams(monkeypatch, layer)
    agent = make_test_agent()
    cid = _chat(agent, session_id=session or None)
    term = None
    if live == "headless":
        layer.alive.add(session)
    if terminal:
        term = _Terminal(cid, session)
        monkeypatch.setitem(interactive_session._sessions, session, term)
    conn = _connection(cid, session or None, agent=agent, layer=layer)
    asyncio.run(conn._handle_implement_plan({"plan_path": "plans/p.md", "mode": "dontAsk"}))
    return _norm({
        "closed_on_layer": layer.closed_sessions,
        "terminal_closed": term.closed if term else None,
        "started": [{"session": sid, "resume": cfg.resume, "mode": cfg.permission_mode,
                     "interactive": cfg.interactive} for sid, cfg in layer.started],
        "frames": _frames(conn.sent),
        "bound_session": conn.session_id,
        "row": _row(cid),
        "slots_acquired": [sid for sid, _kw in slots.acquired],
        "slots_released": slots.released,
    }, {cid: "<chat>", agent: "<agent>"})


def test_the_plans_fresh_start_closes_the_bound_session(temp_db, monkeypatch):
    check_golden("rebuild/plan-close", {
        "a live session": _plan(temp_db, monkeypatch, live="headless"),
        "a dead session": _plan(temp_db, monkeypatch),
        "no session": _plan(temp_db, monkeypatch, session=""),
        "the person's own live terminal": _plan(temp_db, monkeypatch, terminal=True),
    })


def _model_change(temp_db, monkeypatch, *, live):
    from core import concurrency
    from services.scheduler import lanes
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    if live:
        layer.alive.add("sid-lane")
    seen: dict = {"locked_at_close": None, "acquired": []}
    real_close = layer.close_session

    async def _close(sid):
        seen["locked_at_close"] = layer.session_lock(sid).locked()
        await real_close(sid)

    async def _acquire(sid, kind, **kw):
        seen["acquired"].append([sid, kind, kw])
        return True

    monkeypatch.setattr(layer, "close_session", _close)
    monkeypatch.setattr(concurrency, "acquire", _acquire)
    out = asyncio.run(lanes._close_for_model_change("sid-lane", "run-1", slot_target="local"))
    return {"closed": out, "closed_on_layer": layer.closed_sessions, **seen}


def test_a_model_change_closes_the_lanes_live_session(temp_db, monkeypatch):
    check_golden("rebuild/model-close", {
        "a live session": _model_change(temp_db, monkeypatch, live=True),
        "a session nobody holds": _model_change(temp_db, monkeypatch, live=False),
    })
