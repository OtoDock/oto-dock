"""Voice-path auto-resume on a dead CLI process (incident 2026-08-12).

Two layers under test:

- ``duplex_attach``'s new-turn chokepoint (``_ensure_live_session``): a dead
  or registry-reaped session heals through the headless resume seam BEFORE
  dispatch — never sends into the corpse; a satellite in reconnect grace and
  probe failures fail toward ALIVE (dispatch as today).
- ``ws/headless_resume.resume_dead_session_headless``: the connection-free
  twin of the dashboard's ``_resume_dead_session_for_chat`` — chat-row
  rebuild, can_resume BEFORE prepare_resume, same-sid resume vs fresh-sid +
  ``pending_history_seed``, interactive chats refused.
"""


import pytest

import ws.duplex as ws_duplex
import ws.duplex_attach as duplex_attach
import ws.headless_resume as headless_resume
from core.execution_layer import ResumeVerdict


class FakeWebSocket:
    def __init__(self):
        self.sent: list = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.fixture(autouse=True)
def _clean_registries(monkeypatch):
    duplex_attach._states.clear()
    # A compute machine serves: a dead session can be started again. The
    # test of the refusal takes it away.
    from tests._compute import serve
    serve(monkeypatch)
    yield
    duplex_attach._states.clear()


def _bridge(chat_id="chat-1", sub="user-admin"):
    return ws_duplex.DuplexBridge(
        duplex_id="dx-1", sub=sub, chat_id=chat_id, max_seconds=1800,
        browser_ws=FakeWebSocket(), engine_ws=FakeWebSocket(),
    )


class _ProbeLayer:
    """Headless layer stub with controllable liveness probes."""

    def __init__(self, *, process_dead=False, alive=True):
        self.process_dead = process_dead
        self.alive = alive

    async def is_session_process_dead(self, session_id):
        return self.process_dead

    async def is_session_alive(self, session_id):
        return self.alive

    async def wait_session_reconnect(self, session_id, **kw):
        return False


def _attach_state(layer, sid="sid-dead"):
    st = duplex_attach._AttachState(
        chat_id="chat-1", session_id=sid, agent="agent-a",
        execution_path="claude-code-cli", layer=layer,
    )
    duplex_attach._states["dx-1"] = st
    return st


def _capture_dispatch(monkeypatch):
    dispatched = {}

    async def _fake_new_turn(bridge, st, turn, text, barge_in_chars):
        dispatched.update(sid=st.session_id, layer=st.layer, text=text)
    monkeypatch.setattr(duplex_attach, "_run_new_turn", _fake_new_turn)
    return dispatched


# --- chokepoint: _ensure_live_session via run_utterance ---------------------

@pytest.mark.asyncio
async def test_dead_process_heals_before_dispatch(temp_db, monkeypatch):
    _make_chat(temp_db)
    bridge = _bridge()
    dead_layer = _ProbeLayer(process_dead=True)
    st = _attach_state(dead_layer)
    st.context_injected = True
    healed_layer = _ProbeLayer()
    calls = {}

    async def _fake_heal(chat_id, dead_sid, layer, *, user_sub):
        calls.update(chat_id=chat_id, dead_sid=dead_sid, layer=layer,
                     user_sub=user_sub)
        return headless_resume.HeadlessSpawn(
            session_id="sid-new", layer=healed_layer, resumed=True)
    monkeypatch.setattr(
        headless_resume, "resume_dead_session_headless", _fake_heal)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 1, "text": "hello"})

    assert calls == {"chat_id": "chat-1", "dead_sid": "sid-dead",
                     "layer": dead_layer, "user_sub": "user-admin"}
    assert (st.session_id, st.layer) == ("sid-new", healed_layer)
    assert dispatched["sid"] == "sid-new"
    # Resumed history carries the duplex context — no re-injection.
    assert st.context_injected is True
    assert bridge.engine_ws.sent == []  # no error frame


