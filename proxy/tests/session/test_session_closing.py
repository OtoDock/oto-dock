"""A session's close and the next start of its id never overlap
(``core/session/session_closing.py``)."""

from __future__ import annotations

import asyncio
import inspect
import time
from unittest.mock import MagicMock

import pytest

from core.session import session_closing, session_state


@pytest.fixture(autouse=True)
def _no_holds():
    session_closing.reset()
    yield
    session_closing.reset()


class TestTheHold:
    @pytest.mark.asyncio
    async def test_a_start_with_no_close_in_flight_goes_at_once(self):
        began = time.monotonic()
        await session_closing.wait_closed("sid-none")
        assert time.monotonic() - began < 0.5

    @pytest.mark.asyncio
    async def test_a_start_waits_until_the_close_ends(self):
        order: list[str] = []
        release = asyncio.Event()

        async def _close() -> None:
            with session_closing.closing("sid-1"):
                order.append("close begins")
                await release.wait()
                order.append("close ends")

        async def _start() -> None:
            await session_closing.wait_closed("sid-1")
            order.append("start")

        close = asyncio.create_task(_close())
        await asyncio.sleep(0)
        start = asyncio.create_task(_start())
        await asyncio.sleep(0.15)
        assert order == ["close begins"]
        release.set()
        await asyncio.gather(close, start)
        assert order == ["close begins", "close ends", "start"]

    @pytest.mark.asyncio
    async def test_two_closes_of_one_id_both_hold(self):
        with session_closing.closing("sid-2"):
            with session_closing.closing("sid-2"):
                assert session_closing.is_closing("sid-2")
            assert session_closing.is_closing("sid-2")
        assert not session_closing.is_closing("sid-2")
        assert "sid-2" not in session_closing._holds

    @pytest.mark.asyncio
    async def test_a_close_that_raises_lets_go(self):
        with pytest.raises(RuntimeError):
            with session_closing.closing("sid-3"):
                raise RuntimeError("the machine did not answer")
        assert not session_closing.is_closing("sid-3")

    @pytest.mark.asyncio
    async def test_another_ids_close_holds_nothing(self):
        with session_closing.closing("sid-4"):
            await session_closing.wait_closed("sid-other")

    @pytest.mark.asyncio
    async def test_a_close_that_hangs_holds_only_for_the_bound(self, monkeypatch):
        monkeypatch.setattr(session_closing, "HOLD_MAX_S", 0.2)
        with session_closing.closing("sid-5"):
            began = time.monotonic()
            await session_closing.wait_closed("sid-5")
            assert 0.15 <= time.monotonic() - began < 2.0


def _remote_layer(tmp_path, ack: asyncio.Event):
    """A real remote layer over a link whose close ack waits for ``ack``."""
    from core.remote import remote_execution as re_mod

    cm = MagicMock()
    cm.is_connected.return_value = True

    async def _send(machine_id, frame, timeout=30.0):
        await ack.wait()
        return {"ok": True}

    cm.send_command = _send
    layer = re_mod.RemoteExecutionLayer(cm)
    prompt = tmp_path / "system-prompt-abc.json"
    prompt.write_text("old")
    info = re_mod.RemoteSessionInfo(
        session_id="sess-overlap", machine_id="m-1", agent_name="agent-x",
        execution_path="claude-code-cli", event_queue=asyncio.Queue(),
    )
    info.prompt_file = str(prompt)
    layer._sessions["sess-overlap"] = info
    return layer, prompt


