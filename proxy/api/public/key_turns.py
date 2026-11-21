"""A message sent with a person's key, and the wait for its turn's end.

The message is the person's own row (its ``event_data`` names the key) and
it runs a turn of the person's chat, through the gate a typed message
passes and the rebuild a turn with no socket takes
(``core/session/chat_rebuild.py``, in the chat move's resume shape: no
socket, no terminal, never steered into or queued on a running turn), in the chat's own mode, as a chat turn, with the
person's role read now. A turn that needs approval waits for the person in
the dashboard (the parked notice of ``_run_echo_turn_pumped``).

``send`` answers once the turn ended or the bound passed; the turn itself
runs as a tracked background task, so a caller that goes away never ends it.
A repeated request id sends once: the first request's turn answers it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import time
from dataclasses import dataclass, field

from fastapi import HTTPException

from auth.providers import UserContext, acting_role_of
from core import drain, placement
from core.background import spawn
from core.permission_modes import ASK_MODE, JUDGE_MODE, PLAN_MODE
from core.session import key_rows, session_kind, session_manager
from storage import database as task_store
from storage.identity import api_key_store
from storage.pg import run_db

logger = logging.getLogger("otodock.api-keys")

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

DONE = "done"
RUNNING = "running"
NO_ANSWER = "no_answer"
NOT_SENT = "not_sent"

BUSY = ("Someone is using this chat right now (a turn runs, a message waits, "
        "or the chat is being opened). Try again when it is free.")
BACKGROUND_BUSY = ("The chat's last turn started work that is still running. "
                   "Send the message again when it ends.")
UPDATING = "OtoDock is updating. Send the request again in a few minutes."
#: What a request refused for an update is told to wait before it comes again.
UPDATING_RETRY_S = 60
#: The modes where an edit, a shell change and a standard-tier tool wait for
#: a person's answer: all a key not made to run without asking may start a
#: turn in. Auto runs the standard tools with no card, and a chat with no
#: stored word reads as Auto.
ASKING_MODES = frozenset({ASK_MODE, PLAN_MODE})
UNASKED_CHAT = ("This chat runs steps without asking you. This key was not made to run "
                "without asking.")
OTHER_CHAT = "This request id was used for a message in another chat. Use a new request id."
NOT_SENT_DETAIL = ("The message was not sent. The chat could not start a turn now: a person "
                   "took it meanwhile, or the agent has no AI engine you can use here.")


@dataclass
class KeyTurn:
    key_id: str
    request_id: str
    chat_id: str
    message_id: int = 0
    end_id: int = 0
    state: str = RUNNING
    session_id: str = ""
    #: Why the build refused the turn's place, said to the caller as it is.
    refusal: str = ""
    finished_at: float = 0.0
    #: Dropped before its row (the claim failed, the chat was taken): a
    #: request waiting on it sends again.
    dropped: bool = False
    written: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)


# (key id, request id) → its turn; the chats a key turn holds, gate to end.
_turns: dict[tuple[str, str], KeyTurn] = {}
_by_message: dict[tuple[str, int], KeyTurn] = {}
_held_chats: set[str] = set()
# The chats whose turn in flight was started by a key that asks. Read only
# together with ``_held_chats`` (``feeds_allow_memory``): the hold ends with
# the turn, so a mark left behind means nothing.
_asking_chats: set[str] = set()
# A pool cap's "go on with a key" (``pool_caps``).
_CAP_CONTINUE = "continue"
#: How long a finished turn answers a repeated request id from memory (the
#: claim row answers after that).
KEEP_FINISHED_S = 3600.0
#: A claim with no message that this process does not hold belongs to a
#: process that is gone, once it is this old: the request sends again.
CLAIM_STALE_S = 120


def reachable_chat(u: UserContext, chat: dict | None) -> bool:
    """The chats a key reaches: the person's own or a Shared-only pool's, on
    an agent the key reaches, a dashboard chat (never a task's, a call's or
    a meeting's), and one the person may open. An admin's key reaches no
    colleague's chat."""
    from api.public.common import person_of, reaches
    from core.session import visibility as _vis
    if not chat or not reaches(u, chat.get("agent") or ""):
        return False
    owner = chat.get("user_sub") or ""
    if session_kind.of_chat(chat) is not session_kind.DASHBOARD or _vis.is_phone_chat_owner(owner):
        return False
    if owner != u.sub and not _vis.is_shared_chat_owner(owner):
        return False
    return _vis.can_access_chat(person_of(u), chat)


def drive_refusal(u: UserContext, chat: dict) -> str:
    """Why the person may not drive ``chat`` now ("" when they may): the one
    rule every door asks (``visibility.drive_refusal``: the editor tier on a
    chat that runs as the agent, a run's own rule on a delegation's worker),
    then this door's extras: the place's lock and guard, a move in progress,
    and a terminal chat (its approvals are the terminal's own dialog, which
    no caller sees). Synchronous."""
    from core import execution_mode
    from core.session import visibility as _vis
    from services.remote import chat_move
    agent = chat.get("agent") or ""
    refusal = _vis.drive_refusal_for(u, chat)
    if refusal:
        return refusal
    if u.acting_sub is None:
        # A key speaks for its person, never for a session or nobody.
        return _vis.TASK_CONTINUE_REFUSED
    lock = chat_move.lock_sentence(chat, u.sub)
    if lock:
        return lock
    if chat_move.placement_blocked(chat, u.sub, u.acting_role(agent)):
        return "This chat runs on a machine your sessions may not use."
    if chat_move.moving(chat.get("id") or ""):
        return "This chat is moving to another machine. Try again in a moment."
    if execution_mode.is_interactive(chat_override=chat.get("execution_mode") or None):
        return ("This chat runs in a terminal, which an API key cannot drive. "
                "Send the message from the dashboard.")
    return ""


def chat_mode(chat: dict) -> str:
    """The chat's own permission mode, against what its engine offers."""
    from core.session import chat_rebuild
    return chat_rebuild.chat_mode(chat)


def _taken(chat_id: str) -> bool:
    """Someone other than a key holds the chat: a turn or its start, a
    message waiting in its queue, a live terminal, its deletion. No await."""
    from core.events import input_queue
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session, warmup_registry
    pump = _active_pumps.get(chat_id)
    return bool((pump is not None and not pump.is_done)
                or input_queue.is_claimed(chat_id) or bool(input_queue.peek(chat_id))
                or warmup_registry.get(chat_id) is not None
                or interactive_session.find_live_for_chat(chat_id) is not None
                or input_queue.deleting(chat_id))


def busy(chat_id: str) -> bool:
    """The chat is taken, or another key turn holds it. No await: read
    with the claim it guards."""
    return chat_id in _held_chats or _taken(chat_id)


async def _budget_refusal(u: UserContext, chat: dict) -> str:
    """The sender's own gates an idle send meets: the platform-auth budget
    and the person's pool cap on the chat's engine (a gate that fails to
    answer never holds a turn)."""
    try:
        from services.billing import usage_service
        limit = await asyncio.to_thread(usage_service.check_user_limit, u.sub, u.role)
        if not limit["allowed"]:
            return "Your usage limit is reached."
    except Exception:  # noqa: BLE001  # a failed usage-limit check never blocks the message
        pass
    try:
        from services.billing import pool_caps
        from services.engines import subscription_pool
        from services.remote import chat_targets
        layer_path = await run_db(chat_targets.chat_engine, chat)
        cap = await run_db(pool_caps.evaluate, "user", u.sub, layer_path)
        if not cap.allowed and not (cap.on_reached == _CAP_CONTINUE and await run_db(
                subscription_pool.cap_continue_available, layer_path, u.sub)):
            return "Your subscription cap on this engine is reached."
    except Exception:  # noqa: BLE001  # a failed subscription-cap check lets the turn proceed
        logger.debug("pool cap gate failed for a key turn", exc_info=True)
    return ""


def feeds_allow_memory(chat_id: str) -> bool:
    """Whether an Allow a person gives on this chat's card may be remembered
    for the session (the permission hook asks before it records one). Not
    while the chat's turn was started by a key that asks: the Allow covers
    that one call, so nothing the key started runs later with no card, and
    with the close in ``_session_ready`` the turn uses no earlier Allow
    either. With no chat known for the card, nothing is remembered while
    any such turn runs."""
    asking = _asking_chats & _held_chats
    return chat_id not in asking if chat_id else not asking


def _sender_facts(sub: str, sid: str) -> tuple[str, str | None]:
    """The sender's username and the session's payer (``spender.session_payer``),
    as one job."""
    from core.config import spender
    return task_store.get_username_by_sub(sub) or "", spender.session_payer(sid)


async def _session_ready(u: UserContext, chat: dict, mode: str) -> None:
    """A live session of the chat that does not run as this person, in the
    chat's mode, as a chat turn, is closed: the ladder then starts one that
    does (the queue's Shared-only rule, on every chat). So is one that
    remembers a tool the person allowed, for a key not made to run without
    asking: that tool would run with no card, and the close forgets it. The
    chat is marked for the turn of such a key, so no Allow given inside it
    is remembered (``feeds_allow_memory``).
    Never under a turn, and never under work the last turn left running: the
    reads here take a moment, so the chat is read again right before the
    close (409 when someone took it meanwhile or that work still runs)."""
    from core.session import session_state
    from core.session.session_delivery import chat_layer
    _asking_chats.intersection_update(_held_chats)
    if u.api_key_runs_unasked:
        _asking_chats.discard(chat.get("id") or "")
    else:
        _asking_chats.add(chat.get("id") or "")
    sid = chat.get("session_id") or ""
    if not sid:
        return
    layer = await chat_layer(chat)
    if layer is None or not await layer.is_session_alive(sid):
        return
    username, payer = await run_db(_sender_facts, u.sub, sid)
    warmed = session_state.get_session_security(sid)
    from core.config import spender
    serves = (warmed is not None and (warmed.username or "") == username
              and spender.serves_payer(payer, spender.Spender(u.sub, "chat")))
    recorded = session_state.recorded_session_mode(sid)
    kind = session_state.get_session_client_type(sid)
    allows = not u.api_key_runs_unasked and session_state.remembers_tool_allows(sid)
    token = await run_db(_holds_a_token_a_key_may_not_use, sid, warmed)
    if serves and recorded == mode and kind == session_kind.DASHBOARD.name and not allows and not token:
        return
    # From this read to the close: no await. Work the last turn left running
    # dies with its session, whatever the reason for the close.
    from core.session import background_leash
    if _taken(chat.get("id") or ""):
        raise HTTPException(409, BUSY)
    if background_leash.background_pending_count(sid):
        raise HTTPException(409, BACKGROUND_BUSY)
    logger.info("key turn: the session of chat=%s is closed to run as its sender "
                "(serves=%s mode=%s kind=%s remembered_allows=%s browser_token=%s)",
                (chat.get("id") or "")[:8], serves, recorded, kind, allows, token)
    await layer.close_session(sid)


def _holds_a_token_a_key_may_not_use(sid: str, warmed) -> bool:
    """A live session that holds its machine owner's own-browser token, on a
    machine whose owner did not let unattended runs use it: a key's turn is
    one, so it never rides such a session. Synchronous."""
    from services.mcp import mcp_server_resolve
    from storage import remote_store
    if warmed is None or not mcp_server_resolve.session_holds_browser_token(sid):
        return False
    browser = remote_store.get_target_browser_settings(warmed.placement)
    return not mcp_server_resolve.token_reaches(browser, unattended=True)


def _has_device_grants(sid: str) -> bool:
    """The session runs on a paired computer whose owner granted device tools."""
    from core.session import session_state
    ctx = session_state.get_session_security(sid) if sid else None
    return ctx is not None and bool(ctx.placement.device_grants)


async def _give_back_the_device(chat_id: str, sid: str) -> None:
    """A key's turn ran on a paired computer whose owner granted device tools:
    its session closes at the turn's end, so the person's next turn builds
    their own (with the grant and the browser token a key's turn went
    without). Not under a person who took the chat meanwhile, and not while
    background work the turn started still runs: that work stays the key's
    (``key_rows``) until the session closes."""
    from core.session import background_leash
    from core.session.session_manager import find_layer_for_session
    if not _has_device_grants(sid):
        return
    layer = find_layer_for_session(sid)
    if layer is None or _taken(chat_id) or background_leash.background_pending_count(sid):
        return
    logger.info("key turn on chat=%s: its session %s closes, its machine's device tools "
                "are its person's", chat_id[:8], sid[:8])
    try:
        await layer.close_session(sid)
    except Exception:  # noqa: BLE001  # the turn ended: a failed close is the reaper's
        logger.warning("key turn on chat=%s: its session was not closed", chat_id[:8], exc_info=True)


def _answer_rows(rows: list[dict]) -> str:
    """The answer of a turn: its assistant rows' text, up to the next
    person's row."""
    parts = []
    for r in rows:
        if r.get("role") == ROLE_USER:
            break
        if r.get("role") == ROLE_ASSISTANT and (r.get("content") or "").strip():
            parts.append(r["content"])
    return "\n\n".join(parts)


def view(turn: KeyTurn, *, answer: str = "", waiting: bool = False) -> dict:
    detail = ""
    if turn.state == RUNNING:
        detail = ("The turn still runs, or waits for room on the server. Read it again with "
                  f"GET /v1/api/chats/{turn.chat_id}/messages/{turn.message_id}.")
        if waiting:
            detail = "The turn waits for your answer in the dashboard. Open the chat to answer."
    elif turn.state == NOT_SENT:
        detail = f"The message was not sent. {turn.refusal}" if turn.refusal else NOT_SENT_DETAIL
    elif turn.state == NO_ANSWER:
        detail = "The turn ended without an answer. It failed or was stopped."
    return {"chat_id": turn.chat_id, "message_id": turn.message_id or None,
            "request_id": turn.request_id, "state": turn.state, "answer": answer,
            "waiting_for_you": waiting, "detail": detail}


async def _waiting(turn: KeyTurn) -> bool:
    from core.session import session_state
    if turn.state != RUNNING:
        return False
    sid = turn.session_id or ((await run_db(task_store.get_chat, turn.chat_id)) or {}).get(
        "session_id") or ""
    return bool(sid) and session_state.has_pending_prompt(sid)


async def answer_of(turn: KeyTurn) -> dict:
    answer = ""
    if turn.state == DONE:
        rows = await run_db(api_key_store.key_turn_rows, turn.chat_id, turn.message_id,
                            turn.end_id or None)
        answer = _answer_rows(rows)
    return view(turn, answer=answer, waiting=await _waiting(turn))


async def answer_from_rows(key_id: str, chat_id: str, message_id: int, request_id: str) -> dict:
    """The answer of a key's message this process did not deliver (a restart
    since, another worker): a live pump on the chat is a turn still running,
    else the rows after the message up to the next person's row."""
    from core.events.stream_pump import _active_pumps
    turn = KeyTurn(key_id=key_id, request_id=request_id, chat_id=chat_id, message_id=message_id)
    pump = _active_pumps.get(chat_id)
    if pump is not None and not pump.is_done:
        return view(turn, waiting=await _waiting(turn))
    rows = await run_db(api_key_store.key_turn_rows, chat_id, message_id)
    text = _answer_rows(rows)
    turn.state = DONE if text else NO_ANSWER
    return view(turn, answer=text)


def find(key_id: str, message_id: int) -> KeyTurn | None:
    return _by_message.get((key_id, message_id))


async def wait_for(turn: KeyTurn, wait_s: float) -> dict:
    if wait_s > 0 and not turn.finished.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(turn.finished.wait()), wait_s)
    return await answer_of(turn)