@pytest.mark.asyncio
async def test_reaped_session_heals_and_reinjects_context(temp_db, monkeypatch):
    """Registry-reaped (idle timeout): process_dead False but not alive →
    heal fires; a FRESH (non-resumed) spawn re-injects the duplex context."""
    _make_chat(temp_db)
    bridge = _bridge()
    st = _attach_state(_ProbeLayer(process_dead=False, alive=False))
    st.context_injected = True

    async def _fake_heal(chat_id, dead_sid, layer, *, user_sub):
        return headless_resume.HeadlessSpawn(
            session_id="sid-fresh", layer=_ProbeLayer(), resumed=False)
    monkeypatch.setattr(
        headless_resume, "resume_dead_session_headless", _fake_heal)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 2, "text": "anyone?"})

    assert st.session_id == "sid-fresh"
    assert st.context_injected is False  # fresh session lost the context
    assert dispatched["sid"] == "sid-fresh"


@pytest.mark.asyncio
async def test_heal_failure_sends_session_dead_and_skips_dispatch(
        temp_db, monkeypatch):
    _make_chat(temp_db)
    bridge = _bridge()
    st = _attach_state(_ProbeLayer(process_dead=True))

    async def _boom(chat_id, dead_sid, layer, *, user_sub):
        raise RuntimeError("spawn exploded")
    monkeypatch.setattr(
        headless_resume, "resume_dead_session_headless", _boom)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 3, "text": "hello"})

    assert dispatched == {}  # never dispatched into the corpse
    assert st.active_turn is None
    assert bridge.engine_ws.sent == [{
        "type": "error", "turn": 3, "data": {"message": "session_dead"},
    }]


@pytest.mark.asyncio
async def test_a_voice_turn_with_no_compute_machine_hears_the_sentence_at_once(temp_db, monkeypatch, caplog):
    """The chat's session ended and no compute machine can start it again.
    A spoken turn does not wait: the voice engine gets the one sentence, to
    log and to say, before anything of the session is touched (no resume
    is prepared, nothing is built), and nothing is logged as an error."""
    import logging

    from core import placement
    from tests._compute import serve_none
    _make_chat(temp_db)
    bridge = _bridge()
    layer = _ProbeLayer(process_dead=True)
    st = _attach_state(layer)
    serve_none(monkeypatch)
    healed: list = []

    async def _heal(chat_id, dead_sid, layer, *, user_sub):
        healed.append(dead_sid)
        raise AssertionError("the heal ran with no compute machine")
    monkeypatch.setattr(headless_resume, "resume_dead_session_headless", _heal)
    dispatched = _capture_dispatch(monkeypatch)

    with caplog.at_level(logging.INFO):
        await duplex_attach.run_utterance(bridge, {"turn": 5, "text": "are you there"})

    assert healed == [] and dispatched == {} and st.active_turn is None
    assert bridge.engine_ws.sent == [{
        "type": "error", "turn": 5,
        "data": {"message": placement.NO_COMPUTE_SENTENCE, "say": placement.NO_COMPUTE_SENTENCE},
    }]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(placement.NO_COMPUTE_SENTENCE) <= 200, "the voice engine speaks a line of 200 characters at most"


@pytest.mark.asyncio
async def test_replaced_session_is_adopted_not_resurrected(temp_db, monkeypatch):
    """The dashboard's own auto-resume may have already repointed the chat to
    a FRESH session — the voice heal must ADOPT it, not resurrect the stale
    corpse (which would fork the chat across two live sessions)."""
    _make_chat(temp_db, sid="sid-replaced")

    class _PerSidLayer(_ProbeLayer):
        async def is_session_process_dead(self, session_id):
            return session_id == "sid-dead"  # only the stale one is dead

    bridge = _bridge()
    st = _attach_state(_PerSidLayer())

    async def _never(chat_id, dead_sid, layer, *, user_sub):
        raise AssertionError("heal must adopt the replacement, not respawn")
    monkeypatch.setattr(
        headless_resume, "resume_dead_session_headless", _never)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 7, "text": "hello"})

    assert st.session_id == "sid-replaced"
    assert dispatched["sid"] == "sid-replaced"


