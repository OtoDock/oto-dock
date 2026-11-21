"""Characterization of the delegate delivery (``services/scheduler/delivery.py``,
``wake_session.py``): the wake's pin, targets, prompt and spawned config, the event's fields, the
undelivered store, and whole deliveries and sweeps down the real ladder.

Rows, the ladder and the pump are real. Faked at the boundaries: the execution
layer (``get_execution_layer``), the admission budget
(``core.concurrency``), and the collaborators ``tests/fixtures/loop_builder_rig``
stubs for the builders (the MCP build, the prompt, the subscription). A name
of the wake is read off the function that uses it (``_of``), which finds it in
whichever module holds that function. A drift is a behaviour change, and the
commit that records it says why.
"""
import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from core import background, placement
from core.events import turn_ending
from core.events.common_events import DONE, TEXT, CommonEvent
from core.execution_layer import ResumeVerdict
from core.session import session_kind
from core.session.visibility import shared_chat_owner, task_chat_owner
from services.scheduler import delivery
from services.scheduler.shared import TaskDefinition
from storage import database as task_store
from storage.identity import db_users
from storage.pg import get_conn
from tests._compute import serve
from tests.fixtures.characterization.golden_master import check_golden
from tests.fixtures.loop_builder_rig import AGENT, PEER, SHARED, install, normalize
from tests.fixtures.wake_defaults import WAKE

SID = "11111111-2222-3333-4444-555555555555"
TASK_CHAT = session_kind.task_chat_id("run-char")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _of(reader: str, name: str):
    """A name the function ``reader`` reads, from that function's own globals
    (``reader``: on ``delivery``, or a function the wake's spawn reads)."""
    function = getattr(delivery, reader, None) or _of("_spawn_wake_session", reader)
    return function.__globals__[name]


def _norm(value):
    """``normalize`` and the ids a fresh start mints."""
    out = normalize(value)
    text = json.dumps(out, sort_keys=True, default=repr, ensure_ascii=False)
    return json.loads(_UUID.sub(lambda m: "<sid>" if m.group(0) == SID else "<uuid>", text))


class _Layer:
    """The execution layer ``get_execution_layer`` hands out: what it was asked,
    one TEXT turn and quiet watchdog probes, as the echo tests' fake layer."""

    def __init__(self, *, alive=False, resume=ResumeVerdict.RESUMABLE, parts=("ECHO ", "TEXT"),
                 on_alive=None, send_raises=False):
        self.alive, self.resume, self.parts = alive, resume, parts
        self.on_alive, self.send_raises = on_alive, send_raises
        self.calls: list = []
        self.configs: list = []
        self.on_start = None
        self._locks: dict = {}

    def session_lock(self, sid):
        return self._locks.setdefault(sid, asyncio.Lock())

    async def send_message(self, sid, prompt, settle_after_result=None):
        self.calls.append(["send_message", sid, prompt])
        if self.send_raises:
            raise BrokenPipeError("the pipe broke")
        for part in self.parts:
            yield CommonEvent(type=TEXT, data={"content": part})
        yield CommonEvent(type=DONE, data={})

    async def is_session_alive(self, sid):
        self.calls.append(["is_session_alive", sid])
        if self.on_alive is not None:
            self.on_alive()
        return self.alive

    async def can_resume_session(self, sid, agent_name="", username="", external_home=""):
        self.calls.append(["can_resume_session", sid, agent_name, username])
        return self.resume

    async def start_session(self, sid, cfg):
        self.calls.append(["start_session", sid])
        self.configs.append(cfg)
        if self.on_start is not None:
            self.on_start(sid, cfg)

    async def close_session(self, sid):
        self.calls.append(["close_session", sid])

    def remote_stream_severed(self, sid):
        return False

    def session_idle_seconds(self, sid):
        return 0.0

    async def probe_session_process_dead(self, sid):
        return False

    async def wait_for_bg_subagents(self, sid, timeout=120.0):
        return

    async def drain_bg_commands(self, sid, *, budget=2.0):
        return False

    async def prepare_resume(self, sid):
        return


@pytest.fixture
def rig(temp_db, monkeypatch, tmp_path):
    """The builders' rig, the layer, a recording admission budget and frames."""
    import core.concurrency as concurrency
    from auth import path_policy
    from core.config import config_builder
    from core.events import stream_pump
    from core.remote.remote_execution import RemoteExecutionLayer
    from core.remote.satellite_connection import SatelliteConnectionManager
    from core.session import session_manager, session_state, sibling_awareness
    from services.notifications import notification_manager

    rec = install(monkeypatch, tmp_path)
    # A wake of a chat on the platform's place is built on the compute
    # machine that serves it, and takes its slot there.
    serve(monkeypatch)
    mgr = SatelliteConnectionManager()
    monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager", lambda: mgr)
    monkeypatch.setattr(session_manager, "_remote_layer", RemoteExecutionLayer(mgr))
    rig = SimpleNamespace(
        rec=rec, mgr=mgr, layer=_Layer(), layers_asked=[], admission=[], reserve="reserved",
        released=[], seats=[], frames=[], monkeypatch=monkeypatch)

    def _layer(agent, **kw):
        rig.layers_asked.append({"agent": agent, **kw})
        return rig.layer

    async def _reserve(session_id, *, target, execution_path, timeout_s, superseded):
        rig.admission.append({"session": session_id, "target": target, "path": execution_path,
                              "superseded": superseded()})
        return rig.reserve

    async def _quiet(*a, **k):
        return None

    async def _no_siblings(chat_id, text):
        return text

    def _permissions(security, **kw):
        rec.calls.append({"call": "build_permission_context", "args": [security], "kwargs": kw})
        return "\n\nPERMISSIONS"

    monkeypatch.setattr(path_policy, "build_permission_context", _permissions)
    monkeypatch.setattr(session_manager, "get_execution_layer", _layer)
    monkeypatch.setattr(concurrency, "reserve_background", _reserve)
    monkeypatch.setattr(concurrency, "release_unless_live",
                        lambda sid: rig.released.append(sid) or True)
    monkeypatch.setattr(config_builder, "release_config_seat",
                        lambda sid, cfg: rig.seats.append(sid))
    monkeypatch.setattr(session_state, "broadcast_chat_frame",
                        lambda chat_id, frame: rig.frames.append([chat_id, frame]))
    monkeypatch.setattr(notification_manager, "fire_ephemeral", _quiet)
    # A prelude of what else runs now, from a snapshot kept for two seconds.
    monkeypatch.setattr(sibling_awareness, "prepend_if_changed", _no_siblings)
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)
    stream_pump._stored_wake_chats.clear()
    yield rig
    stream_pump._stored_wake_chats.clear()


def _chat(chat_id: str, owner: str, agent: str = AGENT, **fields) -> dict:
    task_store.create_chat(chat_id, owner, agent)
    if fields:
        task_store.update_chat(chat_id, **fields)
    return task_store.get_chat(chat_id)


