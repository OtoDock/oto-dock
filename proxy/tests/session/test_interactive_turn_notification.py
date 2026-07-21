"""Interactive end-of-turn notification behaviour.

Verifies ``InteractiveSession._maybe_fire_turn_complete`` fires the user-facing
end-of-turn notification (``broadcast_chat_status('ready')`` + ``fire_ephemeral``)
exactly the way the ``-p`` pump does, with the right per-turn dedup + gates.

The collaborators the session imports lazily (notification_manager /
title_generator / session_state) are monkeypatched onto their REAL modules, so
this test never touches the DB and never mutates ``sys.modules`` (which would
leak into other test modules).
"""
import asyncio
from collections import deque

import pytest

from core.session.interactive_session import InteractiveSession, MIN_TURN_S

pytestmark = pytest.mark.asyncio

# Recorded calls into the (patched) collaborators.
_calls = {"ready": [], "ephemeral": [], "title": [], "routing": [], "status": [],
          "read": [], "read_bcast": []}


def _fake_broadcast(user_sub, chat_id, status, agent=""):
    if status == "ready":
        _calls["ready"].append((user_sub, chat_id))
    _calls["status"].append((user_sub, chat_id, status, agent))


def _fake_mark_read(chat_id, owner_identity):
    _calls["read"].append((chat_id, owner_identity))


def _fake_broadcast_read(user_sub, chat_id, agent=""):
    _calls["read_bcast"].append((user_sub, chat_id, agent))


async def _fake_fire_ephemeral(user_sub, title, body, chat_id=None,
                               interactive=False, cli_attached=False):
    _calls["ephemeral"].append((user_sub, title, chat_id))
    _calls["routing"].append((interactive, cli_attached))


async def _fake_request_title(chat_id, assistant_excerpt=""):
    _calls["title"].append(chat_id)


class _Reg:
    has_pending = False


def _fake_get_registry(_sid):
    return _Reg()


@pytest.fixture(autouse=True)
def _patch_collaborators(monkeypatch):
    """Patch the session's lazily-imported collaborators on their real modules.

    The session does ``from services.notifications import notification_manager``
    (etc.) at call time, so patching the real module attributes here is enough —
    no ``sys.modules`` stubbing, no leakage, automatic teardown via monkeypatch.
    """
    for v in _calls.values():
        v.clear()
    _Reg.has_pending = False

    from services.notifications import notification_manager as _nm
    from services import title_generator as _tg
    from core.session import session_state as _ss
    from storage import database as _db

    monkeypatch.setattr(_nm, "broadcast_chat_status", _fake_broadcast)
    monkeypatch.setattr(_nm, "broadcast_chat_read", _fake_broadcast_read)
    monkeypatch.setattr(_nm, "fire_ephemeral", _fake_fire_ephemeral)
    monkeypatch.setattr(_tg, "request_chat_title", _fake_request_title)
    monkeypatch.setattr(_ss, "get_subagent_registry", _fake_get_registry)
    monkeypatch.setattr(_db, "mark_chat_read", _fake_mark_read)


async def _drain():
    """Let the ``create_task``'d fire_ephemeral / title coroutines complete."""
    for _ in range(10):
        await asyncio.sleep(0)
        if not [t for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done()]:
            break


def _new_session(*, chat_id="chat-1", on_turn_complete=None, old=True,
                 otodock_attached=False):
    """Build an InteractiveSession bypassing register() (no lease/slot/DB)."""
    s = InteractiveSession.__new__(InteractiveSession)
    s.session_id = "sid-abcdef01"
    s.chat_id = chat_id
    s.agent_name = "researcher"
    s.user_sub = "user-1"
    s.target = "local"
    s.otodock_attached = otodock_attached
    s._loop = asyncio.get_running_loop()
    # Spawned long ago so MIN_TURN_S is satisfied (unless `old=False`).
    s.created_at = (
        asyncio.get_running_loop().time() - (MIN_TURN_S + 5) if old
        else asyncio.get_running_loop().time()
    )
    s._title_fired = False
    s._turn_complete_fired = False
    s.on_turn_complete = on_turn_complete
    # Server-prompt injection state read by _maybe_fire_turn_complete's drain.
    s._prompt_queue = deque()
    s._closing = False
    # Turn-open transition state (sidebar live-dot broadcasts). No DB in these
    # tests: _chat_owner() falls back to user_sub when get_chat raises.
    s._turn_open = False
    s._chat_owner_sub = None
    return s


