"""Mirror-boundary terminal query/reply sanitization (core.terminal_queries).

Standalone harness (the live proxy deadlocks pytest on the conftest DB pool):
    proxy/venv/bin/python tests/execution/test_terminal_query_sanitization.py

The dual-attach input storm (2026-07-05): terminal QUERIES in mirror-bound
output (live or scrollback replay) get auto-answered by whatever terminal
renders them; the answers ride ``pty_input`` back into the PTY as phantom
keystrokes, the TUI re-renders and emits fresh queries, and the loop
self-sustains. Verified here:

 - the QUERY strip removes the full probe vocabulary and preserves rendering;
 - the REPLY strip removes mirror auto-responses and preserves real keystrokes
   (including kitty-encoded keys, which carry no ``?`` marker);
 - InteractiveSession strips queries from LOCAL-session mirror output (live
   fan-out + attach replay) and passes REMOTE bytes through untouched (they
   arrive pre-stripped from remote_pty);
 - RemotePtyProcess strips the full vocabulary at feed (live + ring);
 - write_input drops mirror replies before they reach the PTY.
"""
import asyncio
import os
import sys

import pytest

# Standalone-run bootstrap: proxy/ onto sys.path (tests/<area>/<file>.py -> 3 up).
# Redundant under pytest (conftest handles it); kept for `python <file>` runs.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.terminal_queries import strip_queries, strip_replies  # noqa: E402
from core.session import interactive_session as I  # noqa: E402

pytestmark = pytest.mark.asyncio


QUERIES = [
    b"\x1b[c", b"\x1b[0c", b"\x1b[>c", b"\x1b[=c",          # DA1/DA2/DA3
    b"\x1b[5n", b"\x1b[6n", b"\x1b[?6n",                    # DSR / CPR / DECXCPR
    b"\x1b[?2026$p",                                         # DECRQM probe
    b"\x1b[?u",                                              # kitty probe
    b"\x1b[>q", b"\x1b[>0q",                                 # XTVERSION
    b"\x1b[14t", b"\x1b[18t", b"\x1b[14;2t", b"\x1b[21t",   # XTWINOPS reports
    b"\x1b]11;?\x07", b"\x1b]10;?\x1b\\", b"\x1b]4;5;?\x07",  # OSC color probes
    b"\x1b]52;c;?\x07",                                      # OSC 52 clipboard READ
    b"\x1bP$qm\x1b\\", b"\x1bP+q544e\x1b\\",                # DECRQSS / XTGETTCAP
]

RENDERING = [
    b"\x1b[38;5;196mred\x1b[0m", b"\x1b[2;3H", b"\x1b[2J",
    b"\x1b[?1049h", b"\x1b[?1000h",
    b"\x1b[?2026h\x1b[?2026l",     # synchronized-output SET (not the probe)
    b"\x1b[>1u",                   # kitty PUSH (set, not probe)
    b"\x1b[8;24;80t",              # XTWINOPS resize SET-op (below the 13+ range)
    b"\x1b[4;768;1024t",
    b"\x1b]0;my title\x07",        # OSC title set
    b"\x1b]52;c;aGVsbG8=\x07",     # OSC 52 WRITE (the copy feature must pass)
    b"\x1b[2 q",                   # DECSCUSR (space before q ≠ XTVERSION)
    b"plain text",
]

REPLIES = [
    b"\x1b[?1;2c",                          # DA1 reply
    b"\x1b[24;80R", b"\x1b[0n",             # CPR / DSR replies
    b"\x1b[I", b"\x1b[O",                   # focus reports
    b"\x1b[?2026;2$y",                      # DECRPM report
    b"\x1b[?0u", b"\x1b[?1u",               # kitty flags replies
    b"\x1b[8;24;80t", b"\x1b[4;768;1024t", b"\x1b[3;0;0t",  # XTWINOPS replies
    b"\x1b]11;rgb:1e1e/1e1e/1e1e\x07",      # OSC color report (BEL)
    b"\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\",    # OSC color report (ST)
    b"\x1bP1$r0m\x1b\\",                    # DECRPSS
    b"\x1bP>|xterm.js(5.5.0)\x1b\\",        # XTVERSION reply
]

