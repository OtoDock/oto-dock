"""The dashboard socket's side of moving a chat and of the placement lock.

The move itself is ``services/remote/chat_move.py``'s; this module adapts it
to a ``DashboardConnection`` (``ws/dashboard_dispatch.py`` keeps the calls):

- ``handle_move_chat``: the ``move_chat`` frame (``target`` optional, the
  run person's default when absent; ``from`` the place the page showed).
  The outcome reaches every viewing socket as ``chat_moved``, this one
  included, so a refusal is the only direct answer.
- ``drive_refusal``: the lock's sentence for a frame that drives a chat
  whose live session sits on another person's computer (cached briefly:
  every driving frame asks).
- ``before_drive``: the lock, then the hand-over: a chat whose place the
  sender may not use moves to their default first, and the message waits
  in the chat's queue behind the move (the move holds the chat's claim).
"""

from __future__ import annotations

import functools
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from auth import roles
from core import placement
from services.remote import chat_move
from storage import database as task_store, move_schema
from storage.pg import run_db
from ws import wire_events as wire

if TYPE_CHECKING:
    from ws.dashboard_host import DashboardHost

logger = logging.getLogger("otodock")

# (chat id, sub) -> (monotonic time, sentence): the lock asked per frame.
_LOCK_TTL_S = 2.0
_lock_cache: dict[tuple[str, str], tuple[float, str]] = {}


async def asker_of(conn: DashboardHost, agent: str) -> chat_move.Asker:
    from auth.providers import acting_role_of
    role = await run_db(acting_role_of, conn.user_sub, agent, fallback_user=conn.user)
    return chat_move.Asker(sub=conn.user_sub or "", role=role, platform_admin=roles.is_admin(role))


NEW_CHAT_ELSEWHERE = "You can't start this chat there."


async def new_chat_place(conn: DashboardHost, agent: str, wanted: str,
                         exec_path: str) -> tuple[str, str, str, str]:
    """The place a new chat's ``target`` names, checked as the menu offers it
    to this person (``chat_targets.agent_view``: the guard, the reach, the
    engine): ``(place, label, default, refusal)``. ``place`` is ``""`` when
    nothing was picked or the pick is the person's default (today's start,
    the resolver's own fallback kept); a target is an id, never a name."""
    wanted = (wanted or "").strip() if isinstance(wanted, str) else ""
    if not wanted:
        return "", "", "", ""
    asker = await asker_of(conn, agent)
    from services.remote import chat_targets
    view = await run_db(lambda: chat_targets.agent_view(
        agent, asker.sub or None, asker.role, admin_asker=asker.platform_admin,
        engine_path=exec_path))
    default = view["default_target"]
    entry = next((t for t in view["targets"] if t["target"] == wanted), None)
    if entry is None:
        return "", "", default, NEW_CHAT_ELSEWHERE
    if not entry["pickable"]:
        return "", "", default, entry["why_not"] or NEW_CHAT_ELSEWHERE
    if entry["target"] == default:
        return "", entry["label"], default, ""
    return entry["target"], entry["label"], default, ""


def picked_place(msg: dict) -> str:
    """The place a new chat's frame names (``target``), as sent: the key a
    pre-warm is matched on (``""``: no pick)."""
    raw = msg.get("target")
    return raw.strip() if isinstance(raw, str) else ""


async def prewarm_place(conn: DashboardHost, agent: str, wanted: str,
                        exec_path: str) -> str | None:
    """The place a pre-warm builds on (``""``: the resolver's), or ``None``
    when it pre-warms nothing: a refused pick, or a machine (a pre-warm
    never spawns on a machine)."""
    if not wanted:
        return ""
    place, _label, _default, refusal = await new_chat_place(conn, agent, wanted, exec_path)
    if refusal or (place and not placement.is_local(place)):
        logger.info("WS dashboard pre_warmup: skipped (the new chat's place is %s) agent=%s",
                    "refused" if refusal else "a machine", agent)
        return None
    return place


async def place_new_chat(conn: DashboardHost, agent: str, msg: dict, exec_path: str,
                         role: str) -> tuple[str, str, str] | None:
    """A warmup's check of the place its page picked (``msg["target"]``),
    before the mint: ``(place, label, default)``, or ``None`` once a refusal
    is answered (``warmup_failed``, no chat minted). A pick other than the
    default runs the warmup on the pick's layer (``conn.layer``)."""
    place, label, default, refusal = await new_chat_place(
        conn, agent, msg.get("target") or "", exec_path)
    if refusal:
        logger.info("WS dashboard warmup refused before the mint (the place) agent=%s: %s",
                    agent, refusal)
        await conn._send({"type": wire.WARMUP_FAILED, "error": refusal,
                          "reason": wire.REASON_TARGET_UNAVAILABLE})
        return None
    if place:
        from core.session.session_manager import get_execution_layer
        conn.layer = await run_db(functools.partial(
            get_execution_layer, agent, execution_path=msg.get("execution_path", ""),
            user_sub=conn.user_sub, role=role, execution_target=place))
    return place, label, default


