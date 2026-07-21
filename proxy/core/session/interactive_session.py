"""Interactive CLI session registry, single-process lease, idle reaper, drainer.

An *interactive* session is a PTY-backed CLI (``pty_relay.PtyProcess``) that the
proxy registers but does **not** drive turns for — there is no pump and no
``CommonEvent`` stream, because a PTY emits raw bytes, not events. This module
owns their lifecycle:

  * a per-session registry keyed by ``session_id``;
  * the **single-live-process lease** — registering a ``session_id``
    first kills any process already bound to it (mode toggle / cross-surface
    takeover), so shared session files never get two writers;
  * chat-slot acquisition + a hook into the 120s reconciler (``concurrency.py``)
    so an interactive slot is never mis-reaped or leaked;
  * an **idle reaper** that will NOT reap a session a viewer is watching, and
    treats PTY output as activity (so a long unviewed agent turn isn't killed
    mid-flight);
  * the **drainer** — a task that forwards the session's permission queue
    (permission prompts + display/file-tools artifacts pushed by ``api/hooks``)
    to a viewer callback, since there is no pump to drain it.

Output fan-out to viewers and the dashboard delivery of permission prompts are
supplied by the WS layer through callbacks (``add_output_listener`` /
``on_perm_event``); this module stays transport-agnostic and unit-testable.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from collections import deque
from typing import Awaitable, Callable, Optional

import config
from core.sandbox import pty_relay
from core.sandbox.pty_relay import PtyProcess
from core.terminal_queries import strip_queries as _strip_terminal_queries
from core.terminal_queries import strip_clipboard_writes as _strip_clipboard_writes
from core.terminal_queries import strip_replies as _strip_terminal_replies

logger = logging.getLogger("claude-proxy.interactive")

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
_MOUSE_RE = re.compile(rb"\x1b\[<[0-9;]*[Mm]|\x1b\[M.{3}", re.DOTALL)

# Idle reaping is unified across all session kinds via config.get_idle_timeout()
# (the admin `session_idle_timeout` setting); reap_idle() reads it per-sweep. A
# viewer attached, or any PTY byte in/out, keeps an interactive session alive
# regardless of the timeout (see reap_idle).
_REAPER_PERIOD_S = 60

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
# otodock-attached injection: how long to await the satellite's
# pty_inject_result before treating it as lost and re-sending the SAME
# inject_id (the satellite dedupes recent ids and re-ACKs, so a lost result
# frame can never double-inject).
_SATELLITE_INJECT_TIMEOUT_S = getattr(
    config, "INTERACTIVE_SATELLITE_INJECT_TIMEOUT_S", 20.0,
)

# Callbacks supplied by the WS layer. Either may be sync or return a coroutine.
OutputListener = Callable[[bytes], "Optional[Awaitable[None]]"]
PermEventCb = Callable[[dict], "Optional[Awaitable[None]]"]
CloseCb = Callable[["InteractiveSession", str], "Optional[Awaitable[None]]"]
# Fired once when an autonomous interactive TASK's run completes (turn-end +
# bg-empty). The arg is the final assistant/agent message text. Set by the
# scheduler's interactive-task watcher; left None for human-driven chats.
TurnCompleteCb = Callable[[str], "Optional[Awaitable[None]]"]
# A remote PTY's transport is reconnecting/reconnected (a satellite WS blip).
# Fired to the viewer so it can show a "reconnecting" banner + pause input.
StatusCb = Callable[[str], "Optional[Awaitable[None]]"]

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_sessions: dict[str, "InteractiveSession"] = {}
_lock: asyncio.Lock | None = None
_reaper_task: asyncio.Task | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


class CapacityError(RuntimeError):
    """Raised when no chat slot is available for a new interactive session."""


class InteractiveSession:
    """A registered, PTY-backed interactive CLI session.

    Construct via :func:`register` (which enforces the lease + slot). Holds the
    ``PtyProcess`` and the per-session viewer/permission plumbing.
    """

    # Class-level defaults for the cold-flush retry state: hand-built partial
    # instances (``__new__`` test doubles) reach _set_turn_open → _clear_cold_flush
    # without running __init__, and must degrade to a no-op.
    _cold_flush_pending: bytes = b""
    _cold_flush_seed = ""
    _cold_flush_attempts = 0
    _cold_flush_watcher: Optional[asyncio.TimerHandle] = None

    def __init__(
        self,
        *,
        session_id: str,
        chat_id: str,
        agent_name: str,
        user_sub: str = "",
        role: str = "",
        username: str = "",
        target: str = "local",
        remote_os: str = "",
        rows: int = pty_relay.DEFAULT_ROWS,
        cols: int = pty_relay.DEFAULT_COLS,
        transcript_kind: str = "claude",
        prompt_in_argv: bool = False,
        tui_theme: str = "dark",
    ) -> None:
        self.session_id = session_id
        self.chat_id = chat_id
        self.agent_name = agent_name
        self.user_sub = user_sub
        self.role = role
        self.username = username
        self.target = target
        # The remote satellite's OS ("windows"/"linux"/"darwin") from its reported
        # capabilities, or "" for local — scopes the Windows-ConPTY cold-submit
        # backstop (see _fire_submit). Set by register_remote.
        self.remote_os = remote_os
        self.rows = rows
        self.cols = cols
        # Which CLI's transcript persists this session's turns: "claude" reads the
        # Claude transcript JSONL (transcript_tailer); "codex" reads the Codex
        # rollout JSONL (codex_rollout_tailer). Set by the spawning layer.
        self.transcript_kind = transcript_kind
        # The TUI theme baked into this session at seed time. Dashboard viewers
        # render their xterm with THIS theme, not the dashboard mode — a
        # dark-seeded TUI (otodock-opened and re-warmed sessions default dark)
        # paints its text for a dark background, so a light xterm under it turns
        # white-on-white. Sent to viewers in the pty_status "attached" frame.
        self.tui_theme = "light" if str(tui_theme).lower().startswith("light") else "dark"

        self.pty: Optional[PtyProcess] = None
        self.created_at = time.monotonic()
        self.last_activity = self.created_at

        # otodock-CLI: True while a local `otodock` terminal is attached to
        # this session over the satellite's local control socket. The local
        # viewer is invisible to the proxy (it has no dashboard `_output_listeners`),
        # so without this the idle reaper would kill a watched-but-idle terminal.
        # Set by core.session.otodock_session after start; cleared when the satellite
        # reports the local socket closed (then it reaps like any unviewed session).
        self.otodock_attached = False
        # dual-control: when the dashboard CLAIMS an otodock-controlled session
        # it sends the satellite a detach + waits for the confirmation
        # (local_session_detached) to flip otodock_attached. This is the fallback
        # timer that flips it anyway if the satellite never confirms (unreachable),
        # so the dashboard is never permanently locked out. Set by ws/dashboard
        # _attach_pty_viewer; cancelled in otodock_session.detach_local_session.
        self._otodock_kick_timer: Optional[asyncio.TimerHandle] = None

        self._output_listeners: set[OutputListener] = set()
        # Single-viewer: the eviction callback of the CURRENT viewer.
        # A new viewer attaching evicts the prior one so two devices/tabs don't
        # fight over the one PTY size (cross-device garbling). Multi-viewer with
        # per-viewer sizing is a future enhancement. The callback takes a REASON
        # string (dual-control): "superseded" (another dashboard tab/device) or
        # "superseded_otodock" (a local `otodock` terminal took over) — the viewer
        # maps it to its banner ("opened on another device" / "opened in a local
        # terminal").
        self._viewer_evict: Optional[Callable[[str], "Optional[Awaitable[None]]"]] = None
        # True once ANY real viewer attached during this session's lifetime
        # (dashboard PTY view / otodock attach). A premature PTY death on an
        # interactive WORKER with had_viewer set reads as a deliberate user
        # stop (user_interrupted), not a crash — the scheduler keys on it.
        self.had_viewer = False
        self.on_perm_event: Optional[PermEventCb] = None
        self.on_close: Optional[CloseCb] = None
        # Child exit code, captured by _on_pty_exit BEFORE close(reason=
        # "exited") — the dashboard surfaces a non-zero code ("process exited
        # (code 1)") instead of a blank/ambiguous "session ended".
        self.exit_code: Optional[int] = None
        # Viewer hook fired when the REMOTE PTY transport is
        # reconnecting/reconnected (satellite WS blip). Single-slot (last viewer
        # wins), like on_close/on_perm_event. Stays None for local sessions.
        self.on_status: Optional[StatusCb] = None
        # Interactive-TASK run completion. Set by the scheduler's interactive-task
        # watcher; fired ONCE when a turn-end signal lands with the bg
        # SubagentRegistry empty + min-turn-time elapsed.
        self.on_turn_complete: Optional[TurnCompleteCb] = None
        self._turn_complete_fired = False
        # LLM chat-title generation: fired ONCE on the first completed turn, when
        # the transcript (first prompt + first assistant response) is in the DB.
        # Independent of on_turn_complete above (only autonomous TASKS set that) —
        # title-gen is dashboard-chats-only; the service skips task-/meeting-.
        self._title_fired = False

        self._loop = asyncio.get_running_loop()
        self._drainer: Optional[asyncio.Task] = None
        self._closing = False
        self._closed = False

        # Readiness gate (see module docs): input written before the TUI is ready
        # is buffered here and flushed by _mark_ready.
        #
        # `prompt_in_argv` (Codex fresh): the first prompt rides the launch argv and
        # the CLI auto-runs it after its own MCP warm, so there is NO cold prompt to
        # gate — start READY so the dashboard viewer's bytes (xterm focus reports /
        # DA replies + live keystrokes) pass straight through to the PTY instead of
        # being buffered and flushed LATE into the idle composer, where Codex's
        # Ratatui reads the stray escape sequences as cursor/nav keys (the "cursor
        # bouncing" bug on remote, where WS latency widens the window). The deferred
        # cold-submit Enter is likewise skipped (the argv prompt needs no Enter).
        self._ready = bool(prompt_in_argv)
        # The TUI has started rendering (a terminal control / ESC sequence has
        # appeared). Pre-TUI plain-text output (e.g. Codex's "Can't run AVX2
        # build…" native warning) must NOT arm the readiness settle timer, or the
        # warm gap (model/MCP load) satisfies it and the first prompt flushes
        # before the TUI can accept input. Set on the first ESC byte.
        self._render_started = False
        self._input_buffer: list[bytes] = []
        self._settle_handle: Optional[asyncio.TimerHandle] = None
        self._ready_max_handle: Optional[asyncio.TimerHandle] = None
        # Deferred submit (cold first-prompt Enter — see _SUBMIT_SETTLE_S). ONE
        # Enter once the composer settles (or _SUBMIT_MAX_S backstop), then an
        # OUTPUT-GATED re-fire if no burst follows (the Windows-ConPTY swallow).
        self._submit_settle_handle: Optional[asyncio.TimerHandle] = None
        self._submit_max_handle: Optional[asyncio.TimerHandle] = None
        # The Windows-remote Claude backstop Enter (the 2nd of 2) — its timer +
        # a counter so it fires at most once (0 = first Enter, 1 = backstop fired).
        self._submit_recheck_handle: Optional[asyncio.TimerHandle] = None
        self._submit_refires = 0
        # The FIRST line-submit (cold start) uses the settle path even on the LIVE
        # write path — the chat's first prompt can arrive via pty_input AFTER the
        # session went ready (→ live path), and a fixed-delay Enter then would be
        # too soon. Subsequent warm submits use the fast fixed gap.
        self._submitted_once = False
        # Cold-flush confirm-and-retry (see _COLD_RETRY_S): the flushed
        # first-prompt bytes (+ any seed digest) kept until the transcript
        # confirms the turn opened; a bounded watcher re-emits them if not.
        self._cold_flush_pending: bytes = b""
        self._cold_flush_seed = ""
        self._cold_flush_attempts = 0
        self._cold_flush_watcher: Optional[asyncio.TimerHandle] = None
        self._spawn_ts = time.monotonic()
        # DB-fallback resume: a restored-history digest stashed BEFORE the cold
        # flush; prepended (as a bracketed paste) to the first submission so the
        # fresh CLI continues with context. Cleared once consumed.
        self._pending_seed_digest = ""
        # Post-turn transcript tail (debounced on output quiet — see _POST_OUTPUT_TAIL_S).
        self._tail_handle: Optional[asyncio.TimerHandle] = None
        # Submit-triggered tails (dedicated handles): the post-output debounce
        # above is STARVED during generation (the TUI spinner redraws
        # continuously), so without these a prompt submitted in the terminal
        # never opens the turn — no sidebar dot / stop button — until the
        # turn ends. Remote sessions don't need them (the satellite forwards
        # transcript lines on its own cadence) but tolerate them (no-op tail).
        self._submit_tail_handles: list = []
        # Resume-check tail (LOCAL sessions): PTY output while the turn is
        # CLOSED means the CLI may have resumed on its own (a bg subagent
        # finished and re-invoked the main loop — no prompt, so no submit
        # tail) or a viewer is answering a question in the TUI. The starved
        # debounce above would only read the transcript after the resumed
        # stint ENDS, so the whole stint would run invisibly closed (no live
        # dot, dashboard Stop gated off). One short-fuse tail at a time,
        # re-armed by further output only while the turn is still closed.
        # Remote sessions don't need it (forwarded lines flow on the
        # satellite's cadence regardless of turn state).
        self._resume_tail_handle: Optional[asyncio.TimerHandle] = None
        # Server-prompt queue (delegate results …): injected at CLI quiescence,
        # FIFO, ONE item per turn. Items are dicts carrying full re-delivery
        # context (text/source/chat_id/agent/user_sub/role/hops + the ladder's
        # injected rung callables) so close() can hand undelivered items back
        # to the delivery ladder once the PTY is gone.
        self._prompt_queue: deque[dict] = deque()
        self._inject_backstop_handle: Optional[asyncio.TimerHandle] = None
        self._draining_prompts = False
        # otodock-attached: the ONE in-flight satellite injection —
        # {"inject_id", "sent_at"}. The queue head stays queued until the
        # satellite ACKs (handle_inject_result pops it), so a lost result
        # frame re-sends the same id and the satellite's dedupe re-ACKs.
        self._satellite_inject: Optional[dict] = None
        # Turn-open state, derived SOLELY from the tailers' last_signal (both
        # CLIs write the user message to the transcript at submit time, so no
        # submit tracking is needed): "user"/"tool_use" → open, "end_turn" →
        # closed. Set True synchronously on our own injection — the tailer
        # won't see the injected user line for one poll (0.8–3s). Transitions
        # broadcast the sidebar chat_status (streaming/ready) — interactive has
        # no pump, so these ARE the chat's live-dot signals.
        self._turn_open = False
        # Parked on an unanswered AskUserQuestion: the turn is CLOSED (question
        # fold) but the TUI is showing the dialog — server-prompt injection is
        # gated off (the paste would land in the dialog). Cleared on any
        # reopen or on a real end_turn (ESC dismissed the question).
        self._question_parked = False
        # The chat ROW's owner (lazy) — for shared-only agents it is the
        # synthetic agent::<slug>, which the status broadcast fans out to
        # every user of the agent; self.user_sub would reach only one.
        self._chat_owner_sub: str | None = None
        # Composer-dirty heuristic for the injection gates: printable viewer
        # input (terminal replies + mouse reports excluded) not yet submitted.
        # Never inject into a non-empty composer — the paste + CR would submit
        # the user's partial text merged with the server prompt. The timestamp
        # gives the flag a TTL: mirror bytes that only LOOK like typing (wheel
        # scroll = arrow keys on a TUI alt-screen) otherwise stick forever and
        # starve queued delegate results (observed: depth=3 queue never
        # injected). A genuinely typing user re-dirties on every keystroke.
        self._composer_dirty = False
        self._composer_dirty_at = 0.0
        # Last held-gate reason logged for this session (change-only INFO
        # logging in the drain — the block reason is the whole diagnosis when
        # a delegate result sits queued on a live session).
        self._last_held_reason: str | None = None
        # Serialize persistence of transcript lines FORWARDED from a remote
        # satellite (the satellite tails its JSONL + sends new lines; we persist
        # them in arrival order). Local sessions never use this (they read the
        # on-disk JSONL via the debounced tail above).
        self._transcript_lock = asyncio.Lock()

    # -- activity / idle ------------------------------------------------------
    def _note_activity(self) -> None:
        self.last_activity = time.monotonic()

    @property
    def turn_open(self) -> bool:
        """Live turn state (read by the warmup re-attach path so the dashboard
        can reconcile the sidebar live-dot to server truth on every visit)."""
        return self._turn_open

    @property
    def question_parked(self) -> bool:
        """Turn parked on an unanswered question dialog (read by the dashboard
        input path so a composer send can be held instead of typed into it)."""
        return self._question_parked

    # -- readiness gate -------------------------------------------------------
    def _arm_readiness(self) -> None:
        """Start the force-ready fallback timer (output may never settle)."""
        self._ready_max_handle = self._loop.call_later(READY_MAX_S, self._mark_ready)

    def _mark_ready(self) -> None:
        """The TUI is ready — flush any input buffered before now."""
        if self._ready:
            return
        self._ready = True
        for h in (self._settle_handle, self._ready_max_handle):
            if h is not None:
                h.cancel()
        self._settle_handle = self._ready_max_handle = None
        buf = b"".join(self._input_buffer)
        self._input_buffer = []
        if buf:
            # Keep the flush (+ seed digest) for the confirm-and-retry watcher
            # (_fire_submit arms it once the cold Enter goes out) — readiness is
            # a heuristic and this flush can be swallowed by a still-booting
            # composer.
            self._cold_flush_pending = buf
            self._cold_flush_seed = self._pending_seed_digest
            self._cold_flush_attempts = 0
            # Cold flush: send Enter only after the TUI's echo settles (the input
            # box has just rendered — a fixed short delay can miss it).
            self._emit_to_pty(buf, deferred_submit=True)
            logger.info(
                "interactive %s: ready — flushed %d buffered input byte(s)",
                self.session_id[:8], len(buf),
            )

    @property
    def has_viewer(self) -> bool:
        return bool(self._output_listeners)

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity

    @property
    def alive(self) -> bool:
        return not self._closed and self.pty is not None and not self.pty.closed

    # -- viewers (output fan-out) --------------------------------------------
    def add_output_listener(
        self, cb: OutputListener,
        on_evict: "Optional[Callable[[str], Optional[Awaitable[None]]]]" = None,
    ) -> bytes:
        """Attach a viewer. Returns the scrollback to replay so the viewer sees
        the current screen (reconnect / late-attach).

        Single-viewer: when ``on_evict`` is given, this is a dashboard
        viewer — it EVICTS the previous viewer first (drops its listener + fires
        its evict callback with reason "superseded") so two devices/tabs never
        mirror the same PTY at different sizes (which garbles both). Pass no
        ``on_evict`` for internal/test listeners, which don't evict."""
        prev_evict = self._viewer_evict
        if on_evict is not None and prev_evict is not None and prev_evict is not on_evict:
            self._output_listeners.clear()  # the old viewer's listener goes too
        self._output_listeners.add(cb)
        if on_evict is not None:
            self._viewer_evict = on_evict
            self.had_viewer = True
        self._note_activity()
        if on_evict is not None and prev_evict is not None and prev_evict is not on_evict:
            try:
                res = prev_evict("superseded")
                if asyncio.iscoroutine(res):
                    self._loop.create_task(res)
            except Exception:
                logger.exception("interactive %s: viewer evict failed", self.session_id[:8])
        if self.pty is None:
            return b""
        sb = self.pty.scrollback()
        # Mirror boundary (replay): strip the app's terminal queries so the
        # attaching viewer has nothing stale to auto-answer — a raw replay makes
        # the mirror re-answer every buffered query on each (re)attach (see
        # core.terminal_queries). Remote rings are pre-stripped at feed
        # (remote_pty._feed_output); a local PTY ring is raw, so strip here.
        # Clipboard writes (OSC 52 set) are replay-stripped for BOTH targets:
        # they pass live (the TUI's copy feature) but a replayed one would
        # overwrite the viewer's clipboard on page open.
        if self.target == "local":
            sb = _strip_terminal_queries(sb)
        return _strip_clipboard_writes(sb)

    def remove_output_listener(self, cb: OutputListener) -> None:
        self._output_listeners.discard(cb)
        if not self._output_listeners:
            self._viewer_evict = None

    def evict_viewer(self, reason: str = "superseded") -> None:
        """dual-control: detach the current dashboard viewer (if any) WITHOUT
        killing the PTY — used when the local `otodock` terminal takes over a live
        session. Fires the viewer's evict callback (which sends it a ``pty_exit
        {reason}`` so it shows the take-over banner and stops driving), then drops
        the listener + the single-slot callbacks so output/perm/status stop
        fanning out to the now-detached socket. Fully SYNCHRONOUS (it only
        SCHEDULES the evict's WS send as a task), so it is safe to call under the
        registry lock without racing ``close()``'s ``on_close`` read."""
        ev = self._viewer_evict
        self._output_listeners.clear()
        self._viewer_evict = None
        self.on_perm_event = None
        self.on_close = None
        self.on_status = None
        if ev is not None:
            try:
                res = ev(reason)
                if asyncio.iscoroutine(res):
                    self._loop.create_task(res)
            except Exception:
                logger.exception("interactive %s: evict_viewer failed", self.session_id[:8])

    def notify_status(self, state: str) -> None:
        """Tell the current viewer the REMOTE PTY transport state changed —
        "reconnecting" on a satellite WS drop, "reconnected" on re-adopt.
        Fire-and-forget; no-op when no viewer wired ``on_status`` (e.g. a
        background interactive task, or a local session)."""
        if self.on_status is None:
            return
        try:
            res = self.on_status(state)
            if asyncio.iscoroutine(res):
                self._loop.create_task(res)
        except Exception:
            logger.exception("interactive %s: on_status failed", self.session_id[:8])

    def _fanout_output(self, data: bytes) -> None:
        # PtyProcess.on_output → here. PTY output counts as activity (the agent
        # is working) so an unviewed long turn isn't reaped.
        self._note_activity()
        # Readiness: output is flowing → (re)arm the settle timer. Ready fires
        # when output goes quiet for READY_SETTLE_S (the TUI finished rendering).
        if not self._ready:
            # Only count output as "the TUI is rendering" once a terminal control
            # sequence (ESC) appears — skips Codex's pre-TUI plain-text warnings so
            # the settle timer measures quiet AFTER the render, not during the warm
            # gap. Claude's Ink TUI emits ESC from its first frame, so this is
            # layer-agnostic; READY_MAX_S remains the hard backstop.
            if not self._render_started and b"\x1b" in data:
                self._render_started = True
            if self._render_started:
                if self._settle_handle is not None:
                    self._settle_handle.cancel()
                # Floor the settle so ready can't fire before READY_MIN_S since
                # spawn: the first post-render quiet is usually the MCP warm,
                # not a live composer.
                delay = max(READY_SETTLE_S,
                            READY_MIN_S - (time.monotonic() - self._spawn_ts))
                self._settle_handle = self._loop.call_later(delay, self._mark_ready)
        # A deferred cold-start submit is waiting for the echo to settle — push
        # its Enter out while the text is still echoing (the max-fallback caps it).
        if self._submit_settle_handle is not None:
            self._submit_settle_handle.cancel()
            self._submit_settle_handle = self._loop.call_later(_SUBMIT_SETTLE_S, self._fire_submit)
        # Post-turn tail: once the TUI is ready, debounce a transcript tail on
        # output quiet so chat_messages + the title land within seconds of a turn
        # (the Stop hook is unreliable; the 60s reaper sweep is just the backstop).
        if self._ready and self.chat_id:
            if self._tail_handle is not None:
                self._tail_handle.cancel()
            self._tail_handle = self._loop.call_later(_POST_OUTPUT_TAIL_S, self._run_post_output_tail)
            # Self-resume detection: output while the turn is CLOSED → check the
            # transcript promptly (the debounce above is starved while output
            # flows). NOT re-armed per chunk — one probe per fuse window, and
            # only while the turn stays closed. Local only: remote turn state
            # rides the forwarded transcript lines.
            if (not self._turn_open and self.target == "local"
                    and self._resume_tail_handle is None):
                self._resume_tail_handle = self._loop.call_later(
                    _RESUME_TAIL_S, self._run_resume_tail)
        # Mirror boundary (live): listeners are dashboard mirrors — strip the
        # app's terminal queries so xterm.js never auto-answers one (the answers
        # would ride pty_input back as phantom keystrokes; see
        # core.terminal_queries). Remote sessions arrive pre-stripped
        # (remote_pty._feed_output); local PTY bytes are stripped here. The
        # readiness/settle/tail logic above intentionally saw the RAW bytes.
        mirror_data = _strip_terminal_queries(data) if self.target == "local" else data
        if not mirror_data:
            return
        for cb in list(self._output_listeners):
            try:
                res = cb(mirror_data)
                if asyncio.iscoroutine(res):
                    self._loop.create_task(res)
            except Exception:
                logger.exception("interactive %s: output listener failed", self.session_id[:8])

    # -- input / resize (proxy → PTY) ----------------------------------------
    def deliver_dashboard_input(self, data: bytes, composer: bool = False) -> None:
        """Dashboard input router. ``composer=True`` marks a discrete chat-box
        send (the FE flags it; raw terminal keystrokes are never flagged):
        while the turn is question-parked, typing it through would land the
        paste in the open picker's notes field and the trailing CR would
        submit the recommended option — so the text is HELD in the server
        prompt queue instead and injects via the normal drain once the
        question is answered and that turn completes. Everything else funnels
        straight to :meth:`write_input` (answering in the TUI keeps working)."""
        if composer and self._question_parked:
            text = data.decode("utf-8", errors="replace")
            if text.startswith("\x1b[200~") and text.endswith("\x1b[201~\r"):
                text = text[6:-7]
            text = text.rstrip("\r\n")
            if text and self.queue_prompt(text, source="dashboard"):
                return
        self.write_input(data)

    def write_input(self, data: bytes) -> None:
        if not self.alive:
            return
        # dual-control: while a local `otodock` terminal is the active
        # controller (otodock_attached), the dashboard is a DETACHED (evicted)
        # viewer — DROP its input so the two never fight over the one PTY. This is
        # the authoritative server-side gate covering every dashboard write path
        # (pty_input, pty_attachments, and submit_prompt all funnel here). The
        # otodock terminal drives the PTY satellite-side (session.write), never
        # through this proxy method, so it is unaffected.
        if self.otodock_attached:
            return
        # Drop the mirror xterm's terminal auto-responses FIRST (see
        # core.terminal_queries.TERMINAL_REPLY_RE): they aren't user input,
        # must not reach the PTY, and must not cancel the pending cold-submit
        # Enter (the Windows-remote first-prompt-never-submits bug).
        data = _strip_terminal_replies(data)
        if not data:
            return
        self._note_activity()
        # Composer-dirty tracking (injection gate): only the residue that would
        # also cancel the cold submit counts as typing — mouse reports are
        # pointer, not text. A write ENDING with CR/NL is a submit (interior
        # paste newlines don't submit); Ctrl-C empties the composer.
        _typed = _MOUSE_RE.sub(b"", data)
        if _typed:
            if _typed.endswith((b"\r", b"\n")):
                self._composer_dirty = False
                # A terminal-side SUBMIT (Enter in the dashboard xterm): if it
                # starts a turn, the transcript journals the user line within
                # a beat — tail promptly so the turn OPENS live.
                self._schedule_submit_tails()
            elif b"\x03" in _typed:
                self._composer_dirty = False
            else:
                self._composer_dirty = True
                self._composer_dirty_at = time.monotonic()
        if not self._ready:
            # TUI not ready yet — buffer; _mark_ready flushes it. Prevents the
            # first prompt from being sent before the CLI can accept input.
            self._input_buffer.append(data)
            return
        # Real user input after the cold start → cancel the pending cold-submit
        # Enter (the user is driving now). But mouse-tracking reports (pointer
        # move/click the xterm forwards) are NOT message input — they must reach
        # the TUI yet must not pre-empt the first prompt, so cancel only when
        # something OTHER than mouse bytes remains.
        if (self._submit_settle_handle or self._submit_max_handle
                or self._submit_recheck_handle) and _MOUSE_RE.sub(b"", data):
            # Log the event only — never the input bytes (they can carry message
            # content or pasted secrets).
            logger.info(
                "interactive %s: user input cancels pending cold-submit (%d bytes)",
                self.session_id[:8], len(data),
            )
            self._cancel_deferred_submit()
            self._clear_cold_flush()  # the user is driving — no replay
        self._emit_to_pty(data)

    def interrupt_turn(self) -> bool:
        """Dashboard Stop on an interactive chat: press ESC in the TUI — the
        native stop-generation key in both CLIs. Gated on an OPEN turn (a
        stray ESC on an idle TUI clears the composer / opens codex's
        backtrack picker) and on dashboard control (an attached otodock
        terminal owns its own keys — its viewer is evicted anyway). Returns
        True when the ESC was actually sent; the turn state then closes via
        the transcript interrupt markers, never from this call."""
        if not self.alive or self.otodock_attached or not self._turn_open:
            return False
        self._note_activity()
        self._emit_to_pty(b"\x1b")
        logger.info("interactive %s: dashboard abort sent ESC", self.session_id[:8])
        return True

    def submit_prompt(self, text: str, *, settle: bool = False) -> None:
        """Deliver a COMPLETE prompt to the TUI and submit it.

        Single-line text is a plain line-submit (text + CR). MULTI-LINE text (a
        multi-line task prompt) is sent as a **bracketed paste** (``ESC[200~ …
        ESC[201~``) so the TUI inserts it verbatim instead of submitting at the
        first newline, then a trailing CR submits (mirrors the frontend's
        multi-line interactive send). The CR rides the readiness gate + the single
        deferred Enter like any cold prompt, so it lands even before the TUI is
        ready.

        ``settle=True`` (server-injected prompts — delegate wakes, queued
        follow-ups): a large bracketed paste can still be ingesting when the
        warm fixed-gap Enter lands — the TUI swallows it and the text sits in
        the composer unsent. Route the trailing Enter through the cold-submit
        settle machinery instead (echo-quiet + max backstop). Guards mirror
        ``write_input`` (dual-control drop, readiness buffering) minus the
        composer-dirty tracking — this is not user typing, and it must not
        cancel its own pending Enter the way real user input would."""
        raw = text.encode("utf-8")
        if b"\n" in raw or b"\r" in raw:
            data = b"\x1b[200~" + raw.replace(b"\r\n", b"\n") + b"\x1b[201~\r"
        else:
            data = raw + b"\r"
        if not settle:
            self.write_input(data)
            return
        if not self.alive or self.otodock_attached:
            return
        self._note_activity()
        if not self._ready:
            self._input_buffer.append(data)
            return
        self._emit_to_pty(data, deferred_submit=True)

    def _emit_to_pty(self, data: bytes, deferred_submit: bool = False) -> None:
        """Write to the PTY, ensuring a line-submit actually submits.

        A line-submit (text + a trailing newline, e.g. ChatInput's "text\\r" or
        the flushed first prompt) is written as the text, then Enter SEPARATELY —
        one combined write often lands in the TUI's input box without submitting.
        A bare Enter / single keystroke (live typing) is written as-is.

        ``deferred_submit`` (the cold first-prompt flush): ALWAYS arm the Enter
        (settle path) regardless of the buffer's trailing byte. A viewer's
        just-attached xterm can append terminal-response bytes AFTER the prompt's
        ``\\r``, so for a WATCHED chat the trailing byte is no longer ``\\r`` and the
        generic newline test below would miss it — the prompt then sits unsent. The
        freshly-rendered TUI may not have ingested the text yet, so the single armed
        Enter waits for the echo to settle (_SUBMIT_SETTLE_S) before firing."""
        if not self.alive:
            return
        is_submit = deferred_submit or (len(data) > 1 and data[-1:] in (b"\r", b"\n"))
        # DB-fallback resume: prepend the restored-history digest right before
        # the FIRST submission, as its own bracketed paste, so it lands as composer
        # content (NOT N separate submits) ahead of the user's prompt → one combined
        # turn. Independent of how the prompt was delivered (a pty_input paste for
        # Claude, submit_prompt for Codex). The tailers strip the digest from the
        # persisted user message.
        if is_submit and not self._submitted_once and self._pending_seed_digest:
            digest = self._pending_seed_digest
            self._pending_seed_digest = ""
            self.pty.write(
                b"\x1b[200~"
                + digest.encode("utf-8").replace(b"\r\n", b"\n")
                + b"\n\n\x1b[201~"
            )
        if deferred_submit:
            body = data.rstrip(b"\r\n")
            if body:
                self.pty.write(body)
            self._submitted_once = True
            self._arm_deferred_submit()
            return
        if len(data) > 1 and data[-1:] in (b"\r", b"\n"):
            body = data.rstrip(b"\r\n")
            if body:
                self.pty.write(body)
            # FIRST live line-submit uses the settle path (the TUI may not have
            # ingested the text yet); warm submits after use the fast fixed gap.
            if not self._submitted_once:
                self._arm_deferred_submit()
            else:
                self._loop.call_later(
                    _SUBMIT_ENTER_DELAY_S,
                    lambda: self.pty.write(b"\r") if self.alive else None,
                )
                self._schedule_submit_tails()
            self._submitted_once = True
        else:
            self.pty.write(data)

    def _schedule_submit_tails(self) -> None:
        """A prompt was just SUBMITTED to the TUI (terminal Enter, dashboard
        line-submit, injected prompt, or the deferred cold Enter): tail the
        transcript shortly after, so the turn OPENS (sidebar pulsing dot +
        stop button) the moment the CLI journals the user line. The
        post-output debounce can never do this — the TUI spinner redraws
        continuously during generation, starving it until the turn ends.
        Two one-shots tolerate a slow first journal write; the tailer's line
        cursor makes re-runs idempotent."""
        if self._closed or not self._ready or not self.chat_id:
            return
        for h in self._submit_tail_handles:
            with contextlib.suppress(Exception):
                h.cancel()
        self._submit_tail_handles = [
            self._loop.call_later(delay, self._run_post_output_tail)
            for delay in (1.5, 4.0)
        ]

    def _arm_deferred_submit(self) -> None:
        """Arm the cold-first-prompt Enter.

        Fires the FIRST Enter when the echo goes quiet for _SUBMIT_SETTLE_S (re-armed
        on each output by _fanout_output, so a still-rendering / MCP-warming TUI
        keeps it from firing early), or at _SUBMIT_MAX_S as a backstop. After that,
        :meth:`_fire_submit` then schedules ONE backstop Enter for Windows-remote
        Claude only."""
        self._cancel_deferred_submit()
        self._submit_refires = 0
        self._submit_settle_handle = self._loop.call_later(_SUBMIT_SETTLE_S, self._fire_submit)
        self._submit_max_handle = self._loop.call_later(_SUBMIT_MAX_S, self._fire_submit)

    def _fire_submit(self) -> None:
        self._cancel_deferred_submit()
        if not self.alive:
            return
        self.pty.write(b"\r")
        self._schedule_submit_tails()  # the turn starts NOW — open it live
        logger.info(
            "interactive %s: cold-submit Enter fired (try %d)",
            self.session_id[:8], self._submit_refires + 1,
        )
        # A COLD flush (readiness-buffered first prompt) gets a confirmation
        # watcher: if the transcript never journals the user line, this Enter
        # was swallowed and the flush must be replayed.
        if self._cold_flush_pending:
            self._arm_cold_flush_watch()
        # WINDOWS-remote Claude ONLY: the ConPTY render race can swallow this single
        # post-settle Enter (it lands first-try local + Linux-remote). Fire ONE more
        # Enter _SUBMIT_WIN_BACKSTOP_S later — by then the TUI is idle. 2 Enters
        # total: an extra Enter on an already-submitted/empty composer is a no-op;
        # the narrow accepted risk is a native menu shown within the gap (it'd select
        # an option). Codex / Linux-remote / local: no backstop (single Enter works).
        if (self._submit_refires == 0 and self.transcript_kind == "claude"
                and self.remote_os == "windows"):
            self._submit_refires = 1
            self._submit_recheck_handle = self._loop.call_later(
                _SUBMIT_WIN_BACKSTOP_S, self._fire_submit,
            )

    def _cancel_deferred_submit(self) -> None:
        for h in (self._submit_settle_handle, self._submit_max_handle,
                  self._submit_recheck_handle):
            if h is not None:
                h.cancel()
        self._submit_settle_handle = self._submit_max_handle = None
        self._submit_recheck_handle = None

    # -- cold-flush confirm-and-retry ------------------------------------------
    def _arm_cold_flush_watch(self) -> None:
        if self._cold_flush_watcher is not None:
            self._cold_flush_watcher.cancel()
        self._cold_flush_watcher = self._loop.call_later(
            _COLD_RETRY_S, self._check_cold_flush,
        )

    def _check_cold_flush(self) -> None:
        """The cold Enter fired _COLD_RETRY_S ago. If the transcript never
        confirmed the turn (journaled user line → ``_turn_open``), the flush was
        swallowed by a still-booting composer — replay it. Bounded; stands down
        the moment the turn opens (:meth:`_set_turn_open`) or the user starts
        driving (``write_input`` clears the pending flush on real input)."""
        self._cold_flush_watcher = None
        if (not self._cold_flush_pending or self._turn_open
                or not self.alive or self._closing or self._closed):
            return
        if self._cold_flush_attempts >= _COLD_RETRY_MAX:
            logger.warning(
                "interactive %s: cold first prompt unconfirmed after %d "
                "attempt(s) — giving up (the user must resend)",
                self.session_id[:8], self._cold_flush_attempts + 1,
            )
            self._cold_flush_pending = b""
            self._cold_flush_seed = ""
            return
        self._cold_flush_attempts += 1
        logger.info(
            "interactive %s: cold first prompt unconfirmed — re-emitting "
            "(retry %d/%d)",
            self.session_id[:8], self._cold_flush_attempts, _COLD_RETRY_MAX,
        )
        # Replay the ORIGINAL flush shape: restore the seed digest and the
        # first-submission flag so _emit_to_pty pastes seed + body + arms the
        # deferred Enter exactly like the swallowed attempt did. Synchronous
        # within this callback — nothing can interleave before the emit.
        if self._cold_flush_seed:
            self._pending_seed_digest = self._cold_flush_seed
        self._submitted_once = False
        self._emit_to_pty(self._cold_flush_pending, deferred_submit=True)

    def _clear_cold_flush(self) -> None:
        self._cold_flush_pending = b""
        self._cold_flush_seed = ""
        if self._cold_flush_watcher is not None:
            self._cold_flush_watcher.cancel()
            self._cold_flush_watcher = None

    def set_pending_seed(self, digest: str) -> None:
        """Stash a restored-history digest (DB-fallback) to prepend to the cold
        first prompt. Set by the spawn path right after the session is created and
        BEFORE the readiness flush, so :meth:`_emit_to_pty` pastes it ahead of the
        user's first prompt. No-op for an empty digest."""
        if digest:
            self._pending_seed_digest = digest

    # -- server-prompt injection (delegate results …) --------------------------
    def queue_prompt(self, text: str, source: str, **context) -> bool:
        """Queue a server-originated prompt for injection at CLI quiescence.

        ``steer=True`` in the context marks the item steer-eligible: with an
        OPEN turn on a local PTY it injects mid-turn instead of waiting for
        quiescence (see ``_prompt_gates_blocked``); otherwise the flag is
        inert. Returns False when the session can't take it (dead/closing) so
        the delivery ladder falls through to its headless rungs. ``context`` is
        the re-delivery payload (chat_id/agent/user_sub/role/hops + the ladder's
        rung callables) that close() hands back if the PTY dies first.
        Deliberately does NOT count as activity — a starved queue must not
        immortalize an unviewed session against the idle reaper (reap → close
        → handback is the designed escape for a composer that never clears)."""
        if not self.alive or self._closing or self._closed:
            return False
        self._prompt_queue.append({"text": text, "source": source, **context})
        logger.info(
            "interactive %s: queued server prompt [%s] (depth=%d)",
            self.session_id[:8], source, len(self._prompt_queue),
        )
        self._loop.create_task(self._try_drain_prompt_queue())
        return True

    def _apply_turn_signal(self, last_signal: str | None,
                           question_pending: bool = False) -> None:
        """Fold a tailer batch's ``last_signal`` into the turn-open state.
        ``question_pending`` tracks the question-parked flag alongside: set on
        a question fold, cleared by the next turn-relevant signal (reopen or
        real end_turn); signal-less batches leave it untouched."""
        if question_pending:
            self._question_parked = True
        elif last_signal is not None:
            self._question_parked = False
        if last_signal in ("user", "tool_use"):
            self._set_turn_open(True)
        elif last_signal == "end_turn":
            if self._turn_open:
                self._set_turn_open(False)
            else:
                # A quick turn can fit in ONE tailer batch (fresh sessions
                # surface their transcript late): the fold then sees only the
                # closing signal and the open transition never happens. The
                # close-side effects must still run, or a short background
                # turn never stamps last_response_at / lights the unread dot.
                self._turn_end_effects()

    def _chat_owner(self) -> str:
        """The chat ROW's owner sub (synthetic ``agent::<slug>`` for shared-only
        agents) — the identity the chat_status fan-out keys on. Lazy, cached."""
        if self._chat_owner_sub is None:
            try:
                from storage import database as task_store
                row = task_store.get_chat(self.chat_id) or {}
                self._chat_owner_sub = row.get("user_sub") or self.user_sub or ""
            except Exception:
                self._chat_owner_sub = self.user_sub or ""
        return self._chat_owner_sub

    def _set_turn_open(self, is_open: bool) -> None:
        """Set the turn-open flag; on a TRANSITION, broadcast the sidebar
        live-dot signal (streaming/ready) — interactive sessions have no pump,
        so without this a background interactive turn never lights the dot and
        a crash mid-turn never clears it. The close transition also stamps
        ``chats.last_response_at`` for the unread indicator. Meeting chats are
        skipped (per-speaker turns are not chat-level activity)."""
        was = self._turn_open
        self._turn_open = is_open
        if is_open:
            self._question_parked = False  # any open unparks (answer/inject)
            self._clear_cold_flush()  # prompt confirmed — retry stands down
        if is_open == was or not self.chat_id or self.chat_id.startswith("meeting-"):
            return
        if is_open:
            try:
                from services.notifications import notification_manager
                notification_manager.broadcast_chat_status(
                    self._chat_owner(), self.chat_id, "streaming",
                    agent=self.agent_name,
                )
            except Exception:
                logger.exception(
                    "interactive %s: turn-status broadcast failed", self.session_id[:8]
                )
        else:
            self._turn_end_effects()

    def _turn_end_effects(self) -> None:
        """A turn genuinely ended on this chat: stamp ``last_response_at`` and
        broadcast the ``ready`` live-dot signal. Runs on the open→closed
        transition AND on an end_turn signal with no prior open (whole turn in
        one tailer batch) — both are real turn ends. Idempotent."""
        self._kick_prompt_queue_post_turn()
        if not self.chat_id or self.chat_id.startswith("meeting-"):
            return
        try:
            from services.notifications import notification_manager
            try:
                from datetime import datetime, timezone
                from storage import database as task_store
                task_store.update_chat(
                    self.chat_id,
                    last_response_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                pass
            notification_manager.broadcast_chat_status(
                self._chat_owner(), self.chat_id, "ready",
                agent=self.agent_name,
            )
            if self.otodock_attached:
                # The response just rendered on a live otodock terminal — that
                # IS the read (the dashboard's visible-tab rule, terminal
                # edition). Without this the fixed unread dot lingers until
                # someone opens the chat in the dashboard. Detached-at-turn-end
                # chats keep the dot: nobody saw the answer.
                try:
                    from storage import database as task_store
                    task_store.mark_chat_read(self.chat_id, self._chat_owner())
                    notification_manager.broadcast_chat_read(
                        self._chat_owner(), self.chat_id, agent=self.agent_name,
                    )
                except Exception:
                    pass
        except Exception:
            logger.exception(
                "interactive %s: turn-status broadcast failed", self.session_id[:8]
            )

    def _kick_prompt_queue_post_turn(self) -> None:
        """A turn just closed with prompts queued: schedule a drain right after
        the quiet window instead of leaving delivery to the 5s backstop cadence
        — a queued delegate result should land AT the turn boundary. The drain
        re-checks every gate itself, so a late fire on a closed/busy session is
        a no-op."""
        if not self._prompt_queue or self._closing or self._closed:
            return
        self._loop.call_later(
            _INJECT_QUIET_S + 0.1,
            lambda: self._loop.create_task(self._try_drain_prompt_queue()),
        )

    def _arm_inject_backstop(self) -> None:
        if (self._inject_backstop_handle is None and self._prompt_queue
                and not self._closing and not self._closed):
            self._inject_backstop_handle = self._loop.call_later(
                _INJECT_BACKSTOP_S, self._fire_inject_backstop,
            )

    def _fire_inject_backstop(self) -> None:
        self._inject_backstop_handle = None
        if self._prompt_queue and not self._closing and not self._closed:
            self._loop.create_task(self._try_drain_prompt_queue())

    def _prompt_gates_blocked(
        self, *, for_satellite: bool = False, steering: bool = False,
    ) -> str | None:
        """The first injection gate that blocks right now, or None when clear.
        Cheap, synchronous — callable before AND after the freshness tail.
        ``for_satellite`` skips the composer gate: while otodock-attached the
        dashboard input path is dropped (so ``_composer_dirty`` is stale) and
        the LOCAL terminal's line state is the satellite's to judge.

        ``steering`` + an OPEN turn drops the ``turn_open``/``not_quiet``/
        ``bg_pending`` gates: both TUIs treat mid-turn typed input as a steer
        (consumed between tool calls), and a streaming turn's output resets
        the quiet clock forever, so those gates would starve a steer. All
        SAFETY gates stay — a dialog, a dirty composer, or an in-flight
        cold submit blocks steers too. A steer item with the turn CLOSED is
        an ordinary inject (full gate run)."""
        steer_live = steering and self._turn_open
        if not self.alive or self._closing or self._closed:
            return "dead"
        if not self._ready:
            return "not_ready"
        # A pending cold-submit Enter means the user's first prompt is still in
        # flight — injecting now would cancel that Enter and merge the prompts.
        if (self._submit_settle_handle is not None
                or self._submit_max_handle is not None
                or self._submit_recheck_handle is not None):
            return "cold_submit_pending"
        if self._turn_open and not steer_live:
            return "turn_open"
        # An unstamped dirty flag (no timestamp yet) holds unconditionally —
        # only a KNOWN-stale draft may expire.
        if (not for_satellite and self._composer_dirty
                and (not self._composer_dirty_at
                     or time.monotonic() - self._composer_dirty_at
                     < _COMPOSER_DIRTY_TTL_S)):
            return "composer_dirty"
        if not steer_live and self.idle_seconds < _INJECT_QUIET_S:
            return "not_quiet"
        # Shared completion gates (mirror _maybe_fire_turn_complete): a young
        # session may still be warming; a pending bg subagent means a follow-up
        # turn is coming (irrelevant mid-steer: the target turn is open NOW).
        if (time.monotonic() - self.created_at) < MIN_TURN_S:
            return "too_young"
        if not steer_live:
            try:
                from core.session.session_state import get_subagent_registry
                if get_subagent_registry(self.session_id).has_pending:
                    return "bg_pending"
            except Exception:
                pass
        # A pending hook permission prompt: the TUI is waiting on a dialog —
        # an injected CR would answer it.
        try:
            from core.session.session_state import _session_permission_requests
            if _session_permission_requests.get(self.session_id):
                return "permission_pending"
        except Exception:
            pass
        # Parked on an AskUserQuestion dialog (turn folded closed): the paste +
        # CR would land in the question picker instead of the composer.
        if self._question_parked:
            return "question_parked"
        return None

    def _log_held(self, reason: str) -> None:
        """Held-gate reason, logged at INFO on CHANGE only (the backstop
        retries every few seconds — repeat reasons stay at debug). A delegate
        result sitting queued on a live session was undiagnosable in the
        field: the block reason never reached the logs."""
        if reason != self._last_held_reason:
            self._last_held_reason = reason
            logger.info(
                "interactive %s: prompt injection held (%s, depth=%d)",
                self.session_id[:8], reason, len(self._prompt_queue),
            )
        else:
            logger.debug(
                "interactive %s: prompt injection held (%s)",
                self.session_id[:8], reason,
            )

    async def _try_drain_prompt_queue(self) -> None:
        """Inject the next queued server prompt iff the CLI is quiescent.

        ONE item per attempt — the injected prompt opens a turn, so the next
        item waits for that turn's ``end_turn``. Re-arms the backstop timer
        whenever the queue stays non-empty (gate blocked / more items)."""
        if self._draining_prompts:
            return
        self._draining_prompts = True
        try:
            if not self._prompt_queue:
                return
            if self.otodock_attached:
                # The local otodock terminal owns this PTY's input; the proxy
                # must not write. The satellite injection path (versioned)
                # takes these items instead; until it does, they wait here —
                # a detach flips the flag and this proxy path takes over.
                # Satellite injection is turn-end by design — the steer flag
                # is deliberately inert on this path (recorded follow-up).
                await self._try_satellite_inject()
                return
            # Steer eligibility is the HEAD item's — FIFO order is never
            # reordered, so a steer item behind a normal one waits with it.
            steering = bool(self._prompt_queue[0].get("steer"))
            blocked = self._prompt_gates_blocked(steering=steering)
            if blocked:
                self._log_held(blocked)
                return
            # Freshness: the local debounce tail can be up to 3s stale — force
            # a tail so _turn_open reflects the transcript NOW (a quiet mid-turn
            # lull, e.g. a native permission dialog waiting for input, must not
            # read as idle). Remote sessions are fresh via the satellite's 0.8s
            # forwarded-lines poll, and have no local file to read.
            if self.target == "local" and self.chat_id:
                tailer = self._tailer()
                try:
                    result = await asyncio.to_thread(
                        tailer.resolve_and_tail, self.session_id, self.chat_id,
                    )
                except Exception:
                    logger.exception(
                        "interactive %s: pre-inject tail failed", self.session_id[:8]
                    )
                    return
                self._apply_turn_signal(result.get("last_signal"),
                                question_pending=bool(result.get("question_pending")))
                # Re-check everything after the await — state may have moved
                # (the tail can flip _turn_open; output during the await resets
                # the quiet clock).
                blocked = self._prompt_gates_blocked(steering=steering)
                if blocked:
                    self._log_held(f"{blocked} post-tail")
                    return
            if not self._prompt_queue:
                return
            item = self._prompt_queue.popleft()
            self._last_held_reason = None
            steered_live = self._turn_open
            # The injected prompt opens a turn NOW (no-op transition for a
            # mid-turn steer — the turn is already open) — the tailer confirms
            # within a poll, but the flag must flip synchronously so a second
            # queued item can't ride the stale-idle window.
            self._set_turn_open(True)
            # settle=True: the trailing Enter waits for the paste echo to
            # settle (large delegate results were landing in the composer
            # with the fixed-gap Enter swallowed mid-ingest → never sent).
            self.submit_prompt(item["text"], settle=True)
            logger.info(
                "interactive %s: %s server prompt [%s] (%d left)",
                self.session_id[:8],
                "steered into open turn" if steered_live else "injected",
                item.get("source", "?"), len(self._prompt_queue),
            )
        finally:
            self._draining_prompts = False
            self._arm_inject_backstop()

    async def _try_satellite_inject(self) -> None:
        """otodock-attached: hand the head item to the satellite's stdin-inject
        path — the satellite owns the local terminal's input-line state; the
        proxy owns the turn-state decision (its gates below run on the same
        forwarded-transcript signals as the proxy path, minus the composer).
        The head item stays queued until the satellite ACKs; a local-terminal
        detach or an old satellite leaves it queued for the proxy path /
        close-handback."""
        if not self._prompt_queue or self.target == "local":
            return
        blocked = self._prompt_gates_blocked(for_satellite=True)
        if blocked:
            logger.debug(
                "interactive %s: satellite injection held (%s)",
                self.session_id[:8], blocked,
            )
            return
        from core.remote.satellite_connection import get_connection_manager
        mgr = get_connection_manager()
        if not mgr.satellite_supports_pty_inject(self.target):
            logger.debug(
                "interactive %s: satellite %s predates pty_inject — prompt held",
                self.session_id[:8], self.target[:8],
            )
            return
        now = time.monotonic()
        if self._satellite_inject is not None:
            if now - self._satellite_inject["sent_at"] < _SATELLITE_INJECT_TIMEOUT_S:
                return  # awaiting the result frame
            # Result lost (WS blip) — re-send the SAME id; the satellite's
            # recent-id dedupe re-ACKs without re-injecting.
            self._satellite_inject["sent_at"] = now
            inject_id = self._satellite_inject["inject_id"]
        else:
            inject_id = uuid.uuid4().hex[:12]
            self._satellite_inject = {"inject_id": inject_id, "sent_at": now}
        item = self._prompt_queue[0]
        await mgr.send_fire_and_forget(self.target, {
            "type": "pty_inject",
            "session_id": self.session_id,
            "inject_id": inject_id,
            "text": item["text"],
            "source": item.get("source", ""),
        })
        logger.info(
            "interactive %s: pty_inject sent to satellite %s [%s]",
            self.session_id[:8], self.target[:8], item.get("source", "?"),
        )

    def handle_inject_result(self, inject_id: str, ok: bool, reason: str = "") -> None:
        """satellite ``pty_inject_result``: resolve the in-flight injection.
        ACK pops the queue head (the prompt is in the user's terminal) and
        opens the turn; NACK leaves it queued for the backstop / the proxy path
        (after a detach) / the close-handback (session gone)."""
        inflight = self._satellite_inject
        if not inflight or inflight.get("inject_id") != inject_id:
            return  # stale or unknown — a newer attempt owns the slot
        self._satellite_inject = None
        if ok:
            item = self._prompt_queue.popleft() if self._prompt_queue else None
            self._set_turn_open(True)  # the injected prompt opened a turn
            logger.info(
                "interactive %s: satellite injected server prompt [%s] (%d left)",
                self.session_id[:8],
                (item or {}).get("source", "?"), len(self._prompt_queue),
            )
        else:
            logger.info(
                "interactive %s: satellite injection declined (%s)",
                self.session_id[:8], reason or "?",
            )
            if reason == "not_attached" and not self.otodock_attached:
                # The local terminal detached while the frame was in flight —
                # the proxy path owns the queue again.
                self._loop.create_task(self._try_drain_prompt_queue())
        self._arm_inject_backstop()

    def _tailer(self):
        """The transcript-persistence module for this session's CLI flavor. Both
        expose the same ``resolve_and_tail(session_id, chat_id)`` + ``forget``
        surface, so the call sites are flavor-agnostic."""
        if self.transcript_kind == "codex":
            from core.session import codex_rollout_tailer
            return codex_rollout_tailer
        from core.session import transcript_tailer
        return transcript_tailer

    def _run_post_output_tail(self) -> None:
        """Output settled (turn / long pause likely ended) → tail the transcript
        into chat_messages + backfill the title, off the event loop. Idempotent
        (the tailer's line cursor skips already-seen lines). Also fires the
        interactive-task completion callback when the tailer reports turn-end."""
        self._tail_handle = None
        if self._closed or not self.chat_id:
            return
        self._loop.create_task(self._tail_and_maybe_complete())

    def _run_resume_tail(self) -> None:
        """Resume-check fired: read the transcript now. If the CLI resumed on
        its own, the batch's ``user``/``tool_use`` signal reopens the turn
        (live dot + Stop button back). Further output while STILL closed
        re-arms via ``_fanout_output``; an open turn stops the cycle."""
        self._resume_tail_handle = None
        if self._closed or not self.chat_id or self._turn_open:
            return
        self._loop.create_task(self._tail_and_maybe_complete())

    async def _tail_and_maybe_complete(self) -> None:
        """Tail off the loop, then — if the tailer reports a turn-end signal —
        fire the interactive-task completion callback (gated in
        :meth:`_maybe_fire_turn_complete`). The whole branch is a no-op for chats
        (they never set ``on_turn_complete``)."""
        tailer = self._tailer()
        try:
            result = await asyncio.to_thread(
                tailer.resolve_and_tail, self.session_id, self.chat_id
            )
        except Exception:
            logger.exception(
                "interactive %s: post-output tail failed", self.session_id[:8]
            )
            return
        self._apply_turn_signal(result.get("last_signal"),
                                question_pending=bool(result.get("question_pending")))
        self._post_batch_effects(result)
        if result.get("turn_complete") or result.get("question_pending"):
            self._maybe_fire_turn_complete(
                result.get("last_message", "") or "", result.get("persisted", 0),
                question=bool(result.get("question_pending")),
                compacted=bool(result.get("compacted")),
            )

    def _post_batch_effects(self, result: dict) -> None:
        """Shared per-batch effects after the turn-signal fold (local tail and
        forwarded satellite lines alike):

        * ``chat_rows`` nudge — new rows persisted → tell the user's dashboard
          connections so an OPEN rich-history view (the terminal ⇄ transcript
          toggle) refetches live instead of waiting for a re-toggle.
        * compaction turn-end — a compact boundary landed while the session is
          IDLE (manual ``/compact``): run the normal turn-end effects + a
          reworded finished ping, otherwise the compact ends silently and the
          chat never stamps ready/unread. A MANUAL boundary additionally
          CLOSES a still-open turn: manual compaction only completes with the
          CLI idle at the prompt, so an open turn here is stale state (the
          command record + reseed lines are all signal-filtered — nothing
          else ever closes it and the chat shows "active" forever). Guarded
          on the batch carrying no NEWER open signal (a prompt landing right
          after the boundary in the same batch wins). A mid-turn auto-compact
          is NOT a turn end — the open turn continues and closes normally.
          Codex needs no close twin — its manual /compact journals
          task_complete after the compacted item, which folds to end_turn
          already — but that same task_complete makes ``turn_complete`` true
          in the SAME batch, so the ping is deferred to
          :meth:`_maybe_fire_turn_complete` (compacted-worded there) instead
          of firing twice ("compacted" + "finished").

        Both best-effort; the boundary line is read once (tail cursors), so
        the compaction ping can't double-fire."""
        try:
            if result.get("persisted", 0) > 0 and self.chat_id \
                    and not self.chat_id.startswith("meeting-"):
                from services.notifications import notification_manager
                notification_manager.broadcast_chat_rows(
                    self._chat_owner(), self.chat_id, agent=self.agent_name,
                )
        except Exception:
            logger.exception(
                "interactive %s: chat_rows broadcast failed", self.session_id[:8]
            )
        if result.get("compacted"):
            if result.get("turn_complete"):
                # Codex manual /compact: the end_turn fold already ran the
                # turn-end effects; _maybe_fire_turn_complete owns the (one,
                # compacted-worded) ping.
                pass
            elif not self._turn_open:
                self._turn_end_effects()
                self._fire_turn_notification(compacted=True)
            elif (result.get("compact_trigger") == "manual"
                    and result.get("last_signal") not in ("user", "tool_use")):
                self._set_turn_open(False)  # runs the turn-end effects
                self._fire_turn_notification(compacted=True)

    # -- remote transcript forwarding ----------------------------------------
    def feed_transcript_lines(self, lines: list[str]) -> None:
        """Persist transcript JSONL lines FORWARDED from a remote satellite.

        The satellite tails this session's transcript file (it controls the
        config dir, so it finds the JSONL by session id regardless of cwd) and
        forwards new lines over the WS (``transcript_lines`` frame); the proxy's
        ``satellite_connection`` routes them here. We hand them to the SAME tailer
        parser used for local sessions (``tail_lines``) so persistence +
        turn-complete are identical — no on-disk mirror needed. Persisting hits
        the DB, so it runs off the loop; the per-session lock keeps arrival
        order."""
        if self._closed or not self.chat_id or not lines:
            return
        self._loop.create_task(self._persist_forwarded_lines(list(lines)))

    async def _persist_forwarded_lines(self, lines: list[str]) -> None:
        tailer = self._tailer()
        try:
            async with self._transcript_lock:
                result = await asyncio.to_thread(
                    tailer.tail_lines, self.session_id, self.chat_id, lines
                )
        except Exception:
            logger.exception(
                "interactive %s: forwarded transcript persist failed", self.session_id[:8]
            )
            return
        self._apply_turn_signal(result.get("last_signal"),
                                question_pending=bool(result.get("question_pending")))
        self._post_batch_effects(result)
        if result.get("turn_complete") or result.get("question_pending"):
            self._maybe_fire_turn_complete(
                result.get("last_message", "") or "", result.get("persisted", 0),
                question=bool(result.get("question_pending")),
                compacted=bool(result.get("compacted")),
            )

    def _maybe_fire_turn_complete(self, last_message: str, persisted: int = 0,
                                  question: bool = False,
                                  compacted: bool = False) -> None:
        """A tailer saw a turn-end signal. Three independent reactions, in order:

        1. **LLM chat-title generation** — fired ONCE on the first completed turn
           (the transcript is now in the DB). Dashboard-chats-only (the service
           skips task-/meeting- and atomically claims, so exactly once); chats
           never set ``on_turn_complete``.
        2. **End-of-turn USER notification + audio** — fired once PER TURN (chats
           only — an autonomous task run's completion alert is its
           ``notification_mode`` contract), local AND remote, giving interactive
           the same "<agent> finished" ping the ``-p`` pump fires (interactive has
           no pump, so a turn would otherwise end silently). See
           :meth:`_fire_turn_notification`.
        3. **Interactive-TASK completion callback** — fired ONCE per session (only
           autonomous TASKS set ``on_turn_complete``).

        ``persisted`` is the tailer's count of NEW transcript lines this call — the
        per-TURN dedup for (2): a turn-end's ``end_turn`` line is tailed exactly
        once (cursor-advanced), so ``persisted > 0`` is true only on the call that
        first saw THIS turn-end. (1)/(3) carry their own fire-once flags. The tailer
        fires on debounce + forwarded-lines + close + the 60 s sweep, hence the
        dedup matters.

        (2) + (3) share two gates (mirroring the pump): min-turn-time since spawn
        (no warm/first-render false-trigger) and the bg ``SubagentRegistry`` being
        empty — a still-running bg subagent means a follow-up turn is coming, so
        hold both until the FINAL turn.

        ``question=True``: the "turn end" is really the CLI parked on an
        unanswered AskUserQuestion — (2) words the ping as needs-input and (3)
        is skipped (a question is not a task completion; hooks deny the tool
        for autonomous tasks anyway, this is defense in depth)."""
        # (1) one-time chat-title upgrade.
        if not self._title_fired and self.chat_id:
            self._title_fired = True
            try:
                from services import title_generator
                self._loop.create_task(title_generator.request_chat_title(
                    self.chat_id, assistant_excerpt=last_message,
                ))
            except Exception:
                logger.exception(
                    "interactive %s: title request failed to schedule",
                    self.session_id[:8],
                )

        # Shared gates for the notification (2) + the task callback (3).
        if (time.monotonic() - self.created_at) < MIN_TURN_S:
            return
        bg_pending = False
        try:
            from core.session.session_state import get_subagent_registry
            bg_pending = get_subagent_registry(self.session_id).has_pending
        except Exception:
            pass
        if bg_pending:
            return  # a bg subagent is still running → a follow-up turn is coming

        # (2) per-turn end-of-turn user notification (parity with the -p pump).
        # ``compacted``: the "turn" was a codex manual /compact (task_complete
        # rides the same batch as the compacted item) — compacted wording,
        # not "finished".
        if persisted > 0:
            self._fire_turn_notification(question=question, compacted=compacted)

        # (2b) a turn just closed with the shared gates clear — drain any queued
        # server prompts (delegate results waiting for quiescence). Never during
        # close(): its final tail also lands here, but the PTY is already dead —
        # write_input would silently drop the item (close hands the queue back
        # to the delivery ladder instead). Skipped while parked on a question:
        # an injected prompt would land in (and fight) the question dialog.
        if self._prompt_queue and not self._closing and not question:
            self._loop.create_task(self._try_drain_prompt_queue())

        # (3) one-time interactive-TASK completion callback.
        if question:
            return
        if self._turn_complete_fired or self.on_turn_complete is None:
            return
        self._turn_complete_fired = True
        cb = self.on_turn_complete
        logger.info(
            "interactive %s: turn-complete → firing task completion", self.session_id[:8]
        )
        try:
            res = cb(last_message)
            if asyncio.iscoroutine(res):
                self._loop.create_task(res)
        except Exception:
            logger.exception(
                "interactive %s: on_turn_complete failed", self.session_id[:8]
            )

    def _fire_turn_notification(self, question: bool = False,
                                compacted: bool = False) -> None:
        """Fire the end-of-turn user notification + audio for an interactive turn —
        the same signal the ``-p`` pump fires at turn end (``question=True``
        rewords it: the CLI is waiting on an AskUserQuestion, not finished):
        ``broadcast_chat_status`` "ready" clears the sidebar live-dot on every
        device (a no-op for the dot when interactive never lit "streaming"), and
        ``fire_ephemeral`` routes the alert: to the viewing browser via the
        recorded origin (the FE drops it only when that chat is the visible tab),
        to every active dashboard connection when the origin can't take it
        (``interactive=True`` presence overlay), or FCM to the phone when no
        dashboard is present. An ``otodock``-CLI attachment is NOT dashboard
        presence (``cli_attached``): the FCM still fires while the terminal is
        open, and a stale viewer origin never swallows it. ``user_sub`` +
        ``agent_name`` are on the session (no DB lookup; both calls are
        non-blocking — the first enqueues to in-memory conn queues, the second is
        scheduled as a task). Skipped for meeting chats (per-speaker turn ends
        aren't completions) and for autonomous task runs (the scheduler set
        ``on_turn_complete``) — a task's completion alert is its
        ``notification_mode`` contract; a re-warmed task chat has no callback and
        keeps the per-turn signal. Best-effort."""
        if not self.user_sub or not self.chat_id or self.chat_id.startswith("meeting-"):
            return
        try:
            from services.notifications import notification_manager
            notification_manager.broadcast_chat_status(
                self._chat_owner(), self.chat_id, "ready", agent=self.agent_name,
            )
            if self.on_turn_complete is not None:
                return
            if compacted:
                title = f"{self.agent_name} compacted the conversation"
                body = "Context compacted — session ready"
            elif question:
                title = f"{self.agent_name} needs your input"
                body = "Waiting for your answer"
            else:
                title = f"{self.agent_name} finished"
                body = "Response ready"
            self._loop.create_task(notification_manager.fire_ephemeral(
                self.user_sub,
                title=title,
                body=body,
                chat_id=self.chat_id,
                interactive=True,
                cli_attached=self.otodock_attached,
            ))
        except Exception:
            logger.exception(
                "interactive %s: turn notification failed", self.session_id[:8]
            )

    def resize(self, rows: int, cols: int) -> None:
        # dual-control: drop dashboard resizes while the otodock terminal is
        # the active controller (it owns the PTY size; a dashboard resize would
        # fight it). The otodock client resizes satellite-side. Once the dashboard
        # takes over (otodock_attached cleared), its resizes apply again.
        if self.otodock_attached:
            return
        self.rows, self.cols = rows, cols
        if self.alive:
            self.pty.resize(rows, cols)

    # -- drainer (permission queue → viewer) ---------------------------------
    def _start_drainer(self) -> None:
        if self._drainer is None:
            self._drainer = self._loop.create_task(self._drain_loop())

    async def _drain_loop(self) -> None:
        # Late import: session_state pulls in core deps; keep module load light.
        from core.session.session_state import get_permission_queue
        from core.events.artifact_events import REPLAYABLE_ARTIFACT_EVENT_TYPES
        queue = get_permission_queue(self.session_id)
        try:
            while not self._closing:
                item = await queue.get()
                # Final display/file-tools artifacts persist BEFORE the viewer
                # check: the row must exist even with no viewer attached (the
                # whole point — a user who opens the chat later reconstructs
                # the popups from these rows). The row id rides the forwarded
                # frame as ``db_message_id`` — the dashboard's stable key for
                # PiP replay dedupe + X-dismiss memory. Type-gated here so
                # blocking prompts never pay the DB thread hop.
                if item.get("event_type") in REPLAYABLE_ARTIFACT_EVENT_TYPES:
                    row_id = await asyncio.to_thread(
                        persist_drained_artifact, self.chat_id, item
                    )
                    if row_id is not None:
                        item["db_message_id"] = row_id
                        self._nudge_rich_view()
                if self.on_perm_event is None:
                    # No viewer wired yet — drop. Permission prompts also block
                    # on wait_for_permission; close()/abort releases those.
                    continue
                try:
                    res = self.on_perm_event(item)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    logger.exception(
                        "interactive %s: perm-event handler failed", self.session_id[:8]
                    )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("interactive %s: drainer crashed", self.session_id[:8])

    def _nudge_rich_view(self) -> None:
        """``chat_rows`` nudge after an artifact row persists — same contract as
        ``_post_batch_effects``, so an OPEN rich-history view refetches live
        instead of waiting for the next tail batch. Best-effort."""
        if not self.chat_id or self.chat_id.startswith("meeting-"):
            return
        try:
            from services.notifications import notification_manager
            notification_manager.broadcast_chat_rows(
                self._chat_owner(), self.chat_id, agent=self.agent_name,
            )
        except Exception:
            logger.exception(
                "interactive %s: chat_rows broadcast failed", self.session_id[:8]
            )

    # -- lifecycle ------------------------------------------------------------
    def _on_pty_exit(self, code: Optional[int]) -> None:
        # The CLI process died (normal exit / crash / killed). Tear the session
        # down. Scheduled because PtyProcess fires this from its reaper task.
        if self._closed or self._closing:
            return
        logger.info("interactive %s: pty exited code=%r", self.session_id[:8], code)
        self.exit_code = code
        self._loop.create_task(self.close(reason="exited", _signal_child=False))

    async def close(self, *, reason: str = "closed", _signal_child: bool = True) -> None:
        """Tear down: stop the drainer, kill the PTY, release the slot, purge
        per-session security/permission state, and drop from the registry.
        Idempotent."""
        if self._closing or self._closed:
            return
        self._closing = True

        for h in (self._settle_handle, self._ready_max_handle, self._tail_handle,
                  self._inject_backstop_handle, self._resume_tail_handle):
            if h is not None:
                h.cancel()
        self._settle_handle = self._ready_max_handle = self._tail_handle = None
        self._inject_backstop_handle = self._resume_tail_handle = None
        for _h in self._submit_tail_handles:
            with contextlib.suppress(Exception):
                _h.cancel()
        self._submit_tail_handles = []
        self._cancel_deferred_submit()
        self._clear_cold_flush()

        if self._drainer is not None:
            self._drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._drainer
            self._drainer = None

        if self.pty is not None and not self.pty.closed:
            self.pty.close(signal_child=_signal_child)

        from core import concurrency
        tailer = self._tailer()
        # Persist the conversation to chat_messages from the transcript/rollout JSONL
        # BEFORE clearing state. The native TUI does not reliably fire the Stop hook,
        # so this discover-and-tail on close is the robust persistence path — without
        # it a killed/reaped/toggled-off interactive chat shows no history on reopen.
        # Must run before the cleanup below drops the session→claude_dir / codex_dir
        # mapping the path discovery needs.
        try:
            _r = await asyncio.to_thread(
                tailer.resolve_and_tail, self.session_id, self.chat_id
            )
            # Backstop: a turn that ended exactly as the session closes (idle
            # reap / toggle-off) still fires the task completion (fire-once).
            if _r.get("turn_complete"):
                self._maybe_fire_turn_complete(
                    _r.get("last_message", "") or "", _r.get("persisted", 0)
                )
        except Exception:
            logger.exception(
                "interactive %s: final transcript tail failed", self.session_id[:8]
            )

        # Release concurrency + permission/security/broker state + tailer cursor.
        from core.session.session_state import (
            cleanup_session_permission_state,
            resolve_session_permissions,
        )
        resolve_session_permissions(self.session_id, approved=False)
        concurrency.release_chat_slot(self.session_id)
        # Release the subscription seat acquired at config build. The engine
        # layers' close_session() only runs for pump/daemon sessions — an
        # interactive PTY is torn down here (idle reap, toggle-off, task end,
        # shutdown) — so without this active_sessions drifts up until restart.
        # Safe to call unconditionally: release pops the binding (idempotent,
        # no-op when the session never bound one).
        from services.engines.subscription_pool import release_subscription
        release_subscription(self.session_id)
        cleanup_session_permission_state(self.session_id)
        tailer.forget(self.session_id)

        # Drop from the registry. No lock needed — the get/pop pair is atomic
        # under single-threaded asyncio, and close() runs while register() holds
        # the registry lock during a supersede (taking it here would deadlock).
        if _sessions.get(self.session_id) is self:
            _sessions.pop(self.session_id, None)

        # A session dying mid-turn can never emit its end_turn signal — clear
        # the sidebar dot (and stamp the partial response) via the transition
        # broadcast. No-op when the turn already closed.
        self._set_turn_open(False)

        self._closed = True
        self._output_listeners.clear()
        logger.info("interactive %s: closed (%s)", self.session_id[:8], reason)

        # Hand undelivered server prompts back to the delivery ladder — the PTY
        # is gone (registry entry popped above), so the headless rungs are safe
        # now (the one-shot's live-PTY guard no longer sees this session).
        # Scheduled as a task: close() can run under the registry lock during a
        # supersede, and re-delivery does layer I/O.
        if self._prompt_queue:
            pending = list(self._prompt_queue)
            self._prompt_queue.clear()
            self._loop.create_task(_redeliver_pending(self, pending))

        if self.on_close is not None:
            try:
                res = self.on_close(self, reason)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logger.exception("interactive %s: on_close failed", self.session_id[:8])


async def _redeliver_pending(sess: "InteractiveSession", items: list[dict]) -> None:
    """Re-run undelivered queued prompts through the delivery ladder after the
    owning PTY closed. PTY and WS rungs are excluded: a supersede-respawned PTY
    for the same chat would ping-pong the item between session generations, and
    the WS handler's synthesis turn dead-ends for interactive chats. The pump
    rung stays available (a mode-toggled chat re-warms headless), then the
    persistent/one-shot rungs the item carried from its original delivery.
    ``hops`` caps the close→handback→close loop."""
    from core.session.session_delivery import deliver_prompt

    for item in items:
        hops = int(item.get("hops", 0))
        if hops >= 2:
            logger.warning(
                "interactive %s: dropping server prompt [%s] after %d handbacks",
                sess.session_id[:8], item.get("source", "?"), hops,
            )
            continue
        try:
            outcome = await deliver_prompt(
                item.get("chat_id", ""), item["text"],
                source=item.get("source", "unknown"),
                session_id=sess.session_id,
                agent=item.get("agent", ""),
                user_sub=item.get("user_sub"),
                role=item.get("role", "manager"),
                persistent_fn=item.get("persistent_fn"),
                oneshot_fn=item.get("oneshot_fn"),
                allow_pty=False, allow_ws=False,
                hops=hops + 1,
            )
            on_outcome = item.get("on_outcome")
            if on_outcome is not None:
                res = on_outcome(outcome)
                if asyncio.iscoroutine(res):
                    await res
            logger.info(
                "interactive %s: handed back server prompt [%s] → %s",
                sess.session_id[:8], item.get("source", "?"), outcome.path,
            )
        except Exception:
            logger.exception(
                "interactive %s: server-prompt handback failed", sess.session_id[:8]
            )


def persist_drained_artifact(chat_id: str, item: dict) -> Optional[int]:
    """Persist one drained display/file-tools artifact as a pump-shaped
    chat_messages event row — the interactive twin of the headless pump's
    ``_save_turn_blocks`` (same event shape via ``artifact_event_from_perm_item``,
    so ``eventToBlock`` renders both identically). Only the REPLAYABLE final
    types persist; blocking prompts, placeholders and failure/removal events
    return None untouched. Returns the row id (the dashboard's stable
    replay/dismissal key) or None — persistence must never break live delivery,
    hence the blanket except. Runs off the loop (``asyncio.to_thread``)."""
    from core.events.artifact_events import (
        REPLAYABLE_ARTIFACT_EVENT_TYPES,
        artifact_event_from_perm_item,
    )

    if not chat_id or item.get("event_type") not in REPLAYABLE_ARTIFACT_EVENT_TYPES:
        return None
    try:
        event = artifact_event_from_perm_item(item)
        if event is None:
            return None
        import json
        from storage import database as task_store
        return task_store.add_chat_message(
            chat_id, "event", "",
            event_type=event["type"], event_data=json.dumps(event),
        )
    except Exception:
        logger.exception("interactive: artifact persist failed for chat %s", chat_id)
        return None


# ---------------------------------------------------------------------------
# Registry API
# ---------------------------------------------------------------------------

def get(session_id: str) -> Optional[InteractiveSession]:
    return _sessions.get(session_id)


def find_live_for_chat(
    chat_id: str, *, target: Optional[str] = None
) -> Optional[InteractiveSession]:
    """dual-control: the NEWEST live session for ``chat_id`` (optionally pinned
    to the ``target`` machine). Used by ``otodock --resume`` to ATTACH to a running
    PTY instead of killing + re-spawning it (the in-flight turn survives). Returns
    the NEWEST-alive, not first-found: the spawn lease is per-session_id (not
    per-chat), so a brief fresh-respawn race can transiently leave two live
    sessions for one chat_id."""
    if not chat_id:
        return None
    matches = [
        s for s in _sessions.values()
        if s.chat_id == chat_id and s.alive and (target is None or s.target == target)
    ]
    if not matches:
        return None
    return max(matches, key=lambda s: s.created_at)


async def close_for_chat(chat_id: str, *, reason: str = "superseded") -> int:
    """otodock-CLI take-over: close any LIVE session(s) for ``chat_id`` so a new
    spawn/resume of the same chat is the single writer. The close fires each
    session's ``_on_exit`` → for an otodock session that feeds an EXIT to the old
    local terminal (it restores its tty + exits); a dashboard viewer sees the PTY
    close. Returns how many were closed."""
    if not chat_id:
        return 0
    victims = [s for s in list(_sessions.values()) if s.chat_id == chat_id and s.alive]
    for s in victims:
        try:
            await s.close(reason=reason)
        except Exception:
            logger.exception("close_for_chat: failed to close %s", s.session_id[:8])
    return len(victims)


def live_session_ids(local_only: bool = False) -> set[str]:
    """Session ids with a live PTY — consumed by concurrency.reconcile_chat_slots
    so interactive slots aren't released as orphans. With ``local_only``,
    excludes remote (satellite) sessions: those belong to a satellite budget, not
    the local ceiling G, so they must not appear in the local reconciler's live set."""
    return {sid for sid, s in _sessions.items()
            if s.alive and (not local_only or s.target == "local")}


def streaming_chat_ids() -> set[str]:
    """Chat ids of live interactive sessions currently INSIDE a turn — the
    interactive half of the dashboard connect-time ``chat_status_snapshot``
    (the pump half is ``session_state.streaming_chat_ids``). Meeting chats are
    excluded like every other interactive live-dot signal."""
    return {s.chat_id for s in _sessions.values()
            if s.chat_id and not s.chat_id.startswith("meeting-")
            and s.alive and s._turn_open}


async def _register_session(
    session: "InteractiveSession",
    *,
    pty_factory,
) -> InteractiveSession:
    """Shared registration spine for local + remote interactive sessions.

    Enforces the lease (one live process per ``session_id``), acquires a chat
    slot, creates ``session.pty`` via ``pty_factory`` (a local
    ``pty_relay.spawn_pty`` or a remote ``remote_pty.spawn_remote_pty``), stores
    the session, and starts the drainer + readiness gate. Everything except the
    pty creation is identical for both targets.
    """
    from core import concurrency

    async with _get_lock():
        # Lease: a session_id maps to at most one live process.
        existing = _sessions.get(session.session_id)
        if existing is not None:
            logger.info("interactive %s: superseding existing process", session.session_id[:8])
            await existing.close(reason="superseded")

        # Target-aware: a REMOTE interactive PTY (and any satellite-initiated
        # native-CLI session) runs on the satellite — it must NOT consume a
        # local-G slot (acquire short-circuits to True for target != "local";
        # the satellite enforces its own budget). Local interactive counts as a
        # local session. A session spawned for a task is already tracked as
        # "task" by the scheduler's task_slot — the acquire here is then an
        # idempotent no-op (no double-count).
        adm = await concurrency.acquire_chat_slot(session.session_id, target=session.target)
        if not adm:
            raise CapacityError(
                f"no chat slot for interactive session {session.session_id[:8]} ({adm.reason})"
            )

        # Seek the transcript cursor past any PRE-EXISTING history BEFORE the
        # CLI starts. Resuming an existing chat interactively reuses the same
        # on-disk transcript/rollout the headless turns already wrote; without
        # this the first tail reads from line 0 and re-persists the whole prior
        # conversation into chat_messages as duplicates. Local files only — a
        # remote session resolves nothing here (the satellite does its own seek).
        try:
            await asyncio.to_thread(
                session._tailer().seek_past_existing,
                session.session_id, session.chat_id,
            )
        except Exception:
            logger.exception(
                "interactive %s: transcript seek failed", session.session_id[:8],
            )

        try:
            session.pty = await pty_factory(session)
        except Exception:
            concurrency.release_chat_slot(session.session_id)
            raise

        _sessions[session.session_id] = session
        session._start_drainer()
        if not session._ready:  # prompt_in_argv sessions start ready — no gate
            session._arm_readiness()

    # Durable delegate-wake replay: results whose delivery failed entirely
    # while this chat was dead were stored on the chat row — queue them as
    # server prompts now, so the re-warmed orchestrator continues on its own
    # (the drain injects at quiescence, after the readiness/cold-submit
    # gates). Atomic claim: a racing headless turn can't double-inject.
    # Outside the lock — a DB hop must not serialize other spawns.
    if session.chat_id:
        try:
            from storage import database as task_store
            wakes = await asyncio.to_thread(
                task_store.claim_pending_delegate_wake, session.chat_id,
            )
            for wake in wakes:
                session.queue_prompt(wake, "delegate_result_replay",
                                     chat_id=session.chat_id)
            if wakes:
                logger.info(
                    "interactive %s: queued %d pending delegate wake(s) for "
                    "replay (chat=%s)", session.session_id[:8], len(wakes),
                    session.chat_id[:8],
                )
        except Exception:
            logger.exception(
                "interactive %s: pending delegate-wake replay failed",
                session.session_id[:8],
            )
    return session


async def register(
    *,
    session_id: str,
    chat_id: str,
    agent_name: str,
    argv: list[str],
    env: dict,
    cwd: Optional[str] = None,
    rows: int = pty_relay.DEFAULT_ROWS,
    cols: int = pty_relay.DEFAULT_COLS,
    user_sub: str = "",
    role: str = "",
    username: str = "",
    target: str = "local",
    scrollback_limit: int = pty_relay.DEFAULT_SCROLLBACK_BYTES,
    transcript_kind: str = "claude",
    prompt_in_argv: bool = False,
    tui_theme: str = "dark",
) -> InteractiveSession:
    """Spawn ``argv`` on a PTY and register it under ``session_id``.

    Enforces the lease (kills any existing process for the same ``session_id``)
    and acquires a chat slot. ``argv``/``env`` are pre-assembled by the caller
    (the interactive CLI argv mirrors the ``-p`` spawn minus ``-p``/stream-json,
    plus ``TERM``). The PROXY-side identity (``set_session_security`` etc.) is set
    by the caller BEFORE this call so hooks resolve the moment the CLI starts.

    Raises :class:`CapacityError` if no chat slot is free.
    """
    session = InteractiveSession(
        session_id=session_id, chat_id=chat_id, agent_name=agent_name,
        user_sub=user_sub, role=role, username=username, target=target,
        rows=rows, cols=cols,
        transcript_kind=transcript_kind, prompt_in_argv=prompt_in_argv,
        tui_theme=tui_theme,
    )

    async def _make_local_pty(s: "InteractiveSession"):
        return pty_relay.spawn_pty(
            argv, env=env, cwd=cwd, rows=rows, cols=cols,
            on_output=s._fanout_output,
            on_exit=s._on_pty_exit,
            scrollback_limit=scrollback_limit,
        )

    sess = await _register_session(session, pty_factory=_make_local_pty)
    logger.info(
        "interactive %s registered (chat=%s agent=%s pid=%s)",
        session_id[:8], chat_id, agent_name, sess.pty.pid,
    )
    return sess


async def register_remote(
    *,
    session_id: str,
    chat_id: str,
    agent_name: str,
    machine_id: str,
    execution_path: str,
    config_payload: dict,
    rows: int = pty_relay.DEFAULT_ROWS,
    cols: int = pty_relay.DEFAULT_COLS,
    user_sub: str = "",
    role: str = "",
    username: str = "",
    scrollback_limit: int = pty_relay.DEFAULT_SCROLLBACK_BYTES,
    transcript_kind: str = "claude",
    prompt_in_argv: bool = False,
    tui_theme: str = "dark",
) -> InteractiveSession:
    """Register an interactive session whose PTY runs on a REMOTE satellite.

    Mirrors :func:`register`, but ``session.pty`` is a
    :class:`core.remote.remote_pty.RemotePtyProcess` driven over the satellite WS — the
    satellite assembles the interactive argv from ``config_payload`` (the SAME
    payload the ``-p`` path uses, ``RemoteExecutionLayer._build_start_payload``)
    and spawns it under a PTY. ``target`` is the ``machine_id``. The PROXY-side
    identity (``set_session_security`` etc.) is set by the caller BEFORE this
    call, same as :func:`register`.
    """
    from core.remote import remote_pty
    from core.remote.satellite_connection import get_connection_manager

    # The satellite's OS (from its reported capabilities) — scopes the
    # Windows-ConPTY cold-submit backstop to Windows-remote Claude only.
    remote_os = get_connection_manager().satellite_os(machine_id)

    session = InteractiveSession(
        session_id=session_id, chat_id=chat_id, agent_name=agent_name,
        user_sub=user_sub, role=role, username=username, target=machine_id,
        remote_os=remote_os,
        rows=rows, cols=cols,
        transcript_kind=transcript_kind, prompt_in_argv=prompt_in_argv,
        tui_theme=tui_theme,
    )

    async def _make_remote_pty(s: "InteractiveSession"):
        return await remote_pty.spawn_remote_pty(
            machine_id=machine_id, session_id=session_id,
            agent_slug=agent_name, execution_path=execution_path,
            config_payload=config_payload, rows=rows, cols=cols,
            on_output=s._fanout_output, on_exit=s._on_pty_exit,
            scrollback_limit=scrollback_limit,
        )

    sess = await _register_session(session, pty_factory=_make_remote_pty)
    logger.info(
        "interactive %s registered REMOTE (machine=%s chat=%s agent=%s pid=%s)",
        session_id[:8], machine_id[:8], chat_id, agent_name, sess.pty.pid,
    )
    return sess


async def close_session(session_id: str, *, reason: str = "closed") -> bool:
    """Close a registered interactive session by id. Returns True if it existed."""
    session = _sessions.get(session_id)
    if session is None:
        return False
    await session.close(reason=reason)
    return True


async def close_all(reason: str = "shutdown") -> None:
    for session in list(_sessions.values()):
        try:
            await session.close(reason=reason)
        except Exception:
            logger.exception("interactive %s: close_all failed", session.session_id[:8])


# ---------------------------------------------------------------------------
# Idle reaper
# ---------------------------------------------------------------------------

async def reap_idle(timeout_s: float | None = None) -> int:
    """Close sessions with no viewer and no activity for ``timeout_s``.

    ``timeout_s`` defaults to the platform-wide admin idle timeout
    (``config.get_idle_timeout()``) — ONE knob shared with the headless reapers.
    A viewer attached, or any byte in/out, keeps a session alive, so a
    long unviewed agent turn is never killed mid-flight; an on-screen or
    reconnect-grace terminal is also spared below (don't kill visible state —
    that is NOT a longer timeout). Returns the count reaped.
    """
    if timeout_s is None:
        timeout_s = config.get_idle_timeout()
    reaped = 0
    for session in list(_sessions.values()):
        if session.has_viewer or session.otodock_attached or not session.alive:
            continue
        # A remote session whose satellite is mid-reconnect is held in
        # PTY-grace — still `alive` but transiently unviewable. Don't reap it; the
        # grace timer (or its reconcile on reconnect) decides its fate.
        if session.target != "local":
            from core.remote.satellite_connection import get_connection_manager
            if get_connection_manager().is_pty_in_grace(session.target):
                continue
        if session.idle_seconds > timeout_s:
            logger.info(
                "interactive %s: reaping idle (%.0fs, no viewer)",
                session.session_id[:8], session.idle_seconds,
            )
            await session.close(reason="idle")
            reaped += 1
    return reaped


async def tail_live_sessions() -> None:
    """Persist each live session's transcript incrementally (idempotent via the
    tailer's line cursor) so a proxy crash between turns can't lose history. The
    close()-time tail covers normal teardown; this covers the crash gap."""
    for session in list(_sessions.values()):
        if not session.alive:
            continue
        try:
            tailer = session._tailer()
            _r = await asyncio.to_thread(
                tailer.resolve_and_tail, session.session_id, session.chat_id
            )
            session._apply_turn_signal(
                _r.get("last_signal"),
                question_pending=bool(_r.get("question_pending")))
            # Crash backstop for interactive tasks: if the post-output debounce
            # missed a turn-end (busy loop), the 60s sweep still completes it.
            if _r.get("turn_complete") or _r.get("question_pending"):
                session._maybe_fire_turn_complete(
                    _r.get("last_message", "") or "", _r.get("persisted", 0),
                    question=bool(_r.get("question_pending")),
                )
        except Exception:
            logger.exception(
                "interactive %s: periodic transcript tail failed", session.session_id[:8]
            )


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(_REAPER_PERIOD_S)
        try:
            await tail_live_sessions()
            await reap_idle()
        except Exception:
            logger.exception("interactive idle reaper error")


def start_idle_reaper() -> None:
    """Start the idle-reaper background task (call once at app startup)."""
    global _reaper_task
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = asyncio.get_event_loop().create_task(_reaper_loop())
