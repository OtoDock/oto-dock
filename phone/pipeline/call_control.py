"""The call tools: ``end_call``, ``transfer_call`` and ``ask_initiating_agent``.

Mixin for :class:`pipeline.core.CallPipeline`. The proxy relays an agent's
call tool as a ``call_control`` frame on the per-call socket; the client
hands it here synchronously, whatever its turn. The action is validated and
RECORDED on ``CallState`` and answered at once (the tool's result text); the
pipeline acts at the completion point: the TTS drain after the text produced
before the tool has played (``pipeline/llm.py``), never the engine stream's
end. An action raised between turns is consumed by a task that waits for the
drain. The text markers stay the fallback for models that never call tools.
"""

import asyncio
import contextlib
import logging

from config_manager import transfer_target_for
from telephony.transfer import loggable_label

logger = logging.getLogger("pipeline")

ACTION_END = "end"
ACTION_TRANSFER = "transfer"
ACTION_ASK = "ask"

# The marker regexes' twin for transcripts: what the caller never hears.
END_ACCEPTED = (
    "The line closes as soon as your words have played: say nothing more. "
    "Nothing you write now is heard."
)
TRANSFER_ACCEPTED = (
    "Transferring the caller to {label} as soon as your words have played: "
    "say nothing more. Nothing you write now is heard."
)
ASK_ACCEPTED = (
    "Question relayed to the agent that placed this call. Say that you are "
    "checking (one short sentence) and stop: the answer arrives as your next "
    "message within about 40 seconds."
)
STALE_TURN = "The caller interrupted that reply; the call goes on."
ALREADY_PENDING = "An end of the call is already pending; say nothing more."
ASK_PENDING = "A question is already pending; wait for its answer."
NO_ASK = (
    "No agent is waiting on this call: it was not placed by another agent. "
    "Ask the person on the line instead."
)
NO_TARGETS = "This line has no transfer targets configured: tell the caller you cannot transfer them."
UNKNOWN_TARGET = "No transfer target named {wanted}; this line has: {labels}."
TRANSFER_UNAVAILABLE = "Transfers are not available on this line: {reason}."

#: How long a transfer's outcome is awaited once its request went out: the
#: vendor's own request timeout is 15 s, a normal answer takes well under a
#: second. The media leg closes under the move, so the wait happens at the
#: call's end as often as at the drain.
TRANSFER_SETTLE_S = 5.0


