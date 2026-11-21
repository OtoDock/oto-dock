"""The one rebuild of a chat's session (``core/session/chat_rebuild.py``):
nothing that decides what a session may do is defaulted, the one closer
closes each kind of live session where it runs, the hold a closing rebuild
takes, and the ladder gets exactly what the caller named.
"""
import ast
import asyncio
import inspect
import pathlib
import uuid

import pytest

from core.permission_modes import UNATTENDED_MODE
from core.session import chat_rebuild, session_delivery
from services.scheduler import delivery, wake_session
from tests.fixtures.ws_dashboard_harness import (
    TEST_MODEL,
    FakeExecutionLayer,
    make_test_agent,
    stub_dashboard_seams,
)

PROXY = pathlib.Path(__file__).resolve().parents[2]


def _required(fn, *names) -> bool:
    params = inspect.signature(fn).parameters
    return all(params[n].default is inspect.Parameter.empty for n in names)


def test_nothing_that_shapes_a_session_has_a_default():
    assert _required(chat_rebuild.rebuild_and_resume, "source", "person", "role", "mode", "kind", "build")
    assert _required(session_delivery.deliver_prompt, "role")
    assert _required(delivery._deliver_via_persistent, "role")
    assert _required(delivery._deliver_via_oneshot, "role", "permission_mode", "client_type", "build")
    assert _required(wake_session._spawn_wake_session, "role", "permission_mode", "client_type", "build")
    assert _required(chat_rebuild.close_chat_session, "reason", "require_idle", "lock")


def _rebuild_calls(rel: str) -> list[dict[str, str]]:
    """The keywords of every ``rebuild_and_resume`` call in a file, as
    source, in the file's order."""
    calls = [node for node in ast.walk(ast.parse((PROXY / rel).read_text()))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "rebuild_and_resume"]
    return [{k.arg: ast.unparse(k.value) for k in node.keywords if k.arg}
            for node in sorted(calls, key=lambda n: n.lineno)]


def _decided_before(rel: str) -> bool:
    """Whether a file calls ``chat_rebuild.wake_build`` (the one decision)."""
    return "chat_rebuild.wake_build" in (PROXY / rel).read_text()


def test_a_wake_names_what_the_one_decision_gives():
    """A delegate result, a stored wake's sweep and a continuation name the
    builder, the mode and the kind ``chat_rebuild.wake_build`` gives: a
    person's own chat as the chat, any other as nobody's."""
    wakes = {"services/scheduler/delivery.py": 2, "services/scheduler/firing.py": 1}
    for rel, count in wakes.items():
        calls = _rebuild_calls(rel)
        assert len(calls) == count, rel
        assert _decided_before(rel)
        for kw in calls:
            assert (kw["mode"], kw["kind"], kw["build"]) == (
                "woken.mode", "woken.kind", "woken.build"), rel
    # The sweep and the continuation start nothing below the editor tier.
    assert _rebuild_calls("services/scheduler/delivery.py")[1]["spawns"] == "spawns"
    assert _rebuild_calls("services/scheduler/firing.py")[0]["spawns"] == "spawns"


def test_the_moves_resume_takes_the_builder_the_decision_gives():
    """It keeps its own mode and kind; the builder is the decision's, so a
    person's own chat is built as their warm-up builds it, and a task run's
    chat (whose run may have no person) as before."""
    (kw,) = _rebuild_calls("services/remote/chat_move.py")
    assert kw["build"] == "build" and _decided_before("services/remote/chat_move.py")


def test_a_keys_turn_always_builds_the_chat():
    """A person's message sent with a key is their typed turn: it runs on the
    session their own warm-up builds in every chat it reaches, never on the
    wake's decision, which is for turns nobody typed."""
    (kw,) = _rebuild_calls("api/public/key_turns.py")
    assert kw["build"] == "chat_rebuild.AS_CHAT" and not _decided_before("api/public/key_turns.py")


def _run(coro):
    return asyncio.run(coro)


