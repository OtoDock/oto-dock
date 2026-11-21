"""Interactive CLI session: the class, its registration and its drainer.

An *interactive* session is a PTY-backed CLI on a machine (its handle here is
``remote_pty.RemotePtyProcess``) that the proxy registers but does **not**
drive turns for — there is no pump and no
``CommonEvent`` stream, because a PTY emits raw bytes, not events. This module
owns their lifecycle, and re-exports what importers take from the parts beside it:
``interactive_registry`` (the live table and the idle reaper), ``interactive_common``
(the timing constants and callback types), and the mixins under the class
(``interactive_input``, ``interactive_queue``, ``interactive_turn``,
``interactive_output``):

  * the **single-live-process lease** — registering a ``session_id``
    first kills any process already bound to it (mode toggle / cross-surface
    takeover), so shared session files never get two writers;
  * chat-slot acquisition + a hook into the 120s reconciler (``concurrency.py``)
    so an interactive slot is never mis-reaped or leaked;
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
import functools
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

import psycopg

from core import placement
from core.background import spawn
from core.drain import PLATFORM_STOPPING
from core.session import session_kind
from core.session.interactive_common import (  # noqa: F401
    _BREADCRUMB_MIN_CHARS,
    _COLD_RETRY_MAX,
    _COLD_RETRY_S,
    _COMPOSER_DIRTY_TTL_S,
    _DIALOG_KEYS_RE,
    _INJECT_BACKSTOP_S,
    _INJECT_QUIET_S,
    _MIDTURN_TAIL_S,
    _POST_OUTPUT_TAIL_S,
    _RESUME_TAIL_S,
    _SATELLITE_INJECT_TIMEOUT_S,
    _SESSION_TOKEN_LIFE_S,
    _SUBMIT_ENTER_DELAY_S,
    _SUBMIT_MAX_S,
    _SUBMIT_SETTLE_S,
    _SUBMIT_WIN_BACKSTOP_S,
    _TITLE_EARLY_TIMER_S,
    _TOOLS_RECHECK_S,
    DEFAULT_COLS,
    DEFAULT_ROWS,
    DEFAULT_SCROLLBACK_BYTES,
    MIN_TURN_S,
    READY_MAX_S,
    READY_MIN_S,
    READY_SETTLE_S,
    CloseCb,
    OutputListener,
    PermEventCb,
    StatusCb,
    TurnCompleteCb,
    _batch_listeners,
    _breadcrumb_text,
    _load_chat_row,
    _token_expires_at,
    add_batch_listener,
)
from core.session.interactive_input import (
    InteractiveInputMixin,
)
from core.session.interactive_output import (  # noqa: F401
    InteractiveOutputMixin,
)
from core.session.interactive_queue import (  # noqa: F401
    InteractiveQueueMixin,
)
from core.session.interactive_registry import (  # noqa: F401
    _reaper_loop,
    _sessions,
    close_all,
    close_for_chat,
    close_session,
    find_live_for_chat,
    get,
    held_on_machine,
    live_session_ids,
    live_sessions,
    note_background_report,
    reap_idle,
    reap_spare_reason,
    start_idle_reaper,
    tail_live_sessions,
)
from core.session.interactive_turn import (  # noqa: F401
    InteractiveTurnMixin,
    LimitMark,
)
from core.session.transcript_tailer import leaves_turn_open
from core.session.transcript_tool_events import drop_sent_prompts
from storage.agents.schema import DEFAULT_EXECUTION_PATH
from ws import wire_events as wire

if TYPE_CHECKING:
    from core.events.turn_ending import TurnEnding
    from core.remote.remote_pty import RemotePtyProcess

logger = logging.getLogger("otodock.interactive")
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


class CapacityError(RuntimeError):
    """Raised when no chat slot is available for a new interactive session."""


class InteractiveSession(InteractiveInputMixin):
    """A registered, PTY-backed interactive CLI session.

    Construct via :func:`register_remote` (which enforces the lease + slot).
    Holds the PTY's handle and the per-session viewer/permission plumbing.
    """

    # Class-level defaults for the cold-flush retry state: hand-built partial
    # instances (``__new__`` test doubles) reach _set_turn_open → _clear_cold_flush
    # without running __init__, and must degrade to a no-op.
    _cold_flush_pending: bytes = b""
    _cold_flush_seed = ""
    _cold_flush_attempts = 0
    _cold_flush_watcher: asyncio.TimerHandle | None = None
    _ready_deadline = 0.0
    _tools_recheck: asyncio.TimerHandle | None = None
    _tools_held_at = 0.0
    # A turn an Escape may have dropped (``_check_dropped_turn``).
    _escape_at = 0.0
    _dropped_turn_check: asyncio.TimerHandle | None = None
    _dropped_turn_looks = 0
    # Monotonic stamp of the latest cold Enter (see _check_cold_flush).
    _cold_flush_enter_at = 0.0
    # The hook-driven park already pinged the open dialog (see
    # park_on_native_dialog) — the transcript fold must not ping it again.
    _native_park_pinged = False
    # The gate's stamp of the latest hook park (see _apply_turn_signal).
    _parked_at: float | None = None
    # The question park (see _set_parked): its count, the composer flag the
    # latest park found, the count a pending settle Enter was armed at, and
    # the stamp of an Enter held because a dialog opened after its text.
    _question_parked = False
    _composer_dirty = False
    _composer_dirty_at = 0.0
    _park_seq = 0
    _dirty_at_park: tuple[bool, float] = (False, 0.0)
    _submit_park_seq = 0
    _held_enter_at: float | None = None
    # No turn-end waiters on a partial instance (the turn-complete funnel
    # reads it); __init__ gives every real session its own list.
    _turn_end_waiters: list[TurnCompleteCb] | tuple[()] = ()
    # The idle clock (``last_activity``) and the quiet clock: the newest PTY
    # byte in or out or person's action, which the injection gate reads
    # (``quiet_seconds``).
    _last_activity = 0.0
    _last_io_at = 0.0

    def __init__(
        self,
        *,
        session_id: str,
        chat_id: str,
        agent_name: str,
        user_sub: str = "",
        role: str = "",
        username: str = "",
        target: str = placement.LOCAL,
        remote_os: str = "",
        rows: int = DEFAULT_ROWS,
        cols: int = DEFAULT_COLS,
        execution_path: str = DEFAULT_EXECUTION_PATH,
        prompt_in_argv: bool = False,
        tui_theme: str = "dark",
        chat_row: dict | None = None,
        token_expires_at: float | None = None,
        payer: str | None = None,
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
        # The ENGINE whose TUI this PTY runs — its descriptor answers the
        # per-engine questions here (which transcript tailer persists the
        # turns, whether the TUI needs the ConPTY Enter backstop) and its short
        # name is what session_events reports. Fail-closed: an unknown id is
        # refused at construction, like every builder.
        from core.session.session_manager import capabilities_for_path
        self.execution_path = execution_path
        self._engine_caps = capabilities_for_path(execution_path)
        # "claude" / "codex" — the engine's short name; the transcript persisted
        # for this session is that engine's (see _tailer).
        self.transcript_kind = self._engine_caps.identity.short_name
        # The TUI theme baked into this session at seed time. Dashboard viewers
        # render their xterm with THIS theme, not the dashboard mode — a
        # dark-seeded TUI (otodock-opened and re-warmed sessions default dark)
        # paints its text for a dark background, so a light xterm under it turns
        # white-on-white. Sent to viewers in the pty_status "attached" frame.
        self.tui_theme = "light" if str(tui_theme).lower().startswith("light") else "dark"

        # Set by the registration, a machine's PTY; what runs on a live
        # session casts its reads.
        self.pty: RemotePtyProcess | None = None
        self.created_at = time.monotonic()
        self.last_activity = self.created_at
        # When the session token in the CLI's env ends (wall clock): a parked
        # question is spared no longer (reap_spare_reason).
        self.token_expires_at = (
            token_expires_at if token_expires_at is not None
            else time.time() + _SESSION_TOKEN_LIFE_S)
        # Whose account the start's config was built to pay with ("" the
        # shared subscriptions), None for a session taken back after a
        # restart: a stored wake of a lane's chat asks it before the seat is
        # bound (``spender.serves_lane``).
        self.payer = payer

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
        self._otodock_kick_timer: asyncio.TimerHandle | None = None

        self._output_listeners: set[OutputListener] = set()
        # Single-viewer: the eviction callback of the CURRENT viewer.
        # A new viewer attaching evicts the prior one so two devices/tabs don't
        # fight over the one PTY size (cross-device garbling). Multi-viewer with
        # per-viewer sizing is a future enhancement. The callback takes a REASON
        # string (dual-control): "superseded" (another dashboard tab/device) or
        # "superseded_otodock" (a local `otodock` terminal took over) — the viewer
        # maps it to its banner ("opened on another device" / "opened in a local
        # terminal").
        self._viewer_evict: Callable[[str], Awaitable[None] | None] | None = None
        # The controller's own listener (the one an eviction drops) and the
        # close notices of the read-only mirrors, which outlive a controller.
        self._viewer_listener: OutputListener | None = None
        self._listener_close: dict[OutputListener, CloseCb] = {}
        # True once ANY real viewer attached during this session's lifetime
        # (dashboard PTY view / otodock attach). A premature PTY death on an
        # interactive WORKER with had_viewer set reads as a deliberate user
        # stop (user_interrupted), not a crash — the scheduler keys on it.
        self.had_viewer = False
        self.on_perm_event: PermEventCb | None = None
        self.on_close: CloseCb | None = None
        # Child exit code, captured by _on_pty_exit BEFORE close(reason=
        # "exited") — the dashboard surfaces a non-zero code ("process exited
        # (code 1)") instead of a blank/ambiguous "session ended".
        self.exit_code: int | None = None
        # The machine's memory guard ended the engine (a remote PTY's exit says so).
        self.ended_for_memory = False
        # Viewer hook fired when the REMOTE PTY transport is
        # reconnecting/reconnected (satellite WS blip). Single-slot (last viewer
        # wins), like on_close/on_perm_event. Stays None for local sessions.
        self.on_status: StatusCb | None = None
        # Interactive-TASK run completion. Set by the scheduler's interactive-task
        # watcher; fired ONCE when a turn-end signal lands with the bg
        # SubagentRegistry empty + min-turn-time elapsed.
        self.on_turn_complete: TurnCompleteCb | None = None
        # A chat's terminal a wake or a continue warmed: it has the callback
        # as a task's terminal does, and its turns still need the open-turn row.
        self.chat_terminal_rewarmed = False
        self._turn_complete_fired = False
        # One-shot turn-end waiters (a borrowed-terminal delegate round waits
        # for the first turn end after ITS prompt was injected): fired and
        # cleared at the same point as the task callback.
        self._turn_end_waiters: list[TurnCompleteCb] = []
        # The typed ending of the last turn that ended short (a limit, an API
        # error: its tailer's row), cleared when a turn opens; and the mark
        # of a limit its server prompts wait behind (``LimitMark``).
        self.last_ending: TurnEnding | None = None
        self.limit_mark: LimitMark | None = None
        # The engine's own limit menu may be open (its answer is typed keys):
        # one Escape goes before the limit's continuation is typed.
        self._limit_menu = False
        # When the engine last answered a turn of this terminal: an account's
        # rest that began before it holds nothing here (``limit_ending``).
        self._served_at = 0.0
        # LLM chat-title generation (services/title_generator.py): armed for
        # chats not yet LLM-titled (mirrors the pump's _title_armed — resumed
        # already-titled chats arm False and skip everything, timer included).
        # Fired ONCE from whichever trigger lands first — tail-batch char/tool
        # thresholds (_maybe_fire_title_early), the one-shot early timer, or
        # the turn-complete funnel — then disarmed; exactly-once across
        # sessions/fire points is the DB claim's job. Task chats title like
        # every chat; a meeting never runs an interactive session (its pump
        # is disarmed by its driver kind).
        armed = bool(chat_id)
        if armed:
            if chat_row is None:
                # Construction without a pre-loaded row (tests, legacy
                # callers): one synchronous read. The spawn paths pass
                # ``chat_row`` (loaded via run_db) so this never runs on the
                # event loop in production (storage/pg.py rule).
                try:
                    from storage import database as task_store
                    chat_row = task_store.get_chat(chat_id) or {}
                except (ImportError, psycopg.Error):
                    logger.debug("interactive %s: title-arm lookup failed",
                                 session_id[:8], exc_info=True)
                    chat_row = {}
            armed = not chat_row.get("title_generated")
        self._title_armed = armed
        # Pre-seed the chat owner from the same row so ``_chat_owner()`` (the
        # turn-end broadcast identity) never reads the store on the loop. A
        # row that failed to load answers with the person, so a shared-only
        # chat then broadcasts to that one person; the chat's row exists
        # before every spawn. Consumed below where the attribute is initialised.
        self._chat_owner_seed = (None if chat_row is None
                                 else chat_row.get("user_sub") or self.user_sub or "")
        # A delegated lane's chat (a chat-surface worker's has a plain id):
        # steered by role, never read as a session for nobody.
        self.chat_is_lane = (chat_row or {}).get("origin") == "delegated"
        self._title_chars = 0   # accumulated tail-batch counters (first turn)
        self._title_tools = 0
        self._title_timer: asyncio.TimerHandle | None = None
        # Once-only across the session — an interrupted first turn leaves
        # _title_armed set, and the SECOND turn's open transition must not
        # re-arm the timer (the cap is on the first wait, not every turn).
        self._title_timer_spent = False

        self._loop = asyncio.get_running_loop()
        self._drainer: asyncio.Task | None = None
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
        self._settle_handle: asyncio.TimerHandle | None = None
        # The tools gate (``_tools_pending``): when the readiness bound
        # passes, and the pending look-again.
        self._ready_deadline = 0.0
        self._tools_recheck: asyncio.TimerHandle | None = None
        self._ready_max_handle: asyncio.TimerHandle | None = None
        # Deferred submit (cold first-prompt Enter — see _SUBMIT_SETTLE_S). ONE
        # Enter once the composer settles (or _SUBMIT_MAX_S backstop), then an
        # OUTPUT-GATED re-fire if no burst follows (the Windows-ConPTY swallow).
        self._submit_settle_handle: asyncio.TimerHandle | None = None
        self._submit_max_handle: asyncio.TimerHandle | None = None
        # The Windows-remote Claude backstop Enter (the 2nd of 2) — its timer +
        # a counter so it fires at most once (0 = first Enter, 1 = backstop fired).
        self._submit_recheck_handle: asyncio.TimerHandle | None = None
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
        self._cold_flush_watcher: asyncio.TimerHandle | None = None
        self._spawn_ts = time.monotonic()
        # DB-fallback resume: a restored-history digest stashed BEFORE the cold
        # flush; prepended (as a bracketed paste) to the first submission so the
        # fresh CLI continues with context. Cleared once consumed.
        self._pending_seed_digest = ""
        # Post-turn transcript tail (debounced on output quiet — see _POST_OUTPUT_TAIL_S).
        self._tail_handle: asyncio.TimerHandle | None = None
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
        self._resume_tail_handle: asyncio.TimerHandle | None = None
        # Mid-turn periodic tail (local only): armed on the turn-open
        # transition, re-arms itself each _MIDTURN_TAIL_S while the turn stays
        # open, cancelled on close. The task ref lets a beat be skipped while
        # the previous tail is still off-thread (slow disk / huge batch).
        self._midturn_tail_handle: asyncio.TimerHandle | None = None
        self._midturn_tail_task: asyncio.Task | None = None
        # Server-prompt queue (delegate results …): injected at CLI quiescence,
        # FIFO, ONE item per turn. Items are dicts carrying full re-delivery
        # context (text/source/chat_id/agent/user_sub/role/hops + the ladder's
        # injected rung callables) so close() can hand undelivered items back
        # to the delivery ladder once the PTY is gone.
        self._prompt_queue: deque[dict] = deque()
        self._inject_backstop_handle: asyncio.TimerHandle | None = None
        self._draining_prompts = False
        # otodock-attached: the ONE in-flight satellite injection —
        # {"inject_id", "sent_at", "item"}. The item stays queued until the
        # satellite ACKs (handle_inject_result removes exactly it), so a lost
        # result frame re-sends the same id and the satellite's dedupe re-ACKs.
        self._satellite_inject: dict | None = None
        # Turn-open state, derived SOLELY from the tailers' last_signal (both
        # CLIs write the user message to the transcript at submit time, so no
        # submit tracking is needed): "user"/"tool_use" → open, "end_turn" →
        # closed. Set True synchronously on our own injection — the tailer
        # won't see the injected user line for one poll (0.8–3s). Transitions
        # broadcast the sidebar chat_status (streaming/ready) — interactive has
        # no pump, so these ARE the chat's live-dot signals.
        self._turn_open = False
        # The open turn's token on the chat's open-turn row (``_note_turn_row``).
        self._turn_token = ""
        # Parked on an unanswered AskUserQuestion: the turn is CLOSED (question
        # fold) but the TUI is showing the dialog — server-prompt injection is
        # gated off (the paste would land in the dialog). Cleared on any
        # reopen or on a real end_turn (ESC dismissed the question).
        self._question_parked = False
        # When the hook parked it, by the clock of the CLI's host (the gate's
        # ``hook_sent_at``): a tail batch whose reopening line was journaled
        # before the park is a read that raced the park, not a new turn.
        self._parked_at = None
        # The chat ROW's owner — for shared-only agents it is the synthetic
        # agent::<slug>, which the status broadcast fans out to every user of
        # the agent; self.user_sub would reach only one. Seeded from the
        # spawn-time chat row (loaded off-loop).
        self._chat_owner_sub: str | None = self._chat_owner_seed
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
        # Last reap-spare reason logged for this session (change-only INFO in
        # reap_idle — the spare re-fires every sweep for hours-long parks).
        self._last_reap_spare: str | None = None
        # Whether this terminal's last Stop listed background work the agent
        # started (note_background_report; the background-pending spare).
        self._background_reported = False
        # Serialize persistence of transcript lines FORWARDED from a remote
        # satellite (the satellite tails its JSONL + sends new lines; we persist
        # them in arrival order). Local sessions never use this (they read the
        # on-disk JSONL via the debounced tail above).
        self._transcript_lock = asyncio.Lock()

    # -- activity / idle ------------------------------------------------------
    def _note_activity(self, *, io: bool = True) -> None:
        """Stamp the idle clock; ``io`` also stamps the quiet clock. A
        transcript turn signal passes ``io=False``: the tail may read lines
        written seconds ago, and the PTY was not touched now."""
        now = time.monotonic()
        self._last_activity = now
        if io:
            self._last_io_at = now

    @property
    def last_activity(self) -> float:
        """Monotonic time of the last activity: input, a prompt, an interrupt,
        a viewer attaching, a transcript turn signal, or output inside an open
        turn. The idle reaper and the evictor age a session from it."""
        return self._last_activity

    @last_activity.setter
    def last_activity(self, value: float) -> None:
        # Assigning sets when the session was last heard from: both clocks.
        self._last_activity = value
        self._last_io_at = value

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
        self._ready_deadline = time.monotonic() + READY_MAX_S
        self._ready_max_handle = self._loop.call_later(READY_MAX_S, self._mark_ready)

    def _tools_pending(self) -> bool:
        """The engine still lacks a stand-in's tool list (a Claude Code
        terminal sends its first message without the servers still
        connecting) and the readiness bound has not passed: hold the
        buffered input, look again shortly. At the bound the input goes in
        and one line names what never reported."""
        from services.mcp import tool_listing
        pending = tool_listing.pending_listed(self.session_id)
        if not pending:
            if self._tools_held_at:
                logger.info("interactive %s: the tools are listed, input goes in after %.1f s",
                            self.session_id[:8], time.monotonic() - self._tools_held_at)
            return False
        if time.monotonic() < self._ready_deadline:
            if not self._tools_held_at:
                self._tools_held_at = time.monotonic()
                logger.info("interactive %s: ready, input waits for %d tool list(s)",
                            self.session_id[:8], len(pending))
            if self._tools_recheck is None:
                self._tools_recheck = self._loop.call_later(_TOOLS_RECHECK_S, self._recheck_tools)
            return True
        logger.info("interactive %s: tools not listed by the bound, input goes in: %s",
                    self.session_id[:8], ", ".join(sorted(pending)))
        return False

    def _recheck_tools(self) -> None:
        self._tools_recheck = None
        self._mark_ready()

    def _mark_ready(self) -> None:
        """The TUI is ready — flush any input buffered before now (once the
        engine has its stand-ins' tool lists, ``_tools_pending``)."""
        if self._ready or self._closed:
            return
        if self._tools_pending():
            return
        from services.mcp import tool_listing
        tool_listing.drop_listed(self.session_id)
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
            self._cold_flush_enter_at = 0.0  # stamped by the first Enter
            # Cold flush: send Enter only after the TUI's echo settles (the input
            # box has just rendered — a fixed short delay can miss it).
            self._emit_to_pty(buf, deferred_submit=True)
            logger.info(
                "interactive %s: ready — flushed %d buffered input byte(s)",
                self.session_id[:8], len(buf),
            )

    @property
    def idle_seconds(self) -> float:
        """The age the idle reaper and the evictor read (``last_activity``)."""
        return time.monotonic() - self.last_activity

    @property
    def quiet_seconds(self) -> float:
        """Seconds since the last PTY byte in either direction or person's
        action (the injection gate): output outside a turn is no activity,
        but it is not quiet."""
        return time.monotonic() - self._last_io_at

    @property
    def alive(self) -> bool:
        return not self._closed and self.pty is not None and not self.pty.closed

    @property
    def closing(self) -> bool:
        """``close`` has begun: the session is still ``alive`` until it ends,
        but what is queued for it now is never delivered to its viewer."""
        return self._closing or self._closed

    def resize(self, rows: int, cols: int) -> None:
        # dual-control: drop dashboard resizes while the otodock terminal is
        # the active controller (it owns the PTY size; a dashboard resize would
        # fight it). The otodock client resizes satellite-side. Once the dashboard
        # takes over (otodock_attached cleared), its resizes apply again.
        if self.otodock_attached:
            return
        self.rows, self.cols = rows, cols
        if self.alive:
            cast("RemotePtyProcess", self.pty).resize(rows, cols)

    # -- drainer (permission queue → viewer) ---------------------------------
    def _start_drainer(self) -> None:
        if self._drainer is None:
            self._drainer = self._loop.create_task(self._drain_loop())

    async def _drain_loop(self) -> None:
        # Late import: session_state pulls in core deps; keep module load light.
        from core.events.artifact_events import REPLAYABLE_ARTIFACT_EVENT_TYPES
        from core.session.session_state import get_permission_queue
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
                if (item.get("event_type") in REPLAYABLE_ARTIFACT_EVENT_TYPES
                        or item.get("event_type") in CARD_ITEMS):
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
        if not self.chat_id:
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
    def _on_pty_exit(self, code: int | None) -> None:
        # The CLI process died (normal exit / crash / killed). Tear the session
        # down. Scheduled because the PTY's handle fires this from its own task.
        if self._closed or self._closing:
            return
        logger.info("interactive %s: pty exited code=%r", self.session_id[:8], code)
        self.exit_code = code
        # A remote machine's memory guard ended the engine: the chat hears it
        # as a headless chat does (``close`` writes the card).
        self.ended_for_memory = getattr(self.pty, "exit_reason", "") == "memory"
        spawn(self.close(reason="exited", _signal_child=False),
              name="close", owner="interactive_session")

    async def close(self, *, reason: str = "closed", _signal_child: bool = True) -> None:
        """Tear down: stop the drainer, kill the PTY, release the slot, purge
        per-session security/permission state, and drop from the registry.
        Idempotent."""
        if self._closing or self._closed:
            return
        self._closing = True

        for h in (self._settle_handle, self._ready_max_handle, self._tail_handle,
                  self._inject_backstop_handle, self._resume_tail_handle,
                  self._midturn_tail_handle, self._title_timer, self._tools_recheck,
                  self._dropped_turn_check):
            if h is not None:
                h.cancel()
        self._settle_handle = self._ready_max_handle = self._tail_handle = None
        self._inject_backstop_handle = self._resume_tail_handle = None
        self._midturn_tail_handle = None
        self._title_timer = None
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
        deleted = reason == "chat_deleted"
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
            # A chat being deleted has no audience for it.
            if _r.get("turn_complete") and not deleted:
                self._maybe_fire_turn_complete(
                    _r.get("last_message", "") or "", _r.get("persisted", 0),
                    reopened=leaves_turn_open(_r.get("last_signal")),
                )
        except Exception:
            logger.exception(
                "interactive %s: final transcript tail failed", self.session_id[:8]
            )
        # After the conversation's last rows and before the viewer hears of
        # the exit: its page reloads the chat's history, which holds the card.
        if self.ended_for_memory and self.chat_id and not deleted:
            await _note_memory_end(self.chat_id)
        # The notes this session made for prompts it typed and the journal
        # never gave back: the final tail above was the last chance (a
        # forwarded batch still in flight finishes first).
        if self.chat_id:
            async with self._transcript_lock:
                drop_sent_prompts(self.chat_id, owner=self.session_id)

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
        # no-op when the session never bound one). The home a terminal on a
        # compute machine ran from goes first, while the binding names it.
        from core import file_commit
        from core.sandbox.session_config_dir import close_session_home
        await file_commit.run_in("session-homes", close_session_home, self.session_id)
        # Drop from the registry before the seat goes: a wake its release
        # replays must find no live terminal here, or it is stored again
        # with nothing left to fire it. No lock needed — the get/pop pair is
        # atomic under single-threaded asyncio, and close() runs while a
        # registration holds the registry lock during a supersede (taking it
        # here would deadlock).
        if _sessions.get(self.session_id) is self:
            _sessions.pop(self.session_id, None)
            from core.session.session_state import mark_closing
            mark_closing(self.session_id)
        from services.engines.subscription_pool import release_subscription
        release_subscription(self.session_id)
        placed = cleanup_session_permission_state(self.session_id)
        tailer.forget(self.session_id)
        # A Codex terminal's own profile and text (a no-op for any other),
        # and the stand-ins a first message was waiting for.
        from core.layers.codex import session_text
        from services.mcp import tool_listing
        session_text.forget(self.session_id)
        tool_listing.drop_listed(self.session_id)

        # A session dying mid-turn can never emit its end_turn signal — clear
        # the sidebar dot (and stamp the partial response) via the transition
        # broadcast. No-op when the turn already closed. A chat being deleted
        # gets no ready frame and no unread stamp: its row goes next, and the
        # frame would leave a finished row for an id no list carries.
        if deleted or reason == PLATFORM_STOPPING:
            # (the platform's stop: the turn's open-turn row stays for the
            # boot, and nobody is told the turn ended)
            self._turn_open = False
        else:
            self._set_turn_open(False)
            # Messages a teammate queued while this terminal refused them go
            # out now (core/events/input_queue.py, WAITING_TERMINAL).
            from core.events import input_queue
            if self.chat_id and input_queue.peek(self.chat_id):
                input_queue.clear_waiting(self.chat_id, input_queue.WAITING_TERMINAL)
                input_queue.arm(self.chat_id)

        self._closed = True
        self._output_listeners.clear()
        closers = list(self._listener_close.values())
        self._listener_close.clear()
        logger.info("interactive %s: closed (%s)", self.session_id[:8], reason)

        # Hand undelivered server prompts back to the delivery ladder — the PTY
        # is gone (registry entry popped above), so the headless rungs are safe
        # now (the one-shot's live-PTY guard no longer sees this session).
        # Scheduled as a task: close() can run under the registry lock during a
        # supersede, and re-delivery does layer I/O.
        if self._prompt_queue:
            pending = list(self._prompt_queue)
            self._prompt_queue.clear()
            spawn(_redeliver_pending(self, pending),
                  name="redeliver_pending", owner="interactive_session")

        for notify in ([self.on_close] if self.on_close is not None else []) + closers:
            try:
                res = notify(self, reason)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logger.exception("interactive %s: on_close failed", self.session_id[:8])
        # The context's removal is on disk before the close returns (a
        # terminal on a machine is taken back after a restart by its context).
        # Not for a supersede: it runs under the registry's lock, and the
        # same id registers again at once.
        if placed and reason != "superseded":
            from core.session.session_state import security_index_written
            await security_index_written()


async def _note_memory_end(chat_id: str) -> None:
    """The headless chat's card for a turn whose engine the machine's memory
    guard ended (``TurnEnding(EXITED, MEMORY)``)."""
    from core.events import turn_ending
    from core.session.interactive_turn import write_turn_ended_card
    await write_turn_ended_card(chat_id, turn_ending.TurnEnding(reason=turn_ending.EXITED,
                                                                detail=turn_ending.MEMORY), label="memory_end")


async def _redeliver_pending(sess: InteractiveSession, items: list[dict]) -> None:
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


def settings_card_from_perm_item(item: dict) -> dict:
    """The settings card's event for a drained item (the pump's twin builds
    the same block)."""
    return wire.settings_card_block(
        item.get("setting", ""), item.get("label", ""), int(item.get("catalog_version") or 0))


def action_card_from_perm_item(item: dict) -> dict:
    """The action card's event for a drained item (services/cards)."""
    return wire.action_card_block(
        str(item.get("card_id") or ""), str(item.get("title") or ""), str(item.get("text") or ""),
        str(item.get("note") or ""), list(item.get("buttons") or []), list(item.get("options") or []))


def list_card_from_perm_item(item: dict) -> dict:
    """The list card's event for a drained item (services/cards)."""
    return wire.list_card_block(
        str(item.get("kind") or ""), dict(item.get("filters") or {}), str(item.get("title") or ""),
        str(item.get("person") or ""))


# The card items of the permission queue and the block each becomes: the
# drainer persists them like a final artifact and sends them as windows.
CARD_ITEMS = {
    wire.ITEM_SETTINGS_CARD: settings_card_from_perm_item,
    wire.ITEM_ACTION_CARD: action_card_from_perm_item,
    wire.ITEM_LIST_CARD: list_card_from_perm_item,
}


def persist_drained_artifact(chat_id: str, item: dict) -> int | None:
    """Persist one drained display/file-tools artifact as a pump-shaped
    chat_messages event row — the interactive twin of the headless pump's
    ``_save_turn_blocks`` (same event shape via ``artifact_event_from_perm_item``,
    so ``eventToBlock`` renders both identically). Only the REPLAYABLE final
    types persist; blocking prompts, placeholders and failure/removal events
    return None untouched. Returns the row id (the dashboard's stable
    replay/dismissal key) or None — persistence must never break live delivery,
    hence the blanket except. Runs off the loop (``asyncio.to_thread``)."""
    from core.events.artifact_events import (
        LIVE_ONLY_KEYS,
        REPLAYABLE_ARTIFACT_EVENT_TYPES,
        artifact_event_from_perm_item,
    )

    et = item.get("event_type")
    if not chat_id or (et not in REPLAYABLE_ARTIFACT_EVENT_TYPES and et not in CARD_ITEMS):
        return None
    try:
        # A card persists like a final artifact: the rich view behind the
        # terminal and the history show it.
        event = CARD_ITEMS[et](item) if et in CARD_ITEMS else artifact_event_from_perm_item(item)
        if event is None:
            return None
        # The WOPI token rides the live frame only; a replayed card mints its own.
        event = {k: v for k, v in event.items() if k not in LIVE_ONLY_KEYS}
        import json

        from storage import database as task_store
        return task_store.add_chat_message(
            chat_id, "event", "",
            event_type=event["type"], event_data=json.dumps(event),
        )
    except Exception:
        logger.exception("interactive: artifact persist failed for chat %s", chat_id)
        return None


async def _register_session(
    session: InteractiveSession,
    *,
    pty_factory,
) -> InteractiveSession:
    """The registration spine of an interactive session.

    Enforces the lease (one live process per ``session_id``), acquires a chat
    slot, creates ``session.pty`` via ``pty_factory`` (a machine's
    ``remote_pty.spawn_remote_pty``, or the handle a reconnect adopts), stores
    the session, and starts the drainer + readiness gate.
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
        # idempotent no-op (no double-count). The owner is recorded for the
        # per-person count; the cap never refuses here (the person asked for
        # this chat and the funnel already judged it).
        adm = await concurrency.acquire_chat_slot(
            session.session_id, target=session.target,
            user_sub=session.user_sub or None, per_user_cap=False,
        )
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
            from core.config import spender
            from storage import database as task_store
            # A lane's wake is never typed into a terminal that serves
            # another person: it stays stored for one that serves the lane.
            serves = await asyncio.to_thread(functools.partial(
                spender.serves_lane, session.chat_id, session.session_id, built_for=session.payer))
            wakes = await asyncio.to_thread(
                task_store.claim_pending_delegate_wake, session.chat_id,
            ) if serves else []
            for wake in wakes:
                session.queue_prompt(wake, "delegate_result_replay",
                                     chat_id=session.chat_id, note=True)
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


async def register_remote(
    *,
    session_id: str,
    chat_id: str,
    agent_name: str,
    machine_id: str,
    execution_path: str,
    config_payload: dict,
    rows: int = DEFAULT_ROWS,
    cols: int = DEFAULT_COLS,
    user_sub: str = "",
    role: str = "",
    username: str = "",
    scrollback_limit: int = DEFAULT_SCROLLBACK_BYTES,
    prompt_in_argv: bool = False,
    tui_theme: str = "dark",
    payer: str | None = None,
) -> InteractiveSession:
    """Register an interactive session whose PTY runs on a REMOTE satellite.

    ``session.pty`` is a
    :class:`core.remote.remote_pty.RemotePtyProcess` driven over the satellite WS — the
    satellite assembles the interactive argv from ``config_payload`` (the SAME
    payload the ``-p`` path uses, ``RemoteExecutionLayer._build_start_payload``)
    and spawns it under a PTY. ``target`` is the ``machine_id``. The PROXY-side
    identity (``set_session_security`` etc.) is set by the caller BEFORE this
    call.
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
        execution_path=execution_path, prompt_in_argv=prompt_in_argv,
        tui_theme=tui_theme,
        chat_row=await _load_chat_row(chat_id),
        # The payload's token, minted at the payload build, which can run
        # minutes before this session exists (a satellite MCP install).
        token_expires_at=_token_expires_at(config_payload.get("env")),
        payer=payer,
    )

    async def _make_remote_pty(s: InteractiveSession):
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
        session_id[:8], machine_id[:8], chat_id, agent_name, cast("RemotePtyProcess", sess.pty).pid,
    )
    return sess