KEYSTROKES = [
    b"hello", b"\r", b"\x03",                       # text / Enter / Ctrl-C
    b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D",     # arrows
    b"\x1b[H", b"\x1b[F", b"\x1b[1;5C",             # Home/End/ctrl-arrow
    b"\x1bOP", b"\x1b[11~", b"\x1b[3~",             # F1 (SS3) / F1 / Delete
    b"\x1b[97;5u",                                  # kitty-ENCODED key (no '?')
    b"\x1b[200~pasted text\x1b[201~",               # bracketed paste
    b"\x1b[<35;70;68M",                             # SGR mouse report
]


def _check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return bool(cond)


class FakePty:
    def __init__(self, ring: bytes = b""):
        self.closed = False
        self.written = []
        self._ring = ring

    def write(self, data):
        self.written.append(data)

    def resize(self, rows, cols):
        pass

    def scrollback(self):
        return self._ring

    def close(self, signal_child=True):
        self.closed = True


def _mk(sid, *, target, ring=b"", ready=True):
    s = I.InteractiveSession(
        session_id=sid, chat_id="chat-" + sid, agent_name="a", target=target,
    )
    s.pty = FakePty(ring)
    s._ready = ready
    return s


async def test_query_vocabulary():
    ok = True
    for q in QUERIES:
        ok &= _check(f"query stripped: {q!r}", strip_queries(b"a" + q + b"b") == b"ab")
    for r in RENDERING:
        ok &= _check(f"rendering kept: {r!r}", strip_queries(r) == r)
    assert ok


async def test_reply_vocabulary():
    ok = True
    for r in REPLIES:
        ok &= _check(f"reply stripped: {r!r}", strip_replies(b"a" + r + b"b") == b"ab")
    for k in KEYSTROKES:
        ok &= _check(f"keystroke kept: {k!r}", strip_replies(k) == k)
    assert ok


async def test_write_input_drops_mirror_replies():
    I._sessions.clear()
    s = _mk("wi", target="local")
    try:
        # A pure auto-response chunk never reaches the PTY.
        s.write_input(b"\x1b[?2026;2$y")
        s.write_input(b"\x1b]11;rgb:1e1e/1e1e/1e1e\x07")
        s.write_input(b"\x1b[?0u")
        ok = _check("pure replies dropped", s.pty.written == [])
        # A mixed chunk keeps only the real keystroke bytes.
        s.write_input(b"\x1b[24;80Rhi")
        ok &= _check("mixed chunk keeps keystrokes", s.pty.written == [b"hi"])
        # kitty-encoded keystroke (no '?') passes through.
        s.write_input(b"\x1b[97;5u")
        ok &= _check("kitty-encoded key passes", s.pty.written[-1] == b"\x1b[97;5u")
        assert ok
    finally:
        I._sessions.clear()


async def test_local_mirror_output_is_query_stripped():
    I._sessions.clear()
    s = _mk("lo", target="local", ring=b"\x1b[2Jscreen\x1b[6n\x1b]11;?\x07tail")
    got = []
    try:
        # Attach replay (add_output_listener's return) is sanitized for local.
        sb = s.add_output_listener(got.append)
        ok = _check("local replay stripped", sb == b"\x1b[2Jscreentail")
        # Live fan-out is sanitized for local.
        s._fanout_output(b"live\x1b[6n\x1b[?2026$pbytes\x1b[38;5;2m")
        ok &= _check("local live fanout stripped",
                     got == [b"livebytes\x1b[38;5;2m"])
        # An all-query chunk fans out nothing (no empty frame).
        s._fanout_output(b"\x1b[6n\x1b[6n")
        ok &= _check("all-query chunk suppressed", len(got) == 1)
        assert ok
    finally:
        I._sessions.clear()


async def test_remote_mirror_output_passes_through():
    # Remote bytes arrive PRE-stripped from remote_pty._feed_output — the
    # session layer must not double-process them (and the ring is authoritative).
    I._sessions.clear()
    s = _mk("ro", target="m1", ring=b"pre-stripped-ring")
    got = []
    try:
        sb = s.add_output_listener(got.append)
        ok = _check("remote replay passes through", sb == b"pre-stripped-ring")
        s._fanout_output(b"remote-live")
        ok &= _check("remote live passes through", got == [b"remote-live"])
        assert ok
    finally:
        I._sessions.clear()


