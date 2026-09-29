"""Interactive (PTY) shared-chat identity gate.

On a Shared-only agent several users share one chat, but the terminal runs the
CLI under the identity of whoever WARMED it — their JWT, ``OTO_*`` env, platform
role and subscription. So only that user may drive it; anyone else mirrors it
read-only until an explicit take-over.

Pins:
 - ``may_drive`` ownership rules, including the deliberate exemptions
 - ``deliver_dashboard_input`` drops a non-controller's bytes and reports it
 - the server-injection path (``submit_prompt``) is never gated
 - the cold-start first input still lands before any ``pty_attach``
 - user rows persisted by the interactive tailers carry ``author_sub``
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.session import interactive_session as I  # noqa: E402

# InteractiveSession binds the running loop in __init__ (same as test_dual_control).
pytestmark = pytest.mark.asyncio

OWNER = "user-a-sub"
OTHER = "user-b-sub"


class FakePty:
    def __init__(self):
        self.closed = False
        self.written = []

    def resize(self, rows, cols):
        pass

    def write(self, data):
        self.written.append(data)

    def scrollback(self):
        return b""

    def close(self, signal_child=True):
        self.closed = True


def _mk(chat_id="chat-1", *, user_sub=OWNER, ready=True):
    s = I.InteractiveSession(
        session_id="sid-1", chat_id=chat_id, agent_name="a",
        user_sub=user_sub, role="manager", username="alice",
    )
    s.pty = FakePty()
    s._ready = ready
    return s


async def test_owner_drives_and_stranger_is_refused():
    s = _mk()
    assert s.deliver_dashboard_input(b"ls\r", sender_sub=OWNER) is True
    # The submit machinery may split the trailing CR off, so compare the stream.
    assert b"ls" in b"".join(s.pty.written)
    delivered = list(s.pty.written)

    # The whole point: B's keystrokes would otherwise run as A.
    assert s.deliver_dashboard_input(b"rm -rf /\r", sender_sub=OTHER) is False
    assert s.pty.written == delivered


async def test_composer_paste_from_a_stranger_is_refused():
    """pty_attachments arrives as a flagged composer send — same gate."""
    s = _mk()
    paste = b"\x1b[200~look at this\x1b[201~\r"
    assert s.deliver_dashboard_input(paste, composer=True, sender_sub=OTHER) is False
    assert s.pty.written == []


async def test_server_injection_is_never_gated():
    """Scheduler wakes and cold submits carry no human sender — they must pass
    even though they do not match the owner."""
    s = _mk()
    assert s.may_drive("") is True
    s.submit_prompt("delegate finished")
    assert s.pty.written, "server-injected prompt was swallowed"


async def test_ownerless_session_fails_open():
    """Phone/legacy sessions never recorded an owner. Refusing them would be a
    regression with no security gain — they are not reachable from a second
    dashboard user. Deliberate, hence pinned."""
    s = _mk(user_sub="")
    assert s.may_drive(OTHER) is True
    assert s.deliver_dashboard_input(b"x", sender_sub=OTHER) is True


async def test_task_chats_are_exempt():
    """A task chat carries its OWN authorization model (``_task_continue_allowed``
    is role-based on purpose, so delegate lanes stay steerable by whoever holds
    the role). An ownership gate on top would break live lane steering. A
    meeting never runs an interactive session, so there is no meeting row here
    (core-seams phase 4 dropped the dead ``meeting-`` exemption)."""
    s = _mk("task-run-42")
    assert s.may_drive(OTHER) is True
    assert s.deliver_dashboard_input(b"steer\r", sender_sub=OTHER) is True


async def test_cold_start_first_input_lands_before_attach():
    """Input routes by session id, so the warmer's first prompt arrives BEFORE
    the pty_attach handshake. The gate must not swallow it."""
    s = _mk(ready=False)  # TUI not ready yet → buffered, not dropped
    assert s.deliver_dashboard_input(b"hello\r", sender_sub=OWNER) is True
    assert s._input_buffer == [b"hello\r"]


async def test_takeover_target_is_not_the_controller():
    """Sanity for the take-over handler's guard: it must no-op for the user who
    already controls the session, and engage for anyone else."""
    s = _mk()
    assert s.may_drive(OWNER) is True    # nothing to take
    assert s.may_drive(OTHER) is False   # take-over path


async def test_author_sub_for_resolves_the_controller():
    """Interactive turns never pass the -p send path that stamps author_sub, so
    the tailer resolves it from the session registry."""
    from core.session import transcript_tailer

    s = _mk()
    I._sessions[s.session_id] = s
    try:
        assert transcript_tailer.author_sub_for(s.session_id) == OWNER
        # Headless sessions are not in the registry — their rows are stamped at
        # send time and must stay untouched.
        assert transcript_tailer.author_sub_for("headless-sid") == ""
    finally:
        I._sessions.pop(s.session_id, None)


async def test_a_mirror_keeps_its_listener_and_learns_of_the_close():
    """A read-only mirror survives a controller change and is told when the
    session closes (a take-over is a close: the requester's terminal re-warms
    on that notice)."""
    s = _mk()
    seen: list[str] = []
    mirror_out = []
    ctrl_out = []

    async def _mirror(data): mirror_out.append(data)
    async def _ctrl(data): ctrl_out.append(data)
    async def _ctrl2(data): ctrl_out.append(data)
    async def _mirror_closed(_s, reason): seen.append(f"mirror:{reason}")
    async def _ctrl_closed(_s, reason): seen.append(f"ctrl:{reason}")
    def _evict(reason): seen.append(f"evict:{reason}")

    s.add_output_listener(_mirror, on_close=_mirror_closed)
    s.on_close = _ctrl_closed
    s.add_output_listener(_ctrl, on_evict=_evict)
    # A second controller evicts only the first controller; the mirror stays.
    s.add_output_listener(_ctrl2, on_evict=lambda r: None)
    assert _mirror in s._output_listeners and _ctrl not in s._output_listeners
    assert seen == ["evict:superseded"]
    # The controller's own detach frees the evict slot, so its re-attach on
    # the same socket evicts nobody.
    s.remove_output_listener(_ctrl2)
    assert s._viewer_evict is None and _mirror in s._output_listeners
    s.add_output_listener(_ctrl2, on_evict=_evict)
    assert seen == ["evict:superseded"]

    await s.close(reason="taken_over")
    assert "mirror:taken_over" in seen and "ctrl:taken_over" in seen