def _machine(machine_id: str, owner: str = "user-admin", scope: str = "admin", *, online=False,
             rig=None) -> str:
    from core.remote.satellite_connection import SatelliteConnection
    from storage import remote_store
    remote_store.create_remote_machine(machine_id, machine_id.upper(), owner, pairing_scope=scope)
    if online:
        rig.mgr._connections[machine_id] = SatelliteConnection(machine_id=machine_id, ws=None)
    return machine_id


def _rows(chat_id: str) -> list[dict]:
    """A chat's messages as the golden holds them."""
    out = []
    for row in task_store.get_chat_messages(chat_id):
        event = row.get("event_data")
        out.append({"role": row.get("role"), "content": row.get("content"),
                    "event_type": row.get("event_type"),
                    "event_data": json.loads(event) if event else None})
    return out


def _pending(chat_id: str) -> list[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT pending_delegate_wake AS p FROM chats WHERE id=%s",
                           (chat_id,)).fetchone()
    return json.loads(row["p"]) if row and row["p"] else []


async def _delivered() -> None:
    await asyncio.gather(*background.running("delivery"))


def _asked(rig) -> list:
    """The builders' collaborator calls, a call's large read-only inputs named
    by what identifies them (their shape belongs to other modules)."""
    out = []
    for call in rig.rec.calls:
        kwargs = dict(call["kwargs"])
        if "agent_info" in kwargs:
            kwargs["agent_info"] = (kwargs["agent_info"] or {}).get("slug")
        if "delegation_roster" in kwargs:
            kwargs["delegation_roster"] = sorted(kwargs["delegation_roster"] or {})
        if "meetings_access" in kwargs:
            kwargs["meetings_access"] = [a["slug"] for a in kwargs["meetings_access"] or []]
        out.append({**call, "kwargs": kwargs})
    rig.rec.calls.clear()
    return out


def _seen(rig) -> dict:
    """What reached the fakes since the last look."""
    out = {"layer": rig.layer.calls, "layers_asked": rig.layers_asked,
           "admission": rig.admission, "released": rig.released, "seats": rig.seats,
           "frames": rig.frames}
    rig.layer.calls, rig.layers_asked, rig.admission = [], [], []
    rig.released, rig.seats, rig.frames = [], [], []
    return out


# --- The wake session: where it runs, who it may delegate to, its prompt ------


def test_chat_pin(rig):
    online = _machine("m-online", online=True, rig=rig)
    offline = _machine("m-offline")
    person = "user-viewer"
    other = "user-manager"
    shared_owner = shared_chat_owner(SHARED)
    cases = {
        "no chat": (None, person),
        "a task chat with no pin": ({"id": TASK_CHAT, "agent": AGENT}, None),
        "a task chat pinned local": ({"id": TASK_CHAT, "agent": AGENT, "execution_target": "local"}, None),
        "a task chat pinned to a reachable machine": (
            {"id": TASK_CHAT, "agent": AGENT, "execution_target": online}, person),
        "a task chat pinned to an unreachable machine": (
            {"id": TASK_CHAT, "agent": AGENT, "execution_target": offline}, None),
        "no person": (_chat("c-nobody", person), None),
        "their own chat, no placement": ({**_chat("c-own", person), "execution_target": ""}, person),
        "their own chat, local": (_chat("c-own-l", person, execution_target="local"), person),
        "their own chat, machine": (_chat("c-own-m", person, execution_target=online), person),
        "their own chat, machine away": (_chat("c-own-o", person, execution_target=offline), person),
        "someone else's chat": (_chat("c-other", other, execution_target=online), person),
        "a shared chat, no placement": (
            {**_chat("c-shared", shared_owner, SHARED), "execution_target": ""}, person),
        "a shared chat, machine": (_chat("c-shared-m", shared_owner, SHARED, execution_target=online), person),
        "a shared chat, machine away": (_chat("c-shared-o", shared_owner, SHARED, execution_target=offline), person),
        "a former shared chat": (_chat("c-former", shared_chat_owner(AGENT), AGENT), person),
    }
    check_golden("delivery/chat-pin", {
        name: delivery._chat_pin(chat, sub) for name, (chat, sub) in cases.items()})


def test_a_chat_built_session_pins_a_left_over_shared_chat_as_the_dashboard_does(rig):
    """A key's turn builds a chat as its person's warm-up does, so a shared
    chat left after its agent quit Shared-only runs where its row says, as
    the dashboard resumes it, never where its person's own resolution says."""
    online = _machine("m-online", online=True, rig=rig)
    chat = _chat("c-left-over", shared_chat_owner(AGENT), AGENT, execution_target=online)
    assert delivery._chat_pin(chat, "user-viewer") == ""
    assert delivery._chat_pin(chat, "user-viewer", as_chat=True) == online
    assert delivery._chat_pin(chat, None, as_chat=True) == ""


def test_a_chat_built_rung_runs_a_left_over_shared_chat_where_its_row_says(rig):
    """At the rung itself: a session built as the chat (a key's turn) on a
    shared chat left after its agent quit Shared-only takes the machine its
    row names, and the delivery's own build resolves from the person."""
    from core.session import chat_rebuild
    online = _machine("m-mine", owner="user-viewer", scope="user", online=True, rig=rig)
    _chat("c-left-over", shared_chat_owner(AGENT), AGENT, session_id=SID, execution_target=online)

    async def target(build: str) -> str:
        rig.layers_asked.clear()
        rig.layer = _Layer()
        await delivery._deliver_via_oneshot(SID, AGENT, "WAKE", user_sub="user-viewer", role="editor",
                                            chat_id="c-left-over", **{**WAKE, "build": build})
        return rig.layers_asked[0]["execution_target"]

    assert asyncio.run(target(chat_rebuild.AS_CHAT)) == online
    assert asyncio.run(target(chat_rebuild.AS_WAKE)) == "local"


def test_a_keys_turn_runs_in_the_mode_the_chat_has_when_its_session_is_built(rig):
    """A key's message is sent in the chat's mode read at the send; a person
    who changes the mode while the turn waits for its slot is read again at
    the build, where the role is: a changed mode starts nothing (the request
    is sent again), the same mode starts the session."""
    from core.session import chat_rebuild
    _chat("c-key-mode", "user-viewer", session_id=SID, permission_mode="ask")

    async def start() -> int:
        rig.layer = _Layer()
        await delivery._deliver_via_oneshot(
            SID, AGENT, "WAKE", user_sub="user-viewer", role="editor", chat_id="c-key-mode",
            client_type="dashboard", permission_mode="ask", build=chat_rebuild.AS_CHAT, driven_by="api_key")
        return len(rig.layer.configs)

    assert asyncio.run(start()) == 1
    task_store.update_chat("c-key-mode", permission_mode="plan")
    assert asyncio.run(start()) == 0


