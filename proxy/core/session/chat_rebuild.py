"""One rebuild of a chat's engine session: close what is live keeping the
transcript, then run a turn on a session built from scratch.

Three names. ``close_chat_session`` is the one closer: the terminal of a
chat, its headless session on this host, or its session on a machine. It
never removes a transcript, a rollout or a thread id, so the engine's own
resume finds the conversation again. ``closed`` is the hold a rebuild takes
while it closes (the chat's queue claim and the warm-up registry), with the
idle check under it. ``rebuild_and_resume`` runs a turn with no socket: it
rides the chat's live session or starts one through the delivery ladder
(``core/session/session_delivery.py``), resumed by the engine's own means or
fresh from the history when nothing can be resumed.

Nothing is defaulted that decides what a session may do: the role, the mode,
the kind of session and the builder are the caller's words. A session this
module starts is built from the stored state of that moment (the agent's
tools, the person's accounts and role, the chat's place), never from the
session it replaces, so a rebuild widens nothing.

A start that carries no turn is a socket's own (the switch between a
terminal and a headless chat, the plan's fresh start): it closes here and
starts through the dashboard's funnel, which adopts the session into the
socket and answers with frames.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.execution_layer import ExecutionLayer
    from core.session.session_delivery import DeliveryOutcome

logger = logging.getLogger("otodock.session-delivery")

#: The builder of a session this module starts. ``AS_CHAT``: what the
#: person's own warm-up of the chat builds (``build_agent_config``), at the
#: role the dashboard reads for them at that moment. ``AS_WAKE``: the
#: delivery's own builder (``wake_session._spawn_wake_session``), for a
#: session that runs as whoever a stored wake names.
AS_CHAT = "chat"
AS_WAKE = "wake"
BUILDS = frozenset({AS_CHAT, AS_WAKE})

#: The sources of a rebuilt turn whose text a person wrote: such a turn is
#: theirs to pay for in whatever chat it lands (``core/config/spender.py``).
#: A reload, a continue and a limit's continuation resume a person's cut
#: turn and build that person's own session (``AS_CHAT``): theirs too. What
#: is left, a delegated result, a scheduled wake or a move's resume built
#: as a wake, is a turn nobody typed (``nobody_typed``).
TYPED = frozenset({"api_key"})


def nobody_typed(source: str, build: str) -> bool:
    """Whether a rebuilt turn is nobody's own: not a person's typed text
    (``TYPED``), and not the platform's resume of a person's cut turn,
    which builds that person's own session (``AS_CHAT``). Such a turn in a
    lane's chat is paid as the lane's newest round, and never rides a live
    session that serves another person (``rebuild_and_resume``)."""
    return source not in TYPED and build != AS_CHAT

#: The mode word a wake of a person's chat passes: never a mode itself. The
#: session it starts takes the chat's own mode, read from the row once the
#: wake holds its slot (``own_mode``), so a mode lowered while it waited is
#: the one it runs in.
CHAT_OWN_MODE = "chat-own"


@dataclass(frozen=True)
class WakeBuild:
    """What a session a wake starts takes: the builder, the mode word and
    the kind (``wake_build``)."""

    build: str
    mode: str
    kind: str


def persons_chat(chat: dict | None, person: str | None) -> bool:
    """Whether the chat is this person's to run as: their own chat, or the
    agent's shared chat of an agent that is Shared-only now. Synchronous
    (the agent's mode is read): call it on the DB executor."""
    from core.session.visibility import is_shared_only, shared_chat_owner
    agent = (chat or {}).get("agent") or ""
    if not person or not agent:
        return False
    owner = (chat or {}).get("user_sub") or ""
    if owner == person:
        return True
    return owner == shared_chat_owner(agent) and is_shared_only(agent)


def wake_build(chat: dict | None, person: str | None, role: str) -> WakeBuild:
    """The one decision of what a wake of ``chat`` starts: a person's own
    dashboard chat, or one a call wrote for them once the call ended
    (``visibility.runs_as_chat``), is built as the chat (``AS_CHAT``, its own
    mode, the dashboard kind); every other chat as today, a session nobody drives
    (``AS_WAKE``, the unattended mode, no kind). Never the chat's build for
    an outside person, a task run's, a worker's or a machine's own CLI chat.
    Synchronous: call it on the DB executor."""
    from auth import roles
    from core.permission_modes import UNATTENDED_MODE
    from core.session import session_kind, visibility
    unattended = WakeBuild(AS_WAKE, UNATTENDED_MODE, "")
    if (not chat or not person or roles.is_external(role)
            or not visibility.runs_as_chat(chat)
            or chat.get("origin") in ("delegated", "otodock")
            or not persons_chat(chat, person)):
        return unattended
    return WakeBuild(AS_CHAT, CHAT_OWN_MODE, session_kind.DASHBOARD.name)


def new_chat_mode(agent: str, owner: str, role: str, word: str = "") -> str:
    """The mode a new chat of ``owner`` on ``agent`` is stored with: the
    person's pick ``word``, and with none Auto, the page's own seed when it
    holds no pick of theirs. An agent's shared chat made below the editor
    role stores Ask: it runs as the agent, so no pick of theirs stays on it.
    The warm-up holds the word against the modes the chat's engine offers.
    A chat a call wrote for a person takes this too: after the call it is
    theirs. Synchronous."""
    from auth import roles
    from core.permission_modes import ASK_MODE, AUTO_REVIEW_MODE, entry_mode
    from core.session.visibility import is_shared_chat_owner
    if is_shared_chat_owner(owner) and not roles.can_edit(role):
        return ASK_MODE
    return entry_mode(word or AUTO_REVIEW_MODE)


def own_mode(chat: dict) -> str:
    """The mode a wake of a person's chat runs in: the chat's own, Ask when
    the row holds a word no person picks (it fails closed)."""
    from core.permission_modes import ASK_MODE, STORED_ONLY_MODES
    mode = chat_mode(chat)
    return ASK_MODE if mode in STORED_ONLY_MODES else mode


class ChatBusy(Exception):
    """The chat is taken (a turn, a warm-up, a move, another rebuild): a
    rebuild that closes never runs under it."""


@dataclass(frozen=True)
class Closed:
    """What ``closed`` holds: the chat's row read under the hold and whether
    a live session was closed."""

    chat: dict
    was_live: bool


def turn_open_while_held(chat: dict) -> bool:
    """A turn open on a chat whose holder owns its warm-up registry entry: a
    pump running, a one-shot delivery in flight, a card waiting on a person,
    a terminal's turn, a terminal parked on its question or holding the
    background work its last Stop reported (the reaper's busy reasons). No
    await: read with the close it guards."""
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_registry, interactive_session, session_delivery
    from core.session.session_state import has_pending_prompt
    cid, sid = chat.get("id") or "", chat.get("session_id") or ""
    pump = _active_pumps.get(cid)
    if pump is not None and not pump.is_done:
        return True
    if session_delivery.oneshot_inflight(cid) is not None or (sid and has_pending_prompt(sid)):
        return True
    isess = interactive_session.find_live_for_chat(cid)
    return bool(isess is not None and (
        isess.turn_open or isess.question_parked or isess.submit_settling
        or interactive_registry.background_reported(isess)))


def chat_mode(chat: dict) -> str:
    """The chat's own permission mode, as its engine offers it. Synchronous
    (the engine is read from the chat's agent)."""
    from core.permission_modes import offered_mode
    from core.session.session_manager import get_layer_capabilities
    from services.remote import chat_targets
    caps = get_layer_capabilities(chat_targets.chat_engine(chat))
    stored = chat.get("permission_mode") or ""
    return (offered_mode(stored, caps.permission_modes) if caps and caps.permission_modes
            else offered_mode(stored))


async def _holder(session_id: str) -> ExecutionLayer | None:
    """The layer a headless session closes through: the one that holds it,
    whatever the chat's pin says (a round that fell back to the platform
    leaves a local process behind a machine's pin, and a socket's place is
    its last warm-up's). A session on a machine closes there only while its
    own machine is connected: one that is away is sent nothing, and its
    record goes with the reaper's next pass."""
    from core.session import session_manager
    live = session_manager.live_session(session_id)
    if live is None:
        return None
    return live.layer if not live.machine_id or live.linked else None


async def close_chat_session(chat: dict, *, reason: str, require_idle: bool, lock: bool,
                             fallback_layer: ExecutionLayer | None = None) -> bool:
    """Close the chat's live session where it runs, keeping its transcript.
    True when a live session was closed: a terminal of the chat, or a
    headless session its holder owned.

    ``require_idle``: refuse (``ChatBusy``) while a turn is open, read with
    no await before the close. A caller that stops a running turn on purpose
    (the switch, whose confirm says so) passes False, and so does one that
    checked under its own hold. ``lock``: close a headless session under its
    layer's session lock, which a delivery into the session holds while it
    sends. ``fallback_layer``: the layer to close through when none holds the
    session, for a caller that frees what a dead id still holds (its seat
    and its slot): that close ends nothing live and answers False. An error
    of the close is the caller's to judge."""
    from core.session import interactive_session
    cid, sid = chat.get("id") or "", chat.get("session_id") or ""
    if require_idle and turn_open_while_held(chat):
        raise ChatBusy(cid)
    terminal = interactive_session.find_live_for_chat(cid) if cid else None
    if terminal is not None:
        # The terminal's close tails its transcript into the chat's rows. A
        # terminal on a machine is closed by the token of its open, so one
        # started again under the id never takes the old one's exit: nothing
        # waits for the machine's report here.
        await interactive_session.close_for_chat(cid, reason=reason)
        return True
    if not sid:
        return False
    holder = await _holder(sid)
    layer = holder or fallback_layer
    if layer is None:
        return False
    if lock:
        async with layer.session_lock(sid):
            await layer.close_session(sid)
    else:
        await layer.close_session(sid)
    if holder is None:
        logger.info("chat rebuild: what session %s of chat=%s still held was released (%s)",
                    sid[:8], cid[:8], reason)
        return False
    logger.info("chat rebuild: session %s of chat=%s closed (%s)", sid[:8], cid[:8], reason)
    return True


@asynccontextmanager
async def closed(chat_id: str, *, reason: str, by: str, claim: object | None = None,
                 hold: Callable[[], bool] | None = None):
    """Hold the chat and close its live session: the chat's queue claim
    (``claim``: the caller's own, else taken here), its warm-up registry
    entry, the row read again, the idle check, the close. Yields ``Closed``.
    ``hold``: the caller's own reason to keep the session for now (a
    terminal's unsent text), read with the idle check, no await before the
    close: ``ChatBusy`` while it holds.
    Leaving it gives the registry entry and the claim back, the caller's
    included, with the queue armed again, so a message that waited goes out.

    Nothing starts a session inside it: the ladder's guards read the hold as
    someone else's turn. ``ChatBusy`` when the chat is taken or a turn is
    open, ``LookupError`` when the chat is gone. ``by``: the sub the hold is
    registered for."""
    from core.events import input_queue
    from core.session import warmup_registry
    from storage import database as task_store
    from storage.pg import run_db
    q = await input_queue.loaded(chat_id)
    if claim is None:
        claim = q.try_claim()
        if claim is None:
            raise ChatBusy(chat_id)
    registered = False
    try:
        chat = await run_db(task_store.get_chat, chat_id)
        if not chat:
            raise LookupError(chat_id)
        # A person's warm-up holds no claim: the entry is made only when the
        # chat has none, so this hold's end never removes another's.
        if await warmup_registry.register_new(chat_id, by, chat.get("agent") or "") is None:
            raise ChatBusy(chat_id)
        registered = True
        held = await run_db(task_store.get_chat, chat_id)
        if not held:
            raise LookupError(chat_id)
        if hold is not None and hold():
            raise ChatBusy(chat_id)
        was_live = await close_chat_session(held, reason=reason, require_idle=True, lock=True)
        yield Closed(chat=held, was_live=was_live)
    finally:
        try:
            if registered:
                await warmup_registry.unregister(chat_id)
        finally:
            # Given back whatever the registry's step did.
            q.release(claim, rearm=True)


async def rebuild_and_resume(
    chat_id: str,
    text: str,
    *,
    source: str,
    person: str | None,
    role: str,
    mode: str,
    kind: str,
    build: str,
    session_id: str = "",
    agent: str = "",
    spawns: bool = True,
    fresh_without_session: bool = False,
    notify_payload: dict | None = None,
    pump_event: dict | None = None,
    persist_event: Callable[[str], None] | None = None,
    on_outcome: Callable[[DeliveryOutcome], Awaitable[None] | None] | None = None,
    allow_pty: bool = True,
    allow_ws: bool = True,
    allow_live_turn: bool = True,
    hops: int = 0,
) -> DeliveryOutcome:
    """Run ``text`` as a turn of the chat, on its live session or on one
    started for it, with no socket.

    ``person`` is the sub the session runs as (None: the agent itself) and
    ``role`` their role, read by the caller at that moment and read again by
    the rung that starts a session. ``mode`` and ``kind`` are the permission
    mode and the kind of session a started session takes (``CHAT_OWN_MODE``:
    the chat's own, read when the session is built), ``build`` its builder
    (``AS_CHAT`` or ``AS_WAKE``). None of the five has a default.

    A caller that rebuilds closes first, under ``closed``, and calls this
    once the hold is given back, so the turn runs on a session built now.
    ``spawns`` False: the turn rides a live process only (a wake that runs
    as nobody below the editor tier starts no session). A wake built as
    nobody's in a chat a call holds never starts one either: it rides the
    call's session or is kept, so nothing it starts lands after the hang-up. ``fresh_without_session``: a
    chat with no session at all starts fresh, its pending history seed ahead
    of the turn. The other arguments are the ladder's own
    (``session_delivery.deliver_prompt``), and so is the answer.

    A rung that raises is the caller's to recover from: each caller keeps
    its own undo (a stored wake, a row taken back).
    """
    if build not in BUILDS:
        raise ValueError(f"unknown session build: {build!r}")
    if build == AS_CHAT and not person:
        raise ValueError("a chat-built session runs as a person")
    if mode == CHAT_OWN_MODE and build != AS_CHAT:
        raise ValueError("only a chat-built session takes the chat's own mode")
    if not role:
        raise ValueError("a rebuild names the role its session runs at")
    from services.phone import call_chats
    if build == AS_WAKE and call_chats.holds(chat_id):
        # A call writes there: the turn rides the call's session or is kept.
        spawns = False
    # Read at call time: the ladder and its two rungs are what tests patch.
    from core.config import spender
    from core.session import session_delivery
    from services.scheduler import delivery
    from storage.pg import run_db
    # A turn nobody typed into a lane's chat whose live session serves
    # another person never runs on it, and no second process starts on the
    # conversation that session holds: the caller stores the wake, replayed
    # once a session of the lane's person may take it.
    rides = not nobody_typed(source, build) or await run_db(spender.wake_rides, chat_id, session_id)
    if not rides:
        allow_pty = allow_ws = allow_live_turn = False
    persistent = functools.partial(
        delivery._deliver_via_persistent, driven_by=source) if rides else None
    # What started the turn rides to the pump that runs it (``driven_by``):
    # a limit that cuts a turn nobody watches reads it (``pool_limits``).
    oneshot = functools.partial(
        delivery._deliver_via_oneshot, permission_mode=mode, client_type=kind, build=build,
        fresh_without_session=fresh_without_session, driven_by=source,
    ) if spawns and rides else None
    return await session_delivery.deliver_prompt(
        chat_id, text, source=source, session_id=session_id, agent=agent,
        user_sub=person, role=role,
        notify_payload=notify_payload, pump_event=pump_event, persist_event=persist_event,
        persistent_fn=persistent,
        oneshot_fn=oneshot,
        on_outcome=on_outcome,
        allow_pty=allow_pty, allow_ws=allow_ws, allow_live_turn=allow_live_turn, hops=hops,
    )