def adopt_remote(
    *, session_id: str, chat_id: str, agent_name: str, machine_id: str, execution_path: str,
    user_sub: str, role: str, username: str, mode: str, token_floor: int,
    turn_token: str, chat_row: dict,
) -> InteractiveSession:
    """A terminal the machine still runs, taken back as its chat's: the
    session and its handle are built and registered with no await and no
    lock (the claim runs in the machine's read loop), the TUI reads as ready,
    the mode and floor are restored, and the chat's open-turn token, when
    the row is this session's, is this session's. The slot, the stored wakes
    and the credentials follow in a task of their own."""
    from core.remote import remote_pty
    from core.remote.satellite_connection import get_connection_manager
    from core.session import session_state
    sess = InteractiveSession(
        session_id=session_id, chat_id=chat_id, agent_name=agent_name,
        user_sub=user_sub, role=role, username=username, target=machine_id,
        remote_os=get_connection_manager().satellite_os(machine_id),
        execution_path=execution_path, chat_row=chat_row,
    )
    sess.pty = remote_pty.adopt_remote_pty(
        machine_id=machine_id, session_id=session_id, rows=sess.rows, cols=sess.cols,
        on_output=sess._fanout_output, on_exit=sess._on_pty_exit,
    )
    session_state.restore_session_state(session_id, mode, token_floor)
    session_state._record_session_use(session_id, client_type=session_kind.DASHBOARD.name,
                                      agent=agent_name)
    if turn_token:
        sess.adopt_turn_token(turn_token)
    _sessions[session_id] = sess
    sess._start_drainer()
    sess._mark_ready()
    # Its binding comes last in the tail: on its way until then, so the
    # machine's kept sessions are not all back yet (``reported_since_boot``).
    from core.remote import satellite_link
    satellite_link.adoption_begins(machine_id, session_id)
    spawn(_finish_adoption(sess), name="adopt_remote", owner="interactive_session")
    logger.info("interactive %s adopted REMOTE (machine=%s chat=%s agent=%s turn=%s)",
                session_id[:8], machine_id[:8], chat_id[:8], agent_name, "open" if turn_token else "idle")
    return sess