def test_a_keys_turn_the_build_refuses_a_place_says_the_refusals_own_cause(rig):
    """The person's role or the machine's rule changed between the door and
    the build: the build refuses the place, and the key's caller reads that
    refusal's own sentence, not the generic guess."""
    from api.public import key_turns
    adm = _machine("m-adm-key", online=True, rig=rig)
    _chat("c-key-refused", "user-viewer", session_id=SID, execution_target=adm)
    rig.layer = _Layer()
    who = SimpleNamespace(sub="user-viewer", api_key_id="key-refused", api_key_name="rig key")
    turn = key_turns.KeyTurn("key-refused", "req-refused", "c-key-refused")
    asyncio.run(key_turns._deliver(who, task_store.get_chat("c-key-refused"), "hello", turn, role="editor",
                                   permission_mode="ask", client_type="dashboard"))
    detail = key_turns.view(turn)["detail"]
    assert turn.state == key_turns.NOT_SENT and rig.layer.configs == []
    assert detail.startswith("The message was not sent.") and "can't use" in detail
    assert "a person took it meanwhile" not in detail


def test_the_ladder_answers_the_id_it_was_given_and_the_row_names_a_fresh_start(rig):
    """A chat with no session, down the real ladder and rung: the session
    starts under a new id, the chat's row names it, and the ladder's answer
    names none (the id it was given). A caller that needs the session reads
    the row."""
    from core.session import chat_rebuild
    _chat("c-ladder-fresh", "user-viewer", session_id="")
    rig.layer = _Layer()
    outcome = asyncio.run(chat_rebuild.rebuild_and_resume(
        "c-ladder-fresh", "hello", source="api_key", session_id="", agent=AGENT, person="user-viewer",
        role="editor", mode="ask", kind="dashboard", build=chat_rebuild.AS_CHAT, allow_ws=False,
        allow_pty=False, allow_live_turn=False, fresh_without_session=True))
    started = [c[1] for c in rig.layer.calls if c[0] == "start_session"]
    assert outcome.path == "oneshot" and outcome.session_id == ""
    assert started and task_store.get_chat("c-ladder-fresh")["session_id"] == started[0]


@pytest.mark.parametrize("background", [0, 1])
def test_a_keys_fresh_session_on_a_device_place_carries_its_mark(rig, monkeypatch, background):
    """Down the real ladder and rung: a key's first message into a chat with
    no session starts one under a new id that only the chat's row names. On
    a place with a device grant that session carries the key's mark, and it
    is closed at the turn's end unless work it started still runs."""
    from api.public import key_turns
    from auth import roles
    from auth.path_policy import SecurityContext
    from core import placement
    from core.session import background_leash, key_rows, session_manager, session_state
    cid = f"c-key-fresh-{background}"
    _chat(cid, "user-viewer", session_id="")
    rig.layer = _Layer()
    started: list[str] = []

    def on_a_device_place(sid, cfg):
        started.append(sid)
        session_state.register_session_state(sid, "ask", SecurityContext(
            role=roles.EDITOR, username="viewer", agent=AGENT, is_admin_agent=False,
            placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m-1",
                                                      device_grants={"browser"})))
    rig.layer.on_start = on_a_device_place
    monkeypatch.setattr(session_manager, "find_layer_for_session", lambda s: rig.layer if s in started else None)
    monkeypatch.setattr(background_leash, "background_pending_count", lambda s: background)
    who = SimpleNamespace(sub="user-viewer", api_key_id="key-fresh", api_key_name="rig key")
    turn = key_turns.KeyTurn("key-fresh", f"req-{background}", cid)
    try:
        asyncio.run(key_turns._deliver(who, task_store.get_chat(cid), "hello", turn, role="editor",
                                       permission_mode="ask", client_type="dashboard"))
        sid = task_store.get_chat(cid)["session_id"]
        assert sid and started == [sid] and turn.session_id == sid and turn.state == key_turns.DONE
        assert key_rows.drives("", sid)
        assert (["close_session", sid] in rig.layer.calls) is (background == 0)
    finally:
        for sid in started:
            key_rows.forget_session(sid)
            session_state.cleanup_session_permission_state(sid)


def test_wake_delegation_targets(rig):
    from storage.agents import agent_store
    targets = _of("_spawn_wake_session", "_wake_delegation_targets")
    agent_store.set_delegation_targets(SHARED, [AGENT, PEER])
    agent_store.create_agent("char-self", "Self")
    agent_store.set_delegation_targets("char-self", [PEER, "char-self"])
    cases = {
        "nobody at manager": (AGENT, None, "manager"),
        "a person who holds no target": (AGENT, "user-viewer", "editor"),
        "a person who holds one": (SHARED, "user-viewer", "viewer"),
        "a platform admin": (AGENT, "user-admin", "viewer"),
        "the admin role": (AGENT, "user-viewer", "admin"),
        "the agent among its targets": ("char-self", None, "manager"),
        "no targets": (PEER, None, "manager"),
    }
    db_users.add_user_agent("user-manager", PEER, "viewer", "user-admin")
    cases["a person who holds the target"] = (AGENT, "user-manager", "manager")
    check_golden("delivery/wake-targets", {
        name: targets(agent, sub, role) for name, (agent, sub, role) in cases.items()})


def _security(agent, sub, role, target=placement.LOCAL):
    from core.config.task_config_builder import build_delivery_security_context
    return asyncio.run(build_delivery_security_context(agent, user_sub=sub, role=role, target=target))


def test_wake_prompt(rig):
    from services.scheduler.delivery_identity import _wake_visibility
    build = _of("_spawn_wake_session", "_wake_prompt")
    cases = {
        "a person's headless wake": dict(
            agent=AGENT, sub="user-viewer", name="vic", role="editor", kind="", excluded={},
            contexts=["CTX"], exec_path="claude-code-cli"),
        "a dashboard-kind resume": dict(
            agent=AGENT, sub="user-viewer", name="vic", role="editor", kind="dashboard",
            excluded={"char-files": "why"}, contexts=None, exec_path="codex-cli"),
        "nobody, at the manager role": dict(
            agent=AGENT, sub=None, name="", role="manager", kind="", excluded={}, contexts=[], exec_path=""),
        "a shared-only chat": dict(
            agent=SHARED, sub="user-manager", name="max", role="editor", kind="", excluded={},
            contexts=None, exec_path="claude-code-cli"),
    }
    db_users.add_user_agent("user-manager", SHARED, "editor", "user-admin")
    got = {}
    for name, case in cases.items():
        vis = _wake_visibility(case["agent"], case["name"], case["role"], case["sub"])
        security = _security(case["agent"], case["sub"], case["role"])
        rig.rec.calls.clear()
        prompt = build(case["agent"], role=case["role"], vis=vis, where=placement.LOCAL_PLACEMENT,
                       exec_path=case["exec_path"], security=security, excluded=case["excluded"],
                       dynamic_contexts=case["contexts"], kind=case["kind"])
        got[name] = {"prompt": prompt, "asked": _asked(rig)}
    check_golden("delivery/wake-prompt", _norm(got))