def test_an_unknown_build_and_a_chat_build_with_no_person_are_refused():
    base = {"source": "t", "role": "manager", "mode": UNATTENDED_MODE, "kind": ""}
    with pytest.raises(ValueError):
        _run(chat_rebuild.rebuild_and_resume("c", "x", person="u", build=chat_rebuild.AS_WAKE,
                                             **{**base, "mode": chat_rebuild.CHAT_OWN_MODE}))
    with pytest.raises(ValueError):
        _run(chat_rebuild.rebuild_and_resume("c", "x", person="u", build="other", **base))
    with pytest.raises(ValueError):
        _run(chat_rebuild.rebuild_and_resume("c", "x", person=None, build=chat_rebuild.AS_CHAT, **base))
    with pytest.raises(ValueError):
        _run(chat_rebuild.rebuild_and_resume("c", "x", person="u", build=chat_rebuild.AS_WAKE,
                                             **{**base, "role": ""}))


def test_the_ladder_gets_what_the_caller_named(monkeypatch):
    calls: list = []

    async def _ladder(chat_id, text, **kw):
        calls.append((chat_id, text, kw))
        return session_delivery.DeliveryOutcome("none", chat_id=chat_id)

    monkeypatch.setattr(session_delivery, "deliver_prompt", _ladder)
    hook = object()
    _run(chat_rebuild.rebuild_and_resume(
        "c-1", "the words", source="reload", person="u-1", role="editor", mode="ask",
        kind="dashboard", build=chat_rebuild.AS_CHAT, session_id="s-1", agent="a",
        fresh_without_session=True, persist_event=hook, allow_ws=False, allow_pty=False,
        allow_live_turn=False))
    _run(chat_rebuild.rebuild_and_resume(
        "c-2", "a wake", source="schedule_wake", person=None, role="viewer",
        mode=UNATTENDED_MODE, kind="", build=chat_rebuild.AS_WAKE, spawns=False))
    (cid, text, kw), (_c2, _t2, kw2) = calls
    assert (cid, text, kw["source"], kw["user_sub"], kw["role"]) == (
        "c-1", "the words", "reload", "u-1", "editor")
    # Both rungs are told what started the turn.
    assert kw["persistent_fn"].func is delivery._deliver_via_persistent
    assert kw["persistent_fn"].keywords == {"driven_by": kw["source"]}
    assert kw["oneshot_fn"].func is delivery._deliver_via_oneshot
    assert kw["oneshot_fn"].keywords == {
        "permission_mode": "ask", "client_type": "dashboard", "build": "chat",
        "fresh_without_session": True, "driven_by": "reload"}
    assert kw["persist_event"] is hook
    assert (kw["allow_ws"], kw["allow_pty"], kw["allow_live_turn"]) == (False, False, False)
    # A wake that may start nothing gets no rung that starts.
    assert kw2["oneshot_fn"] is None and kw2["user_sub"] is None and kw2["role"] == "viewer"
    assert (kw2["allow_ws"], kw2["allow_pty"], kw2["allow_live_turn"]) == (True, True, True)


def test_a_rungs_error_reaches_the_caller(monkeypatch):
    async def _raises(*a, **k):
        raise RuntimeError("the spawn's guard")

    monkeypatch.setattr(session_delivery, "deliver_prompt", _raises)
    with pytest.raises(RuntimeError):
        _run(chat_rebuild.rebuild_and_resume(
            "c", "x", source="t", person=None, role="manager", mode=UNATTENDED_MODE, kind="",
            build=chat_rebuild.AS_WAKE))


# -- the closer ---------------------------------------------------------------

class _Terminal:
    def __init__(self, chat_id, session_id, target="local"):
        self.chat_id, self.session_id = chat_id, session_id
        self.alive, self.turn_open, self.created_at = True, False, 0.0
        self.target, self.is_remote = target, target != "local"
        self.closed: list[str] = []

    async def close(self, *, reason="closed"):
        self.alive = False
        self.closed.append(reason)


def _layer(monkeypatch):
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    seen = {"locked": []}
    real = layer.close_session

    async def _close(sid):
        seen["locked"].append(layer.session_lock(sid).locked())
        await real(sid)

    monkeypatch.setattr(layer, "close_session", _close)
    return layer, seen


def _close(chat, **kw):
    kw = {"reason": "test", "require_idle": False, "lock": False, **kw}
    return _run(chat_rebuild.close_chat_session(chat, **kw))


def test_the_closer_closes_a_terminal_through_its_own_close(temp_db, monkeypatch):
    from core.session import interactive_session
    layer, _seen = _layer(monkeypatch)
    term = _Terminal("c-term", "s-term")
    monkeypatch.setitem(interactive_session._sessions, "s-term", term)
    assert _close({"id": "c-term", "session_id": "s-term"}, reason="reload") is True
    assert term.closed == ["reload"] and layer.closed_sessions == []