class CallControlMixin:
    """The call tools' daemon half."""

    def _install_call_control(self) -> None:
        """Receive the call tools' frames from the live LLM client, and the
        loss of the proxy's socket."""
        if self.llm is not None and hasattr(self.llm, "on_call_control"):
            self.llm.on_call_control = self._on_call_control
        if self.llm is not None and hasattr(self.llm, "on_proxy_lost"):
            self.llm.on_proxy_lost = self._on_proxy_lost

    def _can_transfer(self) -> bool:
        """Whether this line can hand the caller over at all (the handle
        exists and names a reachable channel or call)."""
        handle = getattr(self, "_transfer", None)
        return handle is not None and not handle.unavailable_reason()

    def _answer_call_control(self, request_id: str, ok: bool, message: str) -> None:
        sender = getattr(self.llm, "send_call_control_result", None)
        if sender is None:
            return
        asyncio.get_running_loop().create_task(sender(request_id, ok, message))

    def _on_call_control(self, frame: dict) -> None:
        """Validate and record one action; answer at once."""
        data = frame.get("data") or {}
        request_id = str(data.get("request_id") or "")
        action = str(data.get("action") or "")
        turn = frame.get("turn")
        current = getattr(self.llm, "current_turn", None)
        if turn is not None and current is not None and turn != current:
            logger.info(
                f"[{self.conn.peer_addr}] call control {action} for turn {turn} "
                f"refused (current {current})"
            )
            self._answer_call_control(request_id, False, STALE_TURN)
            return
        state = self.state
        if action == ACTION_END:
            if state._control_action in (ACTION_END, ACTION_TRANSFER):
                self._answer_call_control(request_id, False, ALREADY_PENDING)
                return
            self._record_call_control(action, turn, {"reason": str(data.get("reason") or "")})
            logger.info(f"[{self.conn.peer_addr}] call control: end requested (turn {turn})")
            self._answer_call_control(request_id, True, END_ACCEPTED)
        elif action == ACTION_TRANSFER:
            if state._control_action in (ACTION_END, ACTION_TRANSFER):
                self._answer_call_control(request_id, False, ALREADY_PENDING)
                return
            wanted = str(data.get("target") or "").strip()
            targets = list(getattr(self.route, "transfer_targets", None) or [])
            if not targets:
                self._answer_call_control(request_id, False, NO_TARGETS)
                return
            target = transfer_target_for(targets, wanted)
            if target is None:
                labels = ", ".join(str(t.get("label") or t.get("number") or "") for t in targets)
                self._answer_call_control(
                    request_id, False, UNKNOWN_TARGET.format(wanted=wanted, labels=labels))
                return
            handle = getattr(self, "_transfer", None)
            reason = handle.unavailable_reason() if handle is not None else "the PBX connection has no channel for this call"
            if reason:
                self._answer_call_control(
                    request_id, False, TRANSFER_UNAVAILABLE.format(reason=reason))
                return
            state._transfer_target = dict(target)
            self._record_call_control(action, turn, {"target": wanted})
            logger.info(
                f"[{self.conn.peer_addr}] call control: transfer to "
                f"{loggable_label(str(target.get('label') or ''))} requested (turn {turn})"
            )
            self._answer_call_control(
                request_id, True,
                TRANSFER_ACCEPTED.format(label=target.get("label") or target.get("number")))
        elif action == ACTION_ASK:
            if not self._ask_back_available():
                self._answer_call_control(request_id, False, NO_ASK)
                return
            if state._control_action == ACTION_ASK:
                self._answer_call_control(request_id, False, ASK_PENDING)
                return
            if state._control_action in (ACTION_END, ACTION_TRANSFER):
                self._answer_call_control(request_id, False, ALREADY_PENDING)
                return
            self._record_call_control(action, turn, {"question": str(data.get("question") or "")})
            logger.info(f"[{self.conn.peer_addr}] call control: question relayed (turn {turn})")
            self._answer_call_control(request_id, True, ASK_ACCEPTED)
        else:
            self._answer_call_control(request_id, False, f"Unknown call action {action!r}.")
            return
        # Between turns (the frame beat the stream's end, or the turn was
        # cancelled): the completion point is the next TTS drain.
        if action in (ACTION_END, ACTION_TRANSFER) and not self._turn_busy():
            self._schedule_control_between_turns()

    def _ask_back_available(self) -> bool:
        """An outbound call this daemon placed for another agent: someone
        waits for questions. Not inbound, not a call inside the caller's own
        chat."""
        return bool(self._is_outbound and self._call_manager
                    and not getattr(self, "_in_session", False))

    def _record_call_control(self, action: str, turn, args: dict) -> None:
        self.state._control_action = action
        self.state._control_turn = turn
        self.state._control_args = dict(args)
        self.state._control_cut = False
        self.state._control_boundary_seen = False

    def _clear_call_control(self, reason: str, *, keep_transfer: bool = False) -> None:
        """Drop a pending action (a barge-in, a farewell the caller resumed,
        a turn that ended without completing). A transfer survives a
        barge-in: the caller's "thanks" over the hand-over sentence must
        not strand them with the agent."""
        state = self.state
        if not state._control_action:
            return
        if keep_transfer and state._control_action == ACTION_TRANSFER:
            return
        logger.info(f"[{self.conn.peer_addr}] call control: {state._control_action} dropped ({reason})")
        state._control_action = ""
        state._control_turn = None
        state._control_args = {}
        state._control_cut = False
        state._control_boundary_seen = False
        state._transfer_target = None

    def _control_pending_end(self) -> bool:
        return self.state._control_action in (ACTION_END, ACTION_TRANSFER)

    def _schedule_control_between_turns(self) -> None:
        task = self.state._control_between_turns
        if task is not None and not task.done():
            return
        self.state._control_between_turns = asyncio.create_task(
            self._complete_control_between_turns())

    async def _complete_control_between_turns(self) -> None:
        """An end or transfer raised with no engine turn in flight: let any
        playback drain, then complete."""
        try:
            while self.state._running:
                task = self.state._tts_task
                if task is None or task.done():
                    break
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait({task}, timeout=0.5)
            if not self.state._running or not self._control_pending_end():
                return
            if self._turn_busy():
                return  # a turn started meanwhile: its completion point applies
            self.state._call_complete = True
            await self._execute_pending_end()
        except asyncio.CancelledError:
            return

    async def _execute_pending_end(self) -> bool:
        """The completion point: a transfer when one is pending, else the
        hangup after the 0.5 s pause. Returns True when the call ended."""
        state = self.state
        target = state._transfer_target
        if target is not None:
            return await self._do_transfer(target)
        await asyncio.sleep(0.5)
        self.conn.send_hangup()
        state._running = False
        logger.info(f"[{self.conn.peer_addr}] Call complete — hanging up")
        return True

    async def _do_transfer(self, target: dict) -> bool:
        """Move the caller to ``target``. The handle's request runs as its
        own task and ``_settle_transfer`` records the outcome: here when the
        answer comes while the line is still open, else at the call's end
        (the PBX or the vendor closes the media leg the moment the caller
        moves, and the cleanup settles before it cancels anything). On a
        failure the call resumes with the failure in the agent's next
        dispatch. Returns True when the call ended."""
        state = self.state
        handle = getattr(self, "_transfer", None)
        await asyncio.sleep(0.5)
        state._transfer_label = str(target.get("label") or target.get("number") or "")
        state._transfer_previous_outcome = state.call_outcome
        state._transfer_settled = False
        # The media leg closes the moment the caller moves: the outcome is
        # set first so the teardown report reads right.
        state.call_outcome = "transferred"
        if handle is None:
            self._transfer_failed("no transfer handle")
            with contextlib.suppress(Exception):
                await self._play_phrase("transfer_failed")
            return False
        # Twilio speaks a goodbye when the <Dial> ends without the colleague
        # (nobody answered, or the carrier refused): the agent is gone by
        # then, so never "I'm still here", in the call's language.
        state._transfer_task = asyncio.create_task(handle.execute(
            str(target.get("number") or ""),
            unanswered_text=self._phrase("transfer_unanswered"),
            unreachable_text=self._phrase("transfer_unreachable"),
            language=self._locked_language,
            label=state._transfer_label,
        ))
        return await self._settle_transfer()

    async def _settle_transfer(self) -> bool:
        """Record a pending transfer's outcome once (``_transfer_settled``):
        the request's task is awaited up to ``TRANSFER_SETTLE_S``, shielded,
        so a caller cancelled meanwhile (the utterance task under the
        cleanup) leaves the next caller to finish the bookkeeping. A request
        still unanswered at the bound counts as the move when the line
        closed under it (the vendor applied it, the answer was lost), as a
        failure otherwise. Returns True when the call ended."""
        state = self.state
        task = state._transfer_task
        if task is None:
            return False
        if state._transfer_settled:
            return bool(state._transferred_to)
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=TRANSFER_SETTLE_S)
        if state._transfer_settled:
            return bool(state._transferred_to)
        state._transfer_settled = True
        label = state._transfer_label
        if not task.done():
            task.cancel()
            if self.conn.is_closed:
                logger.warning(
                    f"[{self.conn.peer_addr}] Transfer to {loggable_label(label)}: no answer in "
                    f"{TRANSFER_SETTLE_S:.0f}s and the line closed under it — "
                    f"outcome unknown, recorded as transferred"
                )
                self._transfer_done(label)
                return True
            self._transfer_failed(f"no answer in {TRANSFER_SETTLE_S:.0f}s")
        elif task.cancelled():
            self._transfer_failed("the request was cancelled")
        elif task.exception() is None:
            self._transfer_done(label)
            return True
        else:
            self._transfer_failed(str(task.exception()))
        if not self.conn.is_closed:
            with contextlib.suppress(Exception):
                await self._play_phrase("transfer_failed")
        return False

    def _transfer_done(self, label: str) -> None:
        self.state._transferred_to = label
        self.state._running = False
        logger.info(f"[{self.conn.peer_addr}] Caller transferred to {loggable_label(label)}")

    def _transfer_failed(self, reason: str) -> None:
        state = self.state
        label = state._transfer_label
        state.call_outcome = state._transfer_previous_outcome
        logger.warning(
            f"[{self.conn.peer_addr}] Transfer to {loggable_label(label)} failed: {reason}")
        state._call_complete = False
        self._clear_call_control("transfer failed")
        state._control_note = f"[The transfer to {label} failed; the caller is still with you.]"

    def _phrase(self, key: str) -> str:
        return self.cfg.phrase(self._locked_language, key)

    async def _play_phrase(self, key: str) -> None:
        """One-shot speech of a per-language phrase (a failed transfer, a
        tool-first goodbye) through the filler path."""
        text = self._phrase(key)
        if not text:
            return
        pcm = await self.tts.synthesize(
            text, language=self._locked_language, output_sample_rate=self._rate_out)
        if pcm:
            await self._play_filler_audio(pcm)