def test_wake_superseded_and_the_slot(rig, monkeypatch):
    from core.events import input_queue, stream_pump
    from core.session import interactive_session, warmup_registry
    superseded = delivery._wake_superseded
    release = delivery._release_wake_slot
    got: dict = {"nothing live": superseded(SID, "c-wake")}

    def _terminal(**kw):
        return SimpleNamespace(alive=True, chat_id="", target="", created_at=1.0, **kw)

    with monkeypatch.context() as m:
        m.setitem(interactive_session._sessions, SID, _terminal())
        got["a live terminal under the id"] = superseded(SID, "c-wake")
        release(SID, "c-wake")
        got["its slot is kept"] = list(rig.released)
    with monkeypatch.context() as m:
        m.setitem(interactive_session._sessions, SID, SimpleNamespace(
            alive=False, chat_id="", target="", created_at=1.0))
        got["a dead terminal under the id"] = superseded(SID, "c-wake")
    with monkeypatch.context() as m:
        m.setitem(interactive_session._sessions, "s-other", SimpleNamespace(
            alive=True, chat_id="c-wake", target="", created_at=1.0))
        got["a live terminal on the chat"] = superseded(SID, "c-wake")
        got["and with no chat named"] = superseded(SID, "")
    asyncio.run(warmup_registry.register("c-warm", "user-viewer", AGENT))
    got["a warmup on the chat"] = superseded(SID, "c-warm")
    asyncio.run(warmup_registry.unregister("c-warm"))
    with monkeypatch.context() as m:
        m.setitem(stream_pump._active_pumps, "c-pump", SimpleNamespace(is_done=False))
        got["a turn streaming"] = superseded(SID, "c-pump")
        m.setitem(stream_pump._active_pumps, "c-pump", SimpleNamespace(is_done=True))
        got["a turn ended"] = superseded(SID, "c-pump")
    claim = input_queue.get("c-claim").try_claim()
    got["a starter holding the chat"] = superseded(SID, "c-claim")
    input_queue.get("c-claim").release(claim, rearm=False)
    got["no ids"] = superseded("", "")
    release(SID, "c-wake")
    got["a wake giving up releases"] = list(rig.released)
    check_golden("delivery/wake-superseded", got)


def test_role_after_wait(rig):
    role_after = delivery._role_after_wait
    db_users.add_user_agent("user-manager", SHARED, "editor", "user-admin")

    async def scene():
        return {
            "their row, unchanged": await role_after(AGENT, "user-viewer", "editor", "local", SID),
            "their row changed, the same place": await role_after(AGENT, "user-viewer", "manager", "local", SID),
            "their row changed, another place": await role_after(AGENT, "user-viewer", "manager", "m-gone", SID),
            "a pinned chat stays": await role_after(AGENT, "user-viewer", "manager", "m-gone", SID, pinned=True),
            "no longer holds it": await role_after(PEER, "user-viewer", "editor", "local", SID),
            "a shared-only chat at their standing": await role_after(
                SHARED, "user-manager", "editor", "local", SID, agent_state=True),
            "below the editor tier there": await role_after(
                SHARED, "user-viewer", "viewer", "local", SID, agent_state=True),
            "a platform admin with no row": await role_after(AGENT, "user-admin", "viewer", "local", SID),
        }

    check_golden("delivery/role-after-wait", asyncio.run(scene()))


def test_arm_chat_queue_and_wait_turn_open(rig, monkeypatch):
    from core.events import input_queue
    arm = _of("_rewarm_interactive_and_wake", "_arm_chat_queue")
    wait_open = _of("_rewarm_interactive_and_wake", "_wait_turn_open")
    armed: list = []
    held: dict = {"c-queued": [object()], "c-empty": []}
    monkeypatch.setattr(input_queue, "peek", lambda chat_id: held.get(chat_id, []))
    monkeypatch.setattr(input_queue, "arm", lambda chat_id, **kw: armed.append(chat_id))
    arm("c-queued")
    arm("c-empty")
    sleeps: list = []
    real_sleep = asyncio.sleep

    async def _sleep(seconds, *a, **k):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    class _Opens:
        alive = True

        def __init__(self, after):
            self.left = after

        @property
        def turn_open(self):
            self.left -= 1
            return self.left < 0

    async def scene():
        out = {}
        out["open at once"] = await wait_open(_Opens(0), 3.0)
        out["open on the second look"] = await wait_open(_Opens(1), 3.0)
        out["never open"] = await wait_open(SimpleNamespace(turn_open=False, alive=True), 2.0)
        out["a dead terminal"] = await wait_open(SimpleNamespace(turn_open=False, alive=False), 2.0)
        return out

    wait = asyncio.run(scene())
    check_golden("delivery/queue-and-turn-open", {"armed": armed, "wait": wait, "sleeps": sleeps})


# --- What the event says ------------------------------------------------------


def test_ending_fields_and_result_extras():
    ending = delivery._ending_fields
    extras = delivery._result_extras
    limit = turn_ending.TurnEnding(turn_ending.LIMIT, resets_at="2030-01-01T00:00:00Z", detail="five hours")
    file_row = {"name": "out.pdf", "path": "workspace/out.pdf"}
    check_golden("delivery/event-fields", {
        "ending": {
            "none": ending(None),
            "a limit": ending(limit),
            "declined": ending(turn_ending.TurnEnding(turn_ending.DECLINED, detail="cyber")),
        },
        "extras": {
            "nothing": extras(None, None, None),
            "empty lists": extras({}, [], []),
            "a verdict": extras({"check": "answer", "round": 2}, None, None),
            "files": extras(None, [file_row], None),
            "skipped files": extras(None, None, ["big.bin"]),
            "work left running": extras(None, None, None, background_pending=2),
            "no report": extras(None, None, None, report_missing=True),
            "all of it": extras({"check": "answer"}, [file_row], ["big.bin"], background_pending=1,
                                report_missing=True),
        },
    })


def test_store_undelivered_wakes(rig, monkeypatch):
    from core.events import stream_pump
    from core.session import interactive_session
    _chat("c-store", "user-viewer")
    _chat("c-live", "user-viewer")
    _chat("c-refused", "user-viewer")
    queued: list = []

    def _terminal(session_id, chat_id, answer):
        def queue_prompt(text, source, *, chat_id="", note=False):
            queued.append({"text": text, "source": source, "chat_id": chat_id, "note": note})
            return answer
        return SimpleNamespace(alive=True, session_id=session_id, chat_id=chat_id, payer="user-viewer",
                               target="", created_at=1.0, queue_prompt=queue_prompt)

    monkeypatch.setitem(interactive_session._sessions, "s-live", _terminal("s-live", "c-live", object()))
    monkeypatch.setitem(interactive_session._sessions, "s-refused", _terminal("s-refused", "c-refused", None))
    store = delivery._store_undelivered_wakes

    async def scene():
        return {
            "as a person": await store("c-store", ["ONE", "TWO"], person="user-viewer", role="editor"),
            "no person, by its creator": await store("c-store", ["THREE"], by="user-manager", role="viewer"),
            "an empty wake": await store("c-store", [""]),
            "no such chat": await store("c-none", ["FOUR"]),
            "nothing to store": await store("c-store", []),
            "a live terminal takes it": await store("c-live", ["FIVE"], person="user-viewer", role="editor"),
            "a terminal that refuses": await store("c-refused", ["SIX"], person="user-viewer", role="editor"),
        }

    answers = asyncio.run(scene())
    noted = sorted(c for c in ("c-store", "c-live", "c-refused", "c-none")
                   if c in stream_pump._stored_wake_chats)
    check_golden("delivery/undelivered-store", {
        "answers": answers, "queued": queued, "noted_for_the_pump_end": noted,
        "stored": {c: _pending(c) for c in ("c-store", "c-live", "c-refused")}})
    stream_pump._stored_wake_chats.difference_update({"c-store", "c-live", "c-refused"})