@pytest.mark.asyncio
async def test_grace_held_session_is_not_healed(monkeypatch):
    """A satellite inside reconnect grace reads process-dead but is
    'reconnecting' — the heal must not respawn (it would abandon the
    machine-side session over a 10s WS blip). The grace is the holder's
    answer (``live_session``). A machine that does not come back within
    the wait gets no turn either: the speaker hears that it is away."""
    from core.session import session_manager
    bridge = _bridge()
    layer = _ProbeLayer(process_dead=True)
    st = _attach_state(layer)
    monkeypatch.setattr(session_manager, "live_session", lambda sid: session_manager.LiveSession(
        layer, "m-1", linked=False, grace=True) if sid == "sid-dead" else None)

    async def _never(chat_id, dead_sid, layer, *, user_sub):
        raise AssertionError("heal must not run during grace")
    monkeypatch.setattr(
        headless_resume, "resume_dead_session_headless", _never)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 4, "text": "hello"})

    assert st.session_id == "sid-dead" and dispatched == {}
    said = duplex_attach.MACHINE_AWAY_SENTENCE
    assert bridge.engine_ws.sent == [{"type": "error", "turn": 4, "data": {"message": said, "say": said}}]


class _ComputeHolder(_ProbeLayer):
    """The remote layer holding the chat's session on compute machine c-1:
    alive over the machine's link, held by its grace while the link is down,
    back at the first wait."""

    def __init__(self, link):
        super().__init__()
        self.link = link
        self.waited = 0

    async def is_session_alive(self, session_id):
        return self.link["up"]

    async def wait_session_reconnect(self, session_id, **kw):
        self.waited += 1
        self.link["up"] = True
        return True


def _on_compute(monkeypatch, *, linked=True):
    """The chat's session ``sid-live`` runs on compute machine c-1, as its
    own record says; the engine's own layer in the proxy holds nothing, and
    a machine away serves nothing new."""
    from types import SimpleNamespace

    from core import placement
    from core.remote import satellite_connection, satellite_link
    from core.session import session_manager, session_state
    from services.remote import remote_status
    link = {"up": linked}
    holder = _ComputeHolder(link)
    cm = SimpleNamespace(is_compute=lambda mid: link["up"] and mid == "c-1",
                         is_connected=lambda mid: link["up"] and mid == "c-1",
                         machine_dropped_recently=lambda mid: not link["up"],
                         exit_state=lambda mid: (0, False), exit_report=lambda mid: {},
                         links=satellite_link.MachineLinks(None))
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: cm)
    monkeypatch.setattr(remote_status, "serving_compute_machine", lambda *a, **kw: None)
    placed = placement.from_machine(placement.KIND_COMPUTE, {"id": "c-1", "pairing_scope": "compute"})
    monkeypatch.setattr(session_state, "get_session_security",
                        lambda sid: SimpleNamespace(placement=placed) if sid == "sid-live" else None)
    monkeypatch.setattr(session_manager, "live_session", lambda sid: session_manager.LiveSession(
        holder, "c-1", linked=link["up"], grace=not link["up"]) if sid == "sid-live" else None)

    async def _never(chat_id, dead_sid, layer, *, user_sub):
        raise AssertionError("the compute machine's live session was rebuilt")
    monkeypatch.setattr(headless_resume, "resume_dead_session_headless", _never)
    return holder, link


class _AwayHolder(_ProbeLayer):
    """The remote layer holding the chat's session on a compute machine that
    is away and does not come back within a wait."""

    def __init__(self):
        super().__init__()
        self.waits: list = []

    async def is_session_alive(self, session_id):
        return False

    async def wait_session_reconnect(self, session_id, **kw):
        self.waits.append(kw)
        return False


