"""What every part of a terminal session shares: its timing constants, the
callback types, the listeners of forwarded batches and a few small helpers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import psycopg

import config

if TYPE_CHECKING:
    from core.session.interactive_session import InteractiveSession

logger = logging.getLogger("otodock.interactive")

# Terminal auto-RESPONSE sequences the mirror xterm emits but that are NOT user
# input (DA/DSR/CPR replies, focus reports, DECRPM/kitty/XTWINOPS/OSC/DCS
# responses). xterm.js auto-answers an application's terminal queries and emits
# focus reports; the dashboard forwards them over `pty_input`. They must never
# reach the real PTY — the controlling terminal answers the app's queries
# itself — and, critically, must not be mistaken for "the user started typing",
# which would CANCEL the cold first-prompt Enter retry (the Windows-remote
# first-prompt-never-submits bug: ConPTY emits an outer DA query whose mirror
# reply rode `pty_input` mid-cold-submit). Vocabulary + rationale live in
# ``core.terminal_queries`` (shared with the output-side strips; imported at
# the top as ``_strip_terminal_replies`` / ``_strip_terminal_queries``).

# Mouse-tracking reports the xterm forwards when the TUI has mouse mode on: SGR
# (`\x1b[<b;x;yM` / `…m`) + legacy X10 (`\x1b[M` + 3 bytes). These ARE
# user-generated, but they're pointer movement/clicks, not message input — they
# must still reach the PTY (the TUI may use them) yet must NOT pre-empt the
# pending cold first-prompt submit. A stray mouse-MOVE over the terminal during
# warmup otherwise cancelled the cold-submit retry before the Enter landed
# (observed live: `\x1b[<35;70;68M`).

# Minimum recovered-text length for an undelivered-input breadcrumb. A cold
# flush that dies unconfirmed is persisted into the chat so the user can copy
# their prompt back (2026-08-18 field loss: a 1143-byte prompt typed into
# Claude's resume-from-summary picker vanished with no DB trace). Short
# residues (arrow keys, a stray Enter, tiny edits) are noise, not prompts.
_BREADCRUMB_MIN_CHARS = 40


def _breadcrumb_text(raw: bytes) -> str:
    """Human-readable text of a buffered PTY input chunk, for the
    undelivered-input breadcrumb. Strips bracketed-paste framing, CSI/OSC
    escape sequences, and control chars (except newline/tab); collapses the
    trailing submit CR. Pure — unit-tested directly."""
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\x1b[200~", "").replace("\x1b[201~", "")
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)  # OSC
    text = re.sub(r"\x1b\[[0-9;?<>=]*[A-Za-z~]", "", text)         # CSI
    text = re.sub(r"\x1b.", "", text)                              # bare ESC+x
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in ("\n", "\t") or ord(c) >= 0x20)
    return text.strip()
# A turn parked on a question waits on a person, so the reaper spares it like
# a held prompt: until the session's token ends less _TOKEN_MARGIN_S (an answer
# after that lets the turn go on and every later hook is refused), at most
# session_state.PROMPT_WAIT_S idle. Past it the answer path revives the session
# (single-flight). Under memory pressure and at the per-person cap the capacity
# evictor (core/concurrency_scan._turn_live) spares a parked question up to the
# background-work ceiling and reclaims it past that.
_TOKEN_MARGIN_S = 60
# A session token's life (auth/session_token.create_session_token): the
# stand-in from the terminal's start when its own token cannot be read.
_SESSION_TOKEN_LIFE_S = 24 * 3600


def _token_expires_at(env: dict | None) -> float | None:
    """The wall-clock expiry of the session token in a spawn env
    (``PROXY_API_KEY``), or None when there is none or it cannot be read."""
    token = str((env or {}).get("PROXY_API_KEY") or "")
    if not token:
        return None
    from auth.session_token import validate_session_token
    exp = (validate_session_token(token) or {}).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None

# Readiness gate: buffer input until the TUI has rendered + gone quiet, so the
# first prompt (a human's, or an autonomous task's injected prompt) is never sent
# before the CLI's input handler is attached (the "message sent before the CLI
# started" race). Ready = the TUI has STARTED RENDERING (first ESC/control
# sequence) AND output then quiet for READY_SETTLE_S, or READY_MAX_S since spawn
# as a fallback. The render gate skips pre-TUI plain-text noise (e.g. Codex's
# "Can't run AVX2 build…" native warning emitted before its Ratatui renders).
# Layer-agnostic → Claude (Ink emits ESC from frame 1) AND Codex.
READY_SETTLE_S = getattr(config, "INTERACTIVE_READY_SETTLE_S", 0.8)
READY_MAX_S = getattr(config, "INTERACTIVE_READY_MAX_S", 20.0)
# How often a ready terminal looks again for its stand-ins' tool lists before
# its first message goes in (the gate is bounded by READY_MAX_S from spawn).
_TOOLS_RECHECK_S = 0.25
# A turn an Escape stopped before the model's first word: looked at this long
# after the Escape, closed when the terminal has been quiet this long.
_DROPPED_TURN_S = 3.0
_DROPPED_TURN_QUIET_S = 2.5
_DROPPED_TURN_LOOKS = 3
# A line-submit ("text\r" in one write) often lands in the TUI input box WITHOUT
# submitting (Ink/readline). Send the text, then the Enter separately after this
# gap so the TUI registers the text first, then submits. Bare Enter keystrokes
# (live typing) are written as-is.
_SUBMIT_ENTER_DELAY_S = getattr(config, "INTERACTIVE_SUBMIT_ENTER_DELAY_S", 0.12)
# The COLD first prompt: write the text, then fire ONE Enter once the composer has
# settled — output quiet for _SUBMIT_SETTLE_S (re-armed on each output, so a
# still-rendering / warming TUI keeps it from firing early), or _SUBMIT_MAX_S as a
# backstop. We do NOT blind-retry — a stray Enter selects a native question/menu
# (AskUserQuestion) option once the first one submits. This single Enter is
# reliable LOCAL + on a Linux-remote satellite (the text is written first, then the
# Enter only after the echo settles). The ONE exception is WINDOWS-remote Claude,
# where the ConPTY render race can swallow it; THERE ONLY we fire one more Enter
# _SUBMIT_WIN_BACKSTOP_S later as a backstop — see _fire_submit.
_SUBMIT_SETTLE_S = getattr(config, "INTERACTIVE_SUBMIT_SETTLE_S", 0.8)
_SUBMIT_MAX_S = getattr(config, "INTERACTIVE_SUBMIT_MAX_S", 12.0)
# Windows-remote Claude ONLY: one extra Enter this long after the first (the TUI is
# idle by then). 2 Enters total. Codex / Linux-remote / local: single Enter.
_SUBMIT_WIN_BACKSTOP_S = getattr(config, "INTERACTIVE_SUBMIT_WIN_BACKSTOP_S", 7.0)
# Cold-flush CONFIRMATION: the readiness settle is a heuristic — on a cold
# boot the welcome frame renders, output goes quiet ≥READY_SETTLE_S while the
# MCPs are still warming, and the flush lands in a TUI whose composer isn't
# live yet: body + Enter swallowed, first prompt lost (live repro 2026-07-21,
# first message to a brand-new interactive chat). After the cold-submit Enter
# fires, if the transcript hasn't confirmed the turn (_turn_open via the
# journaled user line) within _COLD_RETRY_S, the flushed bytes are re-emitted,
# up to _COLD_RETRY_MAX times. The window sits past the +4s submit tail so a
# prompt that DID land is seen as confirmed before the watcher acts.
_COLD_RETRY_S = getattr(config, "INTERACTIVE_COLD_RETRY_S", 5.0)
_COLD_RETRY_MAX = getattr(config, "INTERACTIVE_COLD_RETRY_MAX", 2)
# Floor on the readiness settle for fresh (non prompt-in-argv) TUIs: never
# mark ready before this much time since spawn — the first post-render quiet
# is usually the MCP warm, not a live composer (same precedent as MIN_TURN_S
# guarding turn-complete false-fires). READY_MAX_S stays the hard backstop.
READY_MIN_S = getattr(config, "INTERACTIVE_READY_MIN_S", 2.5)
# After PTY output goes quiet for this long (a turn / long pause likely ended),
# tail the transcript → chat_messages + the title backfill. The native TUI does
# NOT reliably fire the Stop hook, so without this the DB (and the interactive
# chat's title) only caught up on the 60s reaper sweep — the title appeared up to
# a minute late. This makes both land within a few seconds of the turn.
_POST_OUTPUT_TAIL_S = getattr(config, "INTERACTIVE_POST_OUTPUT_TAIL_S", 3.0)
# Short fuse for the resume-check tail: output arriving while the turn is
# CLOSED (promptless self-resume / in-TUI question answer) triggers a
# transcript read this soon, so the reopened turn shows live instead of
# waiting out the starved post-output debounce.
_RESUME_TAIL_S = getattr(config, "INTERACTIVE_RESUME_TAIL_S", 1.5)
# Mid-turn tail cadence (LOCAL only): while a turn is open the TUI spinner
# redraws continuously, so the post-output debounce never fires and a local
# turn's rows historically landed only at turn end (+3s). Remote sessions
# already persist rows mid-turn (the satellite polls its transcript every
# 0.8s); this periodic tail gives local sessions the same incremental
# persistence — live rich view and voice mode read rows as they land.
_MIDTURN_TAIL_S = getattr(config, "INTERACTIVE_MIDTURN_TAIL_S", 1.0)

# LLM chat-title latency cap: if the FIRST turn is still running this long
# after it opened and no tail batch has crossed the char/tool thresholds
# (the LOCAL TUI's spinner starves the post-output debounce, so long local
# turns produce no mid-turn batches), force one tail and fire the title
# upgrade with whatever partial first turn is persisted — a prompt-only
# title beats waiting minutes for turn end. One-shot per session.
_TITLE_EARLY_TIMER_S = getattr(config, "INTERACTIVE_TITLE_EARLY_TIMER_S", 30.0)

# Interactive TASK completion: the turn-end signal
# (Claude end_turn / Codex task_complete, surfaced by the tailer) fires the
# task's ``on_turn_complete`` callback — but only after the session is at least
# this old, so MCP-warm / first-render quiet can't false-trigger before a real
# turn. Belt-and-braces: a turn-end signal only exists AFTER a real turn anyway.
# Chats never set the callback, so this whole path is a no-op for them.
MIN_TURN_S = getattr(config, "INTERACTIVE_TASK_MIN_TURN_S", 5.0)

# Server-prompt injection (delegate results …): the PTY must have been quiet
# this long before a queued prompt is injected — covers the gap between a user
# submit and the first transcript line (the turn-open flag is transcript-derived
# and can lag by one tail poll).
_INJECT_QUIET_S = getattr(config, "INTERACTIVE_INJECT_QUIET_S", 2.0)
# Re-check period while prompts are queued but a gate blocks (mid-turn, dirty
# composer, young session…). A one-shot timer would starve after the first
# blocked attempt; this one re-arms until the queue drains or the session dies.
_INJECT_BACKSTOP_S = getattr(config, "INTERACTIVE_INJECT_BACKSTOP_S", 5.0)

# TTL on the composer-dirty injection gate. Dirty means "printable bytes with
# no submit yet" — but a mirror viewer's wheel scroll arrives as arrow keys on
# a TUI alternate screen and reads as typing with no Enter ever coming, which
# wedged queued delegate results behind a phantom draft. A real draft keeps
# refreshing the timestamp on every keystroke; three quiet minutes means the
# "draft" is scroll residue and injection may proceed.
_COMPOSER_DIRTY_TTL_S = getattr(config, "INTERACTIVE_COMPOSER_DIRTY_TTL_S", 180.0)
# The keys a TUI dialog takes (a picker's arrows, Home, End and pages, Tab
# and Shift+Tab, space, digits, Esc, Ctrl+C): typed while one shows they
# answer it and leave the TUI's input box as it was.
_DIALOG_KEYS_RE = re.compile(rb"(?:\x1b\[[ABCDHFZ]|\x1bO[ABCDHF]|\x1b\[[56]~|[\x1b\t\x03 0-9])+")
# otodock-attached injection: how long to await the satellite's
# pty_inject_result before treating it as lost and re-sending the SAME
# inject_id (the satellite dedupes recent ids and re-ACKs, so a lost result
# frame can never double-inject).
_SATELLITE_INJECT_TIMEOUT_S = getattr(
    config, "INTERACTIVE_SATELLITE_INJECT_TIMEOUT_S", 20.0,
)

# Callbacks supplied by the WS layer. Either may be sync or return a coroutine.
OutputListener = Callable[[bytes], "Awaitable[None] | None"]
PermEventCb = Callable[[dict], "Awaitable[None] | None"]
CloseCb = Callable[["InteractiveSession", str], "Awaitable[None] | None"]
# Fired once when an autonomous interactive TASK's run completes (turn-end +
# bg-empty). The arg is the final assistant/agent message text. Set by the
# scheduler's interactive-task watcher; left None for human-driven chats.
TurnCompleteCb = Callable[[str], "Awaitable[None] | None"]
# A remote PTY's transport is reconnecting/reconnected (a satellite WS blip).
# Fired to the viewer so it can show a "reconnecting" banner + pause input.
StatusCb = Callable[[str], "Awaitable[None] | None"]

DEFAULT_SCROLLBACK_BYTES = 256 * 1024
DEFAULT_ROWS, DEFAULT_COLS = 24, 80
# on_output(rendered_bytes) / on_exit(exit_code|None) — either may be sync or
# return a coroutine (it's scheduled on the loop).
OutputCb = Callable[[bytes], "Awaitable[None] | None"]
ExitCb = Callable[["int | None"], "Awaitable[None] | None"]

# Told after every persisted batch and turn transition of a tailed session,
# ``fn(chat_id, *, persisted, turn_open)``: the socket package registers its
# duplex feed (``ws/duplex_attach.py``) when it is imported, before any
# duplex session can attach.
_batch_listeners: list[Callable[..., None]] = []


def add_batch_listener(fn: Callable[..., None]) -> None:
    """Register a batch listener (``_batch_listeners``); idempotent."""
    if fn not in _batch_listeners:
        _batch_listeners.append(fn)


async def _load_chat_row(chat_id: str) -> dict:
    """Spawn-time chat row read on the DB executor (title-arm + owner seed
    for the constructor). Best-effort: ``{}`` on any failure."""
    if not chat_id:
        return {}
    from storage import database as task_store
    from storage.pg import run_db
    try:
        return (await run_db(task_store.get_chat, chat_id)) or {}
    except psycopg.Error:
        logger.debug("interactive: chat row load failed for %s", chat_id[:8],
                     exc_info=True)
        return {}
