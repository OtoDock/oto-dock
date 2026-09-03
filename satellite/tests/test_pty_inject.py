"""Server-prompt stdin injection into otodock-attached PTY sessions.

The proxy's delegation-delivery PTY rung sends ``pty_inject``; the satellite
writes the prompt into the CLI's stdin as a VISIBLE bracketed paste + Enter,
gated on what only it can see — the local terminal's pending input line and
the transcript burst state — within a bounded window, then NACKs ``busy``
(the proxy owns the turn-state decision and re-queues). Every request is
answered with a ``pty_inject_result``; recent inject_ids are deduped so a
proxy re-send after a lost result frame re-ACKs without re-injecting.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from satellite.terminal import pty_session_base
from satellite.terminal.pty_session_base import BasePtySession


class _CapturingWS:
    def __init__(self):
        self.sent = []

    async def enqueue_send(self, msg):
        self.sent.append(msg)


class _FakePty:
    def __init__(self):
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)


def _make_session(tmp_path, ws, *, attached=True) -> BasePtySession:
    agent_dir = tmp_path / "agents" / "test-agent"
    agent_dir.mkdir(parents=True)
    sess = BasePtySession(
        "sess-inj", agent_dir, config={}, sat_config=SimpleNamespace(), ws=ws,
    )
    sess.pty = _FakePty()
    # Injectable baseline: transcript located, no burst, local line clean +
    # last keystroke long ago, local relay attached.
    sess._transcript_path = Path(tmp_path / "transcript.jsonl")
    sess._files_dirty = False
    sess._local_line_dirty = False
    sess._last_local_input = time.monotonic() - 60
    if attached:
        sess.attach_local_relay(lambda b: None, lambda c: None)
    return sess


def _results(ws):
    return [m for m in ws.sent if m["type"] == "pty_inject_result"]


@pytest.fixture(autouse=True)
def _fast_timers(monkeypatch):
    monkeypatch.setattr(pty_session_base, "_INJECT_RETRY_S", 0.02)
    monkeypatch.setattr(pty_session_base, "_INJECT_MAX_WAIT_S", 0.1)
    monkeypatch.setattr(pty_session_base, "_INJECT_ENTER_DELAY_S", 0.01)


@pytest.mark.asyncio
async def test_happy_path_paste_then_enter(tmp_path):
    ws = _CapturingWS()
    sess = _make_session(tmp_path, ws)

    await sess.inject_prompt("run the report\nwith details", "inj-1", "delegate_result")

    assert sess.pty.writes[0] == b"\x1b[200~run the report\nwith details\x1b[201~"
    assert sess.pty.writes[1] == b"\r"                # Enter split from the body
    res = _results(ws)
    assert len(res) == 1
    assert res[0]["ok"] is True and res[0]["inject_id"] == "inj-1"


@pytest.mark.asyncio
async def test_duplicate_inject_id_reacks_without_reinjecting(tmp_path):
    ws = _CapturingWS()
    sess = _make_session(tmp_path, ws)

    await sess.inject_prompt("the prompt", "inj-dup", "delegate_result")
    writes_after_first = list(sess.pty.writes)
    await sess.inject_prompt("the prompt", "inj-dup", "delegate_result")

    assert sess.pty.writes == writes_after_first      # nothing written again
    res = _results(ws)
    assert [r["ok"] for r in res] == [True, True]
    assert res[1]["reason"] == "duplicate"


@pytest.mark.asyncio
async def test_nack_not_attached(tmp_path):
    ws = _CapturingWS()
    sess = _make_session(tmp_path, ws, attached=False)

    await sess.inject_prompt("p", "inj-na", "delegate_result")

    assert sess.pty.writes == []
    res = _results(ws)
    assert res[0]["ok"] is False and res[0]["reason"] == "not_attached"


@pytest.mark.asyncio
async def test_nack_session_gone(tmp_path):
    ws = _CapturingWS()
    sess = _make_session(tmp_path, ws)
    sess.pty.closed = True

    await sess.inject_prompt("p", "inj-sg", "delegate_result")

    res = _results(ws)
    assert res[0]["ok"] is False and res[0]["reason"] == "session_gone"


@pytest.mark.asyncio
async def test_dirty_line_times_out_busy(tmp_path):
    ws = _CapturingWS()
    sess = _make_session(tmp_path, ws)
    sess._local_line_dirty = True                     # user mid-thought, never clears

    await sess.inject_prompt("p", "inj-busy", "delegate_result")

    assert sess.pty.writes == []
    res = _results(ws)
    assert res[0]["ok"] is False and res[0]["reason"] == "busy"


@pytest.mark.asyncio
async def test_transcript_burst_defers_then_injects(tmp_path):
    ws = _CapturingWS()
    sess = _make_session(tmp_path, ws)
    sess._files_dirty = True                          # mid-burst at request time

    async def _quiet_soon():
        await asyncio.sleep(0.04)
        sess._files_dirty = False

    task = asyncio.create_task(_quiet_soon())
    await sess.inject_prompt("deferred prompt", "inj-def", "delegate_result")
    await task

    assert any(b"deferred prompt" in w for w in sess.pty.writes)
    assert _results(ws)[0]["ok"] is True


@pytest.mark.asyncio
async def test_concurrent_inject_nacks_busy(tmp_path):
    ws = _CapturingWS()
    sess = _make_session(tmp_path, ws)
    sess._files_dirty = True                          # hold the first in its window

    first = asyncio.create_task(
        sess.inject_prompt("first", "inj-c1", "delegate_result"))
    await asyncio.sleep(0.01)
    await sess.inject_prompt("second", "inj-c2", "delegate_result")
    sess._files_dirty = False
    await first

    by_id = {r["inject_id"]: r for r in _results(ws)}
    assert by_id["inj-c2"]["ok"] is False and by_id["inj-c2"]["reason"] == "busy"
    assert by_id["inj-c1"]["ok"] is True


class TestNoteLocalInput:
    def _sess(self, tmp_path):
        return _make_session(tmp_path, _CapturingWS())

    def test_printable_dirties_trailing_cr_clears(self, tmp_path):
        s = self._sess(tmp_path)
        s.note_local_input(b"typing someth")
        assert s._local_line_dirty is True
        s.note_local_input(b"ing\r")                  # submit
        assert s._local_line_dirty is False

    def test_interior_newline_does_not_clear(self, tmp_path):
        s = self._sess(tmp_path)
        s.note_local_input(b"line one\nline two")     # paste-ish, no trailing NL
        assert s._local_line_dirty is True

    def test_ctrl_c_clears(self, tmp_path):
        s = self._sess(tmp_path)
        s.note_local_input(b"abandoned")
        s.note_local_input(b"\x03")
        assert s._local_line_dirty is False

    def test_replies_and_mouse_are_not_typing(self, tmp_path):
        s = self._sess(tmp_path)
        before = s._last_local_input
        s.note_local_input(b"\x1b[27;5R")             # CPR reply
        s.note_local_input(b"\x1b[?1;2c")             # Primary DA reply
        s.note_local_input(b"\x1b[<35;70;68M")        # SGR mouse move
        assert s._local_line_dirty is False
        assert s._last_local_input == before          # timestamp untouched

    def test_proxy_pty_input_does_not_touch_local_state(self, tmp_path):
        # session.write (the proxy pty_input path) must not count as local
        # typing — only the T_INPUT relay calls note_local_input.
        s = self._sess(tmp_path)
        before = s._last_local_input
        s.write(b"dashboard keystrokes")
        assert s._local_line_dirty is False
        assert s._last_local_input == before

    def test_split_cpr_across_chunks_is_not_typing(self, tmp_path):
        # The 2026-07-08 injection stall: a CPR reply split across two socket
        # reads left residue that bumped the clock AND stuck line_dirty until
        # the next Enter — every retry then NACKed busy, for tens of minutes.
        s = self._sess(tmp_path)
        before = s._last_local_input
        s.note_local_input(b"\x1b[27;")
        s.note_local_input(b"5R")
        assert s._local_line_dirty is False
        assert s._last_local_input == before

    def test_split_mouse_across_chunks_is_not_typing(self, tmp_path):
        s = self._sess(tmp_path)
        before = s._last_local_input
        s.note_local_input(b"\x1b[<35;70")
        s.note_local_input(b";68M")
        assert s._local_line_dirty is False
        assert s._last_local_input == before

    def test_alt_key_across_chunks_still_counts(self, tmp_path):
        # Alt+key arrives as ESC + char: the bare ESC parks in the carry (not
        # typing on its own) and classifies as typing once the pair joins.
        s = self._sess(tmp_path)
        s.note_local_input(b"\x1b")
        assert s._local_line_dirty is False
        s.note_local_input(b"f")
        assert s._local_line_dirty is True

    def test_reply_fragment_then_typing_classifies_the_typing(self, tmp_path):
        s = self._sess(tmp_path)
        s.note_local_input(b"\x1b[27;")
        s.note_local_input(b"5Rhello")
        assert s._local_line_dirty is True

    def test_carry_overflow_drops_classification(self, tmp_path):
        # A runaway unterminated OSC body: past the cap the tail is dropped
        # from classification (this path is observational only) instead of
        # being held — or misread as typing — forever.
        s = self._sess(tmp_path)
        before = s._last_local_input
        s.note_local_input(b"\x1b]52;c;" + b"A" * 400)
        assert s._input_carry == b""
        assert s._local_line_dirty is False
        assert s._last_local_input == before


@pytest.mark.asyncio
async def test_manager_nacks_unknown_session(tmp_path):
    from satellite.sessions.session_manager import SessionManager

    ws = _CapturingWS()
    sm = SessionManager(SimpleNamespace())
    await sm.pty_inject(
        {"session_id": "no-such", "inject_id": "inj-x", "text": "p"}, ws,
    )
    res = _results(ws)
    assert res[0]["ok"] is False and res[0]["reason"] == "session_gone"
    assert res[0]["session_id"] == "no-such"
