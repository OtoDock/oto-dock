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


class FakeWebSocket:
    def __init__(self):
        self.sent: list = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.fixture(autouse=True)
def _clean_registries():
    duplex_attach._states.clear()
    yield
    duplex_attach._states.clear()


def _bridge(chat_id="chat-1", sub="user-admin"):
    return ws_duplex.DuplexBridge(
        duplex_id="dx-1", sub=sub, chat_id=chat_id, max_seconds=1800,
        browser_ws=FakeWebSocket(), engine_ws=FakeWebSocket(),
    )


class _ProbeLayer:
    """Headless layer stub with controllable liveness probes."""

    def __init__(self, *, process_dead=False, alive=True, grace=None):
        self.process_dead = process_dead
        self.alive = alive
        self._grace = grace  # None = no grace concept (local layers)

    async def is_session_process_dead(self, session_id):
        return self.process_dead

    async def is_session_alive(self, session_id):
        return self.alive

    def __getattr__(self, name):
        # is_session_grace_held only exists when the fixture asked for it —
        # mirrors the real layers (remote-only method, getattr-probed).
        if name == "is_session_grace_held" and self._grace is not None:
            return lambda sid: self._grace
        raise AttributeError(name)


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
    machine-side session over a 10s WS blip)."""
    bridge = _bridge()
    st = _attach_state(_ProbeLayer(process_dead=True, grace=True))

    async def _never(chat_id, dead_sid, layer, *, user_sub):
        raise AssertionError("heal must not run during grace")
    monkeypatch.setattr(
        headless_resume, "resume_dead_session_headless", _never)
    dispatched = _capture_dispatch(monkeypatch)

    await duplex_attach.run_utterance(bridge, {"turn": 4, "text": "hello"})

    assert st.session_id == "sid-dead"
    assert dispatched["sid"] == "sid-dead"  # dispatched as-is


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
                                 username=""):
        self.calls.append(f"can_resume:{session_id}")
        return self.resumable

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
        self.interactive = None


def _patch_seam(monkeypatch, spawn_layer, *, interactive=False):
    """Patch the seam's spawn collaborators; returns captured build kwargs."""
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
