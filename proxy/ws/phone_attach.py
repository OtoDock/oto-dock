"""An outbound call that runs inside the caller's own chat.

The daemon's per-call socket presents a grant (``services/phone/
in_session_calls.py``) at its warmup instead of asking for a session of its
own: the call attaches to the chat's live session and runs as ONE call
turn, a pump that lives for the whole call. The chat's queue sees a live
pump, so a message typed meanwhile waits and is delivered at the call's
end; the pump is of the phone kind, never steered into.

Each daemon utterance is one engine turn on the chat's session: the
person's words land as a user row prefixed ``Phone (<number>):`` (the
``queue_turn`` accept writes the row and echoes the bubble to the chat's
viewers), the reply streams back as the ``/ws/phone`` frames the daemon
already speaks, and the pump's turn boundary becomes that turn's ``done``.
A ``system`` utterance (the opening prompt, the resume note) writes no row.
The first utterance waits for the chat's current turn to end (the make_call
turn is usually still finishing) in its own task, so the socket keeps
reading meanwhile; after it, the utterances queue on the pump's producer.
The attach never closes the chat's session nor releases its slot: the
session is the chat's, the call only borrows it.

The person is never left on a silent line. An engine error in ONE turn is
that turn's: the daemon gets the error frame (its client says so, as on any
call) and the pump goes on; a session that died, a chat stopped or deleted
and a first claim that never came END the attach, and the end is told: the
proxy closes the call's socket (the daemon's client marks the attach lost
and the pipeline hangs up with its apology) and leaves the chat a durable
note for its next turn. A proxy restart ends the attach with its socket
alone: the daemon's client notices, and no note is left. The daemon sends a resume note
(``system``, ``resume``) on every end path it owns and closes; a socket that
ends without one makes the producer send a short note itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections import deque
from dataclasses import dataclass, field

from storage import database as task_store
from ws import wire_events as wire

logger = logging.getLogger("otodock")

#: How long the first utterance waits for the chat's turn to end (the
#: daemon's opening wait is longer); later utterances never wait here.
FIRST_CLAIM_WAIT_S = 60.0
DEAD_LINE = "The chat's session is not running; the call cannot run in it."
FALLBACK_RESUME_NOTE = (
    "[The phone call ended; the line is closed. You are back in the text chat: the "
    "phone rules no longer apply. Summarise the call's outcome for the user in one "
    "or two sentences and continue your task.]")
TERMINATED_NOTE = (
    "[The phone call that ran in this chat was cut short: {why}. The line was closed "
    "with an apology to the person. You are back in the text chat: the phone rules "
    "no longer apply. Tell the user the call did not finish and continue your task.]")
_IN_SESSION_CONTEXT = (
    "You are now on a live phone call inside this chat. The person's words arrive as "
    "messages prefixed \"Phone (<number>):\"; answer each as speech, one to three short "
    "sentences, in their language. Messages the user types meanwhile wait until the "
    "call ends. Avoid slow tools: the line is silent while you work. The question rule "
    "above does not apply on this call: no agent placed it for you, so neither the "
    "ask_initiating_agent tool nor a [QUESTION:] line reaches anyone. Decide from what "
    "you know.")


@dataclass
class _TurnOutcome:
    saw_done: bool = False
    error: str = ""


@dataclass
class CallAttach:
    """One in-session call: the chat, its session and the long-lived pump."""

    grant: str
    chat_id: str
    session_id: str
    agent: str
    sub: str
    number: str
    layer: object
    send: object                      # async callable: a frame to the daemon
    close: object = None              # async callable: end the daemon's socket
    context_text: str = ""
    utterances: asyncio.Queue = field(default_factory=asyncio.Queue)
    turns: deque = field(default_factory=deque)   # turn ids in engine order
    pump: object = None
    producer: asyncio.Task | None = None
    forward_task: asyncio.Task | None = None
    start_task: asyncio.Task | None = None
    turn_task: asyncio.Task | None = None
    started: bool = False
    context_injected: bool = False
    interrupted_last_turn: bool = False
    resume_seen: bool = False
    ended: bool = False               # the daemon's socket is gone
    dead: bool = False                # the attach ended under a live call
    terminate_reason: str = ""        # why, when the producer ended it


_attaches: dict[str, CallAttach] = {}    # chat id → the live attach


def live_for_chat(chat_id: str) -> CallAttach | None:
    return _attaches.get(chat_id)


async def begin(msg: dict, send, close=None) -> CallAttach | str:
    """The daemon's warmup with a grant: the attach, or a refusal line."""
    from services.phone import call_control, in_session_calls
    agent = str(msg.get("model") or "")
    session_id = str(msg.get("attach_session_id") or "")
    grant = str(msg.get("attach_grant") or "")
    g = in_session_calls.claim(grant, agent=agent, session_id=session_id)
    if g is None:
        return "The call's grant is unknown or expired."
    live = _attaches.get(g.chat_id)
    if live is not None and not live.ended:
        return in_session_calls.BUSY_LINE
    chat = await asyncio.to_thread(task_store.get_chat, g.chat_id)
    if not chat or (chat.get("session_id") or "") != session_id:
        in_session_calls.release(grant)
        return "The chat moved to another session; the call cannot run in it."
    from auth.providers import acting_role_of
    from core.session.session_manager import get_execution_layer
    from storage.pg import run_db
    role = await run_db(acting_role_of, g.user_sub, agent)
    try:
        layer = get_execution_layer(
            agent, execution_path=chat.get("execution_path") or "",
            user_sub=g.user_sub or None, role=role)
    except RuntimeError:
        in_session_calls.release(grant)
        return DEAD_LINE
    try:
        alive = await layer.is_session_alive(session_id)
    except Exception:
        alive = False
    if not alive:
        in_session_calls.release(grant)
        return DEAD_LINE
    number = str(msg.get("caller_phone") or g.phone_number or "")
    attach = CallAttach(
        grant=grant, chat_id=g.chat_id, session_id=session_id, agent=agent,
        sub=g.user_sub, number=number, layer=layer, send=send, close=close,
        context_text=await asyncio.to_thread(
            _context_text, str(msg.get("phone_route_id") or ""),
            bool(msg.get("transfer", True))),
    )
    _attaches[g.chat_id] = attach
    call_control.bind(session_id, send, ask_back=False)
    logger.info(
        f"phone attach: call in chat={g.chat_id[:8]} session={session_id[:8]} "
        f"agent={agent}")
    return attach