# --- Whole deliveries down the real ladder ------------------------------------


_TEMPLATE = ("[status={{status}} task={{task_id}} name={{task_name}} chat={{chat_id}} "
             "agent={{agent}}]\n{{output}}")


def _result_task(task_id: str, *, chat_id: str | None, session_id: str | None = None,
                 agent: str = AGENT, scope: str = "agent", created_by: str | None = None,
                 template: str = _TEMPLATE) -> TaskDefinition:
    """The task row a delegate's result is delivered from, and its definition."""
    task_store.create_dynamic_task(
        task_id, PEER, "the-worker", "do the work", "cli", "one_time", None, None, None, 600,
        created_by, on_complete_agent=agent, on_complete_prompt=template,
        on_complete_session_id=session_id, on_complete_chat_id=chat_id, scope=scope)
    return TaskDefinition(id=task_id, name="the-worker", agent=PEER, prompt="do the work",
                          scope=scope, created_by=created_by)


def test_a_result_with_nowhere_to_go(rig):
    bare = TaskDefinition(id="t-bare", name="w", agent=PEER, prompt="p")
    no_session = _result_task("t-nosess", chat_id=None, session_id=None)
    no_prompt = TaskDefinition(id="t-noprompt", name="w", agent=PEER, prompt="p", on_complete_agent=AGENT)

    async def scene():
        await delivery._deliver_task_result(bare, "completed", "out")
        await delivery._deliver_task_result(no_session, "completed", "out")
        await delivery._deliver_task_result(no_prompt, "completed", "out")
        tasks = background.running("delivery")
        return len(tasks)

    check_golden("delivery/result-no-route", {"spawned": asyncio.run(scene()), "seen": _seen(rig)})


def test_a_result_to_a_live_idle_session(rig):
    rig.layer = _Layer(alive=True)
    _chat("c-alive", "user-viewer", session_id="s-alive")
    task = _result_task("t-alive", chat_id="c-alive", session_id="s-stale", created_by="user-viewer")
    by_chat = _result_task("t-by-chat", chat_id="c-alive", created_by="user-viewer")

    async def scene():
        await delivery._deliver_task_result(task, "completed", "the worker's answer")
        await _delivered()
        await delivery._deliver_task_result(by_chat, "user_interrupted", "the chat's own session")
        await _delivered()

    asyncio.run(scene())
    check_golden("delivery/result-persistent", _norm({
        "seen": _seen(rig), "rows": _rows("c-alive"), "pending": _pending("c-alive")}))


def test_a_result_to_a_dead_session_spawns_the_wake(rig):
    rig.layer = _Layer(alive=False)
    _chat("c-dead", "user-viewer", session_id=SID, model="claude-char-1")
    task = _result_task("t-dead", chat_id="c-dead", session_id=SID, created_by="user-viewer")

    async def scene():
        await delivery._deliver_task_result(task, "completed", "the worker's answer")
        await _delivered()

    asyncio.run(scene())
    check_golden("delivery/result-oneshot", _norm({
        "config": rig.layer.configs, "collaborators": _asked(rig), "seen": _seen(rig),
        "rows": _rows("c-dead"), "pending": _pending("c-dead")}))


def test_a_result_no_rung_takes_is_stored(rig):
    _chat("c-none", "user-viewer", session_id="s-chat-now", execution_target="local")
    task = _result_task("t-none", chat_id="c-none", session_id="s-none", created_by="user-viewer")
    ending = turn_ending.TurnEnding(turn_ending.EXITED, detail="exit 1", exit_code=1)
    got: dict = {}

    def revoke():
        with get_conn() as conn:
            conn.execute("DELETE FROM user_agents WHERE sub='user-viewer' AND agent=%s", (AGENT,))
            conn.commit()

    async def deliver(name, layer, chat):
        rig.layer = layer
        await delivery._do_deliver("s-none", AGENT, f"RESULT {name}", task, chat_id=chat,
                                   output_text="out", status="failed")
        got[name] = {"seen": _seen(rig), "rows": _rows(chat), "pending": _pending(chat)}

    class _Raises(_Layer):
        async def is_session_alive(self, sid):
            raise OSError("the layer broke")

    async def scene():
        rig.layer = _Layer(resume=ResumeVerdict.FRESH)
        await delivery._deliver_task_result(task, "failed", "half an answer", ending=ending,
                                            background_pending=2)
        await _delivered()
        got["the session moved on, no conversation"] = {
            "seen": _seen(rig), "rows": _rows("c-none"), "pending": _pending("c-none")}
        _chat("c-raise", "user-viewer", session_id="s-raise")
        await deliver("a rung raises", _Raises(), "c-raise")
        _chat("c-revoked", "user-viewer", session_id="s-rev")
        await deliver("the person left during the ladder", _Layer(
            on_alive=revoke, resume=ResumeVerdict.FRESH), "c-revoked")

    asyncio.run(scene())
    check_golden("delivery/result-stored", _norm(got))


def test_a_result_for_a_person_who_left_is_kept_as_an_event(rig):
    _chat("c-left", "user-viewer2", session_id="s-left")
    task = _result_task("t-left", chat_id="c-left", session_id="s-left", scope="user",
                        created_by="user-viewer2")
    by_session = TaskDefinition(id="t-left2", name="w", agent=PEER, prompt="p", scope="user",
                                created_by="user-viewer2")

    async def scene():
        await delivery._deliver_task_result(task, "completed", "for nobody")
        await _delivered()
        await delivery._do_deliver("s-left", AGENT, "RESULT", by_session, output_text="by session")
        await delivery._do_deliver("s-unknown", AGENT, "RESULT", by_session, output_text="lost")

    asyncio.run(scene())
    check_golden("delivery/result-refused", _norm({
        "seen": _seen(rig), "rows": _rows("c-left"), "pending": _pending("c-left")}))


