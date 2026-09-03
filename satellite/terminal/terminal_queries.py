"""Terminal-query/reply sanitization for the local `otodock` terminal boundary.

Satellite-owned mirror of ``proxy/core/terminal_queries.py`` (the satellite is
a separate shippable and cannot import proxy modules) — keep the patterns in
sync with it.

Why: when a local `otodock` terminal ATTACHES to a live PTY session
(``session_manager._wire_local_conn``), the PTY's scrollback ring is replayed
so the terminal shows the current screen. That ring holds every terminal QUERY
the TUI ever emitted — Ink sends a cursor-position request (``CSI 6n``) per
render frame, plus OSC color probes, DECRQM/kitty/XTVERSION capability probes
at startup. A real terminal AUTO-ANSWERS every query it parses, including
replayed ones: hundreds of stale ``CSI 6n`` in one replay burst → hundreds of
CPR replies fired into the PTY as input → the TUI re-renders on that input,
emitting FRESH queries → answered again — a self-sustaining "growing garbage /
stuck arrow key" storm (2026-07-05, dual-attach). OS-agnostic: the ring is
replayed the same way over Unix PTYs and Windows ConPTY, and every terminal
(VSCode/xterm/Terminal.app/Windows Terminal) auto-answers.

The strip applies to the REPLAY only. Live output to the attached terminal
stays raw — it is the controlling terminal, and the app's live handshake needs
its (single) answer.
"""
from __future__ import annotations

import re

# Terminal QUERY sequences an application emits expecting the *terminal* to
# answer — see the proxy twin for the per-alternative rationale. No standard
# rendering sequence matches; mode-SET sequences (alt-screen, mouse tracking,
# kitty push) deliberately pass so the replayed screen renders faithfully.
TERMINAL_QUERY_RE = re.compile(
    rb"\x1b\[[0-9;>=?]*[cn]"                          # DA1/DA2/DA3 + DSR/CPR request
    rb"|\x1b\[\?[0-9;]*\$p"                           # DECRQM mode probe
    rb"|\x1b\[\?u"                                    # kitty keyboard flags probe
    rb"|\x1b\[>[0-9;]*q"                              # XTVERSION
    rb"|\x1b\[(?:1[3-9]|2[01])(?:;[0-9]+)*t"          # XTWINOPS report requests
    rb"|\x1b\](?:4;[0-9]+|1[0-2]);\?(?:\x07|\x1b\\)"  # OSC color probes
    rb"|\x1b\]52;[a-zA-Z]*;\?(?:\x07|\x1b\\)"         # OSC 52 clipboard READ
    rb"|\x1bP[$+]q[^\x07\x1b]*(?:\x07|\x1b\\)"        # DECRQSS / XTGETTCAP requests
)


def strip_queries(data: bytes) -> bytes:
    """Remove terminal-query sequences from a scrollback replay so the
    attaching terminal has nothing (stale) to answer."""
    if b"\x1b" not in data:
        return data
    return TERMINAL_QUERY_RE.sub(b"", data)