def _context_text(route_id: str, can_transfer: bool = True) -> str:
    """The call context the chat's session lacks (its prompt is the
    dashboard's): the outbound rules, the call tools with the route's
    targets (only on a line that can hand the caller over), and the
    in-session lines."""
    from adapters.phone import OUTBOUND, PhoneAdapter, call_tools_block
    from storage.phone import phone_route_store
    targets: list[dict] = []
    if route_id and can_transfer:
        route = phone_route_store.get_route(route_id) or {}
        targets = [t for t in (route.get("transfer_targets") or []) if isinstance(t, dict)]
    return "\n".join([
        PhoneAdapter.get_phone_context(OUTBOUND).strip(),
        call_tools_block(ask_back=False, transfer_targets=targets).strip(),
        _IN_SESSION_CONTEXT,
    ])


def _build_prompt(attach: CallAttach, text: str, barge_in_chars: int | None) -> str:
    parts: list[str] = []
    if not attach.context_injected:
        parts.append(f"<system-reminder>\n{attach.context_text}\n</system-reminder>")
        attach.context_injected = True
    if attach.interrupted_last_turn or barge_in_chars:
        # An engine that rebuilds its history from the DB gets the precise
        # annotation through the kwarg; the others need the note in text.
        behaviour = getattr(getattr(attach.layer, "capabilities", None), "behaviour", None)
        if not getattr(behaviour, "rebuilds_history_from_db", False):
            heard = (f"; they heard only the first {barge_in_chars} characters"
                     if barge_in_chars else "")
            parts.append("[The person interrupted your previous spoken reply"
                         f"{heard} — they did not hear the rest.]")
        attach.interrupted_last_turn = False
    parts.append(text)
    return "\n\n".join(parts)


