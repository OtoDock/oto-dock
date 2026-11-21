"""A terminal session's turns: the signals that open and close one, the transcript
tails that read what it wrote, the title, and the notice when it ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

import psycopg

from core.background import spawn
from core.events import turn_ending
from core.session.interactive_common import (
    _MIDTURN_TAIL_S,
    _TITLE_EARLY_TIMER_S,
    MIN_TURN_S,
    _batch_listeners,
)
from core.session.interactive_output import InteractiveOutputMixin
from core.session.transcript_tailer import leaves_turn_open
from ws import chat_phase, wire_events as wire

if TYPE_CHECKING:
    from core.remote.remote_pty import RemotePtyProcess

logger = logging.getLogger("otodock.interactive")


# How long a limit with no reset known holds a terminal's server prompts:
# then the gate lifts, and an engine still at its limit refuses again, which
# marks again.
_LIMIT_MARK_S = 900.0


def _until_of(ending: turn_ending.TurnEnding) -> float:
    try:
        reset = datetime.fromisoformat(ending.resets_at) if ending.resets_at else None
    except ValueError:
        reset = None
    if reset is not None and reset.tzinfo is None:
        reset = reset.replace(tzinfo=timezone.utc)
    return reset.timestamp() if reset is not None else time.time() + _LIMIT_MARK_S


@dataclass
class LimitMark:
    """A terminal whose turn ended at a vendor's limit: its server prompts wait
    (``_prompt_gates_blocked``) until its account changes, its reset passed or
    a turn ends clean. ``waits_for_account``: the turn's continuation was
    refused only because no account served it (``pool_limits.after_limit``),
    so a re-home continues it (``pool_limits.account_freed``)."""
    ending: turn_ending.TurnEnding
    until: float
    waits_for_account: bool = False

    @classmethod
    def of(cls, ending: turn_ending.TurnEnding) -> LimitMark:
        return cls(ending, _until_of(ending))

    def renew(self, ending: turn_ending.TurnEnding, *, waits_for_account: bool) -> None:
        """The ending as the pool completed it (its reset from the reading)."""
        self.ending, self.until = ending, _until_of(ending)
        self.waits_for_account = waits_for_account

    def holds(self) -> bool:
        return time.time() < self.until


async def write_turn_ended_card(chat_id: str, ending: turn_ending.TurnEnding, *, label: str) -> bool:
    """A terminal chat's card for a turn that ended other than by its result
    (the pump's own block for ``ending``), as one row of the chat and a frame
    to its open tabs: the limit's (``TerminalLimitSide.show_card``) and the
    memory guard's (``interactive_session._note_memory_end``). The card is a
    courtesy: a row that fails is a warning, and False tells the caller no
    card was written."""
    from core.events import chat_writer
    from core.events.stream_pump import turn_ended_block
    from core.session.session_state import broadcast_chat_frame
    from storage import database as task_store
    block = turn_ended_block(ending)
    try:
        await chat_writer.submit(chat_id, functools.partial(
            task_store.add_chat_message, chat_id, "event", "",
            event_type=wire.SYSTEM, event_data=json.dumps(block)), label=label)
    except Exception:  # noqa: BLE001  # the card is a courtesy: what follows the ending goes on
        logger.warning("interactive chat %s: the %s card was not written", chat_id[:8], label, exc_info=True)
        return False
    broadcast_chat_frame(chat_id, {**block, "chat_id": chat_id})
    return True


class TerminalLimitSide:
    """A terminal's answers to what follows a limit
    (``pool_limits.follow_ending``), as a pump's fields answer them: no run (a
    task's terminal is a task chat, whose limit never continues: its run ends
    with the card), the terminal's person on their own chat (never one driven
    through the ``otodock`` command), and two differences ``after_limit``
    reads from ``terminal``: the limit mark is only read (its row is a file
    the session can write) and the chat's headless queue is never held."""

    run_id = ""
    cut_by_platform = False
    turn_kind = ""
    driven_by = ""
    limit_round_taken = False
    terminal = True

    def __init__(self, isess: Any, *, closes: bool = True) -> None:
        from core.session import session_kind
        self.isess = isess
        self.chat_id: str = isess.chat_id or ""
        self.session_id: str = isess.session_id
        task = session_kind.is_task_chat_id(self.chat_id) or isess._a_tasks_terminal()
        self.source_type = (session_kind.TASK if task else session_kind.DASHBOARD).source_type
        # No person waits on a turn the person typed after (``closes`` false)
        # or one driven through the ``otodock`` command: nothing continues.
        attached = getattr(isess, "otodock_attached", False)
        self.wake_person = "" if attached or not closes else (isess.user_sub or "")
        self.driven_for: str = isess.user_sub or ""
        self.last_ending: turn_ending.TurnEnding | None = isess.last_ending
        self.waits_for_account = False

    def close_turn(self) -> None:
        """Closed already: the fold that read the limit's row closed it."""

    async def show_card(self, ending: turn_ending.TurnEnding, frame: dict) -> None:
        """The card through the memory guard's own writer
        (``write_turn_ended_card``), then a nudge to refetch the chat's rows
        (a terminal's page reads its cards from them). A card that was not
        written nudges nothing: the rest and the hold stand."""
        from services.notifications import notification_manager
        self.isess.last_ending = ending
        if not self.chat_id:
            return
        if await write_turn_ended_card(self.chat_id, ending, label="limit_card"):
            notification_manager.broadcast_chat_rows(self.isess._chat_owner(), self.chat_id,
                                                     agent=self.isess.agent_name)


