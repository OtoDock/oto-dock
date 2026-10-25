"""otodock-CLI dual-control — proxy-side logic.

Standalone harness (the live proxy deadlocks pytest on the conftest DB pool):
    proxy/venv/bin/python tests/execution/test_dual_control.py

Verifies the attach-to-live primitives on InteractiveSession:
 - find_live_for_chat: newest-alive, target-pinned, ignores dead.
 - the chokepoint input gate: write_input / resize are DROPPED while a local
   `otodock` terminal is the active controller (otodock_attached), and apply
   again once it is cleared.
 - evict_viewer: fires the viewer's evict callback WITH the reason, then clears
   the listener + the single-slot callbacks (output/perm/status stop fanning out).
"""
import asyncio
import os
import sys

import pytest

# Standalone-run bootstrap: proxy/ onto sys.path (tests/<area>/<file>.py -> 3 up).
# Redundant under pytest (conftest handles it); kept for `python <file>` runs.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.session import interactive_session as I

pytestmark = pytest.mark.asyncio


class FakePty:
    def __init__(self):
        self.closed = False
        self.resized = []
        self.written = []

    def resize(self, rows, cols):
        self.resized.append((rows, cols))

    def write(self, data):
        self.written.append(data)

    def scrollback(self):
        return b""

    def close(self, signal_child=True):
        self.closed = True


def _mk(sid, chat_id, *, target="m1", created=None, ready=True):
    s = I.InteractiveSession(
        session_id=sid, chat_id=chat_id, agent_name="a", target=target,
    )
    s.pty = FakePty()
    s._ready = ready
    if created is not None:
        s.created_at = created
    return s


def _check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return bool(cond)


async def test_find_live_for_chat():
    I._sessions.clear()
    a = _mk("sa", "chatA", created=100.0)
    b = _mk("sb", "chatA", created=200.0)        # newer, same chat
    other = _mk("sc", "chatB", created=300.0)
    dead = _mk("sd", "chatA", created=400.0); dead.pty.closed = True  # newest but dead
    elsewhere = _mk("se", "chatA", target="m2", created=500.0)        # other machine
    for s in (a, b, other, dead, elsewhere):
        I._sessions[s.session_id] = s
    try:
        live = I.find_live_for_chat("chatA", target="m1")
        return (
            _check("find: newest-alive on this machine", live is b)
            and _check("find: ignores dead even if newest", live is not dead)
            and _check("find: target filter excludes other machine",
                       I.find_live_for_chat("chatA", target="m2") is elsewhere)
            and _check("find: no match → None", I.find_live_for_chat("nope") is None)
            and _check("find: empty chat_id → None", I.find_live_for_chat("") is None)
        )
    finally:
        I._sessions.clear()


async def test_input_gate():
    s = _mk("sg", "chatG")
    # Controller = dashboard (otodock_attached False): input + resize apply.
    s.write_input(b"hello\r")
    s.resize(40, 120)
    applied = (b"".join(s.pty.written) == b"hello" and s.pty.resized == [(40, 120)])
    # Controller = otodock terminal: dashboard input + resize are DROPPED.
    s.otodock_attached = True
    before_w = list(s.pty.written)
    before_r = list(s.pty.resized)
    s.write_input(b"XXX\r")
    s.resize(10, 10)
    gated = (s.pty.written == before_w and s.pty.resized == before_r
             and (s.rows, s.cols) != (10, 10))
    # Cleared again (dashboard takes back over): input + resize apply.
    s.otodock_attached = False
    s.resize(50, 160)
    reapplied = s.pty.resized[-1] == (50, 160)
    return (
        _check("gate: dashboard input/resize apply when not otodock-controlled", applied)
        and _check("gate: dropped while otodock controls", gated)
        and _check("gate: re-apply after otodock cleared", reapplied)
    )


async def test_evict_viewer():
    s = _mk("sv", "chatV")
    seen = {}

    def _evict(reason):
        seen["reason"] = reason

    s._viewer_evict = _evict
    s._output_listeners.add(lambda d: None)
    s.on_close = lambda *a: None
    s.on_status = lambda *a: None
    s.on_perm_event = lambda *a: None

    s.evict_viewer("superseded_otodock")
    return (
        _check("evict: fired with reason", seen.get("reason") == "superseded_otodock")
        and _check("evict: listeners cleared", not s._output_listeners)
        and _check("evict: viewer_evict cleared", s._viewer_evict is None)
        and _check("evict: on_close/on_status/on_perm cleared",
                   s.on_close is None and s.on_status is None and s.on_perm_event is None)
    )