@pytest.mark.parametrize("session_id", ["s-quiet", None])
def test_a_result_that_wakes_nobody_writes_its_row_and_runs_no_rung(rig, session_id):
    """A cut round whose lane goes on with another: its result is written on
    the delegating chat, whether or not that chat has a session, and nothing
    is woken, started or stored to replay."""
    from core.events import turn_ending
    _chat("c-quiet", "user-viewer", **({"session_id": session_id} if session_id else {}))
    task = _result_task("t-quiet", chat_id="c-quiet", session_id=session_id, created_by="user-viewer")
    ending = turn_ending.TurnEnding(reason=turn_ending.RESTARTED)

    async def scene():
        await delivery._deliver_task_result(task, "cancelled", "cut, the lane goes on", ending=ending,
                                            lane_chat_id="c-lane", wake=False)
        await _delivered()

    asyncio.run(scene())
    seen = _seen(rig)
    assert seen["layer"] == [] and seen["layers_asked"] == [] and seen["admission"] == []
    rows = [r for r in task_store.get_chat_messages("c-quiet") if r.get("event_type") == "delegate_result"]
    assert len(rows) == 1 and _pending("c-quiet") == []
    # Cancelled, so the bubble reads "delegate canceled", never a missing report.
    event = json.loads(rows[0]["event_data"])
    assert event["status"] == "cancelled" and not event.get("report_missing")


def test_a_result_over_the_dashboard_socket(rig, monkeypatch):
    from core.layers.direct import session as direct_session
    from core.session.session_state import _dashboard_notify_queues
    _chat("c-ws", "user-viewer", session_id="s-ws")
    task = _result_task("t-ws", chat_id="c-ws", session_id="s-ws", created_by="user-viewer")
    queue: asyncio.Queue = asyncio.Queue()
    # The socket rung serves a live session: one the platform holds itself.
    monkeypatch.setitem(direct_session._direct_sessions, "s-ws", SimpleNamespace(mcp_manager=None))
    monkeypatch.setitem(_dashboard_notify_queues, "s-ws", queue)

    async def scene():
        await delivery._deliver_task_result(task, "completed", "the worker's answer")
        await _delivered()
        return queue.get_nowait()

    payload = asyncio.run(scene())
    check_golden("delivery/result-socket", _norm({
        "payload": payload, "seen": _seen(rig), "rows": _rows("c-ws"), "pending": _pending("c-ws")}))


def test_a_result_with_no_chat_saves_its_echo_as_pending(rig, monkeypatch):
    from core.session import session_state
    saved: list = []
    monkeypatch.setattr(session_state, "_save_pending_result",
                        lambda sid, text, *a, **k: saved.append([sid, text]))
    rig.layer = _Layer(alive=True)
    task = TaskDefinition(id="t-pend", name="w", agent=PEER, prompt="p", scope="agent")
    asyncio.run(delivery._do_deliver("s-pend", AGENT, "RESULT", task, output_text="out"))
    check_golden("delivery/result-pending", _norm({"saved": saved, "seen": _seen(rig)}))


def test_a_lane_is_finalized_before_its_result(rig):
    """The worker's chat is collected once more, its abort flag read, the
    template filled and the notes the run earned appended."""
    from storage.checks import db_checks
    _chat("c-parent", "user-viewer", session_id="s-parent")
    _chat("c-worker", "user-viewer", PEER)
    cursor = task_store.get_last_chat_message_id("c-worker")
    own = task_store.add_chat_message("c-worker", "user", "do the work")
    task_store.add_chat_message("c-worker", "assistant", "the answer")
    task_store.add_chat_message("c-worker", "user", "also check Y")
    turn_end = task_store.get_last_chat_message_id("c-worker")
    db_checks.insert_verdict(
        agent=PEER, owner="", check_name="answer", section="schema", status="fail", passed=False,
        score=None, findings=[], summary="no json", reason="", session_id="s", chat_id="c-worker",
        run_id="", judge_run_id="", user_sub="user-viewer", round_no=3, ran_on="local", engine="",
        model="", cost_usd=0.0, duration_ms=1, script_sha256="")
    task = _result_task("t-lane", chat_id="c-parent", session_id="s-parent", created_by="user-viewer")
    rig.layer = _Layer(alive=True)
    calls: list = []

    async def scene():
        await delivery._deliver_task_result(
            task, "failed", "the run's own text", worker_chat_id="c-worker", output_cursor=cursor,
            prompt_row_id=own, prompt_text="do the work", run_started_at="2000-01-01T00:00:00+00:00",
            ending=turn_ending.TurnEnding(turn_ending.SILENT, detail="quiet"),
            background_pending=1, cut_short=turn_ending.SILENT, turn_end_cursor=turn_end)
        await _delivered()
        calls.extend(rig.layer.calls)

    asyncio.run(scene())
    check_golden("delivery/result-lane", _norm({
        "layer": calls, "rows": _rows("c-parent"), "worker_rows": _rows("c-worker")}))


def test_the_one_shot_rung_and_its_gates(rig):
    _chat("c-gate", "user-viewer", session_id=SID, execution_target="local")
    _chat("c-away", "user-viewer", session_id=SID, execution_target=_machine("m-away"))
    _chat("c-new", "user-viewer", session_id="")
    from services.engines import subscription_pool
    steps: dict = {}

    async def rung(name, layer, *, reserve="reserved", chat="c-gate", sub="user-viewer", role="editor", **kw):
        rig.layer, rig.reserve = layer, reserve
        answer = await delivery._deliver_via_oneshot(kw.pop("sid", SID), AGENT, "WAKE", user_sub=sub,
                                                     role=role, chat_id=chat, **{**WAKE, **kw})
        steps[name] = {"answer": answer, "configs": len(layer.configs), **_seen(rig)}

    async def scene():
        await rung("the machine it is pinned to is away", _Layer(), chat="c-away")
        await rung("the machine did not answer the probe", _Layer(resume=ResumeVerdict.UNKNOWN), chat="c-gate")
        await rung("no conversation, not this chat's session", _Layer(resume=ResumeVerdict.FRESH),
                   chat="c-gate", sid="s-other")
        await rung("given way to a person", _Layer(), reserve="superseded")
        await rung("waited out its turn", _Layer(), reserve="timeout")
        await rung("a machine target runs no budget", _Layer(), reserve="untracked")
        await rung("the person left while it waited", _Layer(), sub="user-viewer2", chat="c-gate")
        rig.monkeypatch.setattr(subscription_pool, "resolve_subscription_env",
                                lambda *a, **k: ("", {}))
        await rung("no subscription for the person", _Layer())

        def _none(*a, **k):
            raise subscription_pool.NoSubscriptionError("nothing usable")

        rig.monkeypatch.setattr(subscription_pool, "resolve_subscription_env", _none)
        await rung("no credentials at all", _Layer())
        await rung("and no slot of its own to give back", _Layer(), reserve="untracked")

    asyncio.run(scene())
    check_golden("delivery/oneshot-gates", _norm(steps))