def _held_by_a_machine_that_is_away(monkeypatch, *, recent: bool, serves: bool = False, pin: str = "local"):
    from types import SimpleNamespace

    from core.remote import satellite_connection, satellite_link
    from core.session import session_manager
    from tests._compute import serve, serve_none
    holder = _AwayHolder()
    cm = SimpleNamespace(machine_dropped_recently=lambda mid: recent, is_compute=lambda mid: False,
                         is_connected=lambda mid: False, exit_state=lambda mid: (0, False),
                         exit_report=lambda mid: {}, links=satellite_link.MachineLinks(None))
    monkeypatch.setattr(satellite_connection, "get_connection_manager", lambda: cm)
    monkeypatch.setattr(session_manager, "live_session", lambda sid: session_manager.LiveSession(
        holder, "c-1" if pin == "local" else pin, linked=False, grace=True) if sid == "sid-live" else None)
    (serve if serves else serve_none)(monkeypatch)

    async def _never(chat_id, dead_sid, layer, *, user_sub):
        raise AssertionError("a held session was rebuilt")
    monkeypatch.setattr(headless_resume, "resume_dead_session_headless", _never)
    return holder


@pytest.mark.asyncio
@pytest.mark.parametrize("recent", [False, True])
async def test_a_voice_turn_on_a_session_its_away_compute_machine_holds_hears_the_sentence(
        temp_db, monkeypatch, recent, caplog):
    """The compute machine stopped and still holds the chat's session (its
    return takes it back). A spoken turn does not go to a machine that is
    not there: it hears the sentence, the held session is left as it is and
    nothing is rebuilt. A machine away past its reconnect window is not
    waited for at all. One that dropped a moment ago gets the short wait
    every door gives a link that may come back."""
    import logging

    from core import placement
    _make_chat(temp_db, sid="sid-live", execution_path="claude-code-cli")
    bridge = _bridge()
    holder = _held_by_a_machine_that_is_away(monkeypatch, recent=recent)
    st = _attach_state(holder, sid="sid-live")
    dispatched = _capture_dispatch(monkeypatch)

    with caplog.at_level(logging.INFO):
        await duplex_attach.run_utterance(bridge, {"turn": 6, "text": "are you there"})

    assert holder.waits == ([{}] if recent else [{"timeout": 0.0}])
    assert dispatched == {} and st.session_id == "sid-live" and st.active_turn is None
    assert bridge.engine_ws.sent == [{
        "type": "error", "turn": 6,
        "data": {"message": placement.NO_COMPUTE_SENTENCE, "say": placement.NO_COMPUTE_SENTENCE},
    }]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
@pytest.mark.parametrize("why, kw", [
    ("another compute machine serves", {"serves": True}),
    ("the chat is pinned to a paired computer", {"pin": "m-paired"}),
])
async def test_a_voice_turn_never_goes_to_a_machine_that_is_away_whichever_holds_it(
        temp_db, monkeypatch, why, kw):
    """A typed message waits for the holder. A spoken turn cannot: it is
    refused in words, with the sentence that fits. The compute sentence is
    for a chat no compute machine can take. A chat on a paired computer,
    and one whose session another compute machine could not take over,
    hear that their machine is not connected. The held session is left as
    it is."""
    pin = kw.get("pin", "local")
    _make_chat(temp_db, sid="sid-live", execution_path="claude-code-cli")
    temp_db.update_chat("chat-1", execution_target=pin)
    bridge = _bridge()
    holder = _held_by_a_machine_that_is_away(monkeypatch, recent=False, **kw)
    st = _attach_state(holder, sid="sid-live")
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 6, "text": "are you there"})

    assert dispatched == {} and st.session_id == "sid-live" and st.active_turn is None, why
    said = "The machine running this chat is not connected. Try again in a few minutes."
    assert bridge.engine_ws.sent == [{"type": "error", "turn": 6, "data": {"message": said, "say": said}}]


