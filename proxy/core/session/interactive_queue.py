"""A terminal session's waiting prompts: the queue, the gates that hold it, its
drain at a turn's end and the inject on a machine.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from core.background import spawn
from core.session.interactive_common import (
    _COMPOSER_DIRTY_TTL_S,
    _INJECT_BACKSTOP_S,
    _INJECT_QUIET_S,
    _SATELLITE_INJECT_TIMEOUT_S,
    MIN_TURN_S,
)
from core.session.interactive_turn import InteractiveTurnMixin
from core.session.transcript_tool_events import note_sent_prompt

if TYPE_CHECKING:
    from core.events import turn_ending
    from core.session.interactive_common import TurnCompleteCb

logger = logging.getLogger("otodock.interactive")

#: The source of the continuation after a usage limit (``chat_reload``). Its
#: prompt is never typed over text waiting in the terminal's input box: the
#: composer's hold does not lapse for it, and a person's own send drops it.
ACCOUNT_LIMIT_SOURCE = "account_limit"
#: Called with the chat id when a person sends their own message into a
#: terminal (``services/remote/chat_reload`` registers: the next limit may
#: continue again).
person_sent_hooks: list[Callable[[str], None]] = []



class InteractiveQueueMixin(InteractiveTurnMixin):
    # The session state this mixin assigns, declared where it assigns it: the
    # type checker reads an attribute a mixin assigns from the mixin first.
    # Annotations only, no class attributes.
    _draining_prompts: Any
    _inject_backstop_handle: Any
    _last_held_reason: Any
    _satellite_inject: Any
    _submitted_once: Any

    # -- server-prompt injection (delegate results …) --------------------------
    @property
    def holds_unsent_text(self) -> bool:
        """Text typed into the terminal's own input box and not sent yet: the
        composer flag the injection gate reads, which a submit, a Ctrl-C or an
        Escape that empties the box clears, here without the gate's lapse, and
        which is also set while the engine holds a dropped turn's text. The
        platform never restarts a terminal over it. A terminal attached through
        the ``otodock`` command holds while attached: its line is not known
        here."""
        return bool(self._composer_dirty) or bool(getattr(self, "otodock_attached", False))

    @property
    def submit_settling(self) -> bool:
        """A prompt was just submitted and the tails that open its turn have
        not all run (``_schedule_submit_tails``): the input box is already
        empty, and the turn the Enter opens is not known yet. The platform
        never closes a terminal in that gap."""
        return time.monotonic() < float(getattr(self, "_submit_settle_until", 0.0))

    def queue_prompt(self, text: str, source: str, **context) -> dict | None:
        """Queue a server-originated prompt for injection at CLI quiescence.

        ``steer=True`` in the context marks the item steer-eligible: with an
        OPEN turn it injects mid-turn instead of waiting for quiescence (see
        ``_prompt_gates_blocked``), on this proxy's PTY and a paired
        machine's alike; with the turn closed, or a local ``otodock``
        terminal attached (the satellite injects at the turn end), the flag
        is inert. ``raw`` (a person's held composer send) is typed as given
        instead of submitted as ``text``. ``on_injected`` in the context is called once the prompt is in
        the terminal (the local paste, or the satellite's ACK). Returns the
        queued item — the handle ``cancel_prompt`` takes — or None when the
        session can't take it (dead/closing) so the delivery ladder falls
        through to its headless rungs. ``context`` is the re-delivery payload
        (chat_id/agent/user_sub/role/hops + the ladder's rung callables) that
        close() hands back if the PTY dies first. Deliberately does NOT count
        as activity — a starved queue must not immortalize an unviewed session
        against the idle reaper (reap → close → handback is the designed
        escape for a composer that never clears)."""
        if not self.alive or self._closing or self._closed:
            return None
        if not context.get("raw"):
            # The platform's text (a delegated result, a wake, a lane's round,
            # a task's prompt): only the platform's start block carries the
            # start's tag. A person's own keys (``raw``) stay as typed.
            from core.session.start_context import fence_turn
            text = fence_turn(text)
        item = {"text": text, "source": source, **context}
        self._prompt_queue.append(item)
        logger.info(
            "interactive %s: queued server prompt [%s] (depth=%d)",
            self.session_id[:8], source, len(self._prompt_queue),
        )
        spawn(self._try_drain_prompt_queue(),
              name="try_drain_prompt_queue", owner="interactive_session")
        return item

    def cancel_prompt(self, item: dict) -> bool:
        """Drop a queued prompt that has not been injected yet (a cancelled
        borrowed-terminal round); False when it already left the queue, or
        when it is the satellite's in-flight injection, which may land anyway
        (dropped all the same: a decline or a lost result never re-sends it)."""
        try:
            self._prompt_queue.remove(item)
        except ValueError:
            return False
        inflight = self._satellite_inject
        return inflight is None or inflight.get("item") is not item

    def drop_limit_continuation(self) -> int:
        """A person sent their own message first: the continuation after a
        limit that waits in the queue is dropped, never typed after it. Its
        caller's outcome hook is told "none" (its row taken back), and the
        ``person_sent_hooks`` hear of the chat. Returns how many were dropped."""
        for hook in person_sent_hooks:
            try:
                hook(self.chat_id or "")
            except Exception:  # noqa: BLE001  # a hook's failure never blocks the person's keys
                logger.debug("interactive %s: a person-sent hook failed", self.session_id[:8], exc_info=True)
        dropped = [item for item in list(self._prompt_queue) if item.get("source") == ACCOUNT_LIMIT_SOURCE]
        for item in dropped:
            if not self.cancel_prompt(item):
                continue
            on_outcome = item.get("on_outcome")
            if on_outcome is not None:
                from core.background import spawn
                from core.session.session_delivery import DeliveryOutcome
                res = on_outcome(DeliveryOutcome("none", chat_id=item.get("chat_id") or "",
                                                 session_id=self.session_id))
                if asyncio.iscoroutine(res):
                    spawn(res, name="limit_continuation_dropped", owner="interactive_queue")
        if dropped:
            logger.info("interactive %s: the person sent first, the continuation after a limit is dropped",
                        self.session_id[:8])
        return len(dropped)

    def add_turn_end_waiter(self, cb: TurnCompleteCb) -> None:
        """Fire ``cb`` once at the next turn end that passes the completion
        gates (min turn time, no background subagent, not a question)."""
        cast("list[TurnCompleteCb]", self._turn_end_waiters).append(cb)

    def remove_turn_end_waiter(self, cb: TurnCompleteCb) -> None:
        if cb in self._turn_end_waiters:
            cast("list[TurnCompleteCb]", self._turn_end_waiters).remove(cb)

    def _note_injected(self, item: dict, *, cancelled: bool = False) -> None:
        """The prompt is in the terminal: note it for the tailer when its row
        exists (``note``: the item's row is its queuer's, the owner this
        session), then tell whoever queued it. An item cancelled while the
        satellite typed it (``cancelled``) gets no hook, its round is gone;
        one whose row that hook would have written gets no note either, so
        the tailer's plain row stays the one record of what the agent was
        told."""
        cb = item.get("on_injected")
        if cancelled and cb is not None:
            return
        if item.get("note") and self.chat_id:
            note_sent_prompt(self.chat_id, item["text"], owner=self.session_id)
        if cancelled or cb is None:
            return
        try:
            cb()
        except Exception:
            logger.exception("interactive: on_injected hook failed [%s]", item.get("source", "?"))

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
            spawn(self._try_drain_prompt_queue(),
                  name="try_drain_prompt_queue", owner="interactive_session")

    def _prompt_gates_blocked(
        self, *, for_satellite: bool = False, steering: bool = False, hold_draft: bool = False,
    ) -> str | None:
        """The first injection gate that blocks right now, or None when clear.
        Cheap, synchronous — callable before AND after the freshness tail.
        ``for_satellite`` skips the composer gate: while otodock-attached the
        dashboard input path is dropped (so ``_composer_dirty`` is stale) and
        the LOCAL terminal's line state is the satellite's to judge.
        ``hold_draft``: the composer's hold does not lapse (the head item is a
        continuation after a limit, which never types over a draft).

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
        if self._held_at_limit():
            return "at_limit"
        if self._turn_open and not steer_live:
            return "turn_open"
        # An unstamped dirty flag (no timestamp yet) holds unconditionally —
        # only a KNOWN-stale draft may expire.
        if (not for_satellite and self._composer_dirty
                and (hold_draft or not self._composer_dirty_at
                     or time.monotonic() - self._composer_dirty_at
                     < _COMPOSER_DIRTY_TTL_S)):
            return "composer_dirty"
        if not steer_live and self.quiet_seconds < _INJECT_QUIET_S:
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
            except ImportError:
                pass
        # A pending hook permission prompt: the TUI is waiting on a dialog —
        # an injected CR would answer it.
        try:
            from core.session.session_state import _session_permission_requests
            if _session_permission_requests.get(self.session_id):
                return "permission_pending"
        except ImportError:
            pass
        # Parked on an AskUserQuestion dialog (turn folded closed): the paste +
        # CR would land in the question picker instead of the composer.
        if self._question_parked:
            return "question_parked"
        return None

    def limit_ending(self) -> turn_ending.TurnEnding | None:
        """The limit this terminal is at now, the one reading the queue's gate,
        the voice door and a borrowed round ask: its own mark while it stands
        (a mark whose reset passed is cleared here), else its account's rest
        for it (``pool_limits.rest_ending_for``), which another session's
        limit or a restart leaves with no mark. A rest that began before the
        engine last answered a turn here holds nothing (``_served_at``: the
        person left the full model, or bought credits). None when nothing
        holds."""
        mark = self.limit_mark
        if mark is not None:
            if mark.holds():
                return mark.ending
            self.clear_limit_mark("its reset passed")
        from services.engines import subscription_pool
        return subscription_pool.rest_ending_for(self.session_id, since=self._served_at)

    def _held_at_limit(self) -> bool:
        """A server prompt at the head waits while the terminal is at a limit
        (``limit_ending``). The engine would refuse it at once and spend it,
        and its own limit menu takes typed keys as answers. A person's held
        send and the limit's own continuation pass."""
        head = self._prompt_queue[0] if self._prompt_queue else None
        if head is None or head.get("raw") or head.get("source") == ACCOUNT_LIMIT_SOURCE:
            return False
        return self.limit_ending() is not None

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
                # must not write. The satellite injection path takes these
                # items instead, and until it does they wait here (a detach
                # flips the flag and this proxy path takes over).
                # Satellite injection is turn-end by design — the steer flag
                # is deliberately inert on this path (recorded follow-up).
                # A continuation after a limit is never handed over: the
                # machine never tells of the person's own Enter.
                if self._prompt_queue[0].get("source") == ACCOUNT_LIMIT_SOURCE:
                    self.drop_limit_continuation()
                    if not self._prompt_queue:
                        return
                await self._try_satellite_inject()
                return
            # Steer eligibility is the HEAD item's — FIFO order is never
            # reordered, so a steer item behind a normal one waits with it.
            # The continuation after a limit is never steered into a turn: a
            # person's own turn open now is theirs.
            hold_draft = self._prompt_queue[0].get("source") == ACCOUNT_LIMIT_SOURCE
            steering = bool(self._prompt_queue[0].get("steer")) and not hold_draft
            blocked = self._prompt_gates_blocked(steering=steering, hold_draft=hold_draft)
            if blocked:
                self._log_held(blocked)
                return
            # Freshness: the local debounce tail can be up to 3s stale — force
            # a tail so _turn_open reflects the transcript NOW (a quiet mid-turn
            # lull, e.g. a native permission dialog waiting for input, must not
            # read as idle). Remote sessions are fresh via the satellite's 0.8s
            # forwarded-lines poll, and have no local file to read.
            if not self.is_remote and self.chat_id:
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
                # The whole fold: the cursor moved past this batch, so a turn
                # end in it is reported here or never.
                self._fold_tail_result(result)
                if not self._prompt_queue:
                    return
                # Re-check everything after the await — state may have moved
                # (the tail can flip _turn_open; output during the await resets
                # the quiet clock; a cancel can change the head, and with it
                # the steer eligibility).
                hold_draft = self._prompt_queue[0].get("source") == ACCOUNT_LIMIT_SOURCE
                steering = bool(self._prompt_queue[0].get("steer")) and not hold_draft
                blocked = self._prompt_gates_blocked(steering=steering, hold_draft=hold_draft)
                if blocked:
                    self._log_held(f"{blocked} post-tail")
                    return
            if not self._prompt_queue:
                return
            item = self._prompt_queue.popleft()
            if item.get("source") == ACCOUNT_LIMIT_SOURCE and self._limit_menu:
                # The engine's limit menu may still be open, and its answers
                # are typed keys: one Escape closes it, the continuation is
                # typed at the next drain.
                self._limit_menu = False
                self._prompt_queue.appendleft(item)
                self.write_input(b"\x1b")
                return
            self._last_held_reason = None
            steered_live = self._turn_open
            raw = item.get("raw")
            if raw:
                # A person's composer send held for a dialog: typed as the
                # page sent it (its bracketed paste, then the warm Enter, in a
                # session whose earlier prompts were only typed keys too).
                self._submitted_once = True
                written = self.write_input(raw)
            else:
                # settle=True: the trailing Enter waits for the paste echo to
                # settle (large delegate results were landing in the composer
                # with the fixed-gap Enter swallowed mid-ingest → never sent).
                written = self.submit_prompt(item["text"], settle=True)
            if not written:
                # The PTY went away, or a local terminal took the input,
                # between the gate and the write: the item waits at the head
                # (the close hands the queue back, the satellite path takes
                # it) and the turn stays as it was.
                self._prompt_queue.appendleft(item)
                return
            # The injected prompt opens a turn NOW (no-op transition for a
            # mid-turn steer — the turn is already open) — the tailer confirms
            # within a poll, but the flag must flip synchronously so a second
            # queued item can't ride the stale-idle window.
            self._set_turn_open(True)
            logger.info(
                "interactive %s: %s server prompt [%s] (%d left)",
                self.session_id[:8],
                "steered into open turn" if steered_live else "injected",
                item.get("source", "?"), len(self._prompt_queue),
            )
            self._note_injected(item)
        finally:
            self._draining_prompts = False
            self._arm_inject_backstop()

    async def _try_satellite_inject(self) -> None:
        """otodock-attached: hand the head item to the satellite's stdin-inject
        path — the satellite owns the local terminal's input-line state; the
        proxy owns the turn-state decision (its gates below run on the same
        forwarded-transcript signals as the proxy path, minus the composer).
        The head item stays queued until the satellite ACKs; a local-terminal
        detach leaves it queued for the proxy path / close-handback."""
        if not self._prompt_queue or not self.is_remote:
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
        now = time.monotonic()
        inflight = self._satellite_inject
        if inflight is not None:
            if now - inflight["sent_at"] < _SATELLITE_INJECT_TIMEOUT_S:
                return  # awaiting the result frame
            if any(q is inflight["item"] for q in self._prompt_queue):
                # Result lost (WS blip) — re-send the SAME id; the
                # satellite's recent-id dedupe re-ACKs without re-injecting.
                inflight["sent_at"] = now
            else:
                inflight = None  # cancelled in flight — never re-sent
        if inflight is None:
            inflight = {"inject_id": uuid.uuid4().hex[:12], "sent_at": now,
                        "item": self._prompt_queue[0]}
            self._satellite_inject = inflight
        inject_id, item = inflight["inject_id"], inflight["item"]
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
        ACK removes exactly the item that was sent (the prompt is in the
        user's terminal) and opens the turn; an item cancelled in flight is
        already gone and nothing else leaves the queue. NACK leaves it queued
        for the backstop / the proxy path (after a detach) / the
        close-handback (session gone)."""
        inflight = self._satellite_inject
        if not inflight or inflight.get("inject_id") != inject_id:
            return  # stale or unknown — a newer attempt owns the slot
        self._satellite_inject = None
        if ok:
            item = inflight["item"]
            queued = False
            for i, q in enumerate(self._prompt_queue):
                if q is item:
                    del self._prompt_queue[i]
                    queued = True
                    break
            self._set_turn_open(True)  # the injected prompt opened a turn
            logger.info(
                "interactive %s: satellite injected server prompt [%s]%s (%d left)",
                self.session_id[:8], item.get("source", "?"),
                "" if queued else " cancelled in flight", len(self._prompt_queue),
            )
            # The satellite typed it either way; a cancelled item's round is
            # gone, and with it the row its hook would have written.
            self._note_injected(item, cancelled=not queued)
        else:
            logger.info(
                "interactive %s: satellite injection declined (%s)",
                self.session_id[:8], reason or "?",
            )
            if reason == "not_attached" and not self.otodock_attached:
                # The local terminal detached while the frame was in flight —
                # the proxy path owns the queue again.
                spawn(self._try_drain_prompt_queue(),
                      name="try_drain_prompt_queue", owner="interactive_session")
        self._arm_inject_backstop()
