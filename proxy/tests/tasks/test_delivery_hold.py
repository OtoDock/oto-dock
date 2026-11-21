"""The delivery's rungs under a planned stop, and the wake sweep beside the
continue pass (``services/scheduler/delivery.py``)."""

import asyncio

from core import drain
from services.scheduler import delivery
from storage import database as task_store
from storage.chat import cut_records
from tests.fixtures.wake_defaults import WAKE


def test_no_rung_starts_a_turn_while_new_work_is_held(temp_db):
    """A result or a wake into an idle or a dead session is a new turn: under
    a hold both rungs answer that they could not deliver, so the caller
    stores the wake for the boot's sweep."""
    async def scene():
        await drain.drain(60)
        persistent = await delivery._deliver_via_persistent(
            "sid", "pa", "the result", user_sub=None, role="manager", chat_id="c-hold")
        oneshot = await delivery._deliver_via_oneshot(
            "sid", "pa", "the result", user_sub=None, role="manager", chat_id="c-hold", **WAKE)
        await drain.resume()
        return persistent, oneshot

    assert asyncio.run(scene()) == (None, None)


def test_a_live_terminal_and_a_live_socket_take_no_turn_while_new_work_is_held(temp_db, monkeypatch):
    """A delegate result into a chat with a live terminal or a socket waits as
    any other: the ladder answers none and the caller stores the wake."""
    from core.session import interactive_session, session_delivery
    typed: list[str] = []

    class _Terminal:
        session_id, alive = "sid-hold", True

        def queue_prompt(self, text, source, **kw):
            typed.append(source)
            return True
    terminal = _Terminal()
    monkeypatch.setattr(interactive_session, "find_live_for_chat", lambda chat_id: terminal)
    monkeypatch.setattr(interactive_session, "get", lambda sid: terminal)
    task_store.create_chat("c-rungs", "user-1", "pa", "default")
    task_store.update_chat("c-rungs", session_id="sid-hold")
    notify: asyncio.Queue = asyncio.Queue()
    from core.session import session_state
    monkeypatch.setitem(session_state._dashboard_notify_queues, "sid-hold", notify)

    async def scene():
        await drain.drain(60)
        outcome = await session_delivery.deliver_prompt(
            "c-rungs", "the result", source="delegate_result", session_id="sid-hold", agent="pa",
            user_sub=None, role="manager", notify_payload={"type": "x"},
            persistent_fn=delivery._deliver_via_persistent, oneshot_fn=delivery._deliver_via_oneshot)
        await drain.resume()
        return outcome

    assert asyncio.run(scene()).path == "none"
    assert typed == [] and notify.empty()


def test_the_wake_sweep_leaves_a_chat_whose_cut_turn_is_not_settled(temp_db, monkeypatch):
    """A wake that started a session first would take the cut turn's place:
    the chat is the continue pass's until its record is settled."""
    task_store.create_chat("c-cut", "user-admin", "pa")
    task_store.append_pending_delegate_wake("c-cut", "a result")
    record = cut_records.add("c-cut", cause="crash", state=cut_records.PENDING, kind="dashboard")
    asked: list[str] = []

    async def _group(chat, prompts, **kw):
        asked.append(chat["id"])
        return True
    monkeypatch.setattr(delivery, "_redeliver_group", _group)

    assert asyncio.run(delivery._redeliver_chat(task_store.get_chat("c-cut"))) is False
    assert asked == []
    assert len(task_store.claim_pending_wake_records("c-cut", notes=True)) == 1
    task_store.append_pending_delegate_wake("c-cut", "a result")
    cut_records.settle(record["id"], cut_records.CONTINUED)
    assert asyncio.run(delivery._redeliver_chat(task_store.get_chat("c-cut"))) is True
    assert asked == ["c-cut"]


def test_a_second_start_of_one_session_on_a_machine_is_refused(monkeypatch):
    """A machine budgets its own sessions, so its target takes no slot. The
    remote layer cannot tell a second start from a re-warm of a session it
    holds: the second start stops here, and its caller keeps its prompt."""
    from core import concurrency
    from core.session import session_state

    async def reserve(session_id, superseded=lambda: False):
        return await concurrency.reserve_background(
            session_id, target="machine-1", execution_path="claude-code-cli", timeout_s=1.0,
            superseded=superseded)

    monkeypatch.setattr(session_state, "session_is_held", lambda sid: sid == "held")
    assert asyncio.run(reserve("free")) == "untracked"
    assert asyncio.run(reserve("held")) == "superseded"
    assert asyncio.run(reserve("free", lambda: True)) == "superseded"
    session_state.mark_starting("starting", 30.0)
    try:
        assert asyncio.run(reserve("starting")) == "superseded"
    finally:
        session_state.clear_starting("starting")


def test_a_keys_turn_holds_the_chat_against_every_other_starter_but_itself(temp_db):
    from core.session import key_rows
    from services.scheduler import wake_session

    async def own_turn() -> bool:
        key_rows.hold("c-key", "req-1")
        # Its own start, also from a task of its own.
        return await asyncio.create_task(asyncio.to_thread(wake_session._wake_superseded, "", "c-key"))

    async def scene() -> tuple[bool, bool]:
        own = await asyncio.create_task(own_turn())
        return own, wake_session._wake_superseded("", "c-key")

    try:
        assert asyncio.run(scene()) == (False, True)
    finally:
        key_rows.release("c-key", "req-1")
    assert wake_session._wake_superseded("", "c-key") is False