@pytest.mark.asyncio
async def test_a_voice_turn_goes_to_a_holder_that_came_back_while_it_was_checked(temp_db, monkeypatch):
    """The wait for the link ended without it, and the machine signed in
    during the reads after: the session is live again and the turn is its."""
    from core.session import session_manager
    _make_chat(temp_db, sid="sid-live", execution_path="claude-code-cli")
    bridge = _bridge()
    holder = _held_by_a_machine_that_is_away(monkeypatch, recent=False)
    reads = {"n": 0}

    def _live(sid):
        reads["n"] += 1
        return session_manager.LiveSession(holder, "c-1", linked=reads["n"] > 1, grace=reads["n"] == 1)
    monkeypatch.setattr(session_manager, "live_session", _live)
    st = _attach_state(holder, sid="sid-live")
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 6, "text": "are you there"})

    assert dispatched.get("sid") == "sid-live" and st.layer is holder and bridge.engine_ws.sent == []


@pytest.mark.asyncio
async def test_a_voice_turn_rides_the_session_the_compute_machine_runs(temp_db, monkeypatch):
    """The first utterance reads the chat's layer as every chat door does:
    the compute machine's session is the turn's, nothing is rebuilt."""
    _make_chat(temp_db, sid="sid-live", execution_path="claude-code-cli")
    holder, _link = _on_compute(monkeypatch)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(_bridge(), {"turn": 1, "text": "hello"})

    assert dispatched == {"sid": "sid-live", "layer": holder, "text": "hello"}


@pytest.mark.asyncio
async def test_a_first_voice_turn_while_the_compute_machine_reconnects_heals_nothing(
        temp_db, monkeypatch):
    """A compute machine that is reconnecting still holds the chat's
    session: the reading is its holder's, the reconnect is waited for there,
    and the turn goes to the holder. Nothing is rebuilt."""
    _make_chat(temp_db, sid="sid-live", execution_path="claude-code-cli")
    holder, _link = _on_compute(monkeypatch, linked=False)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(_bridge(), {"turn": 1, "text": "hello"})

    assert holder.waited == 1
    assert dispatched == {"sid": "sid-live", "layer": holder, "text": "hello"}


@pytest.mark.asyncio
async def test_a_reading_taken_while_the_machine_was_away_is_its_holders(temp_db, monkeypatch):
    """The bridge read the chat's layer while c-1 was reconnecting: the
    holder's, never a layer of the platform's own (it runs no session). At
    the next turn c-1 is back and the turn goes there, nothing healed."""
    _make_chat(temp_db, sid="sid-live", execution_path="claude-code-cli")
    holder, link = _on_compute(monkeypatch, linked=False)
    bridge = _bridge()
    st = await duplex_attach._resolve(bridge)
    assert st.layer is holder
    link["up"] = True
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 2, "text": "again"})

    assert dispatched == {"sid": "sid-live", "layer": holder, "text": "again"}
    assert st.layer is holder and holder.waited == 0


@pytest.mark.asyncio
async def test_a_voice_turn_rides_a_chat_pinned_to_a_paired_computer(temp_db, monkeypatch):
    """The chat's pin decides, not the agent's target (the platform here):
    the paired computer's session is the turn's, nothing is rebuilt."""
    from core.session import session_manager
    from services.remote import remote_status
    from storage import remote_store
    _make_chat(temp_db, sid="sid-live", execution_path="claude-code-cli")
    temp_db.update_chat("chat-1", execution_target="m-1")
    holder = _ProbeLayer()
    monkeypatch.setattr(remote_store, "get_remote_machine", lambda m: {
        "id": m, "pairing_scope": "admin", "registered_by": ""} if m == "m-1" else None)
    monkeypatch.setattr(session_manager, "_get_remote_layer", lambda: holder)
    monkeypatch.setattr(remote_status, "resolve_execution_target", lambda *a, **kw: ("local", None))
    monkeypatch.setattr(session_manager, "live_session", lambda sid: session_manager.LiveSession(
        holder, "m-1") if sid == "sid-live" else None)

    async def _never(chat_id, dead_sid, layer, *, user_sub):
        raise AssertionError("the paired computer's live session was rebuilt")
    monkeypatch.setattr(headless_resume, "resume_dead_session_headless", _never)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(_bridge(), {"turn": 1, "text": "hello"})

    assert dispatched == {"sid": "sid-live", "layer": holder, "text": "hello"}


