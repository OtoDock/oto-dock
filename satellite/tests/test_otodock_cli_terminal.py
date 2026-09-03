"""otodock-CLI terminal restoration + detach messaging + title.

The detach escape-flood (2026-07-08, temp/bug.png): the inner TUI enables
mouse tracking / bracketed paste / focus reporting on the REAL terminal
through the mirrored PTY output. On every "inner TUI still alive" detach —
dashboard takeover, CLI→CLI --resume takeover, satellite restart/update,
``Ctrl-] q`` — the CLI restored only cooked mode via termios and never reset
those DEC private modes, so the emulator kept firing SGR mouse reports
(``ESC [ < b;x;y M``) at a process that no longer consumed them; cooked-mode
echo rendered them as endless ``35;37;30M…`` garbage. The fix emits
``TERMINAL_MODE_RESET`` on every restore path and flushes pending stdin.

Also covered: the disconnect/detach messages (socket-drop prints a resume
hint; a clean child exit stays silent) and the OSC title emitted at attach
(without one, emulators fall back to the process name — "python").
"""
from __future__ import annotations

import asyncio

import pytest

from satellite.terminal import otodock_cli
from satellite.terminal.otodock_cli import (
    _feed_stdin,
    _PosixTerminal,
    _relay,
)
from satellite.terminal.terminal_queries import (
    TERMINAL_MODE_RESET,
    split_trailing_partial,
)


# ---------------------------------------------------------------------------
# TERMINAL_MODE_RESET vocabulary
# ---------------------------------------------------------------------------


class TestModeResetConstant:
    def test_contains_the_load_bearing_resets(self):
        # Mouse tracking off (all variants — the flood is mouse reports),
        # bracketed paste off, focus reporting off, cursor visible, alt-screen
        # exit. Losing any of these re-opens a flood/stuck-terminal class.
        for seq in (b"\x1b[?1000l", b"\x1b[?1002l", b"\x1b[?1003l",
                    b"\x1b[?1005l", b"\x1b[?1006l", b"\x1b[?1015l",
                    b"\x1b[?2004l", b"\x1b[?1004l", b"\x1b[?25h",
                    b"\x1b[?1049l", b"\x1b[0m"):
            assert seq in TERMINAL_MODE_RESET

    def test_pure_escape_sequences_no_printable_leak(self):
        # Nothing outside escape sequences — the reset must be invisible on
        # every terminal. Strip all known-shape CSI/ESC sequences; nothing
        # printable may remain.
        import re
        residue = re.sub(rb"\x1b\[[0-9;<>=?]*[a-zA-Z]|\x1b>", b"",
                         TERMINAL_MODE_RESET)
        assert residue == b""


# ---------------------------------------------------------------------------
# split_trailing_partial (the inject-gate carry helper lives here too)
# ---------------------------------------------------------------------------


class TestSplitTrailingPartial:
    def test_plain_text_passes_whole(self):
        assert split_trailing_partial(b"hello") == (b"hello", b"")

    def test_complete_cpr_reply_not_held(self):
        assert split_trailing_partial(b"\x1b[27;5R") == (b"\x1b[27;5R", b"")

    def test_split_cpr_tail_held(self):
        complete, partial = split_trailing_partial(b"abc\x1b[27;")
        assert complete == b"abc"
        assert partial == b"\x1b[27;"

    def test_bare_esc_held(self):
        assert split_trailing_partial(b"x\x1b") == (b"x", b"\x1b")

    def test_csi_intermediate_held(self):
        # DECRPM reply split right at the intermediate byte: ESC [ ? 1 $ | y
        complete, partial = split_trailing_partial(b"\x1b[?2026$")
        assert complete == b""
        assert partial == b"\x1b[?2026$"

    def test_sgr_mouse_partial_held(self):
        complete, partial = split_trailing_partial(b"\x1b[<35;10")
        assert (complete, partial) == (b"", b"\x1b[<35;10")

    def test_x10_mouse_needs_three_payload_bytes(self):
        complete, partial = split_trailing_partial(b"\x1b[M\x20\x21")
        assert (complete, partial) == (b"", b"\x1b[M\x20\x21")
        # ... and a complete X10 report is not held.
        whole = b"\x1b[M\x20\x21\x22"
        assert split_trailing_partial(whole) == (whole, b"")

    def test_osc_body_without_terminator_held(self):
        complete, partial = split_trailing_partial(b"ok\x1b]11;rgb:00")
        assert (complete, partial) == (b"ok", b"\x1b]11;rgb:00")

    def test_osc_with_st_esc_half_held(self):
        # ST is ESC-backslash; the lone ESC half must ride the carry.
        complete, partial = split_trailing_partial(b"\x1b]11;rgb:00/00/00\x1b")
        assert complete == b""
        assert partial == b"\x1b]11;rgb:00/00/00\x1b"

    def test_complete_osc_not_held(self):
        whole = b"\x1b]11;rgb:00/00/00\x07"
        assert split_trailing_partial(whole) == (whole, b"")

    def test_earlier_complete_sequences_pass_only_tail_held(self):
        data = b"\x1b[31mred\x1b[0m\x1b[6"
        complete, partial = split_trailing_partial(data)
        assert complete == b"\x1b[31mred\x1b[0m"
        assert partial == b"\x1b[6"

    def test_arrow_key_is_complete(self):
        assert split_trailing_partial(b"\x1b[A") == (b"\x1b[A", b"")