def mint_new_chat(chat_id: str, owner: str, agent: str, permission_mode: str, *, model: str,
                  execution_path: str, execution_mode: str, placed: tuple[str, str, str],
                  actor: str) -> None:
    """A new chat's row; on the place its person picked (``placed``, from
    ``place_new_chat``) the row is pinned there and the placement recorded
    as a settled move with nothing carried (the row that makes the place a
    person's choice: no mismatch banner, a fresh spawn honours it, a
    personal computer's lock window). One chat-lane job. Synchronous."""
    task_store.create_chat(chat_id, owner, agent, permission_mode, model=model,
                           execution_path=execution_path, execution_mode=execution_mode)
    place, label, default = placed
    if not place:
        return
    from services.remote import chat_targets
    from storage import remote_store
    task_store.update_chat(chat_id, execution_target=place)
    now = datetime.now(timezone.utc).isoformat()
    source = default or placement.LOCAL
    remote_store.create_chat_move({
        "id": str(uuid.uuid4()), "chat_id": chat_id, "agent": agent, "engine": execution_path,
        "source": source, "target": place, "from_label": chat_targets.label_of(source),
        "to_label": label, "memory": move_schema.MEMORY_SUMMARY, "why": move_schema.WHY_PLACED,
        "state": move_schema.SETTLED, "actor": actor, "origin": move_schema.ORIGIN_CHIP,
        "created_at": now, "settled_at": now,
    })
    logger.info("WS dashboard: new chat %s placed on %s by %s", chat_id[:8], label, actor[:16])


def voice_attached(chat_id: str) -> str:
    """A spoken conversation holds the chat: it stays where it runs."""
    from ws import duplex
    return chat_move.VOICE_IN_USE if duplex.live_for_chat(chat_id) is not None else ""


async def handle_move_chat(conn: DashboardHost, msg: dict) -> None:
    """Move the OPEN chat (the socket's own) to ``msg['target']``."""
    cid = conn.chat_id or ""
    chat = await run_db(task_store.get_chat, cid) if cid else None
    if not chat:
        await conn._send({"type": wire.ERROR, "message": chat_move.NOT_FOUND})
        return
    asker = await asker_of(conn, chat.get("agent") or "")
    target = str(msg.get("target") or "default")
    expect_from = msg.get("from")
    logger.info("WS dashboard: move_chat requested chat=%s -> %s", cid[:8], target[:16])
    try:
        await chat_move.request_move(
            cid, target, asker=asker,
            expect_from=str(expect_from) if isinstance(expect_from, str) and expect_from else None,
            origin=move_schema.ORIGIN_SOCKET, blocked=voice_attached)
    except chat_move.MoveRefused as e:
        await conn._send({"type": wire.ERROR, "message": str(e)})
        return
    # The move closes the session this socket held; the outcome frame makes
    # the page open the chat again where it went.
    conn.session_id = None
    _lock_cache.pop((cid, conn.user_sub or ""), None)


async def drive_refusal(conn: DashboardHost, chat_id: str | None) -> str:
    """The lock's sentence when this person may not drive ``chat_id`` now."""
    sub = getattr(conn, "user_sub", "") or ""
    if not chat_id or not sub:
        return ""
    key = (chat_id, sub)
    hit = _lock_cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _LOCK_TTL_S:
        return hit[1]

    def _job() -> str:
        chat = task_store.get_chat(chat_id)
        return chat_move.lock_sentence(chat, sub) if chat else ""

    sentence = await run_db(_job)
    if len(_lock_cache) > 4096:
        _lock_cache.clear()
    _lock_cache[key] = (now, sentence)
    return sentence


async def before_drive(conn: DashboardHost, chat_id: str | None) -> bool:
    """Before a frame that would warm or drive ``chat_id``: False when it was
    answered here (the lock's sentence sent), True when the frame goes on: as
    it is, or behind a hand-over move this call started (its message then
    waits in the chat's queue: the move holds the chat's claim)."""
    if not chat_id:
        return True
    sentence = await drive_refusal(conn, chat_id)
    if sentence:
        await conn._send({"type": wire.ERROR, "message": sentence})
        return False
    chat = await run_db(task_store.get_chat, chat_id)
    if not chat:
        return True
    asker = await asker_of(conn, chat.get("agent") or "")
    try:
        moved = await chat_move.hand_over(chat_id, asker=asker)
    except chat_move.MoveRefused as e:
        await conn._send({"type": wire.ERROR, "message": str(e)})
        return False
    if moved is not None:
        logger.info("WS dashboard: chat %s hands over to %s for %s", chat_id[:8],
                    moved.get("to_label", "?"), (conn.user_sub or "")[:16])
        conn.session_id = None
    return True