async def test_chat_turn_fires_once_per_turn():
    s = _new_session()
    s._maybe_fire_turn_complete("done", persisted=1)
    await _drain()
    assert _calls["ready"] == [("user-1", "chat-1")], _calls["ready"]
    assert _calls["ephemeral"] == [("user-1", "researcher finished", "chat-1")], _calls["ephemeral"]
    assert _calls["title"] == ["chat-1"], _calls["title"]


async def test_question_pending_words_ping_as_needs_input():
    # The CLI parked on an AskUserQuestion: same close path, reworded ping.
    s = _new_session()
    s._maybe_fire_turn_complete("", persisted=1, question=True)
    await _drain()
    assert _calls["ready"] == [("user-1", "chat-1")], _calls["ready"]
    assert _calls["ephemeral"] == [
        ("user-1", "researcher needs your input", "chat-1")
    ], _calls["ephemeral"]


async def test_question_never_fires_task_callback():
    # Defense in depth: hooks deny AskUserQuestion for autonomous tasks, but a
    # question fold must never count as a task completion regardless.
    done = []
    s = _new_session(on_turn_complete=done.append)
    s._maybe_fire_turn_complete("", persisted=1, question=True)
    await _drain()
    assert done == []
    assert s._turn_complete_fired is False


async def test_persisted_zero_is_deduped():
    s = _new_session()
    # A re-read with no NEW lines (cursor already past the end_turn line).
    s._maybe_fire_turn_complete("done", persisted=0)
    await _drain()
    assert _calls["ready"] == [], _calls["ready"]
    assert _calls["ephemeral"] == [], _calls["ephemeral"]
    # Title still fires once (its own flag, persisted-independent).
    assert _calls["title"] == ["chat-1"], _calls["title"]


async def test_bg_pending_holds_notification():
    _Reg.has_pending = True
    s = _new_session()
    s._maybe_fire_turn_complete("partial", persisted=1)
    await _drain()
    assert _calls["ready"] == [], _calls["ready"]
    assert _calls["ephemeral"] == [], _calls["ephemeral"]


async def test_min_turn_time_guard():
    s = _new_session(old=False)  # just spawned
    s._maybe_fire_turn_complete("done", persisted=1)
    await _drain()
    assert _calls["ready"] == [], _calls["ready"]
    assert _calls["ephemeral"] == [], _calls["ephemeral"]


async def test_meeting_chat_suppressed():
    s = _new_session(chat_id="meeting-7")
    s._maybe_fire_turn_complete("done", persisted=1)
    await _drain()
    assert _calls["ephemeral"] == [], _calls["ephemeral"]


async def test_task_run_suppresses_ping_but_fires_callback():
    fired = []
    s = _new_session(chat_id="task-99", on_turn_complete=fired.append)
    s._maybe_fire_turn_complete("result", persisted=1)
    await _drain()
    # An autonomous task run keeps the run callback + the ready broadcast, but
    # never the user ping — completion is the task's notification_mode contract.
    assert _calls["ephemeral"] == [], _calls["ephemeral"]
    assert _calls["ready"] == [("user-1", "task-99")], _calls["ready"]
    assert fired == ["result"], fired
    assert s._turn_complete_fired is True
    # Second turn-end: callback is fire-once, ping stays suppressed.
    s._maybe_fire_turn_complete("result2", persisted=1)
    await _drain()
    assert fired == ["result"], fired
    assert _calls["ephemeral"] == [], _calls["ephemeral"]