def test_the_closer_waits_for_no_machine(temp_db, monkeypatch):
    """A terminal on a machine is closed by the token of its open, so a
    terminal started again under the id never takes the old one's exit: the
    close returns when the terminal's own close did, on a machine that is
    connected or away alike."""
    from core.session import interactive_session
    _layer(monkeypatch)
    _places(monkeypatch, None, "m-office")
    far = _Terminal("c-far", "s-far", "m-office")
    away = _Terminal("c-away", "s-away", "m-home")
    monkeypatch.setitem(interactive_session._sessions, "s-far", far)
    monkeypatch.setitem(interactive_session._sessions, "s-near", _Terminal("c-near", "s-near"))
    monkeypatch.setitem(interactive_session._sessions, "s-away", away)
    assert _close({"id": "c-far", "session_id": "s-far"}, reason="reload") is True
    assert _close({"id": "c-near", "session_id": "s-near"}, reason="reload") is True
    assert _close({"id": "c-away", "session_id": "s-away"}, reason="reload") is True
    assert far.closed == ["reload"] and away.closed == ["reload"]


def test_the_closer_closes_a_headless_session_through_its_holder(temp_db, monkeypatch):
    layer, seen = _layer(monkeypatch)
    layer.alive.update({"s-a", "s-b"})
    assert _close({"id": "c", "session_id": "s-a"}) is True
    assert _close({"id": "c", "session_id": "s-b"}, lock=True) is True
    assert layer.closed_sessions == ["s-a", "s-b"] and seen["locked"] == [False, True]


def test_the_closer_leaves_what_nobody_holds_unless_given_a_layer(temp_db, monkeypatch):
    layer, _seen = _layer(monkeypatch)
    assert _close({"id": "c", "session_id": "s-dead"}) is False
    assert _close({"id": "c", "session_id": ""}, fallback_layer=layer) is False
    assert layer.closed_sessions == []
    # The plan's fresh start frees what a dead id still holds: the close
    # runs, and it ended nothing live.
    assert _close({"id": "c", "session_id": "s-dead"}, fallback_layer=layer) is False
    assert layer.closed_sessions == ["s-dead"]


class _Remote(FakeExecutionLayer):
    """The remote layer: the sessions it holds, each on its own machine."""

    def __init__(self, **machines):
        super().__init__()
        self.machines = machines
        self.alive.update(machines)

    async def get_session(self, sid):
        from types import SimpleNamespace
        return SimpleNamespace(machine_id=self.machines[sid]) if sid in self.machines else None

    def machine_of_session(self, sid):
        return self.machines.get(sid, "")

    def is_session_grace_held(self, sid):
        return False


def _places(monkeypatch, remote, *online):
    from core.remote import satellite_connection
    from core.session import session_manager
    up = set(online)
    from core.remote import satellite_link
    manager = type("M", (), {"is_connected": staticmethod(up.__contains__),
                             "compute_machine_ids": staticmethod(frozenset),
                             "is_compute": staticmethod(lambda _mid: False),
                             "exit_state": staticmethod(lambda _mid: (0, False)),
                             "exit_report": staticmethod(lambda _mid: {}),
                             "links": satellite_link.MachineLinks(None)})()
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: manager)
    monkeypatch.setattr(session_manager, "_remote_layer", remote)
    return up


def test_the_closer_sends_nothing_to_a_machine_that_is_away(temp_db, monkeypatch):
    local, _seen = _layer(monkeypatch)
    remote = _Remote(**{"s-far": "m-1"})
    online = _places(monkeypatch, remote)
    chat = {"id": "c", "session_id": "s-far", "execution_target": "m-1"}
    assert _close(chat) is False and remote.closed_sessions == []
    online.add("m-1")
    assert _close(chat) is True and remote.closed_sessions == ["s-far"]
    assert local.closed_sessions == []