# OSC 52 clipboard WRITE (``]52;<sel>;<b64>``) — passes LIVE (the TUI's copy
# feature) but must not survive a scrollback REPLAY: a replayed copy silently
# overwrites the attaching terminal's system clipboard. Proxy twin:
# ``strip_clipboard_writes`` in ``proxy/core/terminal_queries.py``.
_OSC52_WRITE_RE = re.compile(rb"\x1b\]52;[^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_clipboard_writes(data: bytes) -> bytes:
    """Remove OSC 52 clipboard-set sequences from REPLAYED output only."""
    if b"\x1b" not in data:
        return data
    return _OSC52_WRITE_RE.sub(b"", data)


# Terminal auto-RESPONSE sequences the local terminal emits that are NOT user
# typing: DA replies, DSR/CPR reports (a real terminal answers the TUI's
# ``CSI 6n`` once per render frame — these ride T_INPUT continuously during any
# repaint), focus reports, DECRPM/kitty/XTWINOPS replies, OSC/DCS responses.
# Used by the local-input line-state tracking (``note_local_input``): a naive
# "printable bytes = typing" heuristic would see the CPR digits and stick the
# dirty flag forever. Ordinary keys survive (arrows end A–H/F, function keys
# use SS3 or ``~``). Proxy twin: ``TERMINAL_REPLY_RE`` in
# ``proxy/core/terminal_queries.py``.
TERMINAL_REPLY_RE = re.compile(
    rb"\x1b\[[0-9;>=?]*[cnR]"                          # DA / DSR / CPR replies
    rb"|\x1b\[[IO]"                                    # focus in/out reports
    rb"|\x1b\[\?[0-9;]*\$y"                            # DECRPM mode report
    rb"|\x1b\[\?[0-9;]*u"                              # kitty flags reply
    rb"|\x1b\[[0-9;]*t"                                # XTWINOPS replies
    rb"|\x1b\][0-9]{1,4};[^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC responses
    rb"|\x1bP[^\x07\x1b]*(?:\x07|\x1b\\)"              # DCS responses
)

# Mouse-tracking reports (SGR + legacy X10): user-generated but pointer, not
# text — they must not count as "typing" for the injection line-state either.
# Proxy twin: ``_MOUSE_RE`` in ``proxy/core/session/interactive_session.py``.
MOUSE_RE = re.compile(rb"\x1b\[<[0-9;]*[Mm]|\x1b\[M.{3}", re.DOTALL)


def strip_replies(data: bytes) -> bytes:
    """Remove terminal auto-responses (see TERMINAL_REPLY_RE)."""
    if b"\x1b" not in data:
        return data
    return TERMINAL_REPLY_RE.sub(b"", data)


# A trailing INCOMPLETE escape sequence: everything from the last ESC when that
# ESC has not yet been terminated — a CSI still in its parameter bytes, an
# OSC/DCS body without its BEL/ST, an X10 mouse report short of its 3 payload
# bytes, or a bare ESC. Input arrives in arbitrary socket-read chunks, so a
# terminal auto-reply (CPR etc.) can split across two reads; classifying the
# fragments through TERMINAL_REPLY_RE/MOUSE_RE leaves residue that reads as
# typing. Callers hold the partial tail back (a small carry) and prepend it to
# the next chunk. A bare user Esc keypress parks in the carry until more bytes
# arrive — benign for line-state (Esc never dirties a line).
_PARTIAL_TAIL_RE = re.compile(
    rb"\x1b(?:"
    rb"\[M.{0,2}"          # legacy X10 mouse: needs exactly 3 payload bytes
    rb"|\[[0-9;<>=?]*[\x20-\x2f]*"  # CSI params (+intermediates), no final byte yet
    rb"|\][^\x07\x1b]*\x1b?"  # OSC body, no BEL/ST yet (or ST's ESC half)
    rb"|P[^\x07\x1b]*\x1b?"   # DCS body, no ST yet
    rb")?$",
    re.DOTALL,
)


def split_trailing_partial(data: bytes) -> "tuple[bytes, bytes]":
    """Split ``data`` into ``(complete, partial)`` where ``partial`` is a
    trailing incomplete escape sequence (possibly a bare ESC) and ``complete``
    is everything before it. ``partial`` is empty when the chunk ends cleanly."""
    if b"\x1b" not in data:
        return data, b""
    m = _PARTIAL_TAIL_RE.search(data)
    if m is None or m.start() == len(data):
        return data, b""
    return data[: m.start()], data[m.start():]


# Restores a real terminal to a sane state after mirroring a TUI that died or
# was detached mid-flight (the inner CLI enables mouse tracking / bracketed
# paste / focus reporting on the REAL terminal through the mirrored output and
# only resets them on its own clean exit). Every sequence is a no-op on
# terminals that don't know it or already have the mode off. ``?1049l`` exits
# the alt screen (needed for alt-screen TUIs); note its implied cursor restore
# (DECRC) can home the cursor on emulators that "restore" an unsaved cursor —
# callers print any post-detach message with a leading newline. Proxy twin:
# ``TERMINAL_MODE_RESET`` in ``proxy/core/terminal_queries.py``.
TERMINAL_MODE_RESET = (
    b"\x1b[0m"          # SGR reset (no color/attr leak into the shell)
    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l"  # mouse off
    b"\x1b[?2004l"      # bracketed paste off
    b"\x1b[?1004l"      # focus reporting off
    b"\x1b[>4;0m"       # xterm modifyOtherKeys off
    b"\x1b[<u"          # kitty keyboard-protocol pop (no-op on empty stack)
    b"\x1b[?1l\x1b>"    # normal cursor keys + numeric keypad
    b"\x1b[?1049l"      # exit the alt screen (no-op when not in it)
    b"\x1b[?7h"         # autowrap back on
    b"\x1b[?25h"        # cursor visible
)