async def test_rewarmed_task_chat_keeps_ping():
    # A continued (re-warmed) task chat is a plain session on the task's chat_id
    # (no scheduler callback) — its follow-up turns keep the end-of-turn ping,
    # which is the only completion signal they have.
    s = _new_session(chat_id="task-99", on_turn_complete=None)
    s._maybe_fire_turn_complete("done", persisted=1)
    await _drain()
    assert _calls["ephemeral"] == [("user-1", "researcher finished", "task-99")], _calls["ephemeral"]


async def test_dashboard_driven_routing_flags():
    s = _new_session()
    s._maybe_fire_turn_complete("done", persisted=1)
    await _drain()
    assert _calls["routing"] == [(True, False)], _calls["routing"]


async def test_otodock_attached_routing_flags():
    # An otodock-CLI attachment rides fire_ephemeral's cli_attached rule: it is
    # not dashboard presence and must never suppress the phone push.
    s = _new_session(otodock_attached=True)
    s._maybe_fire_turn_complete("done", persisted=1)
    await _drain()
    assert _calls["routing"] == [(True, True)], _calls["routing"]


async def test_empty_user_sub_noops():
    s = _new_session()
    s.user_sub = ""
    s._maybe_fire_turn_complete("done", persisted=1)
    await _drain()
    assert _calls["ready"] == [] and _calls["ephemeral"] == [], (_calls["ready"], _calls["ephemeral"])


# ---------------------------------------------------------------------------
# Turn-open transitions drive the sidebar live-dot (streaming/ready)
# ---------------------------------------------------------------------------

async def test_turn_open_transitions_broadcast_status():
    s = _new_session()
    s._apply_turn_signal("user")
    assert ("user-1", "chat-1", "streaming", "researcher") in _calls["status"]
    # Same-state signals are not transitions — no repeat broadcast.
    s._apply_turn_signal("tool_use")
    assert len([c for c in _calls["status"] if c[2] == "streaming"]) == 1
    s._apply_turn_signal("end_turn")
    assert ("user-1", "chat-1", "ready", "researcher") in _calls["status"]


async def test_meeting_turn_transitions_silent():
    s = _new_session(chat_id="meeting-7")
    s._apply_turn_signal("user")
    s._apply_turn_signal("end_turn")
    assert _calls["status"] == [], _calls["status"]


async def test_whole_turn_in_one_batch_still_ends():
    # A quick turn can land in a SINGLE tailer batch (fresh sessions surface
    # their transcript late) — the fold sees only end_turn and the open
    # transition never happens. The end effects (ready broadcast + stamp)
    # must still run or the turn never lights the unread dot.
    s = _new_session()
    s._apply_turn_signal("end_turn")
    assert ("user-1", "chat-1", "ready", "researcher") in _calls["status"]
    assert not s._turn_open


async def test_turn_end_attached_marks_read():
    # The answer rendered on a live attached otodock terminal — that IS the
    # read (the dashboard's visible-tab rule, terminal edition): the marker
    # upsert + live clear fire right after the ready broadcast.
    s = _new_session(otodock_attached=True)
    s._apply_turn_signal("end_turn")
    assert _calls["read"] == [("chat-1", "user-1")], _calls["read"]
    assert _calls["read_bcast"] == [("user-1", "chat-1", "researcher")], _calls["read_bcast"]


async def test_turn_end_detached_keeps_unread():
    # Nobody saw the answer (no otodock terminal attached — dashboard-driven
    # or detached session): the unread dot must survive until a real open.
    s = _new_session()
    s._apply_turn_signal("end_turn")
    assert _calls["read"] == [], _calls["read"]
    assert _calls["read_bcast"] == [], _calls["read_bcast"]
