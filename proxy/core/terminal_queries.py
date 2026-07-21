"""Terminal query/reply sanitization at MIRROR boundaries — shared vocabulary.

A PTY-backed TUI (Claude's Ink, Codex's Ratatui) talks to ONE controlling
terminal. OtoDock mirrors that PTY onto additional surfaces — the dashboard's
xterm.js viewer and (satellite-side) a local `otodock` terminal — and a mirror
must never act like a second controlling terminal, or the app's query/answer
handshake becomes a feedback storm:

  * OUTPUT direction (PTY → mirror): the app emits terminal QUERIES it expects
    exactly one answer to — Device Attributes (``CSI c``), cursor position
    (``CSI 6n``; Ink sends one per render frame), DECRQM mode probes
    (``CSI ? 2026 $p``), the kitty keyboard probe (``CSI ? u``), XTVERSION
    (``CSI > q``), XTWINOPS size/title reports (``CSI 13–21 t``), OSC
    4/10/11/12 color probes, the OSC 52 clipboard READ, and DCS capability
    requests (DECRQSS / XTGETTCAP). Every real terminal that parses a live or
    REPLAYED query auto-answers it: a scrollback replay holding hundreds of
    per-frame ``CSI 6n`` makes the attaching terminal fire hundreds of CPR
    replies into the PTY at once; the TUI re-renders on that input, emitting
    FRESH queries, which get answered again — the self-sustaining "growing
    garbage / stuck arrow key" storm (2026-07-05: `otodock` terminal attach
    while the dashboard mirrored the same session). Queries are therefore
    stripped from every mirror-bound stream (live + replay); only the
    controlling terminal sees and answers them.

  * INPUT direction (mirror → PTY): xterm.js still auto-answers whatever it
    happens to parse (and emits focus reports); those responses ride the
    ``pty_input`` lane exactly like keystrokes. They are not user input, and
    the controlling terminal answers the app itself — so mirror-origin
    responses are dropped before they reach the PTY.

Both directions are needed: the output strip keeps the storm from IGNITING;
the input strip is belt-and-braces for whatever slips through (a query split
across two live chunks, an older satellite that doesn't strip its replay).

Chunk-boundary caveat: a query split across two LIVE output chunks escapes the
output strip — accepted, matching the pre-existing design; the replay paths
(where storms actually ignite) always operate on the joined buffer, and the
input strip catches the mirror's answer to a split query.

The strips deliberately do NOT touch mode-SET sequences (alt-screen, mouse
tracking, kitty push ``CSI > 1 u``, bracketed-paste toggles…): a mirror must
apply those to render faithfully, and they solicit no answer. The OSC 52
clipboard WRITE (``]52;c;<b64>``) also passes — the dashboard's ClipboardAddon
implements the TUI's copy feature — only the ``;?`` read form is stripped
(a mirror answering it would leak the viewer's clipboard).

Mirror copy: ``satellite/terminal/terminal_queries.py`` (the satellite is a
separate shippable and cannot import proxy modules) — keep the QUERY pattern
in sync with it.
"""
from __future__ import annotations

import re

# Terminal QUERY sequences an application emits expecting the *terminal* to
# answer. No standard *rendering* sequence matches: rendering CSIs never end in
# ``c``/``n``; ``$p`` / ``?u`` / ``>…q`` are query-only finals; XTWINOPS 13–21
# is the report-request subset (1–12 are window OPS — move/resize/de-iconify —
# and the ``4;h;w`` / ``8;r;c`` set-forms start below 13, so they pass); OSC
# 4/10/11/12/52 match only with an explicit ``;?`` payload (set-forms carry a
# color spec / base64 payload instead); DCS matches only the ``$q``/``+q``
# request introducers.
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