def test_the_closer_asks_who_holds_the_session_not_where_the_chat_is_pinned(temp_db, monkeypatch):
    """A round that fell back to the platform leaves a local process behind
    a machine's pin, and a socket's place is its last warm-up's: the layer
    that holds the id closes it, on the record's own machine."""
    local, _seen = _layer(monkeypatch)
    remote = _Remote(**{"s-there": "m-2"})
    _places(monkeypatch, remote, "m-1", "m-2")
    local.alive.add("s-here")
    # A machine's pin, a local holder.
    assert _close({"id": "c", "session_id": "s-here", "execution_target": "m-1"}) is True
    assert local.closed_sessions == ["s-here"] and remote.closed_sessions == []
    # A local or a stale pin, a holder on a machine: closed there.
    assert _close({"id": "c", "session_id": "s-there", "execution_target": "local"}) is True
    assert remote.closed_sessions == ["s-there"]
    # A machine's pin, nothing held anywhere: the remote layer is not asked
    # to close an id it does not hold, and the fallback frees what is left.
    assert _close({"id": "c", "session_id": "s-gone", "execution_target": "m-1"}) is False
    assert _close({"id": "c", "session_id": "s-gone", "execution_target": "m-1"},
                  fallback_layer=local) is False
    assert local.closed_sessions == ["s-here", "s-gone"] and remote.closed_sessions == ["s-there"]


def test_a_model_change_closes_what_holds_the_lanes_session(temp_db, monkeypatch):
    """The round's target is a machine. A session the last round left on the
    platform by fallback is closed there and its slot taken back. One on a
    machine that is away is left: nothing closed, said so, no slot taken."""
    from core import concurrency
    from services.scheduler import lanes
    local, seen = _layer(monkeypatch)
    remote = _Remote(**{"s-away": "m-2"})
    _places(monkeypatch, remote, "m-1")
    local.alive.add("s-fell-back")
    taken: list = []

    async def _acquire(sid, kind, **kw):
        taken.append((sid, kw["target"]))
        return True

    monkeypatch.setattr(concurrency, "acquire", _acquire)

    def change(sid):
        return _run(lanes._close_for_model_change(sid, "run-1", slot_target="m-1"))

    assert change("s-fell-back") is True
    assert local.closed_sessions == ["s-fell-back"] and seen["locked"] == [True]
    assert taken == [("s-fell-back", "m-1")]
    assert change("s-away") is False and change("s-nobody") is False
    assert remote.closed_sessions == [] and taken == [("s-fell-back", "m-1")]


def test_the_plans_fresh_start_closes_the_session_whatever_place_the_socket_last_warmed(
        temp_db, monkeypatch):
    """A socket that warmed a chat on a machine and then took another
    chat's live session on the platform: Implement closes that session."""
    from tests.session.test_rebuild_characterization import (
        _chat as chat_row,
        _connection,
    )
    local, _seen = _layer(monkeypatch)
    _places(monkeypatch, _Remote(), "m-1")
    agent = make_test_agent()
    cid = chat_row(agent, session_id="s-here")
    local.alive.add("s-here")
    conn = _connection(cid, "s-here", agent=agent, layer=local)
    conn.session_execution_target = "m-1"
    _run(conn._handle_implement_plan({"plan_path": "plans/p.md", "mode": "dontAsk"}))
    assert local.closed_sessions == ["s-here"]
    assert [sid for sid, _cfg in local.started] == [conn.session_id] != ["s-here"]


def test_the_closer_refuses_under_a_turn_only_when_asked(temp_db, monkeypatch):
    from core.events import stream_pump
    layer, _seen = _layer(monkeypatch)
    layer.alive.add("s-busy")
    monkeypatch.setitem(stream_pump._active_pumps, "c-busy",
                        type("P", (), {"is_done": False})())
    chat = {"id": "c-busy", "session_id": "s-busy"}
    with pytest.raises(chat_rebuild.ChatBusy):
        _close(chat, require_idle=True)
    assert layer.closed_sessions == []
    # The switch stops a running turn on purpose.
    assert _close(chat) is True


# -- the hold -----------------------------------------------------------------

def _chat(agent, session_id="s-held"):
    from storage import database as task_store
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-admin", agent, "default", model=TEST_MODEL,
                           execution_path="claude-code-cli")
    task_store.update_chat(cid, session_id=session_id)
    return cid