async def _finish_adoption(sess: InteractiveSession) -> None:
    """What a start does after its registration, for an adopted terminal: the
    connected accounts refilled from the session's descriptor (the same call
    as a headless take-back's; first, so a failure below cannot skip it), the
    chat slot (a paired machine's counts as none here; a compute machine's
    is the reconciler's, ``concurrency._adopt_compute_sessions``, as for a
    headless take-back), the stored wakes, the seat and the credential
    fan-out (on a compute machine the platform's own file, by the session's
    own placement)."""
    from core import concurrency
    from core.session import session_state
    from core.session.session_manager import _get_remote_layer
    from storage import database as task_store
    try:
        remote = _get_remote_layer()
        if remote is not None and sess.alive:
            await remote._reprovision_gateway(sess.session_id, sess.target)
        ctx = session_state.get_session_security(sess.session_id)
        compute_pl = placement.compute_of(getattr(ctx, "placement", None))
        on_compute = compute_pl is not None
        if not on_compute:
            await concurrency.acquire_chat_slot(sess.session_id, target=sess.target,
                                                user_sub=sess.user_sub or None, per_user_cap=False)
        from core.config import spender
        serves = await asyncio.to_thread(functools.partial(
            spender.serves_lane, sess.chat_id, sess.session_id, built_for=sess.payer))
        wakes = (await asyncio.to_thread(task_store.claim_pending_delegate_wake, sess.chat_id)
                 if serves else [])
        for wake in wakes:
            sess.queue_prompt(wake, "delegate_result_replay", chat_id=sess.chat_id, note=True)
        if remote is not None:
            await asyncio.to_thread(remote._restore_adopted_credentials, sess.session_id,
                                    sess.target, sess.agent_name, sess.execution_path,
                                    local_file=compute_pl is not None and not compute_pl.own_copy)
    except Exception:
        logger.exception("interactive %s: the adoption's tail failed", sess.session_id[:8])
    finally:
        from core.remote import satellite_link
        satellite_link.adoption_ends(sess.target, sess.session_id)