@pytest.mark.asyncio
async def test_live_session_skips_heal(monkeypatch):
    bridge = _bridge()
    st = _attach_state(_ProbeLayer())

    async def _never(chat_id, dead_sid, layer, *, user_sub):
        raise AssertionError("heal must not run for a live session")
    monkeypatch.setattr(
        headless_resume, "resume_dead_session_headless", _never)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 5, "text": "hello"})
    assert dispatched["sid"] == "sid-dead"
    assert st.session_id == "sid-dead"


@pytest.mark.asyncio
async def test_probe_failure_fails_toward_alive(monkeypatch):
    class _BrokenProbeLayer(_ProbeLayer):
        async def is_session_process_dead(self, session_id):
            raise RuntimeError("probe RPC failed")

    bridge = _bridge()
    _attach_state(_BrokenProbeLayer())

    async def _never(chat_id, dead_sid, layer, *, user_sub):
        raise AssertionError("heal must not run on probe uncertainty")
    monkeypatch.setattr(
        headless_resume, "resume_dead_session_headless", _never)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 6, "text": "hello"})
    assert dispatched["sid"] == "sid-dead"  # dispatched; send_message decides


# --- the seam: resume_dead_session_headless ---------------------------------

class _SeamLayer:
    """Layer stub for the seam's resume protocol, recording call order."""

    def __init__(self, *, resumable):
        self.resumable = resumable
        self.calls: list[str] = []

    async def can_resume_session(self, session_id, *, agent_name="",
                                 username="", external_home=""):
        self.calls.append(f"can_resume:{session_id}")
        return ResumeVerdict.RESUMABLE if self.resumable else ResumeVerdict.FRESH

    async def prepare_resume(self, session_id):
        self.calls.append(f"prepare:{session_id}")


class _SpawnLayer:
    def __init__(self):
        self.started: list = []

    async def start_session(self, session_id, config):
        self.started.append((session_id, config))


class _Cfg:
    def __init__(self, captured):
        self.captured = captured
        self.execution_target = "local"
        self.execution_path = "claude-code-cli"
        self.mcp_config_path = "/tmp/mcp-heal.json"
        self.interactive = None


def _patch_seam(monkeypatch, spawn_layer, *, interactive=False):
    """Patch the seam's spawn collaborators; returns captured build kwargs
    (plus ``captured["admission"]``, the kwargs of the slot acquire)."""
    captured = {}

    async def _fake_build(**kwargs):
        captured.update(kwargs)
        return _Cfg(captured)
    monkeypatch.setattr(headless_resume, "build_agent_config", _fake_build)
    monkeypatch.setattr(
        headless_resume, "get_execution_layer",
        lambda *a, **kw: spawn_layer)
    monkeypatch.setattr(
        headless_resume, "_resolve_session_interactive",
        lambda cfg, mode: interactive)

    import core.concurrency as concurrency
    slots = {"acquired": [], "released": []}

    async def _acquire(sid, **kw):
        slots["acquired"].append(sid)
        captured["admission"] = kw
        return True
    monkeypatch.setattr(concurrency, "acquire_chat_slot", _acquire)
    monkeypatch.setattr(
        concurrency, "release_chat_slot",
        slots["released"].append)
    return captured, slots


def _make_chat(task_store, chat_id="chat-1", sid="sid-dead", **kw):
    task_store.create_chat(chat_id, "user-admin", "agent-a", "auto", **kw)
    task_store.update_chat(chat_id, session_id=sid)