# ---------------------------------------------------------------------------
# _PosixTerminal._restore — reset + termios + stdin flush, in that order
# ---------------------------------------------------------------------------


class _FakeTermios:
    TCSADRAIN = object()
    TCIFLUSH = object()

    def __init__(self, log, fail_tcsetattr=False):
        self.log = log
        self.fail_tcsetattr = fail_tcsetattr

    def tcsetattr(self, fd, when, attr):
        if self.fail_tcsetattr:
            raise OSError("bad fd")
        self.log.append(("tcsetattr", fd, when, attr))

    def tcflush(self, fd, queue):
        self.log.append(("tcflush", fd, queue))


def _bare_posix_terminal(log, monkeypatch, *, fail_write=False,
                         fail_tcsetattr=False):
    """A _PosixTerminal without __init__ (tcgetattr needs a real tty)."""
    term = _PosixTerminal.__new__(_PosixTerminal)
    term._termios = _FakeTermios(log, fail_tcsetattr=fail_tcsetattr)
    term.stdin_fd = 33
    term.old_attr = "OLD"
    term._restored = False

    def fake_write(fd, payload):
        if fail_write:
            raise OSError("gone")
        log.append(("write", fd, payload))
        return len(payload)

    monkeypatch.setattr(otodock_cli.os, "write", fake_write)
    return term


class TestPosixRestore:
    def test_reset_then_termios_then_flush(self, monkeypatch):
        log = []
        term = _bare_posix_terminal(log, monkeypatch)
        term._restore()
        assert [e[0] for e in log] == ["write", "tcsetattr", "tcflush"]
        assert log[0][2] == TERMINAL_MODE_RESET
        assert log[1][1:] == (33, _FakeTermios.TCSADRAIN, "OLD")
        assert log[2][1:] == (33, _FakeTermios.TCIFLUSH)

    def test_idempotent(self, monkeypatch):
        log = []
        term = _bare_posix_terminal(log, monkeypatch)
        term._restore()
        term._restore()
        assert len([e for e in log if e[0] == "write"]) == 1

    def test_dead_terminal_write_never_masks_termios_restore(self, monkeypatch):
        log = []
        term = _bare_posix_terminal(log, monkeypatch, fail_write=True)
        term._restore()
        assert ("tcsetattr", 33, _FakeTermios.TCSADRAIN, "OLD") in log

    def test_termios_failure_never_masks_flush(self, monkeypatch):
        log = []
        term = _bare_posix_terminal(log, monkeypatch, fail_tcsetattr=True)
        term._restore()
        assert ("tcflush", 33, _FakeTermios.TCIFLUSH) in log


# ---------------------------------------------------------------------------
# _relay exit paths — messages + title
# ---------------------------------------------------------------------------


class _FakeTerm:
    """Stands in for _PosixTerminal inside _relay."""

    instances: "list[_FakeTerm]" = []

    def __init__(self, loop, writer, escape_armed, finish, exit_code):
        self.finish = finish
        self.output = bytearray()
        self.torn_down = False
        _FakeTerm.instances.append(self)

    def setup(self):
        pass

    def write_output(self, payload: bytes):
        self.output.extend(payload)

    def teardown(self):
        self.torn_down = True


class _FakeWriter:
    def write(self, data):
        pass

    def close(self):
        pass


def _frame_reader(frames):
    """otodock_cli.read_frame stand-in: pops scripted frames; blocks forever
    when the script runs out (like a live socket with nothing to say)."""
    queue = list(frames)

    async def read_frame(reader):
        if queue:
            return queue.pop(0)
        await asyncio.Event().wait()

    return read_frame


@pytest.fixture
def fake_term(monkeypatch):
    _FakeTerm.instances = []
    monkeypatch.setattr(otodock_cli, "_PosixTerminal", _FakeTerm)
    monkeypatch.setattr(otodock_cli, "_WindowsTerminal", _FakeTerm)
    return _FakeTerm