def test_the_spawned_config_by_place_and_engine(rig):
    """The session a wake starts, for each place and engine a chat can be on."""
    _machine("m-admin", online=True, rig=rig)
    _machine("m-vic", "user-viewer", "user", online=True, rig=rig)
    db_users.add_user_agent("user-manager", SHARED, "editor", "user-admin")
    _chat("c-codex", "user-viewer", session_id=SID, execution_path="codex-cli", codex_thread_id="thread-9")
    _chat("c-admin", "user-viewer", session_id=SID, execution_target="m-admin")
    _chat("c-vic", "user-viewer", session_id=SID, execution_target="m-vic")
    _chat("c-so", shared_chat_owner(SHARED), SHARED, session_id=SID)
    _chat(TASK_CHAT, task_chat_owner(AGENT), session_id=SID)
    _chat("c-moved", "user-viewer", session_id="")
    got: dict = {}

    async def wake(name, agent, chat, sub, role, sid=SID, **kw):
        rig.layer = _Layer()
        answer = await delivery._deliver_via_oneshot(sid, agent, "WAKE", user_sub=sub, role=role,
                                                     chat_id=chat, **{**WAKE, **kw})
        got[name] = {"answer": answer, "config": rig.layer.configs, "asked": _asked(rig), **_seen(rig)}

    async def scene():
        await wake("a chat on the other engine, its thread kept", AGENT, "c-codex", "user-viewer", "editor")
        await wake("an admin-paired machine", AGENT, "c-admin", "user-viewer", "editor")
        await wake("the person's own machine", AGENT, "c-vic", "user-viewer", "editor")
        await wake("a shared-only chat as the person", SHARED, "c-so", "user-manager", "editor")
        await wake("a task run's chat as nobody", AGENT, TASK_CHAT, None, "manager")
        await wake("the resume after the agent's own move", AGENT, "c-moved", "user-viewer", "editor", sid="",
                   client_type="dashboard", permission_mode="default", fresh_without_session=True)

    asyncio.run(scene())
    check_golden("delivery/oneshot-configs", _norm(got))


def test_the_spawned_config_by_builder(rig):
    """One chat, started by each builder: as the person's own warm-up builds
    it and as the delivery builds it, for a member and for a platform admin
    with no row on the agent, and the person a multi-provider engine's
    session names for its next provider."""
    _chat("c-own", "user-viewer", session_id=SID)
    _chat("c-boss", "user-admin", session_id=SID)
    _chat("c-direct", "user-viewer", session_id=SID, execution_path="direct-llm")
    got: dict = {}

    async def start(name, chat, sub, role, **kw):
        rig.layer = _Layer()
        answer = await delivery._deliver_via_oneshot(
            SID, AGENT, "WAKE", user_sub=sub, role=role, chat_id=chat,
            **{**WAKE, "client_type": "dashboard", "permission_mode": "ask", **kw})
        got[name] = {"answer": answer, "config": rig.layer.configs, "asked": _asked(rig), **_seen(rig)}

    async def scene():
        await start("a member, as the chat builds it", "c-own", "user-viewer", "editor", build="chat")
        await start("a member, as the delivery builds it", "c-own", "user-viewer", "editor")
        await start("an admin with no row, as the chat builds it", "c-boss", "user-admin", "manager",
                    build="chat")
        await start("an admin with no row, as the delivery builds it", "c-boss", "user-admin", "manager")
        await start("the multi-provider engine names its person", "c-direct", "user-viewer", "editor")

    asyncio.run(scene())
    check_golden("delivery/oneshot-builders", _norm(got))


def test_the_persistent_rung(rig, monkeypatch):
    from core.session import session_state
    from services.remote import chat_move
    _chat("c-rung", "user-viewer", session_id="s-rung")
    monkeypatch.setitem(session_state._sessions, "s-tracked", {"last_active": "old"})
    got: dict = {}

    async def rung(name, layer, sid="s-rung", chat="c-rung", sub="user-viewer", role="editor"):
        rig.layer = layer
        answer = await delivery._deliver_via_persistent(sid, AGENT, "WAKE", user_sub=sub, role=role, chat_id=chat)
        got[name] = {"answer": answer, "layer": _seen(rig)["layer"]}

    async def scene():
        await rung("the session is dead", _Layer())
        await rung("alive, an echo through the pump", _Layer(alive=True))
        await rung("alive with no chat, the text comes back", _Layer(alive=True), chat="", sub=None, role="manager")
        await rung("its last-active moves", _Layer(alive=True), sid="s-tracked", chat="", sub=None)
        got["last_active"] = session_state._sessions["s-tracked"]["last_active"]
        await rung("a pipe that breaks closes it", _Layer(alive=True, send_raises=True), chat="", sub=None)
        monkeypatch.setattr(chat_move, "lock_sentence", lambda row, sub: "It runs on Ann's computer.")
        rig.layers_asked.clear()
        await rung("locked on a colleague's computer: no layer asked", _Layer(alive=True))
        got["layers_asked_when_locked"] = rig.layers_asked

    asyncio.run(scene())
    check_golden("delivery/persistent-rung", _norm(got))


def test_a_fresh_start_when_the_conversation_is_gone(rig):
    rig.layer = _Layer(resume=ResumeVerdict.FRESH)
    _chat("c-fresh", "user-viewer", session_id=SID, execution_target="local")
    answer = asyncio.run(delivery._deliver_via_oneshot(
        SID, AGENT, "WAKE", user_sub="user-viewer", role="editor", chat_id="c-fresh", **WAKE))
    configs = [{"resume": c.resume, "resume_handle": c.resume_handle} for c in rig.layer.configs]
    check_golden("delivery/oneshot-fresh", _norm({
        "answer": answer, "configs": configs, "seen": _seen(rig),
        "chat_session": task_store.get_chat("c-fresh")["session_id"], "rows": _rows("c-fresh")}))


class _Terminal:
    """The terminal a re-warm starts: its turn opens and completes on submit
    unless told otherwise."""

    def __init__(self, chat_id, *, opens=True, refuses=False, viewer=False):
        self.alive, self.has_viewer, self.turn_open = True, viewer, False
        self.chat_id, self.opens, self.refuses = chat_id, opens, refuses
        self.target, self.created_at, self.on_turn_complete = "", 1.0, None
        self.submitted: list = []

    def submit_prompt(self, text, *, settle=False):
        self.submitted.append([text, settle])
        if self.refuses:
            return False
        if self.opens:
            self.turn_open = True
            self.on_turn_complete("done")
        return True