@pytest.mark.asyncio
async def test_seam_resumable_keeps_sid_and_orders_calls(temp_db, monkeypatch):
    _make_chat(temp_db, model="model-x", execution_path="claude-code-cli")
    layer = _SeamLayer(resumable=True)
    spawn_layer = _SpawnLayer()
    captured, slots = _patch_seam(monkeypatch, spawn_layer)

    res = await headless_resume.resume_dead_session_headless(
        "chat-1", "sid-dead", layer, user_sub="user-admin")

    assert (res.session_id, res.resumed) == ("sid-dead", True)
    assert res.layer is spawn_layer
    # ORDER IS LOAD-BEARING: the remote check needs the machine_id that
    # prepare_resume pops.
    assert layer.calls == ["can_resume:sid-dead", "prepare:sid-dead"]
    assert captured["resume"] is True
    assert captured["model"] == "model-x"
    assert captured["chat_id"] == "chat-1"
    assert spawn_layer.started and spawn_layer.started[0][0] == "sid-dead"
    assert slots == {"acquired": ["sid-dead"], "released": []}
    # Chat row untouched — same session id still mapped.
    assert temp_db.get_chat("chat-1")["session_id"] == "sid-dead"


@pytest.mark.asyncio
async def test_seam_unresumable_fresh_sid_and_seed_flag(temp_db, monkeypatch):
    _make_chat(temp_db)
    layer = _SeamLayer(resumable=False)
    spawn_layer = _SpawnLayer()
    captured, slots = _patch_seam(monkeypatch, spawn_layer)

    res = await headless_resume.resume_dead_session_headless(
        "chat-1", "sid-dead", layer, user_sub="user-admin")

    assert res.resumed is False
    assert res.session_id != "sid-dead"
    assert captured["resume"] is False
    assert slots["released"] == ["sid-dead"]
    assert slots["acquired"] == [res.session_id]
    chat = temp_db.get_chat("chat-1")
    assert chat["session_id"] == res.session_id
    assert chat["pending_history_seed"] == "resume_failed"


@pytest.mark.asyncio
async def test_seam_refuses_interactive_chat(temp_db, monkeypatch):
    _make_chat(temp_db, execution_mode="interactive")
    layer = _SeamLayer(resumable=True)
    spawn_layer = _SpawnLayer()
    _patch_seam(monkeypatch, spawn_layer, interactive=True)

    with pytest.raises(headless_resume.ResumeUnavailable):
        await headless_resume.resume_dead_session_headless(
            "chat-1", "sid-dead", layer, user_sub="user-admin")
    assert spawn_layer.started == []


@pytest.mark.asyncio
async def test_seam_refuses_unknown_chat_and_user(temp_db, monkeypatch):
    layer = _SeamLayer(resumable=True)
    spawn_layer = _SpawnLayer()
    _patch_seam(monkeypatch, spawn_layer)

    with pytest.raises(headless_resume.ResumeUnavailable):
        await headless_resume.resume_dead_session_headless(
            "chat-missing", "sid-dead", layer, user_sub="user-admin")

    _make_chat(temp_db)
    with pytest.raises(headless_resume.ResumeUnavailable):
        await headless_resume.resume_dead_session_headless(
            "chat-1", "sid-dead", layer, user_sub="user-nobody")
    assert spawn_layer.started == []
    # Refusals must not have consumed the resume protocol.
    assert layer.calls == []


@pytest.mark.asyncio
async def test_seam_queues_for_a_slot_and_reserves_for_its_mcps(temp_db, monkeypatch):
    """A headless heal waits its turn in the admission queue
    and names its MCP config so a Direct-LLM session reserves for its stdio
    MCPs."""
    import config
    _make_chat(temp_db, model="model-x", execution_path="claude-code-cli")
    layer = _SeamLayer(resumable=True)
    spawn_layer = _SpawnLayer()
    captured, _slots = _patch_seam(monkeypatch, spawn_layer)
    await headless_resume.resume_dead_session_headless(
        "chat-1", "sid-dead", layer, user_sub="user-admin")
    adm = captured["admission"]
    assert adm["queue_wait_s"] == config.ADMISSION_QUEUE_WAIT_S
    assert adm["mcp_config_path"] == "/tmp/mcp-heal.json"
    assert adm["user_sub"] == "user-admin"