def _run_relay(monkeypatch, frames, **kwargs):
    monkeypatch.setattr(otodock_cli, "read_frame", _frame_reader(frames))
    return asyncio.run(_relay(None, _FakeWriter(), **kwargs))


class TestRelayExitPaths:
    def test_socket_drop_prints_resume_hint(self, monkeypatch, capsys,
                                            fake_term):
        _run_relay(monkeypatch, [None], cli="claude", agent="dev")
        err = capsys.readouterr().err
        assert "disconnected" in err
        assert "otodock claude dev --resume" in err

    def test_socket_drop_hint_without_agent(self, monkeypatch, capsys,
                                            fake_term):
        _run_relay(monkeypatch, [None], cli="codex")
        assert "otodock codex --resume" in capsys.readouterr().err

    def test_takeover_reason_printed_verbatim(self, monkeypatch, capsys,
                                              fake_term):
        frames = [(otodock_cli.T_EXIT,
                   b'{"code": 75, "reason": "taken over by another terminal"}')]
        code = _run_relay(monkeypatch, frames, cli="claude", agent="dev")
        err = capsys.readouterr().err
        assert "otodock: taken over by another terminal" in err
        assert "disconnected" not in err
        assert code == 75

    def test_clean_child_exit_stays_silent(self, monkeypatch, capsys,
                                           fake_term):
        frames = [(otodock_cli.T_EXIT, b'{"code": 0, "reason": ""}')]
        code = _run_relay(monkeypatch, frames, cli="claude", agent="dev")
        assert capsys.readouterr().err == ""
        assert code == 0

    def test_local_detach_prints_session_keeps_running(self, monkeypatch,
                                                       capsys, fake_term):
        async def go():
            monkeypatch.setattr(otodock_cli, "read_frame", _frame_reader([]))
            task = asyncio.ensure_future(
                _relay(None, _FakeWriter(), cli="claude", agent="dev"))
            await asyncio.sleep(0)
            fake_term.instances[0].finish(0)  # what Ctrl-] q drives
            return await task

        asyncio.run(go())
        err = capsys.readouterr().err
        assert "detached — the session keeps running" in err

    def test_terminal_torn_down_on_every_path(self, monkeypatch, fake_term):
        _run_relay(monkeypatch, [None], cli="claude")
        assert fake_term.instances[0].torn_down

    def test_title_emitted_as_osc_before_output(self, monkeypatch, fake_term):
        frames = [(otodock_cli.T_OUTPUT, b"hi"),
                  (otodock_cli.T_EXIT, b'{"code": 0, "reason": ""}')]
        _run_relay(monkeypatch, frames, cli="claude", agent="dev",
                   title="otodock: dev — my chat")
        out = bytes(fake_term.instances[0].output)
        assert out.startswith(b"\x1b]0;otodock: dev \xe2\x80\x94 my chat\x07")
        assert out.endswith(b"hi")

    def test_no_title_no_osc(self, monkeypatch, fake_term):
        frames = [(otodock_cli.T_EXIT, b'{"code": 0, "reason": ""}')]
        _run_relay(monkeypatch, frames, cli="claude")
        assert b"\x1b]0;" not in bytes(fake_term.instances[0].output)


# ---------------------------------------------------------------------------
# _feed_stdin detach state machine (previously untested)
# ---------------------------------------------------------------------------


class _RecordingWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)


class TestFeedStdin:
    def _feed(self, chunks):
        writer = _RecordingWriter()
        finished = []
        escape_armed = {"v": False}
        for chunk in chunks:
            _feed_stdin(chunk, writer=writer, escape_armed=escape_armed,
                        finish=finished.append,
                        exit_code={"v": 0})
        return writer, finished

    def test_plain_bytes_forwarded(self):
        writer, finished = self._feed([b"hello"])
        assert finished == []
        assert writer.frames == [otodock_cli.encode(otodock_cli.T_INPUT,
                                                    b"hello")]

    def test_ctrl_rbracket_q_detaches(self):
        _, finished = self._feed([b"\x1dq"])
        assert finished == [0]

    def test_escape_split_across_chunks(self):
        _, finished = self._feed([b"\x1d", b"q"])
        assert finished == [0]

    def test_ctrl_rbracket_other_key_passes_through(self):
        writer, finished = self._feed([b"\x1dx"])
        assert finished == []
        assert writer.frames == [otodock_cli.encode(otodock_cli.T_INPUT,
                                                    b"\x1dx")]

    def test_eof_finishes(self):
        _, finished = self._feed([b""])
        assert finished == [0]