async def utterance(attach: CallAttach, frame: dict) -> None:
    """A ``chat`` frame from the daemon: queued onto the call's pump (the
    first one starts it, in its own task: the socket keeps reading)."""
    turn = int(frame.get("turn") or 0)
    text = str(frame.get("prompt") or "")
    system = bool(frame.get("system"))
    resume = bool(frame.get("resume"))
    if resume:
        attach.resume_seen = True
    if attach.dead or (attach.ended and not resume):
        await _send(attach, {"type": wire.ERROR, "turn": turn,
                             "data": {"message": DEAD_LINE}})
        return
    attach.utterances.put_nowait((turn, text, system, frame.get("barge_in_chars"), resume))
    if not attach.started:
        attach.started = True
        attach.start_task = asyncio.create_task(_start_or_terminate(attach))


async def _start_or_terminate(attach: CallAttach) -> None:
    try:
        ok = await _start(attach)
    except Exception as e:
        logger.error(f"phone attach start error: {e}", exc_info=True)
        ok = False
    if not ok:
        await _terminate(attach, "the chat stayed busy with its own turn")


async def _start(attach: CallAttach) -> bool:
    """Claim the chat once its current turn ends and start the one pump."""
    from core.events import input_queue
    from core.events.common_events import CommonEvent, ERROR, PRODUCER_DONE
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.session import session_kind
    from core.session import visibility as _vis
    from core.session.session_state import get_permission_queue

    queue = input_queue.get(attach.chat_id)
    claim = await input_queue.claim_when_free(attach.chat_id, timeout=FIRST_CLAIM_WAIT_S)
    if claim is None:
        return False
    if attach.ended:
        queue.release(claim)
        return False
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        try:
            await _run_call(attach, event_queue)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"phone attach producer error: {e}", exc_info=True)
            await event_queue.put(CommonEvent(type=ERROR, data={"message": str(e)}))
        finally:
            with contextlib.suppress(Exception):
                event_queue.put_nowait(CommonEvent(type=PRODUCER_DONE, data={}))

    producer = asyncio.create_task(_produce())
    pump = ChatStreamPump(
        chat_id=attach.chat_id,
        session_id=attach.session_id,
        producer=producer,
        event_queue=event_queue,
        perm_queue=get_permission_queue(attach.session_id),
        scope="agent" if _vis.is_shared_only(attach.agent) else "user",
        source_type=session_kind.PHONE.source_type,
    )
    pump.wake_person = attach.sub
    _active_pumps[attach.chat_id] = pump
    queue.release(claim)
    pump.start()
    attach.pump = pump
    attach.producer = producer
    attach.forward_task = asyncio.create_task(_forward(attach, pump))
    return True