@pytest.mark.asyncio
async def test_seam_waits_for_an_inflight_wake_before_resuming(temp_db, monkeypatch):
    """A one-shot wake writing the chat's conversation file finishes before
    the heal reads it (the dashboard warmup's rule): the heal never renders
    a half-written turn or dual-writes the file."""
    import asyncio

    from core.session import session_delivery
    _make_chat(temp_db, model="model-x", execution_path="claude-code-cli")
    layer = _SeamLayer(resumable=True)
    spawn_layer = _SpawnLayer()
    _patch_seam(monkeypatch, spawn_layer)
    inflight = asyncio.Event()
    order: list[str] = []
    monkeypatch.setattr(session_delivery, "oneshot_inflight",
                        lambda cid: inflight if cid == "chat-1" else None)
    orig = layer.can_resume_session

    async def _can(sid, **kw):
        order.append("resume_check")
        return await orig(sid, **kw)
    layer.can_resume_session = _can

    async def _finish_wake():
        await asyncio.sleep(0.05)
        order.append("wake_done")
        inflight.set()
    finisher = asyncio.create_task(_finish_wake())
    await headless_resume.resume_dead_session_headless(
        "chat-1", "sid-dead", layer, user_sub="user-admin")
    await finisher
    assert order == ["wake_done", "resume_check"]


# --- the chat's claim: voice takes turns with the other starters ------------

@pytest.mark.asyncio
async def test_an_utterance_waits_for_another_starter_then_starts(temp_db, monkeypatch):
    """A queued message's delivery holds the chat while it brings its turn
    up: the utterance waits for it, never a second turn beside it."""
    import asyncio

    from core.events import input_queue
    input_queue._registry.clear()
    _make_chat(temp_db)
    bridge = _bridge()
    _attach_state(_ProbeLayer())
    dispatched = _capture_dispatch(monkeypatch)
    q = input_queue.get("chat-1")
    held = q.try_claim()

    async def _release_soon():
        await asyncio.sleep(0.3)
        q.release(held)
    releaser = asyncio.create_task(_release_soon())
    await duplex_attach.run_utterance(bridge, {"turn": 1, "text": "hello"})
    await releaser
    assert dispatched["text"] == "hello"
    # The utterance's own claim is released once its turn started.
    assert not q.claimed
    input_queue._registry.clear()


@pytest.mark.asyncio
async def test_an_utterance_the_chat_stays_busy_for_says_so(temp_db, monkeypatch):
    from core.events import input_queue
    input_queue._registry.clear()
    monkeypatch.setattr(duplex_attach, "_CLAIM_WAIT_S", 0.2)
    _make_chat(temp_db)
    bridge = _bridge()
    _attach_state(_ProbeLayer())
    dispatched = _capture_dispatch(monkeypatch)
    q = input_queue.get("chat-1")
    held = q.try_claim()
    await duplex_attach.run_utterance(bridge, {"turn": 2, "text": "hello"})
    assert dispatched == {}
    assert bridge.engine_ws.sent[-1] == {"type": "error", "turn": 2,
                                         "data": {"message": duplex_attach._BUSY_LINE}}
    assert q.claimed
    q.release(held)
    input_queue._registry.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,stored,runs", [
    ("direct-llm", "autoReview", "ask"),       # Direct LLM offers no Auto
    ("direct-llm", "acceptEdits", "ask"),
    ("claude-code-cli", "autoReview", "autoReview"),
    ("claude-code-cli", "auto", "auto"),       # an unattended chat keeps its word
])
async def test_seam_resumes_in_a_mode_the_chats_engine_offers(temp_db, monkeypatch, path, stored, runs):
    temp_db.create_chat("chat-1", "user-admin", "agent-a", stored, execution_path=path)
    temp_db.update_chat("chat-1", session_id="sid-dead")
    captured, _slots = _patch_seam(monkeypatch, _SpawnLayer())
    await headless_resume.resume_dead_session_headless(
        "chat-1", "sid-dead", _SeamLayer(resumable=True), user_sub="user-admin")
    assert captured["permission_mode"] == runs
    from core.events import chat_writer
    await chat_writer.drain("chat-1", timeout=5)
    assert temp_db.get_chat("chat-1")["permission_mode"] == runs
