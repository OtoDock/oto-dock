"""A terminal's turn on its chat's open-turn row
(``interactive_turn._note_turn_row``): written when the turn opens, deleted
when it closes, kept by a close for the platform's stop."""

import asyncio

import pytest

from core import drain
from core.events import chat_writer
from core.session import session_state
from core.session.interactive_session import InteractiveSession
from services.scheduler import cut_turns
from storage import database as task_store
from storage.chat import open_turns

pytestmark = pytest.mark.asyncio


def _terminal(chat_id: str, sid: str = "sid-term") -> InteractiveSession:
    s = InteractiveSession.__new__(InteractiveSession)
    s.session_id, s.chat_id = sid, chat_id
    s._turn_token = ""
    s.on_turn_complete = None
    return s


async def test_the_turn_opens_and_closes_the_chats_row(temp_db):
    task_store.create_chat("term-a", "user-admin", "a1")
    s = _terminal("term-a")
    s._note_turn_row(True)
    await chat_writer.drain("term-a", timeout=5)
    row = open_turns.get_open("term-a")
    assert (row["kind"], row["session_id"], row["turn_token"]) == (
        cut_turns.KIND_TERMINAL, "sid-term", s._turn_token)
    s._note_turn_row(False)
    await chat_writer.drain("term-a", timeout=5)
    assert open_turns.get_open("term-a") is None and s._turn_token == ""


async def test_a_tasks_terminal_writes_no_row(temp_db, monkeypatch):
    """A task's terminal is its run's: the run is what the boot reopens."""
    task_store.create_chat("term-task", "user-admin", "a1")
    task_store.create_chat("task-run-9", "user-admin", "a1")
    by_callback = _terminal("term-task")
    by_callback.on_turn_complete = lambda msg: None
    by_callback._note_turn_row(True)
    by_kind = _terminal("term-task", "sid-task")
    monkeypatch.setattr(session_state, "get_session_client_type",
                        lambda sid: "task" if sid == "sid-task" else "")
    by_kind._note_turn_row(True)
    by_chat = _terminal("task-run-9")
    by_chat._note_turn_row(True)
    no_chat = _terminal("")
    no_chat._note_turn_row(True)
    await chat_writer.drain("term-task", timeout=5)
    await chat_writer.drain("task-run-9", timeout=5)
    assert open_turns.list_open() == []


async def test_the_transition_writes_the_row_and_the_platforms_stop_keeps_it(temp_db):
    """``_set_turn_open`` is the one place a turn opens and closes. A close
    for the platform's stop does not go through it, so the row stays for the
    boot."""
    import inspect

    from core.session import interactive_session, interactive_turn
    setter = inspect.getsource(interactive_turn.InteractiveTurnMixin._set_turn_open)
    assert "self._note_turn_row(is_open)" in setter
    close = inspect.getsource(interactive_session.InteractiveSession.close)
    kept = close.index("if deleted or reason == PLATFORM_STOPPING:")
    assert close.index("self._set_turn_open(False)") > kept
    assert interactive_session.PLATFORM_STOPPING == drain.PLATFORM_STOPPING == "platform-stopping"


async def test_close_all_for_the_stop_closes_local_terminals_side_by_side(monkeypatch):
    from types import SimpleNamespace

    from core.session import interactive_registry
    order: list[str] = []
    gate = asyncio.Event()

    def _session(sid: str, *, remote: bool, slow: bool = False):
        async def close(reason: str) -> None:
            order.append(f"{sid} starts {reason}")
            if slow:
                await gate.wait()
            order.append(f"{sid} done")
        return SimpleNamespace(session_id=sid, is_remote=remote, close=close)

    monkeypatch.setattr(interactive_registry, "_sessions", {
        "a": _session("a", remote=False, slow=True), "b": _session("b", remote=False),
        "m": _session("m", remote=True)})
    await interactive_registry.close_all(drain.PLATFORM_STOPPING, local_only=True, timeout=0.2)
    # Both local closes started before either was waited for, the slow one
    # did not hold the stop past its bound, and the machine's was not closed.
    assert order == ["a starts platform-stopping", "b starts platform-stopping", "b done"]
    gate.set()


async def test_a_terminal_a_wake_warmed_keeps_its_turns_on_the_row(temp_db):
    """The re-warm sets the callback a task's terminal has. The chat's terminal
    still needs its row, or a second stop would cut a turn with no record."""
    task_store.create_chat("term-woken", "user-admin", "a1")
    s = _terminal("term-woken")
    s.on_turn_complete = lambda msg: None
    s.chat_terminal_rewarmed = True
    for _ in range(2):
        s._note_turn_row(True)
        await chat_writer.drain("term-woken", timeout=5)
        assert open_turns.get_open("term-woken")["kind"] == cut_turns.KIND_TERMINAL
        s._note_turn_row(False)
        await chat_writer.drain("term-woken", timeout=5)
        assert open_turns.get_open("term-woken") is None
