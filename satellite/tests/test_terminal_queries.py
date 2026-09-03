"""Terminal-query sanitization of the otodock local-terminal scrollback replay.

The dual-attach input storm (2026-07-05): attaching a local `otodock` terminal
to a live PTY session replayed the RAW scrollback ring — including every
terminal query the TUI ever emitted (Ink sends ``CSI 6n`` per render frame).
The real terminal auto-answered the whole stale backlog at once, the TUI
re-rendered on that input and emitted fresh queries, and the loop self-sustained
("growing garbage / stuck arrow key", unstoppable). The fix strips query
sequences from the REPLAY only (``terminal_queries.strip_queries``); live tee
bytes stay raw because the attached terminal is the controlling one and must
answer live queries.
"""

from satellite.config import SatelliteConfig
from satellite.sessions.session_manager import SessionManager
from satellite.terminal.terminal_queries import strip_queries


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

QUERIES = [
    b"\x1b[c",              # DA1
    b"\x1b[0c",             # DA1 (param form)
    b"\x1b[>c",             # DA2
    b"\x1b[=c",             # DA3
    b"\x1b[5n",             # DSR status
    b"\x1b[6n",             # DSR cursor position (Ink, per render frame)
    b"\x1b[?6n",            # DECXCPR
    b"\x1b[?2026$p",        # DECRQM: synchronized-output probe
    b"\x1b[?u",             # kitty keyboard flags probe
    b"\x1b[>q",             # XTVERSION
    b"\x1b[>0q",            # XTVERSION (param form)
    b"\x1b[14t",            # XTWINOPS: window pixel size report
    b"\x1b[18t",            # XTWINOPS: text-area size report
    b"\x1b[14;2t",          # XTWINOPS: with sub-param
    b"\x1b[21t",            # XTWINOPS: title report
    b"\x1b]11;?\x07",       # OSC 11 background-color probe (BEL)
    b"\x1b]10;?\x1b\\",     # OSC 10 foreground probe (ST)
    b"\x1b]4;5;?\x07",      # OSC 4 palette probe
    b"\x1b]52;c;?\x07",     # OSC 52 clipboard READ
    b"\x1bP$qm\x1b\\",      # DECRQSS
    b"\x1bP+q544e\x1b\\",   # XTGETTCAP
]

# Rendering / state sequences a mirror needs verbatim to draw the screen.
RENDERING = [
    b"\x1b[38;5;196mred\x1b[0m",   # SGR colors
    b"\x1b[2;3H",                  # cursor position
    b"\x1b[2J",                    # clear screen
    b"\x1b[?1049h",                # alt-screen enable
    b"\x1b[?1000h",                # mouse tracking enable
    b"\x1b[?2026h\x1b[?2026l",     # synchronized-output SET (not the $p probe)
    b"\x1b[>1u",                   # kitty keyboard PUSH (set, not probe)
    b"\x1b[8;24;80t",              # XTWINOPS resize SET-op (below the 13+ report range)
    b"\x1b[4;768;1024t",           # XTWINOPS pixel-resize SET-op
    b"\x1b]0;my title\x07",        # OSC title set
    b"\x1b]52;c;aGVsbG8=\x07",     # OSC 52 clipboard WRITE (copy feature)
    b"\x1b[2 q",                   # DECSCUSR cursor style (space before q)
    b"plain text",
]


def test_queries_stripped():
    for q in QUERIES:
        assert strip_queries(b"a" + q + b"b") == b"ab", q


def test_rendering_preserved():
    for r in RENDERING:
        assert strip_queries(r) == r, r


def test_mixed_stream():
    data = b"\x1b[2J\x1b[6nhello\x1b]11;?\x07\x1b[38;5;196m\x1b[?2026$p!"
    assert strip_queries(data) == b"\x1b[2Jhello\x1b[38;5;196m!"


def test_no_esc_fast_path():
    data = b"no escapes at all"
    assert strip_queries(data) is data


# ---------------------------------------------------------------------------
# _wire_local_conn: replay sanitized, live tee raw
# ---------------------------------------------------------------------------

class _FakePty:
    def __init__(self, ring: bytes):
        self._ring = ring

    def scrollback(self) -> bytes:
        return self._ring


class _FakeSession:
    def __init__(self, ring: bytes):
        self.session_id = "s1"
        self.pty = _FakePty(ring)
        self.relay_out = None
        self.relay_exit = None
        self.repainted = 0

    def attach_local_relay(self, on_out, on_exit):
        self.relay_out = on_out
        self.relay_exit = on_exit

    def force_repaint(self):
        self.repainted += 1