def test_the_hold_claims_registers_closes_and_gives_back(temp_db, monkeypatch):
    from core.events import input_queue
    from core.session import warmup_registry
    layer, seen = _layer(monkeypatch)
    layer.alive.add("s-held")
    cid = _chat(make_test_agent())

    async def scene():
        async with chat_rebuild.closed(cid, reason="reload", by="user-admin") as held:
            inside = (input_queue.is_claimed(cid), warmup_registry.get(cid) is not None,
                      held.was_live, held.chat["id"])
        return inside, input_queue.is_claimed(cid), warmup_registry.get(cid)

    inside, claimed_after, entry_after = _run(scene())
    assert inside == (True, True, True, cid)
    assert claimed_after is False and entry_after is None
    # Idle by the hold's own check, closed under the session's lock.
    assert layer.closed_sessions == ["s-held"] and seen["locked"] == [True]


def test_the_hold_refuses_a_chat_that_is_taken(temp_db, monkeypatch):
    from core.events import input_queue, stream_pump
    from core.session import warmup_registry
    layer, _seen = _layer(monkeypatch)
    layer.alive.add("s-held")
    cid = _chat(make_test_agent())

    async def held_by_a_turn():
        q = await input_queue.loaded(cid)
        claim = q.try_claim()
        try:
            async with chat_rebuild.closed(cid, reason="reload", by="u"):
                raise AssertionError("held")
        finally:
            q.release(claim)

    async def a_warmup_is_in_flight():
        theirs = await warmup_registry.register(cid, "user-admin", "a")
        try:
            async with chat_rebuild.closed(cid, reason="reload", by="u"):
                raise AssertionError("held")
        finally:
            # The hold made no entry of its own, so it removed none.
            assert warmup_registry.get(cid) is theirs
            await warmup_registry.unregister(cid)

    async def a_turn_is_open():
        stream_pump._active_pumps[cid] = type("P", (), {"is_done": False})()
        try:
            async with chat_rebuild.closed(cid, reason="reload", by="u"):
                raise AssertionError("held")
        finally:
            stream_pump._active_pumps.pop(cid, None)

    async def the_chat_is_gone():
        async with chat_rebuild.closed("no-such-chat", reason="reload", by="u"):
            raise AssertionError("held")

    for scene in (held_by_a_turn, a_warmup_is_in_flight, a_turn_is_open):
        with pytest.raises(chat_rebuild.ChatBusy):
            _run(scene())
    with pytest.raises(LookupError):
        _run(the_chat_is_gone())
    assert layer.closed_sessions == []
    assert not _run(_claimed(cid))


async def _claimed(cid):
    from core.events import input_queue
    from core.session import warmup_registry
    return input_queue.is_claimed(cid) or warmup_registry.get(cid) is not None


def test_the_hold_gives_the_callers_claim_back(temp_db, monkeypatch):
    from core.events import input_queue
    layer, _seen = _layer(monkeypatch)
    cid = _chat(make_test_agent())

    async def scene():
        q = await input_queue.loaded(cid)
        claim = q.try_claim()
        async with chat_rebuild.closed(cid, reason="reload", by="u", claim=claim) as held:
            assert held.was_live is False
        return input_queue.is_claimed(cid)

    assert _run(scene()) is False


# -- the rung's standing check ------------------------------------------------

def test_a_chat_built_session_takes_the_role_the_dashboard_reads(temp_db, monkeypatch):
    from services.remote import remote_status
    monkeypatch.setattr(remote_status, "resolve_execution_target", lambda *a, **k: ("local", None))
    agent = make_test_agent()

    def role(sub, **kw):
        return _run(wake_session._role_after_wait(agent, sub, "manager", "local", "s-1", **kw))

    # A platform admin with no row on the agent: the delivery's own build
    # and the chat's both run at the role the dashboard reads (the team
    # account's floor judges the wake as it judges the person).
    assert role("user-admin", pinned=True) == "admin"
    assert role("user-admin", pinned=True, as_chat=True) == "admin"
    # Nobody who lost the agent gets a session from either.
    assert role("user-viewer", pinned=True) is None
    assert role("user-viewer", pinned=True, as_chat=True) is None


# -- what a wake starts ---------------------------------------------------------

def _shared_only(monkeypatch, agents: set[str]) -> None:
    from core.session import visibility
    monkeypatch.setattr(visibility, "is_shared_only", lambda a: a in agents)