async def send(u: UserContext, chat: dict, text: str, *, request_id: str, wait_s: float) -> dict:
    """The gate, the row, the turn and the bounded wait (module docstring)."""
    from core.events import input_queue
    cid = chat["id"]
    _prune()
    while (earlier := _turns.get((u.api_key_id, request_id))) is not None:
        if earlier.chat_id != cid:
            raise HTTPException(409, OTHER_CHAT)
        await earlier.written.wait()
        if not earlier.dropped:
            return await wait_for(earlier, wait_s)
    refusal = await run_db(drive_refusal, u, chat)
    if refusal:
        raise HTTPException(409, refusal)
    mode = await run_db(chat_mode, chat)
    if mode == JUDGE_MODE:
        raise HTTPException(409, "This chat cannot take a message.")
    if mode not in ASKING_MODES and not u.api_key_runs_unasked:
        raise HTTPException(403, UNASKED_CHAT)
    refusal = await _budget_refusal(u, chat)
    if refusal:
        raise HTTPException(403, refusal)
    if await run_db(session_manager.no_compute_for, chat):
        # Nothing can run the turn now: said before any row is written, so
        # the caller sends the same request again. While the platform holds
        # new work its compute machine has not signed in yet (a start) or
        # was stopped on purpose (an update, a backup): the hold's own line.
        raise HTTPException(503, drain.refusal(UPDATING) if drain.holding() else placement.NO_COMPUTE_SENTENCE,
                            headers={"Retry-After": str(UPDATING_RETRY_S)})
    await input_queue.loaded(cid)
    # From here to the registration: no await.
    if (u.api_key_id, request_id) in _turns:
        # The same request arrived twice at once: the other one sends.
        return await send(u, chat, text, request_id=request_id, wait_s=wait_s)
    if drain.holding():
        # The platform holds new work for an update. A request sent before
        # it is answered above, or from its rows once the platform is back.
        raise HTTPException(503, drain.refusal(UPDATING), headers={"Retry-After": str(UPDATING_RETRY_S)})
    if busy(cid):
        raise HTTPException(409, BUSY)
    turn = KeyTurn(key_id=u.api_key_id, request_id=request_id, chat_id=cid)
    _turns[(u.api_key_id, request_id)] = turn
    _held_chats.add(cid)
    key_rows.hold(cid, request_id)
    try:
        prior = await run_db(api_key_store.claim_key_request, u.api_key_id, request_id, cid)
        if prior is not None and not prior.get("message_id") and await run_db(
                api_key_store.retake_stale_key_request, u.api_key_id, request_id, cid,
                CLAIM_STALE_S):
            prior = None
    except BaseException:
        _forget(turn)
        raise
    if prior is not None:
        # Sent before this process knew it (a restart since): answered from
        # the rows, never sent again.
        _forget(turn)
        if (prior.get("chat_id") or cid) != cid:
            raise HTTPException(409, OTHER_CHAT)
        if not prior.get("message_id"):
            raise HTTPException(409, "This request is already being sent. Try again in a moment.")
        return await answer_from_rows(u.api_key_id, cid, int(prior["message_id"]), request_id)
    try:
        await _session_ready(u, chat, mode)
        role = await run_db(acting_role_of, u.sub, chat.get("agent") or "")
    except BaseException:
        await run_db(api_key_store.drop_key_request, u.api_key_id, request_id)
        _forget(turn)
        raise
    spawn(_deliver(u, chat, text, turn, role=role, permission_mode=mode,
                   client_type=session_kind.DASHBOARD.name),
          name="api_key_turn", owner="api_public")
    await turn.written.wait()
    if turn.state == NOT_SENT and not turn.message_id:
        return view(turn)
    return await wait_for(turn, wait_s)