def test_an_interactive_chat_is_rewarmed(rig, monkeypatch):
    from core.session import (
        interactive_session,
        transcript_tool_events,
        warmup_registry,
    )
    closed: list = []
    real_sleep = asyncio.sleep

    async def _sleep(seconds, *a, **k):
        await real_sleep(0)

    async def _close(sid, *, reason=""):
        closed.append(reason)

    monkeypatch.setattr(interactive_session, "close_session", _close)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    steps: dict = {}
    _chat("c-pty", "user-viewer", session_id=SID, execution_mode="interactive", tui_theme="dark")

    def terminal_for(rig_layer, terminal):
        def _start(sid, cfg):
            if terminal is not None:
                monkeypatch.setitem(interactive_session._sessions, sid, terminal)
        rig_layer.on_start = _start

    async def rewarm(name, terminal, *, start_raises=False):
        layer = _Layer()
        terminal_for(layer, terminal)
        if start_raises:
            def _boom(sid, cfg):
                raise RuntimeError("spawn failed")
            layer.on_start = _boom
        rig.layer = layer
        answer = await delivery._deliver_via_oneshot(
            SID, AGENT, "WAKE", user_sub="user-viewer", role="editor", chat_id="c-pty", **WAKE)
        interactive_session._sessions.pop(SID, None)
        cfg = layer.configs[0] if layer.configs else None
        steps[name] = {
            "answer": answer, "closed": list(closed),
            "submitted": terminal.submitted if terminal else None,
            "config": cfg and {"interactive": cfg.interactive, "chat_id": cfg.chat_id,
                               "theme": cfg.interactive_theme, "resume": cfg.resume,
                               "mode": cfg.permission_mode},
            "registered": warmup_registry.get("c-pty") is not None,
            "note_left": transcript_tool_events.consume_sent_prompt("c-pty", "WAKE"),
            **_seen(rig)}
        closed.clear()

    async def scene():
        await rewarm("the turn opens and completes", _Terminal("c-pty"))
        await rewarm("a viewer keeps it", _Terminal("c-pty", viewer=True))
        await rewarm("the paste never submits", _Terminal("c-pty", opens=False))
        await rewarm("the terminal refuses the wake", _Terminal("c-pty", refuses=True))
        await rewarm("no terminal registers", None)
        await rewarm("the spawn fails", None, start_raises=True)

    asyncio.run(scene())
    check_golden("delivery/rewarm", _norm(steps))


# --- The sweep ----------------------------------------------------------------


def test_sweep_identity(rig):
    identity = _of("_redeliver_group", "_sweep_identity")
    db_users.add_user_agent("user-manager", SHARED, "editor", "user-admin")
    own = {"id": "c1", "agent": AGENT, "user_sub": "user-viewer"}
    left = {"id": "c2", "agent": PEER, "user_sub": "user-viewer"}
    shared = {"id": "c3", "agent": SHARED, "user_sub": shared_chat_owner(SHARED)}
    task = {"id": "c4", "agent": AGENT, "user_sub": task_chat_owner(AGENT)}
    phone = {"id": "c5", "agent": AGENT, "user_sub": "phone"}
    check_golden("delivery/sweep-identity", {name: identity(*args) for name, args in {
        "a person's own chat": (own, "", "", ""),
        "a person's chat whose owner left": (left, "", "", ""),
        "a shared chat as the person": (shared, "user-manager", "", ""),
        "a shared chat below the editor tier": (shared, "user-viewer", "", ""),
        "a task chat as a stored person": (task, "user-viewer", "", ""),
        "a task chat, the creator's role read again": (task, "", "viewer", "user-viewer"),
        "by the agent itself": (task, "", "editor", AGENT),
        "a stored role": (task, "", "editor", ""),
        "a task chat with nothing stored": (task, "", "", ""),
        "a shared chat with nothing stored": (shared, "", "", ""),
        "a phone chat with nothing stored": (phone, "", "", ""),
        "no owner": ({"id": "c6", "agent": AGENT}, "", "", ""),
    }.items()})


def test_redeliver_pending_wakes(rig):
    from services.scheduler import run_recovery
    rig.layer = _Layer(alive=True)
    append = task_store.append_pending_delegate_wake
    _chat("c-own", "user-viewer", session_id="s-own")
    append("c-own", "OWN ONE", person="user-viewer", role="editor")
    append("c-own", "OWN TWO", person="user-manager", role="viewer")
    _chat("c-note", "user-viewer", session_id="s-note")
    append("c-note", "A NOTE", note=True)
    _chat("c-gone", "user-viewer", PEER, session_id="s-gone")
    append("c-gone", "FOR THE DEPARTED")
    append("c-gone", "A DEPARTED NOTE", note=True)
    _chat("c-parked", "user-viewer", session_id="s-parked")
    append("c-parked", "PARKED")
    db_users.add_user_agent("user-manager", SHARED, "editor", "user-admin")
    db_users.add_user_agent("user-viewer2", SHARED, "editor", "user-admin")
    _chat("c-shared", shared_chat_owner(SHARED), SHARED, session_id="s-shared")
    append("c-shared", "FOR MAX", person="user-manager", role="editor")
    append("c-shared", "FOR VICTORIA", person="user-viewer2", role="editor")
    append("c-shared", "FOR THE VIEWER", person="user-viewer", role="viewer")
    _chat(TASK_CHAT, task_chat_owner(AGENT), session_id="s-task")
    append(TASK_CHAT, "AGENT SCOPE")
    rig.monkeypatch.setitem(run_recovery._parked, "s-parked", {"chat_id": "c-parked"})
    chats = ("c-own", "c-note", "c-gone", "c-parked", "c-shared", TASK_CHAT)

    woken = asyncio.run(delivery.redeliver_pending_wakes())
    seen = _seen(rig)
    seen["layer"] = sorted(seen["layer"], key=json.dumps)
    # (normalized for the key: a layer asked with a compute placement carries the object)
    seen["layers_asked"] = sorted(seen["layers_asked"],
                                  key=lambda r: json.dumps(normalize(r), sort_keys=True))
    check_golden("delivery/redeliver", _norm({
        "woken": woken, "seen": seen, "pending": {c: _pending(c) for c in chats},
        "rows": {c: _rows(c) for c in chats}}))


def test_a_failing_sweep_keeps_its_wakes(rig):
    rig.layer = _Layer(alive=False, resume=ResumeVerdict.UNKNOWN)
    append = task_store.append_pending_delegate_wake
    _chat("c-keep", "user-viewer", session_id="s-keep")
    append("c-keep", "KEEP ONE")
    append("c-keep", "KEEP TWO")
    _chat("c-so", shared_chat_owner(SHARED), SHARED, session_id="s-so")
    append("c-so", "NO PERSON BELOW EDITOR", role="viewer")
    _chat("c-task", task_chat_owner(AGENT), session_id="s-task2")
    append("c-task", "DROPPED: NOBODY BELOW EDITOR", role="viewer")
    _chat("c-pinned", "user-viewer", session_id="s-pinned", execution_target=_machine("m-pin"))
    append("c-pinned", "FOR THAT MACHINE")
    _chat("c-other", "user-viewer", session_id="s-other2", execution_target=_machine("m-other"))
    append("c-other", "NOT THIS SWEEP")
    chats = ("c-keep", "c-so", "c-task", "c-pinned", "c-other")
    got: dict = {}

    def broken(*a, **k):
        raise RuntimeError("a broken spawn")

    async def sweep(name, *args):
        woken = await delivery.redeliver_pending_wakes(*args)
        seen = _seen(rig)
        got[name] = {"woken": woken, "layer": sorted(seen["layer"], key=json.dumps),
                     "pending": {c: _pending(c) for c in chats}}

    async def scene():
        await sweep("all of them")
        await sweep("one machine's", "m-pin")
        await sweep("a machine that runs one of the sessions", "m-none", ("s-keep",))
        rig.monkeypatch.setattr("core.session.session_manager.get_execution_layer", broken)
        await sweep("a rung that raises keeps them", "m-none", ("s-keep",))

    asyncio.run(scene())
    check_golden("delivery/redeliver-kept", _norm(got))
