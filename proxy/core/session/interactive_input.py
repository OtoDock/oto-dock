"""What reaches a terminal session's PTY: a person's keys, a submitted prompt with
its deferred Enter and tails, and the cold-start flush with its retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from core import host_os
from core.background import spawn
from core.session.interactive_common import (
    _BREADCRUMB_MIN_CHARS,
    _COLD_RETRY_MAX,
    _COLD_RETRY_S,
    _DIALOG_KEYS_RE,
    _DROPPED_TURN_LOOKS,
    _DROPPED_TURN_QUIET_S,
    _DROPPED_TURN_S,
    _SUBMIT_ENTER_DELAY_S,
    _SUBMIT_MAX_S,
    _SUBMIT_SETTLE_S,
    _SUBMIT_WIN_BACKSTOP_S,
    _breadcrumb_text,
)
from core.session.interactive_queue import InteractiveQueueMixin
from core.terminal_queries import (
    MOUSE_RE as _MOUSE_RE,
    strip_replies as _strip_terminal_replies,
)
from ws import wire_events as wire

if TYPE_CHECKING:
    from core.remote.remote_pty import RemotePtyProcess

logger = logging.getLogger("otodock.interactive")

# When a submit's tails read the transcript, so the turn it opens is known.
_SUBMIT_TAILS_S: tuple[float, ...] = (1.5, 4.0)


class InteractiveInputMixin(InteractiveQueueMixin):
    # The session state this mixin assigns, declared where it assigns it: the
    # type checker reads an attribute a mixin assigns from the mixin first.
    # Annotations only, no class attributes.
    _cold_flush_attempts: Any
    _dropped_turn_check: Any
    _dropped_turn_looks: Any
    _escape_at: Any
    _cold_flush_enter_at: Any
    _cold_flush_pending: Any
    _cold_flush_seed: Any
    _cold_flush_watcher: Any
    _composer_dirty: Any
    _composer_dirty_at: Any
    _dirty_at_park: Any
    _held_enter_at: Any
    _pending_seed_digest: Any
    _submit_max_handle: Any
    _submit_park_seq: Any
    _submit_recheck_handle: Any
    _submit_refires: Any
    _submit_settle_handle: Any
    _submit_tail_handles: Any
    _submitted_once: Any

    # -- input / resize (proxy → PTY) ----------------------------------------
    def deliver_dashboard_input(self, data: bytes, composer: bool = False,
                                sender_sub: str = "") -> bool:
        """Dashboard input router. ``composer=True`` marks a discrete chat-box
        send (the FE flags it; raw terminal keystrokes are never flagged):
        while the turn is question-parked, typing it through would land the
        paste in the open picker's notes field and the trailing CR would
        submit the recommended option — so the text is HELD in the server
        prompt queue instead, as a steer carrying the page's own bytes: the
        answer unparks the turn and the drain types it into the running turn
        as the page sent it (a dismissed question: as the next prompt).
        Everything else funnels straight to :meth:`write_input` (answering
        in the TUI keeps working).

        ``sender_sub`` is the user whose socket this input arrived on. Input
        from anyone but the session's controller is DROPPED (:meth:`may_drive`)
        — the identity gate for Shared-only chats. Returns False when the input
        was refused for that reason, so the caller can tell the sender why;
        every other outcome (delivered, queued, buffered) returns True.

        This is the right chokepoint precisely because it is the HUMAN dashboard
        funnel (``pty_input`` + ``pty_attachments``). The gate must not sink to
        ``write_input``, which also carries ``submit_prompt`` — scheduler wakes
        and cold submits legitimately have no human sender.
        """
        if not self.may_drive(sender_sub):
            logger.info(
                "interactive %s: dropped %d input byte(s) from a non-controller",
                self.session_id[:8], len(data),
            )
            return False
        if composer and self._question_parked:
            text = data.decode("utf-8", errors="replace")
            if text.startswith("\x1b[200~") and text.endswith("\x1b[201~\r"):
                text = text[6:-7]
            text = text.rstrip("\r\n")
            if text and self.queue_prompt(text, source="dashboard", steer=True, raw=data):
                return True
        self.write_input(data)
        return True

    def write_input(self, data: bytes) -> bool:
        """Write ``data`` to the PTY (or its readiness buffer). False when
        nothing was written: a dead PTY, a local ``otodock`` terminal holding
        the input, or bytes that were all terminal auto-responses."""
        if not self.alive:
            return False
        # dual-control: while a local `otodock` terminal is the active
        # controller (otodock_attached), the dashboard is a DETACHED (evicted)
        # viewer — DROP its input so the two never fight over the one PTY. This is
        # the authoritative server-side gate covering every dashboard write path
        # (pty_input, pty_attachments, and submit_prompt all funnel here). The
        # otodock terminal drives the PTY satellite-side (session.write), never
        # through this proxy method, so it is unaffected.
        if self.otodock_attached:
            return False
        # Drop the mirror xterm's terminal auto-responses FIRST (see
        # core.terminal_queries.TERMINAL_REPLY_RE): they aren't user input,
        # must not reach the PTY, and must not cancel the pending cold-submit
        # Enter (the Windows-remote first-prompt-never-submits bug).
        data = _strip_terminal_replies(data)
        if not data:
            return False
        self._note_activity()
        if data == b"\x1b":
            # The Escape key (the person's, or the control bar's button):
            # a Claude Code turn it stops before its first word ends with no
            # line in the transcript (``_check_dropped_turn``).
            self._note_escape()
        # Composer-dirty tracking (injection gate): only the residue that would
        # also cancel the cold submit counts as typing — mouse reports are
        # pointer, not text. A write ENDING with CR/NL is a submit (interior
        # paste newlines don't submit); Ctrl-C empties the composer.
        _typed = _MOUSE_RE.sub(b"", data)
        if _typed and self._question_parked:
            # While a dialog shows the keys go to it, not to the TUI's input
            # box: its Enter puts the flag back as the park found it (what was
            # typed since went to the dialog) and still tails at once so the
            # answer reopens the turn live; the keys a picker takes leave the
            # flag; other text may be a new draft (the park lags the dialog's
            # close by a tail) and marks it.
            if _typed.endswith((b"\r", b"\n")):
                self._composer_dirty, self._composer_dirty_at = self._dirty_at_park
                self._schedule_submit_tails()
            elif not _DIALOG_KEYS_RE.fullmatch(_typed):
                self._composer_dirty = True
                self._composer_dirty_at = time.monotonic()
        elif _typed:
            if _typed.endswith((b"\r", b"\n")):
                if self._composer_dirty or _typed.strip(b"\r\n"):
                    # The person's own message: a continuation after a
                    # limit never follows it.
                    self.drop_limit_continuation()
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
            return True
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
            # The user is driving — no replay. But the flushed text is not yet
            # a confirmed turn: usually it sits in the composer (their next
            # Enter sends it), yet a TUI dialog can have swallowed it (the
            # resume-from-summary picker field loss) — so breadcrumb it
            # before dropping. Redundancy in the happy case is the accepted
            # cost of never losing a prompt.
            if self._cold_flush_pending:
                self._persist_undelivered_input(self._cold_flush_pending)
            self._clear_cold_flush()
        self._emit_to_pty(data)
        return True

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
        self._note_escape()
        logger.info("interactive %s: dashboard abort sent ESC", self.session_id[:8])
        return True

    # -- a turn the engine dropped ---------------------------------------------
    def _note_escape(self) -> None:
        """An Escape reached the engine: look again in ``_DROPPED_TURN_S``
        whether a turn went on. The turn may not be open yet: the transcript
        journals the person's line a moment after the engine took it (T1,
        2026-11-11: the line was read 0.2 s after the Escape)."""
        self._escape_at = time.monotonic()
        self._dropped_turn_looks = 0
        if self._dropped_turn_check is not None:
            self._dropped_turn_check.cancel()
        self._dropped_turn_check = self._loop.call_later(_DROPPED_TURN_S, self._check_dropped_turn)

    def _check_dropped_turn(self) -> None:
        """Claude Code stops a turn on an Escape that comes before the
        model's first word without a line in its transcript (a later one
        writes its interrupt line, which closes the turn), and puts the text
        back in its input box. So a turn still open after the Escape, with no
        hook of the session since and no terminal output for
        ``_DROPPED_TURN_QUIET_S`` (a request in flight keeps the spinner
        moving), has ended: close it once, and hold the platform's own
        prompts while the input box holds that text (the composer flag, which
        the person's next Enter or Ctrl-C clears). Nothing is typed again: an
        Escape is someone's choice."""
        self._dropped_turn_check = None
        if not self.alive or self._closing or self._closed:
            return
        if not self._turn_open:
            # Not opened yet, or no turn at all (an Escape in an idle input
            # box): look a few times more, then let it be.
            self._dropped_turn_looks += 1
            if self._dropped_turn_looks < _DROPPED_TURN_LOOKS:
                self._dropped_turn_check = self._loop.call_later(
                    _DROPPED_TURN_S, self._check_dropped_turn)
            return
        from core.session.session_state import get_hook_activity
        if get_hook_activity(self.session_id) >= self._escape_at:
            return
        if self.quiet_seconds < _DROPPED_TURN_QUIET_S:
            return
        logger.info("interactive %s: the turn ended with no answer after an Escape",
                    self.session_id[:8])
        self._composer_dirty = True
        self._composer_dirty_at = time.monotonic()
        self._set_turn_open(False)
        self._note_dropped_turn()

    def _note_dropped_turn(self) -> None:
        """The card of a turn the engine dropped, written on the chat's lane
        and sent to its viewers: the person sees why it ended with no answer
        and where their text went (``SystemEvent``'s words)."""
        if not self.chat_id:
            return
        import functools
        import json

        from core.events import chat_writer
        from core.session.session_state import broadcast_chat_frame
        from storage import database as task_store
        block = {"type": wire.SYSTEM, "subtype": wire.SUBTYPE_TURN_DROPPED}
        chat_writer.submit(self.chat_id, functools.partial(
            task_store.add_chat_message, self.chat_id, "event", "",
            event_type=wire.SYSTEM, event_data=json.dumps(block)), label="turn_dropped")
        broadcast_chat_frame(self.chat_id, {**block, "chat_id": self.chat_id})

    def submit_prompt(self, text: str, *, settle: bool = False, typed: bool = False) -> bool:
        """Deliver a COMPLETE prompt to the TUI and submit it. False when the
        bytes went nowhere (the PTY gone, a local terminal holding the input):
        the caller's note of the typed text comes back then.

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
        cancel its own pending Enter the way real user input would.

        The platform's text is fenced (``start_context.fence_turn``): only
        the platform's start block carries the start's tag. ``typed``: a
        person's own words (their first message), which stay as typed."""
        if not typed:
            from core.session.start_context import fence_turn
            text = fence_turn(text)
        raw = text.encode("utf-8")
        if b"\n" in raw or b"\r" in raw:
            data = b"\x1b[200~" + raw.replace(b"\r\n", b"\n") + b"\x1b[201~\r"
        else:
            data = raw + b"\r"
        if not settle:
            return self.write_input(data)
        if not self.alive or self.otodock_attached:
            return False
        self._note_activity()
        if not self._ready:
            self._input_buffer.append(data)
            return True
        self._emit_to_pty(data, deferred_submit=True)
        return True

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
            cast("RemotePtyProcess", self.pty).write(
                b"\x1b[200~"
                + digest.encode("utf-8").replace(b"\r\n", b"\n")
                + b"\n\n\x1b[201~"
            )
        if deferred_submit:
            body = data.rstrip(b"\r\n")
            if body:
                cast("RemotePtyProcess", self.pty).write(body)
            self._submitted_once = True
            self._arm_deferred_submit()
            return
        if len(data) > 1 and data[-1:] in (b"\r", b"\n"):
            body = data.rstrip(b"\r\n")
            if body:
                cast("RemotePtyProcess", self.pty).write(body)
            # FIRST live line-submit uses the settle path (the TUI may not have
            # ingested the text yet); warm submits after use the fast fixed gap.
            if not self._submitted_once:
                self._arm_deferred_submit()
            else:
                self._loop.call_later(_SUBMIT_ENTER_DELAY_S, self._warm_enter, self._park_seq)
                self._schedule_submit_tails()
            self._submitted_once = True
        else:
            cast("RemotePtyProcess", self.pty).write(data)

    def _warm_enter(self, park_seq: int) -> None:
        """The warm line-submit's Enter, a fixed gap after its text."""
        if self.alive and not self._enter_lands_in_a_dialog(park_seq):
            cast("RemotePtyProcess", self.pty).write(b"\r")

    def _enter_lands_in_a_dialog(self, park_seq: int) -> bool:
        """Whether a park began after the text this Enter submits (the park
        count moved since ``park_seq``): the Enter would answer the new
        dialog. It is held: the text stays in the TUI's input box, the
        composer is marked dirty (so no prompt lands on it) and the unpark
        presses the Enter when nothing was typed since."""
        if not (self._question_parked and self._park_seq != park_seq):
            return False
        now = time.monotonic()
        self._held_enter_at = now
        self._composer_dirty, self._composer_dirty_at = True, now
        self._dirty_at_park = (True, now)
        logger.info("interactive %s: Enter held: a dialog opened after its text",
                    self.session_id[:8])
        return True

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
            for delay in _SUBMIT_TAILS_S
        ]
        # Read by ``submit_settling``: a second past the later tail.
        self._submit_settle_until = time.monotonic() + _SUBMIT_TAILS_S[-1] + 1.0

    def _arm_deferred_submit(self) -> None:
        """Arm the cold-first-prompt Enter.

        Fires the FIRST Enter when the echo goes quiet for _SUBMIT_SETTLE_S (re-armed
        on each output by _fanout_output, so a still-rendering / MCP-warming TUI
        keeps it from firing early), or at _SUBMIT_MAX_S as a backstop. After that,
        :meth:`_fire_submit` then schedules ONE backstop Enter for Windows-remote
        Claude only."""
        self._cancel_deferred_submit()
        self._submit_refires = 0
        self._submit_park_seq = self._park_seq
        self._submit_settle_handle = self._loop.call_later(_SUBMIT_SETTLE_S, self._fire_submit)
        self._submit_max_handle = self._loop.call_later(_SUBMIT_MAX_S, self._fire_submit)

    def _fire_submit(self) -> None:
        self._cancel_deferred_submit()
        if not self.alive or self._enter_lands_in_a_dialog(self._submit_park_seq):
            return
        cast("RemotePtyProcess", self.pty).write(b"\r")
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
        # WINDOWS-remote, and only a TUI that declares it (Claude's Ink): the
        # ConPTY render race can swallow this single post-settle Enter (it lands
        # first-try local + Linux-remote). Fire ONE more Enter
        # _SUBMIT_WIN_BACKSTOP_S later — by then the TUI is idle. 2 Enters
        # total: an extra Enter on an already-submitted/empty composer is a no-op;
        # the narrow accepted risk is a native menu shown within the gap (it'd select
        # an option). Codex / Linux-remote / local: no backstop (single Enter works).
        _row = host_os.of(self.remote_os)
        if (self._submit_refires == 0
                and self._engine_caps.runtime.interactive_submit_backstop
                and _row is not None and _row.conpty):
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
        # The reference is the FIRST Enter of this flush: a retry or the
        # Windows backstop Enter must not move it past a hook stamp that
        # already proved the first Enter was accepted.
        if self._cold_flush_enter_at <= 0:
            self._cold_flush_enter_at = time.monotonic()
        self._cold_flush_watcher = self._loop.call_later(
            _COLD_RETRY_S, self._check_cold_flush,
        )

    def _cold_flush_confirmed_by_hooks(self) -> bool:
        """True when a hook of THIS session fired after the cold Enter — the
        CLI is running a turn, so the prompt was accepted even if the
        transcript has not shown the user line (yet, or at all)."""
        if self._cold_flush_enter_at <= 0:
            return False
        from core.session.session_state import get_hook_activity
        return get_hook_activity(self.session_id) >= self._cold_flush_enter_at

    def _check_cold_flush(self) -> None:
        """The cold Enter fired _COLD_RETRY_S ago. If the transcript never
        confirmed the turn (journaled user line → ``_turn_open``), the flush was
        swallowed by a still-booting composer — replay it. Bounded; stands down
        the moment the turn opens (:meth:`_set_turn_open`) or the user starts
        driving (``write_input`` clears the pending flush on real input).

        A second confirmation source: hook activity. A PreToolUse / Stop /
        SubagentStop hook stamped after the Enter proves the turn is running,
        so the flush is done even when no transcript line has arrived — a
        late satellite forward, or no transcript at all (a CLI that inherited
        ``CLAUDE_CODE_CHILD_SESSION`` journals nothing). Re-emitting there
        pasted the prompt into the open question dialog and answered it with
        the Enter, then left a false undelivered-input breadcrumb (T1,
        2026-09-10)."""
        self._cold_flush_watcher = None
        if (not self._cold_flush_pending or self._turn_open
                or not self.alive or self._closing or self._closed):
            return
        if self._cold_flush_confirmed_by_hooks():
            logger.info(
                "interactive %s: cold first prompt confirmed by hook activity "
                "— retry stands down", self.session_id[:8],
            )
            self._clear_cold_flush()
            return
        if self._cold_flush_attempts >= _COLD_RETRY_MAX:
            logger.warning(
                "interactive %s: cold first prompt unconfirmed after %d "
                "attempt(s) — giving up (the user must resend)",
                self.session_id[:8], self._cold_flush_attempts + 1,
            )
            # Definitive loss (no turn ever opened with this text) — leave
            # the recovery breadcrumb before dropping the bytes.
            self._persist_undelivered_input(self._cold_flush_pending)
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
        self._cold_flush_enter_at = 0.0
        if self._cold_flush_watcher is not None:
            self._cold_flush_watcher.cancel()
            self._cold_flush_watcher = None

    def _persist_undelivered_input(self, raw: bytes) -> None:
        """Persist an abandoned cold flush into the chat as a recovery
        breadcrumb (system event ``undelivered_input``).

        A cold flush that never became a confirmed turn is otherwise GONE —
        interactive persistence is transcript-derived, so text swallowed by a
        TUI dialog (the CLI's resume-from-summary picker ate a 1143-byte
        prompt in the field, 2026-08-18) leaves no DB trace. Called from the
        two places the flush is abandoned: the confirm-and-retry watcher's
        give-up branch, and the user-input cancel (there the text USUALLY
        sits safely in the composer — the breadcrumb may then be redundant,
        which is the acceptable side of the trade). Length-gated so key
        residue never becomes a row; best-effort, off-loop."""
        if not self.chat_id:
            return
        text = _breadcrumb_text(raw)
        if len(text) < _BREADCRUMB_MIN_CHARS:
            return
        from core.session.transcript_tool_events import persist_event
        from storage import database as task_store
        block = {
            "type": wire.SYSTEM,
            "subtype": wire.SUBTYPE_UNDELIVERED_INPUT,
            "message": text,
        }
        async def _write() -> None:
            try:
                await asyncio.to_thread(
                    persist_event, task_store, self.chat_id, block,
                )
                logger.warning(
                    "interactive %s: cold flush abandoned — %d char(s) "
                    "persisted as undelivered-input breadcrumb",
                    self.session_id[:8], len(text),
                )
            except Exception:
                logger.exception(
                    "interactive %s: undelivered-input breadcrumb persist "
                    "failed", self.session_id[:8],
                )
        spawn(_write(), name="write", owner="interactive_session")

    def set_pending_seed(self, digest: str) -> None:
        """Stash a restored-history digest (DB-fallback) to prepend to the cold
        first prompt. Set by the spawn path right after the session is created and
        BEFORE the readiness flush, so :meth:`_emit_to_pty` pastes it ahead of the
        user's first prompt. No-op for an empty digest."""
        if digest:
            self._pending_seed_digest = digest