def _prune() -> None:
    now = time.monotonic()
    for key, turn in [(k, t) for k, t in _turns.items()
                      if t.finished_at and now - t.finished_at > KEEP_FINISHED_S]:
        _turns.pop(key, None)
        _by_message.pop((turn.key_id, turn.message_id), None)


def _forget(turn: KeyTurn) -> None:
    """Drop a send that wrote no row. A request waiting on it wakes and
    sends again (``send``)."""
    _held_chats.discard(turn.chat_id)
    key_rows.release(turn.chat_id, turn.request_id)
    if not turn.finished.is_set():
        _turns.pop((turn.key_id, turn.request_id), None)
        turn.dropped = True
        turn.written.set()
        turn.finished.set()


async def _role_in_force(turn: KeyTurn, at_send: str) -> str:
    """The role the turn's session runs at, for the log. The rung that
    starts a session reads the person's row on the agent again
    (``wake_session._role_after_wait``), so it may sit below the role read
    at send. That one, marked as such, when the session is gone."""
    from core.session import session_state
    with contextlib.suppress(Exception):
        row = await run_db(task_store.get_chat, turn.chat_id)
        sid = (row or {}).get("session_id") or turn.session_id
        ctx = session_state.get_session_security(sid) if sid else None
        if ctx is not None and ctx.role:
            return str(ctx.role)
    return f"{at_send} at send"