async def _run_call(attach: CallAttach, event_queue: asyncio.Queue) -> None:
    """The producer: every utterance an engine turn on the chat's session,
    under its lock for the whole call."""
    from core.events.common_events import CommonEvent, DONE, QUEUE_TURN
    from services.phone import call_control
    layer, sid = attach.layer, attach.session_id
    async with layer.session_lock(sid):
        while True:
            item = await attach.utterances.get()
            if item is None:
                break
            turn, text, system, barge_in_chars, resume = item
            if not system:
                # The person's words: the row the chat shows and the prompt
                # the engine gets carry the same line.
                text = f"Phone ({attach.number}): {text}" if attach.number else text
                accepted = asyncio.get_running_loop().create_future()
                await event_queue.put(CommonEvent(type=QUEUE_TURN, data={
                    "text": text, "accepted": accepted,
                }))
                await accepted
            prompt = _build_prompt(attach, text, barge_in_chars)
            attach.turns.append(turn)
            call_control.set_turn(sid, turn)
            attach.turn_task = asyncio.create_task(
                _engine_turn(layer, sid, prompt, barge_in_chars, event_queue))
            try:
                await asyncio.wait({attach.turn_task})
            except asyncio.CancelledError:
                attach.turn_task.cancel()
                raise
            finally:
                call_control.set_turn(sid, None)
            task, attach.turn_task = attach.turn_task, None
            outcome = _TurnOutcome()
            if not task.cancelled() and task.exception() is None:
                outcome = task.result()
            if outcome.error:
                # One engine turn failed: the daemon hears it for that turn
                # (its client says so and the call goes on, as on any call);
                # the pump never sees the ERROR, which would end it.
                await _send(attach, {"type": wire.ERROR, "turn": turn,
                                     "data": {"message": outcome.error}})
            if not outcome.saw_done:
                # The engine's own boundary never came (an aborted stream, an
                # error): the pump closes the turn and the daemon gets its done.
                await event_queue.put(CommonEvent(type=DONE, data={}))
            if outcome.error and not await _session_alive(layer, sid):
                # Nothing would answer the next utterance: the attach ends
                # (the forward's end terminates it) rather than the person
                # hearing an error on every turn.
                logger.warning(f"phone attach: session {sid[:8]} died under the call")
                attach.terminate_reason = "the chat's session stopped running"
                break
            if resume:
                break
        if attach.pump is not None:
            attach.pump.close_queue()


async def _session_alive(layer, sid: str) -> bool:
    try:
        return bool(await layer.is_session_alive(sid))
    except Exception:
        return False


async def _engine_turn(layer, sid: str, prompt: str, barge_in_chars, event_queue) -> _TurnOutcome:
    """One engine turn onto the pump: whether the engine closed it itself,
    and the error it ended on (kept off the pump)."""
    from core.events.common_events import DONE, ERROR
    outcome = _TurnOutcome()
    try:
        async for event in layer.send_message(
                sid, prompt, barge_in_chars=barge_in_chars, inject_time=True):
            if event.type == ERROR:
                outcome.error = str((event.data or {}).get("message") or "engine error")
                continue
            await event_queue.put(event)
            outcome.saw_done = outcome.saw_done or event.type == DONE
    except asyncio.CancelledError:
        raise
    except Exception as e:
        outcome.error = str(e) or "engine error"
    return outcome


async def _forward(attach: CallAttach, pump) -> None:
    """The pump's items to the daemon, each stamped with the turn it belongs
    to (the producer's order); a turn boundary is that turn's ``done``."""
    from ws.phone import _pump_item_to_phone_ws
    q = pump.attach()
    try:
        while True:
            item = await q.get()
            pt = item.get("pump_type", "")
            turn = attach.turns[0] if attach.turns else 0
            if pt == wire.PUMP_IS_DONE:
                if attach.turns:
                    attach.turns.popleft()
                await _send(attach, {"type": wire.DONE, "turn": turn, "data": {}})
                continue
            if pt in wire.PUMP_TERMINAL:
                if pt == wire.PUMP_ERROR:
                    await _send(attach, {"type": wire.ERROR, "turn": turn, "data": {
                        "message": item.get("message", "Unknown error")}})
                while attach.turns:
                    await _send(attach, {"type": wire.DONE, "turn": attach.turns.popleft(),
                                         "data": {}})
                break
            msg = _pump_item_to_phone_ws(item, turn)
            if msg is not None and msg.get("type") != wire.DONE:
                await _send(attach, msg)
    except asyncio.CancelledError:
        pass
    finally:
        with contextlib.suppress(Exception):
            pump.detach(q)
        if attach.ended or attach.resume_seen:
            # The call ended its own way: the daemon's note ran (or the
            # socket's end queued ours).
            _release(attach)
        else:
            # The pump ended under a live call (a Stop, a deleted chat, a
            # session that died): the person is told and the line closes.
            await _terminate(attach, attach.terminate_reason or "the chat's turn was stopped")