# Terminal auto-RESPONSE sequences a mirror xterm emits that are NOT user
# input: DA replies (``CSI … c``), DSR/CPR reports (``CSI … n`` / ``… R``),
# focus in/out (``CSI I`` / ``CSI O``), DECRPM mode reports (``CSI ? … $y``),
# kitty flags replies (``CSI ? … u`` — the ``?`` marker is what a
# kitty-ENCODED keystroke ``CSI code;mods u`` never carries, so real keys
# survive), XTWINOPS replies (``CSI … t`` — no keystroke encoding ends in
# ``t``), OSC responses (color/clipboard reports) and DCS responses
# (DECRPSS / XTGETTCAP / XTVERSION) — no keystroke is OSC or DCS. Ordinary
# keys stay intact: arrows/Home/End end in A–H/F, function keys use SS3 or
# ``~``. (Known pre-existing tradeoff: modifier-F3 arrives as ``CSI 1;mR``
# and is eaten by the CPR pattern — rare enough to accept.)
TERMINAL_REPLY_RE = re.compile(
    rb"\x1b\[[0-9;>=?]*[cnR]"                          # DA / DSR / CPR replies
    rb"|\x1b\[[IO]"                                    # focus in/out reports
    rb"|\x1b\[\?[0-9;]*\$y"                            # DECRPM mode report
    rb"|\x1b\[\?[0-9;]*u"                              # kitty flags reply
    rb"|\x1b\[[0-9;]*t"                                # XTWINOPS replies
    rb"|\x1b\][0-9]{1,4};[^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC responses
    rb"|\x1bP[^\x07\x1b]*(?:\x07|\x1b\\)"              # DCS responses
)


def strip_queries(data: bytes) -> bytes:
    """Remove terminal-query sequences from MIRROR-BOUND output (live bytes and
    scrollback replays) so no mirror ever answers the app's handshake."""
    if b"\x1b" not in data:
        return data
    return TERMINAL_QUERY_RE.sub(b"", data)


def strip_replies(data: bytes) -> bytes:
    """Remove terminal auto-responses from MIRROR-ORIGIN input so they never
    reach the PTY as phantom keystrokes."""
    if b"\x1b" not in data:
        return data
    return TERMINAL_REPLY_RE.sub(b"", data)


# OSC 52 clipboard WRITE (``]52;<sel>;<b64>``) — the TUI's copy feature. It
# must PASS on live output (the dashboard's ClipboardAddon is how Ctrl-C in
# the TUI reaches the system clipboard) but must NOT survive into scrollback
# REPLAYS: re-delivering the buffered sequence on every (re)attach silently
# OVERWRITES the viewer's current clipboard with whatever was copied in the
# session earlier — opening a terminal page must never mutate the clipboard.
# The ``;?`` READ form is already stripped by TERMINAL_QUERY_RE.
_OSC52_WRITE_RE = re.compile(rb"\x1b\]52;[^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_clipboard_writes(data: bytes) -> bytes:
    """Remove OSC 52 clipboard-set sequences from REPLAYED output only —
    apply at scrollback-replay boundaries, never on the live stream."""
    if b"\x1b" not in data:
        return data
    return _OSC52_WRITE_RE.sub(b"", data)


# A trailing INCOMPLETE escape sequence: everything from the last ESC when that
# ESC has not yet been terminated — a CSI still in its parameter/intermediate
# bytes, an OSC/DCS body without its BEL/ST, an X10 mouse report short of its 3
# payload bytes, or a bare ESC. Input arrives in arbitrary chunks, so a
# terminal auto-reply (CPR etc.) can split across two reads; classifying the
# fragments through TERMINAL_REPLY_RE leaves residue that reads as typing.
# Callers hold the partial tail back (a small carry) and prepend it to the next
# chunk. A bare user Esc keypress parks in the carry until more bytes arrive —
# benign for line-state (Esc never dirties a line). Satellite twin:
# ``split_trailing_partial`` in ``satellite/terminal/terminal_queries.py``.
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
# callers print any post-detach message with a leading newline. Satellite twin:
# ``TERMINAL_MODE_RESET`` in ``satellite/terminal/terminal_queries.py``.
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