async def test_interrupt_turn():
    s = _mk("si", "chatI")
    # Idle TUI: no open turn → no stray ESC (codex backtrack / composer clear).
    idle_gated = s.interrupt_turn() is False and s.pty.written == []
    # Open turn under dashboard control → ESC lands in the PTY.
    s._turn_open = True
    esc_sent = s.interrupt_turn() is True and s.pty.written == [b"\x1b"]
    # An attached otodock terminal owns its own keys → dashboard Stop no-ops.
    s.otodock_attached = True
    otodock_gated = s.interrupt_turn() is False and s.pty.written == [b"\x1b"]
    # Dead session → no write, no crash.
    s.otodock_attached = False
    s.pty.closed = True
    dead_gated = s.interrupt_turn() is False and s.pty.written == [b"\x1b"]
    return (
        _check("interrupt: idle turn gated", idle_gated)
        and _check("interrupt: ESC sent on open turn", esc_sent)
        and _check("interrupt: otodock-attached gated", otodock_gated)
        and _check("interrupt: dead session gated", dead_gated)
    )


async def test_submit_tail_scheduling():
    """A submitted prompt (Enter in the terminal) arms the short-fuse
    transcript tails that OPEN the turn — the post-output debounce is starved
    by spinner redraws for the whole turn, so without these the sidebar dot /
    stop button never light for in-terminal sends on local sessions."""
    s = _mk("st", "chatT")
    s._ready = True
    s.write_input(b"do the thing\r")
    armed = len(s._submit_tail_handles) == 2
    # Re-submit re-arms (cancel + replace) — never stacks handles.
    s.write_input(b"again\r")
    rearmed = len(s._submit_tail_handles) == 2
    # Plain typing (no Enter) must NOT arm.
    for h in s._submit_tail_handles:
        h.cancel()
    s._submit_tail_handles = []
    s.write_input(b"still typing")
    not_armed = s._submit_tail_handles == []
    # Pre-ready input is buffered — no tails yet (the cold-submit path arms
    # its own via _fire_submit when the Enter actually lands).
    s2 = _mk("st2", "chatT2")
    s2._ready = False
    s2.write_input(b"early\r")
    cold_gated = s2._submit_tail_handles == []
    return (
        _check("submit tails: armed on terminal Enter", armed)
        and _check("submit tails: re-armed not stacked", rearmed)
        and _check("submit tails: plain typing does not arm", not_armed)
        and _check("submit tails: pre-ready gated", cold_gated)
    )


async def test_mark_attached_read(monkeypatch):
    """Attach clears the unread marker under the chat row's owner identity —
    the scrollback replay just showed any pending answer — and is a silent
    no-op on missing chats / DB failures (never blocks the open)."""
    from core.session import otodock_session as O
    from services.notifications import notification_manager as nm
    from storage import database as db

    marked, fanned = [], []
    monkeypatch.setattr(db, "get_chat", lambda cid: {
        "id": cid, "user_sub": "agent::researcher", "agent": "researcher",
    })
    monkeypatch.setattr(db, "mark_chat_read", lambda cid, ident: marked.append((cid, ident)))
    monkeypatch.setattr(nm, "broadcast_chat_read",
                        lambda owner, cid, agent="": fanned.append((owner, cid, agent)))

    await O._mark_attached_read("chatR")
    ok_marks = (marked == [("chatR", "agent::researcher")]
                and fanned == [("agent::researcher", "chatR", "researcher")])

    # Ownerless / missing chat rows → no marker, no fan-out.
    marked.clear(); fanned.clear()
    monkeypatch.setattr(db, "get_chat", lambda cid: None)
    await O._mark_attached_read("gone")
    ok_missing = marked == [] and fanned == []

    # A DB failure is swallowed (best-effort — the open must proceed).
    def _boom(cid):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_chat", _boom)
    await O._mark_attached_read("chatR")
    ok_guard = marked == [] and fanned == []

    assert (
        _check("mark-read: owner identity + live clear", ok_marks)
        and _check("mark-read: missing chat is a no-op", ok_missing)
        and _check("mark-read: DB failure swallowed", ok_guard)
    )


async def main():
    results = []
    for t in (test_find_live_for_chat, test_input_gate, test_evict_viewer,
              test_interrupt_turn, test_submit_tail_scheduling):
        print(f"\n{t.__name__}:")
        results.append(await t())
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} test groups passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