class TestTheRemoteLayer:
    @pytest.mark.asyncio
    async def test_a_start_inside_a_running_close_keeps_its_file_and_its_state(
            self, tmp_path, monkeypatch):
        """The walk's case: the reaper closes a session, the chat starts
        again under the same id before the machine acked the close. Before
        the hold the close's tail removed the new start's prompt file and
        cleared its state."""
        from core.remote import remote_session_start as start_mod
        ack = asyncio.Event()
        layer, prompt = _remote_layer(tmp_path, ack)

        async def _impl(self, session_id, config, registered):
            # What a start puts under the id: its prompt file and its mode.
            prompt.write_text("new")
            session_state._session_modes[session_id] = "ask"
            registered.append(True)

        monkeypatch.setattr(start_mod.RemoteSessionStartMixin, "_start_session_impl", _impl)
        close = asyncio.create_task(layer.close_session("sess-overlap"))
        await asyncio.sleep(0.05)          # the close waits for the machine's ack
        start = asyncio.create_task(layer.start_session("sess-overlap", MagicMock()))
        await asyncio.sleep(0.2)
        assert not start.done(), "the start ran inside the close"
        ack.set()
        await asyncio.gather(close, start)
        try:
            assert prompt.read_text() == "new"
            assert session_state._session_modes.get("sess-overlap") == "ask"
        finally:
            session_state._session_modes.pop("sess-overlap", None)

    @pytest.mark.asyncio
    async def test_a_close_of_an_id_the_layer_does_not_hold_takes_no_hold(self, tmp_path):
        layer, _prompt = _remote_layer(tmp_path, asyncio.Event())
        await layer.close_session("sess-unknown")
        assert not session_closing.is_closing("sess-unknown")

    @pytest.mark.asyncio
    async def test_an_adoption_waits_for_the_close_too(self, tmp_path):
        ack = asyncio.Event()
        layer, _prompt = _remote_layer(tmp_path, ack)
        close = asyncio.create_task(layer.close_session("sess-overlap"))
        await asyncio.sleep(0.05)
        adopt = asyncio.create_task(layer.adopt_idle_session(
            machine_id="m-1", session_id="sess-overlap", agent_name="agent-x",
            execution_path="no-such-engine"))
        await asyncio.sleep(0.2)
        assert not adopt.done(), "the adoption ran inside the close"
        ack.set()
        await close
        adopt.cancel()
        await asyncio.gather(adopt, return_exceptions=True)


class TestTheLayerInTheProxy:
    """Direct LLM's sessions run in the proxy. Its close that finds no
    session while a start of the id is in flight clears nothing: what is
    under the id is the start's."""

    @pytest.mark.asyncio
    async def test_a_close_during_a_start_keeps_the_starts_state(self):
        from core.layers.direct.layer import DirectLLMExecutionLayer
        layer = DirectLLMExecutionLayer()
        sid = "sess-starting-direct"
        session_state.mark_starting(sid, 30)
        session_state._session_modes[sid] = "ask"
        try:
            await layer.close_session(sid)
            assert session_state._session_modes.get(sid) == "ask"
        finally:
            session_state.clear_starting(sid)
            session_state._session_modes.pop(sid, None)

    @pytest.mark.asyncio
    async def test_a_close_with_no_start_in_flight_clears_as_before(self):
        from core.layers.direct.layer import DirectLLMExecutionLayer
        sid = "sess-not-starting"
        session_state._session_modes[sid] = "ask"
        await DirectLLMExecutionLayer().close_session(sid)
        assert sid not in session_state._session_modes


def _layers():
    """The layers that hold sessions: a machine's and Direct LLM's."""
    from core.layers.direct.layer import DirectLLMExecutionLayer
    from core.remote.remote_execution import RemoteExecutionLayer
    return [DirectLLMExecutionLayer, RemoteExecutionLayer]


@pytest.mark.parametrize("layer", _layers(), ids=lambda c: c.__name__)
def test_every_layer_waits_at_its_start_and_holds_at_its_close(layer):
    """A new engine layer, or a rewrite of one, keeps both lines."""
    start = inspect.getsource(layer.start_session)
    assert "session_closing.wait_closed(session_id)" in start, f"{layer.__name__}.start_session"
    close = inspect.getsource(layer.close_session)
    assert "session_closing.closing(session_id)" in close, f"{layer.__name__}.close_session"


def test_both_adoptions_wait_for_a_close():
    from core.remote.remote_resume import RemoteResumeMixin
    for fn in (RemoteResumeMixin.adopt_session, RemoteResumeMixin.adopt_idle_session):
        assert "session_closing.wait_closed(session_id)" in inspect.getsource(fn), fn.__name__