def _row_meta(u: UserContext, request_id: str) -> str:
    return json.dumps({"api_key": {"id": u.api_key_id, "name": u.api_key_name,
                                   "request_id": request_id}})


async def _deliver(u: UserContext, chat: dict, text: str, turn: KeyTurn, *, role: str,
                   permission_mode: str, client_type: str) -> None:
    """The turn: the row first (the ladder's ``persist_event``), then the
    rungs that start a session or use the idle one
    (``chat_rebuild.rebuild_and_resume``), awaited to the turn's end. Role,
    mode and kind are the caller's: the ladder has no default for them."""
    from core.events import chat_writer
    from core.session import chat_rebuild
    from core.session.session_delivery import DeliveryOutcome
    from storage.chat.chat_titles import title_from_prompt
    cid = chat["id"]
    meta = _row_meta(u, turn.request_id)
    title = title_from_prompt(text)
    pending: list[asyncio.Future] = []

    def _write() -> int:
        mid = task_store.add_chat_message(cid, ROLE_USER, text, event_data=meta, author_sub=u.sub)
        if title:
            task_store.set_chat_title_if_unset(cid, title)
        api_key_store.set_key_request_message(turn.key_id, turn.request_id, mid)
        return mid

    def _landed(fut: asyncio.Future) -> None:
        if not fut.cancelled() and fut.exception() is None:
            turn.message_id = fut.result()
            _by_message[(turn.key_id, turn.message_id)] = turn
        turn.written.set()

    def _persist(target_chat_id: str) -> None:
        if target_chat_id == cid and not pending:
            fut = chat_writer.submit(cid, _write, label="api_key_send")
            fut.add_done_callback(_landed)
            pending.append(fut)

    async def _outcome(outcome: DeliveryOutcome) -> None:
        # A fresh start runs under a new id that only the chat's row names:
        # the ladder answers the id it was given.
        row = await run_db(task_store.get_chat, cid) if outcome.path != "none" else None
        turn.session_id = (row or {}).get("session_id") or outcome.session_id or turn.session_id

    key_rows.hold(cid, turn.request_id)
    outcome = DeliveryOutcome("none", chat_id=cid)
    try:
        outcome = await chat_rebuild.rebuild_and_resume(
            cid, text, source="api_key", session_id=chat.get("session_id") or "",
            agent=chat.get("agent") or "", person=u.sub, role=role,
            # The person typed it: the session their own warm-up of the chat
            # builds, in every chat a key reaches (``wake_build`` is the
            # decision for turns nobody typed).
            mode=permission_mode, kind=client_type, build=chat_rebuild.AS_CHAT,
            persist_event=_persist, allow_ws=False, allow_pty=False, allow_live_turn=False,
            fresh_without_session=True, on_outcome=_outcome,
        )
    except placement.PlacementRefused as refused:
        # The role or the machine's rule changed between the door and the
        # build: the caller reads the refusal's own cause.
        turn.refusal = str(refused)
        logger.info("key turn on chat=%s: the build refused its place", cid[:8])
    except Exception:  # noqa: BLE001  # a rung that raises is undone like an undelivered message
        logger.warning("key turn on chat=%s raised", cid[:8], exc_info=True)
    finally:
        # Background work the turn started calls tools after it: still the
        # key's, on a place with device tools (``_give_back_the_device``).
        if _has_device_grants(turn.session_id or outcome.session_id):
            key_rows.mark_session(turn.session_id or outcome.session_id)
        key_rows.release(cid, turn.request_id)
        if pending:
            with contextlib.suppress(Exception):
                await pending[0]
    try:
        # No rung took it: the row goes back, unless rows followed it (a rung
        # that raised mid-turn), and then the turn is read as one that ran.
        unsent = outcome.path == "none" and (not turn.message_id or await chat_writer.submit(
            cid, functools.partial(api_key_store.take_back_key_message, cid, turn.message_id),
            label="api_key_take_back"))
        if unsent:
            await run_db(api_key_store.drop_key_request, turn.key_id, turn.request_id)
            turn.state = NOT_SENT
            logger.info("key turn on chat=%s not sent (key %s)", cid[:8], turn.key_id[:8])
        else:
            turn.end_id = await chat_writer.submit(
                cid, functools.partial(api_key_store.last_chat_row_id, cid), label="api_key_end")
            rows = await run_db(api_key_store.key_turn_rows, cid, turn.message_id, turn.end_id)
            turn.state = DONE if _answer_rows(rows) else NO_ANSWER
            logger.info("key turn on chat=%s ended via %s (key %s, role %s, mode %s)",
                        cid[:8], outcome.path, turn.key_id[:8],
                        await _role_in_force(turn, role), permission_mode)
            await _give_back_the_device(cid, turn.session_id or outcome.session_id)
    except Exception:  # noqa: BLE001  # the answer is read again from the rows
        logger.warning("key turn on chat=%s: its end was not recorded", cid[:8], exc_info=True)
        turn.state = NO_ANSWER
    finally:
        _held_chats.discard(cid)
        turn.finished_at = time.monotonic()
        turn.written.set()
        turn.finished.set()