async def abort(attach: CallAttach, frame: dict) -> None:
    """Barge-in: graceful first (the engine closes the turn itself), else
    the turn's task is cancelled; the pump and the call go on."""
    turn = frame.get("turn")
    task = attach.turn_task
    if task is None or task.done() or (attach.turns and turn is not None
                                        and attach.turns[-1] != turn):
        return
    graceful = False
    with contextlib.suppress(Exception):
        graceful = bool(await attach.layer.abort(attach.session_id))
    if not graceful and not task.done():
        task.cancel()
    attach.interrupted_last_turn = True
    logger.info(f"phone attach: turn {turn} aborted "
                f"({'graceful' if graceful else 'cancelled'}) chat={attach.chat_id[:8]}")


async def _terminate(attach: CallAttach, why: str) -> None:
    """The attach ends under a live call. The daemon's socket closes (its
    client marks the attach lost and the pipeline hangs up with the apology),
    the chat gets a durable note for its next turn, the grant goes."""
    if attach.dead:
        return
    attach.dead = True
    from services.phone import call_control
    call_control.unbind(attach.session_id, attach.send)
    logger.warning(f"phone attach: chat={attach.chat_id[:8]} ended under the call: {why}")
    if not attach.resume_seen:
        _store_note(attach, TERMINATED_NOTE.format(why=why))
    if attach.close is not None and not attach.ended:
        with contextlib.suppress(Exception):
            await attach.close()
    _release(attach)


def _store_note(attach: CallAttach, text: str) -> None:
    """A note the chat's next turn replays (the pump is gone; a direct turn
    would race the chat's own)."""
    from core.events import chat_writer
    from core.events.stream_pump import store_chat_wake
    try:
        chat_writer.submit(
            attach.chat_id,
            functools.partial(store_chat_wake, attach.chat_id, text, attach.sub),
            label="phone_attach_note",
        )
    except Exception as e:
        logger.warning(f"phone attach: the chat's note was not stored: {e}")


def end(attach: CallAttach) -> None:
    """The daemon's socket is gone. A pump that runs finishes its queue (the
    resume note the daemon sent, or a note of our own) and exits; nothing of
    the chat's session is closed."""
    if attach.ended:
        return
    attach.ended = True
    from services.phone import call_control
    call_control.unbind(attach.session_id, attach.send)
    if attach.start_task is not None and not attach.start_task.done() and attach.pump is None:
        # The daemon left while the chat's own turn still ran: nothing to
        # drain, and the chat learns of the call at its next turn.
        attach.start_task.cancel()
        attach.dead = True
        _store_note(attach, FALLBACK_RESUME_NOTE)
    if attach.started and not attach.dead and attach.producer is not None \
            and not attach.producer.done():
        if not attach.resume_seen:
            attach.utterances.put_nowait((0, FALLBACK_RESUME_NOTE, True, None, True))
        attach.utterances.put_nowait(None)
    else:
        _release(attach)


def _release(attach: CallAttach) -> None:
    from services.phone import in_session_calls
    in_session_calls.release(attach.grant)
    if _attaches.get(attach.chat_id) is attach:
        _attaches.pop(attach.chat_id, None)


async def _send(attach: CallAttach, payload: dict) -> None:
    if attach.ended:
        return
    with contextlib.suppress(Exception):
        await attach.send(payload)


def reset() -> None:
    """Tests only."""
    _attaches.clear()


__all__ = ["CallAttach", "begin", "utterance", "abort", "end", "live_for_chat",
           "FIRST_CLAIM_WAIT_S", "DEAD_LINE", "FALLBACK_RESUME_NOTE", "TERMINATED_NOTE"]