@pytest.mark.parametrize("chat,person,role,want", [
    # a person's own chat, an API-made one too
    ({"id": "c1", "agent": "a", "user_sub": "u"}, "u", "editor", "chat"),
    ({"id": "c1", "agent": "a", "user_sub": "u", "origin": "api"}, "u", "viewer", "chat"),
    # the agent's shared chat while the agent is Shared-only
    ({"id": "c2", "agent": "s", "user_sub": "agent::s"}, "u", "editor", "chat"),
    # the agent's shared chat once the agent left Shared-only
    ({"id": "c3", "agent": "a", "user_sub": "agent::a"}, "u", "editor", "wake"),
    # someone else's chat, a stored person who does not own it
    ({"id": "c4", "agent": "a", "user_sub": "other"}, "u", "editor", "wake"),
    # an outside person, a run's chat, a worker's, a machine's own terminal
    ({"id": "c5", "agent": "a", "user_sub": "u"}, "u", "external", "wake"),
    ({"id": "task-c6", "agent": "a", "user_sub": "u", "source_type": "task"}, "u", "editor", "wake"),
    ({"id": "c7", "agent": "a", "user_sub": "u", "origin": "delegated"}, "u", "editor", "wake"),
    ({"id": "c8", "agent": "a", "user_sub": "u", "origin": "otodock"}, "u", "editor", "wake"),
    # nobody behind it, no chat at all
    ({"id": "c9", "agent": "a", "user_sub": "u"}, None, "manager", "wake"),
    (None, "u", "editor", "wake"),
])
def test_a_wake_builds_the_chat_only_for_its_persons_own_chat(monkeypatch, chat, person, role, want):
    _shared_only(monkeypatch, {"s"})
    woken = chat_rebuild.wake_build(chat, person, role)
    if want == "chat":
        assert woken == chat_rebuild.WakeBuild("chat", chat_rebuild.CHAT_OWN_MODE, "dashboard")
    else:
        assert woken == chat_rebuild.WakeBuild("wake", UNATTENDED_MODE, "")


@pytest.mark.parametrize("owner,held,want", [
    ("u", False, "chat"),          # a person's chat after its call: the person's chat
    ("u", True, "wake"),           # while the call holds it: nobody drives it
    ("agent::s", False, "chat"),   # the shared chat a call wrote, after it
    ("phone", False, "wake"),      # the agent's conversation: the call's for good
])
def test_a_wake_of_a_chat_a_call_wrote_builds_it_as_the_persons_once_the_call_ended(
        monkeypatch, owner, held, want):
    """A delegated result or a scheduled wake that lands after the call builds
    the chat as its person's: its own mode, the dashboard kind, no call
    banner. While the call holds the chat, and in the agent's conversation,
    nothing changes."""
    from services.phone import call_chats
    _shared_only(monkeypatch, {"s"})
    chat = {"id": "c-call", "agent": "s" if owner.startswith("agent::") else "a",
            "user_sub": owner, "source_type": "phone"}
    call_chats._held.clear()
    if held:
        call_chats.hold("c-call", "sid-call")
    try:
        woken = chat_rebuild.wake_build(chat, "u", "editor")
    finally:
        call_chats._held.clear()
    if want == "chat":
        assert woken == chat_rebuild.WakeBuild("chat", chat_rebuild.CHAT_OWN_MODE, "dashboard")
    else:
        assert woken == chat_rebuild.WakeBuild("wake", UNATTENDED_MODE, "")


@pytest.mark.parametrize("held", [True, False])
def test_a_wake_decided_while_a_call_holds_the_chat_never_starts_a_session(monkeypatch, held):
    """A wake decided under the hold is built as nobody's. It rides the call's
    live session or is kept for the chat's next turn: it never starts a
    session there, which could land after the hang-up and be bound by the
    person's resume. Without a hold the start rung stays."""
    from services.phone import call_chats
    seen: dict = {}

    async def _ladder(chat_id, text, **kw):
        seen.update(kw)
        return session_delivery.DeliveryOutcome("none", chat_id=chat_id)
    monkeypatch.setattr(session_delivery, "deliver_prompt", _ladder)
    call_chats._held.clear()
    if held:
        call_chats.hold("c-call", "sid-call")
    try:
        asyncio.run(chat_rebuild.rebuild_and_resume(
            "c-call", "the result", source="delegate_result", person="u", role="editor",
            mode=UNATTENDED_MODE, kind="", build=chat_rebuild.AS_WAKE, session_id="sid-call"))
    finally:
        call_chats._held.clear()
    assert (seen["oneshot_fn"] is None) is held
    assert seen["persistent_fn"] is not None