async def test_remote_pty_feed_strips_full_vocabulary():
    from core.remote import remote_pty as R

    got = []
    rp = R.RemotePtyProcess(
        machine_id="m1", session_id="rs", rows=24, cols=80,
        on_output=got.append, on_exit=None, scrollback_limit=4096,
    )
    try:
        rp._feed_output(b"\x1b[2Jhello\x1b[6n\x1b]11;?\x07\x1b[?u\x1b[?2026$p!")
        ok = _check("remote feed strips queries", got == [b"\x1b[2Jhello!"])
        ok &= _check("remote ring holds stripped bytes",
                     rp.scrollback() == b"\x1b[2Jhello!")
        rp._feed_output(b"\x1b[6n")
        ok &= _check("all-query frame suppressed", len(got) == 1)
        assert ok
    finally:
        R._remote_ptys.pop(("m1", "rs"), None)


async def test_split_trailing_partial_twin():
    """Chunk-boundary carry helper (satellite twin: the pty_inject line-state
    gate holds an incomplete trailing escape sequence instead of misreading
    the fragments as typing — the 2026-07-08 injection stall)."""
    from core.terminal_queries import split_trailing_partial

    ok = True
    for name, data, expect in [
        ("plain text passes whole", b"hello", (b"hello", b"")),
        ("complete CPR not held", b"\x1b[27;5R", (b"\x1b[27;5R", b"")),
        ("split CPR tail held", b"abc\x1b[27;", (b"abc", b"\x1b[27;")),
        ("bare ESC held", b"x\x1b", (b"x", b"\x1b")),
        ("CSI intermediate held", b"\x1b[?2026$", (b"", b"\x1b[?2026$")),
        ("SGR mouse partial held", b"\x1b[<35;10", (b"", b"\x1b[<35;10")),
        ("X10 short of payload held", b"\x1b[M\x20\x21", (b"", b"\x1b[M\x20\x21")),
        ("X10 complete not held", b"\x1b[M\x20\x21\x22", (b"\x1b[M\x20\x21\x22", b"")),
        ("OSC body unterminated held", b"ok\x1b]11;rgb:00", (b"ok", b"\x1b]11;rgb:00")),
        ("OSC ST half held", b"\x1b]11;x\x1b", (b"", b"\x1b]11;x\x1b")),
        ("complete OSC not held", b"\x1b]11;x\x07", (b"\x1b]11;x\x07", b"")),
        ("earlier sequences pass", b"\x1b[31mred\x1b[0m\x1b[6",
         (b"\x1b[31mred\x1b[0m", b"\x1b[6")),
        ("arrow key complete", b"\x1b[A", (b"\x1b[A", b"")),
    ]:
        ok &= _check(name, split_trailing_partial(data) == expect)
    assert ok


if __name__ == "__main__":
    async def _main():
        for fn in (
            test_query_vocabulary,
            test_reply_vocabulary,
            test_write_input_drops_mirror_replies,
            test_local_mirror_output_is_query_stripped,
            test_remote_mirror_output_passes_through,
            test_remote_pty_feed_strips_full_vocabulary,
            test_split_trailing_partial_twin,
        ):
            print(f"\n{fn.__name__}:")
            await fn()
        print("\nall checks passed")

    asyncio.run(_main())


async def test_replay_strips_osc52_clipboard_write():
    # OSC 52 SET passes LIVE (the TUI's copy feature via the ClipboardAddon)
    # but must never survive an attach replay — a replayed copy silently
    # overwrites the viewer's clipboard on page open (2026-07-19).
    from core.terminal_queries import strip_clipboard_writes
    assert strip_clipboard_writes(b"a\x1b]52;c;aGk=\x07b") == b"ab"
    assert strip_clipboard_writes(b"a\x1b]52;c;aGk=\x1b\\b") == b"ab"
    assert strip_clipboard_writes(b"\x1b]0;title\x07x") == b"\x1b]0;title\x07x"

    I._sessions.clear()
    ring = b"scr\x1b]52;c;c2VjcmV0\x07een\x1b]52;c;aGk=\x1b\\tail"
    for target in ("local", "m1"):
        s = _mk("cb-" + target, target=target, ring=ring)
        got = []
        try:
            sb = s.add_output_listener(got.append)
            ok = _check(f"{target} replay strips ]52 writes", b"]52;" not in sb)
            ok &= _check(f"{target} replay keeps rendering",
                         b"scr" in sb and b"een" in sb and b"tail" in sb)
            # LIVE fan-out keeps the copy feature working (write form passes).
            s._fanout_output(b"live\x1b]52;c;bGl2ZQ==\x07bytes")
            ok &= _check(f"{target} live keeps ]52 writes",
                         any(b"]52;" in g for g in got))
            assert ok
        finally:
            I._sessions.clear()