class _FakeConn:
    def __init__(self):
        self.session = None
        self.outputs = []

    def bind_session(self, s):
        self.session = s

    def feed_output(self, data):
        self.outputs.append(bytes(data))

    def feed_exit(self, code, reason=""):
        pass


def _sm(tmp_path, session) -> SessionManager:
    sm = SessionManager(SatelliteConfig(
        machine_id="m", machine_secret="s",
        platform_url="ws://localhost:8400/v1/satellite",
        agents_dir=tmp_path / "agents", mcps_dir=tmp_path / "mcps",
        claude_bin="claude", codex_bin="codex",
    ))
    sm.pty_sessions["s1"] = session
    return sm


def test_attach_replay_is_query_sanitized(tmp_path):
    # A long-lived TUI ring: render frames interleaved with per-frame CPR
    # queries + startup probes — the storm fuel.
    ring = (b"\x1b[2Jframe1\x1b[6n\x1b[38;5;2mok\x1b[6n"
            b"\x1b]11;?\x07\x1b[?u\x1b[?2026$pframe2\x1b[6n")
    session = _FakeSession(ring)
    conn = _FakeConn()

    _sm(tmp_path, session).attach_local_conn("s1", conn, attach=True)

    assert conn.outputs, "replay must be delivered"
    replay = conn.outputs[0]
    assert b"\x1b[6n" not in replay
    assert b"\x1b]11;?" not in replay
    assert b"\x1b[?u" not in replay
    assert b"$p" not in replay
    # Rendering survives so the terminal still shows the current screen.
    assert b"frame1" in replay and b"frame2" in replay
    assert b"\x1b[2J" in replay and b"\x1b[38;5;2m" in replay
    # attach-to-live forces the TUI repaint (unchanged behavior).
    assert session.repainted == 1


def test_live_tee_stays_raw(tmp_path):
    # After the wire, LIVE output tees raw: the attached terminal is the
    # controlling one — the app's live queries need its (single) answer.
    session = _FakeSession(b"")
    conn = _FakeConn()
    _sm(tmp_path, session).attach_local_conn("s1", conn, attach=True)

    assert session.relay_out == conn.feed_output
    session.relay_out(b"live\x1b[6nbytes")
    assert conn.outputs[-1] == b"live\x1b[6nbytes"


def test_empty_after_strip_sends_nothing(tmp_path):
    # A ring that is ONLY stale queries strips to empty — no replay frame at
    # all (feed_output of b"" is skipped), not a garbage one.
    session = _FakeSession(b"\x1b[6n\x1b[6n\x1b]11;?\x07")
    conn = _FakeConn()
    _sm(tmp_path, session).attach_local_conn("s1", conn, attach=True)
    assert conn.outputs == []


# ---------------------------------------------------------------------------
# OSC 52 clipboard writes: pass LIVE, stripped from REPLAY (2026-07-19 —
# opening a terminal page must never overwrite the viewer's clipboard with a
# copy made inside the session earlier).
# ---------------------------------------------------------------------------

def test_strip_clipboard_writes_vocabulary():
    from satellite.terminal.terminal_queries import strip_clipboard_writes
    assert strip_clipboard_writes(b"a\x1b]52;c;aGk=\x07b") == b"ab"       # BEL
    assert strip_clipboard_writes(b"a\x1b]52;c;aGk=\x1b\\b") == b"ab"     # ST
    # Other OSC sequences (title set) pass untouched.
    assert strip_clipboard_writes(b"\x1b]0;title\x07x") == b"\x1b]0;title\x07x"
    data = b"no escapes"
    assert strip_clipboard_writes(data) is data


def test_replay_strips_osc52_clipboard_write(tmp_path):
    session = _FakeSession(b"before\x1b]52;c;c2VjcmV0\x07after")
    conn = _FakeConn()
    _sm(tmp_path, session).attach_local_conn("s1", conn, attach=True)
    replay = conn.outputs[0]
    assert b"]52;" not in replay
    assert b"before" in replay and b"after" in replay
    # Live tee still passes the copy feature through raw.
    session.relay_out(b"live\x1b]52;c;bGl2ZQ==\x07bytes")
    assert conn.outputs[-1] == b"live\x1b]52;c;bGl2ZQ==\x07bytes"