@pytest.mark.parametrize("owner,role,word,want", [
    ("u", "viewer", "", "autoReview"),          # nobody picked: Auto, the page's own seed
    ("u", "editor", "plan", "plan"),            # the person's pick
    ("u", "editor", "default", "ask"),          # an older word read as today's
    ("agent::s", "editor", "", "autoReview"),   # the shared chat, at the editor role
    ("agent::s", "viewer", "dontAsk", "ask"),   # below it: runs as the agent, no pick stays
])
def test_a_new_chat_is_stored_in_the_mode_its_person_starts_in(owner, role, word, want):
    assert chat_rebuild.new_chat_mode("s", owner, role, word) == want


def test_a_wake_of_a_persons_chat_runs_in_its_mode_and_never_in_a_word_nobody_picks(monkeypatch):
    monkeypatch.setattr(chat_rebuild, "chat_mode", lambda chat: chat["permission_mode"])
    for mode in ("ask", "autoReview", "dontAsk", "plan"):
        assert chat_rebuild.own_mode({"permission_mode": mode}) == mode
    for stored in ("auto", "judge"):
        assert chat_rebuild.own_mode({"permission_mode": stored}) == "ask"


def test_the_chats_mode_is_read_when_the_session_is_built(temp_db, monkeypatch):
    """A person who lowers the chat's mode while the wake waits for room
    gets the lower mode: the row is read again after the wait."""
    from storage import database as task_store
    agent = make_test_agent()
    task_store.create_chat("c-mode", "user-admin", agent, permission_mode="ask")
    seen: list[str] = []

    async def _built(*a, permission_mode, **k):
        seen.append(permission_mode)
        return None
    monkeypatch.setattr(wake_session, "_chat_built_config", _built)
    monkeypatch.setattr(chat_rebuild, "chat_mode", lambda chat: chat["permission_mode"])
    stale = {"id": "c-mode", "agent": agent, "user_sub": "user-admin", "permission_mode": "plan"}
    started, _ = _run(wake_session._spawn_wake_session(
        FakeExecutionLayer(), "s-mode", agent, "wake", user_sub="user-admin", role="admin",
        chat_id="c-mode", chat_row=stale, exec_path="claude-code-cli", target="local",
        username="admin", permission_mode=chat_rebuild.CHAT_OWN_MODE, client_type="dashboard",
        build=chat_rebuild.AS_CHAT))
    assert started is False and seen == ["ask"]


def test_a_parked_question_or_reported_background_work_holds_the_chat(monkeypatch):
    """The rebuilds see the reaper's busy reasons (TERMINAL-CONTROLS' H1): a
    terminal parked on its question, or holding background work its last
    Stop reported, is never closed under a person."""
    from types import SimpleNamespace

    from core.session import interactive_registry, interactive_session
    term = SimpleNamespace(turn_open=False, question_parked=False, submit_settling=False)
    monkeypatch.setattr(interactive_session, "find_live_for_chat", lambda _cid: term)
    reported = {"on": False}
    monkeypatch.setattr(interactive_registry, "background_reported", lambda _s: reported["on"])
    chat = {"id": "c-held", "session_id": ""}
    assert chat_rebuild.turn_open_while_held(chat) is False
    term.question_parked = True
    assert chat_rebuild.turn_open_while_held(chat) is True
    term.question_parked = False
    reported["on"] = True
    assert chat_rebuild.turn_open_while_held(chat) is True
    reported["on"] = False
    # A message just sent, its turn not open yet: never closed under it.
    term.submit_settling = True
    assert chat_rebuild.turn_open_while_held(chat) is True


def test_a_session_built_for_no_person_on_a_chat_people_open_is_always_unattended():
    """Why no ownerless terminal in a person's mode is left on a chat people
    open (the rule reads the mode alone): a wake with nobody behind it takes
    the unattended word, the move's resume and a key's turn always name a
    person outside a task run's chat."""
    from services.remote import chat_move
    shared = {"id": "c-old", "agent": "a", "user_sub": "agent::a"}
    assert chat_rebuild.wake_build(shared, None, "editor").mode == UNATTENDED_MODE
    assert chat_move._builds_as(shared, "u-asker", "editor") == ("u-asker", "editor")
    assert chat_move._builds_as({"id": "c-own", "agent": "a", "user_sub": "u"}, "u", "viewer") == (
        "u", "viewer")