class InteractiveTurnMixin(InteractiveOutputMixin):
    # The session state this mixin assigns, declared where it assigns it: the
    # type checker reads an attribute a mixin assigns from the mixin first.
    # Annotations only, no class attributes.
    _chat_owner_sub: Any
    _composer_dirty: Any
    _dirty_at_park: Any
    _held_enter_at: Any
    _midturn_tail_handle: Any
    _midturn_tail_task: Any
    _native_park_pinged: Any
    _park_seq: Any
    _parked_at: Any
    _question_parked: Any
    _resume_tail_handle: Any
    _tail_handle: Any
    _title_armed: Any
    _title_chars: Any
    _title_timer: Any
    _title_timer_spent: Any
    _title_tools: Any
    _turn_complete_fired: Any
    _turn_end_waiters: Any
    _turn_open: Any
    _turn_token: str
    _limit_menu: Any
    _served_at: Any
    last_ending: Any
    limit_mark: Any

    def _apply_turn_signal(self, last_signal: str | None,
                           question_pending: bool = False,
                           signal_at: float | None = None) -> None:
        """Fold a tailer batch's ``last_signal`` into the turn-open state.
        ``question_pending`` tracks the question-parked flag alongside: set on
        a question fold, cleared by the next turn-relevant signal (reopen or
        real end_turn); signal-less batches leave it untouched. A batch with
        a signal is activity: the CLI journaled turn work, which covers a turn
        it opened with no input (a background subagent's result, a self-wake).

        ``signal_at`` is the journal time of the line behind the signal: while
        a hook park holds the turn, a reopen journaled before the park (an
        earlier call's lines, read before the park and folded after it) is
        dropped, so the composer hold stays on and a send cannot land in the
        dialog. A line from after the park (the answer) folds as always."""
        if (signal_at is not None and not question_pending
                and leaves_turn_open(last_signal) and self._question_parked
                and self._parked_at is not None and signal_at < self._parked_at):
            return
        if last_signal is not None or question_pending:
            self._note_activity(io=False)
        if question_pending:
            self._set_parked(True)
        elif last_signal is not None:
            self._set_parked(False)
            self._parked_at = None
        if leaves_turn_open(last_signal):
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

    def _set_parked(self, parked: bool) -> None:
        """The question park, on and off, for every site. A park counts
        itself (an Enter armed before it is held, ``_enter_lands_in_a_dialog``)
        and keeps the composer flag it found (the dialog's own Enter puts it
        back, ``write_input``). The unpark presses an Enter held for its text
        when nothing was typed since, and drains a message held for the
        dialog at once instead of at the backstop."""
        if parked == self._question_parked:
            return
        self._question_parked = parked
        if parked:
            self._park_seq += 1
            self._dirty_at_park = (self._composer_dirty, self._composer_dirty_at)
            return
        held, self._held_enter_at = self._held_enter_at, None
        if (held is not None and self.alive and not self.otodock_attached
                and self._composer_dirty and self._composer_dirty_at == held):
            cast("RemotePtyProcess", self.pty).write(b"\r")
            self._composer_dirty = False
            self._schedule_submit_tails()
            logger.info("interactive %s: pressed the Enter held for a dialog",
                        self.session_id[:8])
        if self._prompt_queue and not (self._closing or self._closed):
            spawn(self._try_drain_prompt_queue(),
                  name="try_drain_prompt_queue", owner="interactive_session")

    def park_on_native_dialog(self, tool_name: str, *, sent_at: float | None = None) -> None:
        """The permission hook saw the CLI open a dialog that blocks the turn
        on the human — the AskUserQuestion cards (hook ``allow``), the
        ExitPlanMode approval (hook ``defer``) or Codex's request_user_input
        picker. Park the turn NOW: close it (live dot / Stop button clear,
        ``last_response_at`` stamped), hold composer sends and injections
        (``_question_parked``) and fire the "needs your input" ping. The
        transcript fold (``question_pending``) does the same once the CLI
        journals the ``tool_use`` line — but Claude writes that line late in
        some runs (T1, 2.1.263, 2026-09-10: two of three plan approvals
        journaled while the dialog was open, one only with the answer; the
        operator's live question journaled NOTHING of the reply until the
        answer), and then the chat sat live for the whole dialog. The hook
        call is deterministic and precedes the dialog, so it is the primary
        trigger; the fold stays as the fallback and ``_native_park_pinged``
        keeps it from pinging the same dialog twice. The chat rows stay the
        tailers' job: while the CLI holds its reply the history shows the
        prompt only, and the whole turn lands in order with the answer (a
        hook-written card was tried and dropped — it could not be answered
        from the history and the late-arriving text rendered below it).
        ``sent_at`` is the gate's ``hook_sent_at`` (the clock of the host the
        CLI runs on), kept so a tail batch read before the park cannot reopen
        the turn (``_apply_turn_signal``). Loop thread (called from the hook
        handler)."""
        if self._closed or not self.chat_id:
            return
        logger.info("interactive %s: parked on the native %s dialog",
                    self.session_id[:8], tool_name)
        self._note_activity(io=False)
        self._set_parked(True)
        self._parked_at = sent_at
        if self._turn_open:
            self._set_turn_open(False)
        else:
            # The transcript never opened this turn (late user line): the
            # close-side effects must still run so the dot clears.
            self._turn_end_effects()
        # The flag dedupes the transcript fold, never a hook park: a dialog
        # chained on the previous one (a follow-up question, a plan approval
        # after a question) parks before any tail consumed the flag, and
        # each of them is a fresh wait on the human.
        self._native_park_pinged = False
        self._maybe_fire_turn_complete("", persisted=1, question=True)
        self._native_park_pinged = True

    def _chat_owner(self) -> str:
        """The chat ROW's owner sub (synthetic ``agent::<slug>`` for shared-only
        agents) — the identity the chat_status fan-out keys on. Seeded at spawn;
        this runs on the loop, so the read is only for a session with no seed
        (one built without a chat row, never a spawn's)."""
        if self._chat_owner_sub is None:
            try:
                from storage import database as task_store
                row = task_store.get_chat(self.chat_id) or {}
                self._chat_owner_sub = row.get("user_sub") or self.user_sub or ""
            except (ImportError, psycopg.Error):
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
        if was and not is_open:
            # No call of the ended turn waits on the person any more.
            from core.session.session_state import clear_terminal_asks
            clear_terminal_asks(self.session_id)
        if is_open != was:
            self._note_activity(io=False)
            self._note_turn_row(is_open)
        if is_open:
            self._set_parked(False)  # any open unparks (answer/inject)
            self._native_park_pinged = False
            self._clear_cold_flush()  # prompt confirmed — retry stands down
            if not was:
                # A new turn: the last one's ending is not this one's, and a
                # limit menu that was open has been answered.
                self.last_ending = None
                self._limit_menu = False
        if is_open == was or not self.chat_id:
            return
        if is_open:
            try:
                from services.notifications import notification_manager
                notification_manager.broadcast_chat_status(
                    self._chat_owner(), self.chat_id, chat_phase.STREAMING,
                    agent=self.agent_name,
                )
            except Exception:
                logger.exception(
                    "interactive %s: turn-status broadcast failed", self.session_id[:8]
                )
            # Early-title latency cap: one-shot timer armed at the FIRST open
            # transition of an untitled chat (see _TITLE_EARLY_TIMER_S).
            if self._title_armed and not self._title_timer_spent:
                self._title_timer_spent = True
                self._title_timer = self._loop.call_later(
                    _TITLE_EARLY_TIMER_S, self._fire_title_timer)
            # Mid-turn tail cycle (local): rows land as the turn produces them.
            if not self.is_remote and self._midturn_tail_handle is None:
                self._midturn_tail_handle = self._loop.call_later(
                    _MIDTURN_TAIL_S, self._run_midturn_tail)
        else:
            if self._midturn_tail_handle is not None:
                self._midturn_tail_handle.cancel()
                self._midturn_tail_handle = None
            self._turn_end_effects()

    def adopt_turn_token(self, token: str) -> None:
        """A terminal taken back from its machine with the chat's open-turn
        row: the row's token is this session's, and the turn reads open, with
        no transition minted, so the one transition left is the close that
        ends that row."""
        self._turn_token = token
        self._turn_open = True

    def _note_turn_row(self, opened: bool) -> None:
        """A chat's terminal turn on its open-turn row
        (``storage/chat/open_turns.py``), as a pump writes its own: a turn
        the platform's stop cuts leaves the row, and the boot continues the
        chat by warming the terminal again. Not for a task's terminal (its
        run is what the boot reopens)."""
        from core.events import chat_writer
        from core.session import session_kind
        from storage.chat import open_turns
        if not self.chat_id or session_kind.is_task_chat_id(self.chat_id):
            return
        if opened:
            if not self._runs_for_its_chat():
                return
            from core.events.stream_pump import _open_turn_row
            from services.scheduler.cut_turns import KIND_TERMINAL
            self._turn_token = uuid.uuid4().hex
            chat_writer.submit(self.chat_id, functools.partial(
                _open_turn_row, self.chat_id, self._turn_token, self.session_id,
                KIND_TERMINAL, "", ""), label="turn_open")
        elif getattr(self, "_turn_token", ""):
            token, self._turn_token = self._turn_token, ""
            chat_writer.submit(self.chat_id, functools.partial(
                open_turns.end_turn, self.chat_id, token), label="turn_close")

    def _turn_end_effects(self) -> None:
        """A turn genuinely ended on this chat: stamp ``last_response_at`` and
        broadcast the ``ready`` live-dot signal. Runs on the open→closed
        transition AND on an end_turn signal with no prior open (whole turn in
        one tailer batch) — both are real turn ends. Idempotent."""
        from core.session.session_state import clear_terminal_asks
        clear_terminal_asks(self.session_id)
        self._kick_prompt_queue_post_turn()
        # The platform's turn_end, as an OBSERVATION: on a terminal the CLI
        # drives the turn and the Stop hook is the one point that can hold it
        # open, so the verdict is computed there (HOOKS.md); this call only
        # records the end for the session's bookkeeping.
        try:
            from core.session import session_events
            spawn(session_events.turn_end(
                self.session_id, source="transcript",
                engine=self.transcript_kind, driven_by="cli",
            ), name="turn_end", owner="interactive_session")
        except ImportError:
            logger.debug("interactive %s: turn_end observation failed",
                         self.session_id[:8], exc_info=True)
        if not self.chat_id:
            return
        try:
            from datetime import datetime, timezone

            from services.notifications import notification_manager
            from storage.pg import db_executor

            _chat_id = self.chat_id
            _owner = self._chat_owner()
            _mark_read = bool(self.otodock_attached)
            _now_iso = datetime.now(timezone.utc).isoformat()
            _sid = self.session_id[:8]

            def _stamp_job() -> None:
                # ONE ordered executor job: ``last_response_at`` first, then
                # the read mark — two separate jobs could land reversed and
                # leave an otodock-attached chat showing unread. Per-turn DB
                # writes never run on the loop thread (storage/pg.py rule).
                from storage import database as task_store
                task_store.update_chat(_chat_id, last_response_at=_now_iso)
                if _mark_read:
                    # The response just rendered on a live otodock terminal —
                    # that IS the read (the dashboard's visible-tab rule,
                    # terminal edition). Without this the fixed unread dot
                    # lingers until someone opens the chat in the dashboard.
                    # Detached-at-turn-end chats keep the dot: nobody saw it.
                    task_store.mark_chat_read(_chat_id, _owner)

            def _stamp_done(fut) -> None:
                try:
                    fut.result()
                except psycopg.Error:
                    logger.debug("interactive %s: turn-end stamp failed", _sid,
                                 exc_info=True)

            # Loop closing → the stamp is best-effort.
            with contextlib.suppress(RuntimeError):
                self._loop.run_in_executor(db_executor(), _stamp_job).add_done_callback(
                    _stamp_done,
                )
            notification_manager.broadcast_chat_status(
                _owner, _chat_id, chat_phase.READY, agent=self.agent_name,
            )
            if _mark_read:
                with contextlib.suppress(Exception):
                    notification_manager.broadcast_chat_read(
                        _owner, _chat_id, agent=self.agent_name,
                    )
        except Exception:
            logger.exception(
                "interactive %s: turn-status broadcast failed", self.session_id[:8]
            )

    def _tailer(self):
        """The transcript-persistence module for this session's engine
        (``ExecutionLayer.transcript_tailer``). Both engines' modules expose
        the same ``resolve_and_tail(session_id, chat_id)`` + ``forget``
        surface, so the call sites are engine-agnostic."""
        from core.session.session_manager import get_layer_by_path
        return get_layer_by_path(self.execution_path).transcript_tailer()

    def _run_post_output_tail(self) -> None:
        """Output settled (turn / long pause likely ended) → tail the transcript
        into chat_messages + backfill the title, off the event loop. Idempotent
        (the tailer's line cursor skips already-seen lines). Also fires the
        interactive-task completion callback when the tailer reports turn-end."""
        self._tail_handle = None
        if self._closed or not self.chat_id:
            return
        spawn(self._tail_and_maybe_complete(),
              name="tail_and_maybe_complete", owner="interactive_session")

    def _run_resume_tail(self) -> None:
        """Resume-check fired: read the transcript now. If the CLI resumed on
        its own, the batch's ``user``/``tool_use`` signal reopens the turn
        (live dot + Stop button back). Further output while STILL closed
        re-arms via ``_fanout_output``; an open turn stops the cycle."""
        self._resume_tail_handle = None
        if self._closed or not self.chat_id or self._turn_open:
            return
        spawn(self._tail_and_maybe_complete(),
              name="tail_and_maybe_complete", owner="interactive_session")

    def _run_midturn_tail(self) -> None:
        """Periodic mid-turn tail (local): persist the open turn's rows as they
        land instead of at turn end. Re-arms itself while the turn stays open;
        the close transition (or ``close()``) cancels the cycle. A beat is
        skipped while the previous tail is still off-thread — the tailer's
        line cursor plus row claim-keys make overlapping reads harmless, but
        there is no point stacking them."""
        self._midturn_tail_handle = None
        if self._closed or not self.chat_id or not self._turn_open:
            return
        if self._midturn_tail_task is None or self._midturn_tail_task.done():
            self._midturn_tail_task = self._loop.create_task(
                self._tail_and_maybe_complete())
        self._midturn_tail_handle = self._loop.call_later(
            _MIDTURN_TAIL_S, self._run_midturn_tail)

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
        self._fold_tail_result(result)

    def _fold_tail_result(self, result: dict, *, effects: bool = True) -> None:
        """One tail batch into the session: the turn-signal fold, the
        per-batch effects, then a turn end's completion (gated in
        :meth:`_maybe_fire_turn_complete`). A batch whose engine reported a
        turn ending short (``ending``: a limit, an API error) is noted before
        its signal folds, since the close may drain the queue and the gate
        reads the mark; what follows it goes on off the fold. Every tail path
        folds here: the local tail, a machine's forwarded lines, the
        pre-inject tail and the 60 s sweep, which is a backstop for the turn's
        end only (``effects=False``: no page nudge, no batch listener)."""
        last_signal = result.get("last_signal")
        ending = turn_ending.from_dict(result.get("ending"))
        # The row is the batch's last turn line, or a person typed after it.
        closes = ending is not None and last_signal == "end_turn"
        if ending is not None:
            self._note_ending(ending, closes=closes, menu=bool(result.get("limit_menu")))
        self._apply_turn_signal(last_signal,
                                question_pending=bool(result.get("question_pending")),
                                signal_at=result.get("last_signal_at"))
        if effects:
            self._post_batch_effects(result)
            self._note_model_chosen(result.get("model_chosen") or "")
        if ending is not None:
            self._after_ending(ending, closes=closes)
            return
        if last_signal == "end_turn":
            self.clear_limit_mark("a turn ended clean")
        if result.get("turn_complete") or result.get("question_pending"):
            # The engine answered: the account serves this terminal again,
            # whatever rest began before. An interrupt's marker is no answer.
            self._served_at = time.time()
            self._maybe_fire_turn_complete(
                result.get("last_message", "") or "", result.get("persisted", 0),
                question=bool(result.get("question_pending")),
                compacted=bool(result.get("compacted")),
                reopened=leaves_turn_open(last_signal),
            )

    def _note_ending(self, ending: turn_ending.TurnEnding, *, closes: bool, menu: bool) -> None:
        """A batch's typed ending: the session keeps it, and a limit that
        closed the turn marks it (its server prompts wait). ``menu``: the
        engine's own limit menu may be open."""
        self.last_ending = ending
        if ending.reason == turn_ending.LIMIT and closes:
            self.limit_mark = LimitMark.of(ending)
            self._limit_menu = menu
            logger.info("interactive %s: at its usage limit, its server prompts wait",
                        self.session_id[:8])

    def _after_ending(self, ending: turn_ending.TurnEnding, *, closes: bool) -> None:
        """What follows a typed ending, off the fold: a limit's rest, move and
        card (``pool_limits.terminal_limit``); an API error's end of what
        waits on the turn (the error text is in the chat and the terminal);
        a refused sign-in's repair (``pool_tokens.repair_sign_in_soon``, the
        pump's door asks it too)."""
        if ending.sign_in:
            from services.engines import pool_tokens
            pool_tokens.repair_sign_in_soon(self.session_id)
        if ending.reason == turn_ending.LIMIT:
            from services.engines import subscription_pool
            spawn(subscription_pool.terminal_limit(self, ending, closes=closes),
                  name="terminal_limit", owner="interactive_session")
        elif closes:
            self.end_waiters_with(ending)

    def end_waiters_with(self, ending: turn_ending.TurnEnding | None) -> None:
        """A turn that ended short ends what waits on it with its ending: a
        borrowed round's waiters and a task's completion, once, past the gates
        of a clean end (the ending is the run's verdict)."""
        self.last_ending = ending
        self._fire_turn_end_waiters("")
        self._fire_completion("")

    def clear_limit_mark(self, why: str) -> None:
        """The limit's hold on the server prompts ends; the queue is tried again."""
        if self.limit_mark is None:
            return
        self.limit_mark = None
        logger.info("interactive %s: its limit's hold ends (%s)", self.session_id[:8], why)
        if self._prompt_queue:
            self._arm_inject_backstop()

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
        self._maybe_fire_title_early(result)
        # The batch listeners: the duplex feed (an attached full-duplex session
        # speaks new assistant rows as they persist, interactive chats having
        # no pump to tap) needs the turn-close edge even when the batch
        # persisted nothing.
        for listener in _batch_listeners:
            listener(
                self.chat_id or "",
                persisted=int(result.get("persisted", 0) or 0),
                turn_open=self._turn_open,
            )
        try:
            if result.get("persisted", 0) > 0 and self.chat_id:
                from services.notifications import notification_manager
                notification_manager.broadcast_chat_rows(
                    self._chat_owner(), self.chat_id, agent=self.agent_name,
                )
        except Exception:
            logger.exception(
                "interactive %s: chat_rows broadcast failed", self.session_id[:8]
            )
        if result.get("compacted"):
            # The summary took the start's data with it: kept only for what
            # the hook's ``compact`` answer served (``start_context``).
            from core.session import start_context
            start_context.compacted(self.session_id)
            if result.get("turn_complete"):
                # Codex manual /compact: the end_turn fold already ran the
                # turn-end effects; _maybe_fire_turn_complete owns the (one,
                # compacted-worded) ping.
                pass
            elif not self._turn_open:
                self._turn_end_effects()
                self._fire_turn_notification(compacted=True)
            elif (result.get("compact_trigger") == "manual"
                    and not leaves_turn_open(result.get("last_signal"))):
                self._set_turn_open(False)  # runs the turn-end effects
                self._fire_turn_notification(compacted=True)

    # -- early LLM chat-title (interactive twin of the pump's triggers) ------
    def _maybe_fire_title_early(self, result: dict) -> None:
        """Accumulate a tail batch's first-turn counters and fire the title
        upgrade once either threshold is crossed. Remote sessions cross them
        naturally (the satellite forwards lines all through the turn); long
        LOCAL turns are covered by the one-shot timer instead (the starved
        debounce yields no mid-turn batches). Counters only ever grow while
        armed, so a resumed/titled chat costs nothing here."""
        if not self._title_armed or not self.chat_id:
            return
        self._title_chars += int(result.get("assistant_chars") or 0)
        self._title_tools += int(result.get("tool_rows") or 0)
        from services.title_generator import TITLE_CHAR_THRESHOLD, TITLE_TOOL_THRESHOLD
        if (self._title_chars >= TITLE_CHAR_THRESHOLD
                or self._title_tools >= TITLE_TOOL_THRESHOLD):
            self._fire_title()

    def _fire_title(self, assistant_excerpt: str = "") -> None:
        """Disarm + schedule the one-time LLM title upgrade. The local flag
        only stops wasted calls; exactly-once across sessions and fire points
        is the DB claim's job (services/title_generator.py). No excerpt is
        needed from the batch paths — the tailer has already persisted the
        partial first turn, so the service's DB read sees it."""
        if not self._title_armed:
            return
        self._title_armed = False
        if self._title_timer is not None:
            self._title_timer.cancel()
            self._title_timer = None
        try:
            from services import title_generator
            spawn(title_generator.request_chat_title(
                self.chat_id, assistant_excerpt=assistant_excerpt,
            ), name="request_chat_title", owner="interactive_session")
        except Exception:
            logger.exception(
                "interactive %s: title request failed to schedule",
                self.session_id[:8],
            )

    def _fire_title_timer(self) -> None:
        """The early-title latency cap fired (loop-affine callback)."""
        self._title_timer = None
        if self._closing or self._closed or not self._title_armed:
            return
        spawn(self._title_timer_task(),
              name="title_timer_task", owner="interactive_session")

    async def _title_timer_task(self) -> None:
        """Force one full tail pass (so the locally-starved transcript catches
        up AND its turn signals apply — a raw tail here would advance the
        cursor past turn-relevant lines the normal tails then never see), then
        fire with whatever partial first turn is persisted. A batch-threshold
        fire during that tail wins harmlessly (_fire_title no-ops disarmed)."""
        if self._closing or self._closed or not self._title_armed or not self.chat_id:
            return
        await self._tail_and_maybe_complete()
        if not self._closing and not self._closed:
            self._fire_title()

    # -- remote transcript forwarding ----------------------------------------
    def feed_transcript_lines(self, lines: list[str], *, ack_offset: int | None = None) -> None:
        """Persist transcript JSONL lines FORWARDED from a remote satellite.

        The satellite tails this session's transcript file (it controls the
        config dir, so it finds the JSONL by session id regardless of cwd) and
        forwards new lines over the WS (``transcript_lines`` frame); the proxy's
        ``satellite_connection`` routes them here. We hand them to the SAME tailer
        parser used for local sessions (``tail_lines``) so persistence +
        turn-complete are identical — no on-disk mirror needed. Persisting hits
        the DB, so it runs off the loop; the per-session lock keeps arrival
        order. ``ack_offset`` (the frame's ``offset``, 0.5.171) is acked to the
        machine only once the rows are written. A platform that ends before
        then gets the lines again at the machine's next sign-in, once an
        earlier batch of the terminal was acked (the machine rewinds to its
        last ack). The ack is cumulative: a later batch's ack also covers an
        earlier batch that failed to persist."""
        if self._closed or not self.chat_id or not lines:
            return
        spawn(self._persist_forwarded_lines(list(lines), ack_offset),
              name="persist_forwarded_lines", owner="interactive_session")

    async def _persist_forwarded_lines(self, lines: list[str], ack_offset: int | None = None) -> None:
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
        self._fold_tail_result(result)
        if ack_offset is not None and self.target:
            from core.remote.satellite_connection import get_connection_manager
            await get_connection_manager().ack_transcript_lines(
                self.target, self.session_id, ack_offset,
                getattr(self.pty, "open_token", "") or "")

    def _maybe_fire_turn_complete(self, last_message: str, persisted: int = 0,
                                  question: bool = False,
                                  compacted: bool = False,
                                  reopened: bool = False) -> None:
        """A tailer saw a turn-end signal. Three independent reactions, in order:

        1. **LLM chat-title generation** — the FALLBACK fire for first turns
           the early triggers (tail-batch thresholds / the one-shot timer)
           never reached; disarms ``_title_armed``. Task chats title like any
           chat — the service skips no id shape (a meeting pump is disarmed
           by its driver kind) and atomically claims, so exactly once across
           every fire point.
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
        unanswered AskUserQuestion / ExitPlanMode — (2) words the ping as
        needs-input and (3) is skipped (a question is not a task completion;
        hooks deny the tool for autonomous tasks anyway, this is defense in
        depth). A transcript fold for a dialog the hook-driven park
        (:meth:`park_on_native_dialog`) already pinged is a no-op."""
        if question and self._native_park_pinged:
            self._native_park_pinged = False
            return
        # (1) one-time chat-title upgrade — the fallback fire for first turns
        # the early triggers (batch thresholds / timer) never reached.
        if self._title_armed and self.chat_id:
            self._fire_title(assistant_excerpt=last_message)

        # Shared gates for the notification (2) + the task callback (3).
        if (time.monotonic() - self.created_at) < MIN_TURN_S:
            return
        bg_pending = False
        try:
            from core.session.session_state import get_subagent_registry
            bg_pending = get_subagent_registry(self.session_id).has_pending
        except ImportError:
            pass
        if bg_pending:
            return  # a bg subagent is still running → a follow-up turn is coming

        # (2) per-turn end-of-turn user notification (parity with the -p pump).
        # ``compacted``: the "turn" was a codex manual /compact (task_complete
        # rides the same batch as the compacted item) — compacted wording,
        # not "finished".
        if persisted > 0:
            self._fire_turn_notification(question=question, compacted=compacted)

        # (2a) shared-library turn-end reconcile (parity with the -p pump): an
        # interactive turn that deleted/wrote inside a library in its sandbox
        # propagates now, not at the next 5-min sweep. Loop-thread hop — this
        # runs on the tailer thread. Best-effort, never blocks the tailer.
        try:
            from services.knowledge import library_projector
            self._loop.call_soon_threadsafe(
                library_projector.schedule_reconcile_for_agent, self.agent_name)
        except (ImportError, RuntimeError):
            pass

        # (2b) a turn just closed with the shared gates clear — drain any queued
        # server prompts (delegate results waiting for quiescence). Never during
        # close(): its final tail also lands here, but the PTY is already dead —
        # write_input would silently drop the item (close hands the queue back
        # to the delivery ladder instead). Skipped while parked on a question:
        # an injected prompt would land in (and fight) the question dialog.
        if self._prompt_queue and not self._closing and not question:
            spawn(self._try_drain_prompt_queue(),
                  name="try_drain_prompt_queue", owner="interactive_session")

        # (3) one-time interactive-TASK completion callback, and the one-shot
        # turn-end waiters (a borrowed-terminal round's) — the same gates.
        if question:
            return
        # ``reopened``: the batch ends with the next turn already open. A
        # prompt steered in as this turn closed runs as that next turn, so a
        # waiter armed at its injection waits for that turn's end.
        if not reopened:
            self._fire_turn_end_waiters(last_message)
        self._fire_completion(last_message)

    def _fire_turn_end_waiters(self, last_message: str) -> None:
        """The one-shot turn-end waiters (a borrowed-terminal round's)."""
        if self._turn_end_waiters:
            waiters, self._turn_end_waiters = self._turn_end_waiters, []
            for waiter in waiters:
                try:
                    waiter(last_message)
                except Exception:
                    logger.exception(
                        "interactive %s: turn-end waiter failed", self.session_id[:8],
                    )

    def _fire_completion(self, last_message: str) -> None:
        """The interactive task's completion callback, once."""
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
                spawn(res, name="turn_complete", owner="interactive_session")
        except Exception:
            logger.exception(
                "interactive %s: on_turn_complete failed", self.session_id[:8]
            )

    def _runs_for_its_chat(self) -> bool:
        """The terminal is its chat's own, not a task's: no task chat, no
        scheduler's callback (``_a_tasks_terminal``), no session recorded as
        a task's. A task's run has its own end and its own model."""
        from core.session import session_kind
        from core.session.session_state import get_session_client_type
        return (bool(self.chat_id) and not session_kind.is_task_chat_id(self.chat_id)
                and not self._a_tasks_terminal()
                and get_session_client_type(self.session_id) != session_kind.TASK.name)

    def _note_model_chosen(self, model: str) -> None:
        """A model the person picked inside the terminal (a batch's
        ``model_chosen``, which the engine's tailer names when its capability
        says so) becomes the chat's model, so the chat's next start, a
        re-warm, a wake or a continue, runs on it: one job on the chat's lane
        (``_terminal_model_job``), then the menu's own frame to the chat's
        viewers. Never for a task's terminal."""
        if (not model or not self._engine_caps.runtime.interactive_reports_model
                or not self._runs_for_its_chat()):
            return
        from core.events import chat_writer
        chat_id = self.chat_id
        done = chat_writer.submit(
            chat_id, functools.partial(_terminal_model_job, chat_id, model, self.execution_path),
            label="terminal_model")
        done.add_done_callback(functools.partial(_tell_viewers_model, chat_id, model))

    def _a_tasks_terminal(self) -> bool:
        """The scheduler's callback marks a task's terminal, whose run has its
        own end. A wake's re-warm of a chat's terminal sets the callback too,
        and that terminal stays the chat's."""
        return (getattr(self, "on_turn_complete", None) is not None
                and not getattr(self, "chat_terminal_rewarmed", False))

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
        ``agent_name`` are on the session; both calls are non-blocking — the
        first enqueues to in-memory conn queues, the second (with the agent's
        display name, read off the loop) is scheduled as a task. Skipped for
        autonomous task runs (``_a_tasks_terminal``) — a task's completion alert
        is its ``notification_mode`` contract; a re-warmed task chat keeps the
        per-turn signal. A meeting never runs an interactive session.
        Best-effort."""
        if not self.user_sub or not self.chat_id:
            return
        try:
            from services.notifications import notification_manager
            notification_manager.broadcast_chat_status(
                self._chat_owner(), self.chat_id, chat_phase.READY, agent=self.agent_name,
            )
            if self._a_tasks_terminal():
                return
            # Read now: the alert routes by the attachment at the turn's end.
            cli_attached = self.otodock_attached

            async def _notify() -> None:
                # Display name, not slug — the title is what the phone shows.
                # A cold agents cache reads the table: off the loop.
                from storage.pg import run_db
                label = await run_db(notification_manager.agent_label, self.agent_name)
                if compacted:
                    title = f"{label} compacted the conversation"
                    body = "Context compacted. The session is ready."
                else:
                    title, body = notification_manager.turn_end_words(label, asked=question)
                await notification_manager.fire_ephemeral(
                    self.user_sub,
                    title=title,
                    body=body,
                    chat_id=self.chat_id,
                    interactive=True,
                    cli_attached=cli_attached,
                )

            spawn(_notify(), name="fire_ephemeral", owner="interactive_session")
        except Exception:
            logger.exception(
                "interactive %s: turn notification failed", self.session_id[:8]
            )


def _terminal_model_job(chat_id: str, model: str, execution_path: str) -> bool:
    """A terminal's model pick on the chat's lane: the row takes ``model``
    when the chat is a person's own (no delegated worker's, no phone's), it
    still runs this engine, and the agent may run the model there (offered
    enabled on this engine among the agent's, and a local one where the
    agent allows only local models), so no later start refuses the row's
    model. True when the row changed."""
    from core.session import session_kind
    from services.engines import engine_resolution
    from storage import database as task_store
    from storage.agents import agent_store
    row = task_store.get_chat(chat_id) or {}
    if (not row or (row.get("model") or "") == model
            or session_kind.of_chat(row) is not session_kind.DASHBOARD
            or row.get("delegate_role") == "worker"
            or (row.get("execution_path") or execution_path) != execution_path):
        return False
    agent_row = agent_store.get_agent(row.get("agent") or "") or {}
    if (engine_resolution.engine_for_model(agent_row, model, first=execution_path) != execution_path
            or not engine_resolution.model_allowed_on(
                agent_row, execution_path, model, engine_resolution.engine_catalog([execution_path]))):
        logger.info("chat %s: its terminal runs %s, which this agent does not offer on %s; "
                    "the chat keeps %s", chat_id[:8], model, execution_path, row.get("model") or "its default")
        return False
    task_store.update_chat(chat_id, model=model)
    return True


def _tell_viewers_model(chat_id: str, model: str, done: asyncio.Future) -> None:
    """The chat's viewers see the model the terminal now runs in the menu (a
    failed job was logged by the lane)."""
    if done.cancelled() or done.exception() is not None or not done.result():
        return
    from core.session.session_state import broadcast_chat_frame
    broadcast_chat_frame(chat_id, {"type": wire.MODEL_CHANGED, "model": model, "chat_id": chat_id})
