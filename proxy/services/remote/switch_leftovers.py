"""The session files an engine switch leaves behind.

A switch to another engine restarts the chat from its history and drops the
old engine's ids from the row (``rebind_chat_for_engine_switch``), so that
engine's session (Claude's transcript and the folder beside it, Codex's
rollout) is never resumed again and nothing would ever find it: retention
walks the ids a row names. ``record`` keeps what the switch dropped, the old
engine, its ids, the scope home and the place the chat ran then, as an
``engine_switch_leftovers`` row; ``sweep`` (the reaper's 60 s loop, beside
the move's proof sweep) removes the files there through
``session_files.delete_files`` once the new engine has answered in the chat
and that turn ended well (``answered_well``): no turn is open on the chat,
and no turn's ending row (``turn_ended``: an error, a limit, a stop, a
restart that cut it) follows the last answer since the switch. Text a turn
saves while it still runs (a steer's early save, a meeting's, a long turn's
checkpoints) is no answer yet. A switch whose new engine never answers well
keeps the files. Every row ends after ``WINDOW``, answered or not (a machine
that never comes back, or never updates), and then the files stay. A
session the chat names again, or that another chat or a task still names,
stays too. A delete waits ``DELETE_WAIT_S`` at most: a machine that reads
as reachable and does not answer is asked again later, never holding the
reaper's loop.

The daily retention sweep's orphan pass (``services/infra/retention.py``)
removes an unreferenced transcript a week after its last write anyway, but
not the folder beside a Claude transcript; this removes both as soon as the
new engine has proved itself.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from core import placement

logger = logging.getLogger("otodock.remote")

#: How long a leftover lives: past it the row goes and the files stay.
WINDOW = timedelta(days=30)
#: The per-row back-off between two looks: the first wait and the longest.
BACKOFF_S = (60.0, 900.0)
#: The longest the sweep waits for one place's delete (the satellite's own
#: answer may take 120 s); a row that runs past it is asked again later.
DELETE_WAIT_S = 20.0


def record(chat: dict, old_engine: str, asker_sub: str, asker_role: str) -> bool:
    """Record the session files ``chat`` (its row as the switch read it,
    before the rebind) leaves on ``old_engine``. Nothing to record when the
    engine keeps no session files or the row named no id. Synchronous: call
    it on the DB executor."""
    from core.remote import session_files
    from core.session.session_manager import get_layer_capabilities
    from services.remote import chat_move, chat_targets
    from storage import remote_store
    caps = get_layer_capabilities(old_engine)
    if caps is None or not caps.runtime.config_dir_name:
        return False
    scope = session_files.SessionScope(
        agent=chat.get("agent") or "", engine=old_engine, home_user="",
        session_id=chat.get("session_id") or "", resume_handle=chat.get("codex_thread_id") or "")
    if not scope.refs:
        return False
    person = chat_targets.run_person_of_chat(chat, asker_sub, asker_role)
    remote_store.add_switch_leftover({
        "id": str(uuid.uuid4()), "chat_id": chat.get("id") or "", "agent": scope.agent,
        "engine": old_engine, "session_id": scope.sid, "resume_handle": scope.handle,
        "home_user": chat_move.home_user_of(chat, person),
        "place": chat_targets.current_target(chat),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return True


# Leftover id -> (monotonic time of its next look, the wait after it).
_next: dict[str, tuple[float, float]] = {}


def _later(leftover_id: str) -> None:
    first, longest = BACKOFF_S
    wait = min(_next.get(leftover_id, (0.0, first / 2))[1] * 2, longest)
    _next[leftover_id] = (time.monotonic() + wait, wait)


def _place_ready(place: str) -> bool | None:
    """True when the place can take the delete now, False when it should be
    asked later, None when it is gone for good (a deleted machine)."""
    if placement.is_local(place):
        return True
    from core.remote.satellite_connection import get_connection_manager
    from services.remote.remote_status import is_reachable
    from storage import remote_store
    if remote_store.get_remote_machine(place) is None:
        return None
    return is_reachable(place) and get_connection_manager().satellite_supports_session_move(place)


async def _drop(leftover_id: str) -> None:
    from storage import remote_store
    from storage.pg import run_db
    await run_db(functools.partial(remote_store.drop_switch_leftover, leftover_id))
    _next.pop(leftover_id, None)


def answered_well(chat: dict, since: str) -> bool:
    """Whether the chat's agent has answered since ``since`` and that turn
    ended well: no turn open on the chat now (``chat_move.open_turn``: a
    pump, a warmup, a delivery, a terminal turn, a running task), and no
    ``turn_ended`` row after the last answer written since then.
    Synchronous: call it on the DB executor."""
    import json

    from services.remote import chat_move
    from storage import remote_store
    from ws import wire_events as wire
    if chat_move.open_turn(chat):
        return False
    found = remote_store.last_answer_since(chat.get("id") or "", since)
    if found is None:
        return False
    for raw in found[1]:
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("subtype") == wire.SUBTYPE_TURN_ENDED:
            return False
    return True


async def sweep() -> None:
    """Remove the files of every switch whose new engine has answered."""
    from core.remote import session_files
    from storage import database as task_store, remote_store
    from storage.pg import run_db
    rows = await run_db(remote_store.switch_leftovers)
    live = {r["id"] for r in rows}
    for lid in [k for k in _next if k not in live]:
        _next.pop(lid, None)
    now = datetime.now(timezone.utc)
    for row in rows:
        lid = row["id"]
        if time.monotonic() < _next.get(lid, (0.0, 0.0))[0]:
            continue
        scope = session_files.SessionScope(
            agent=row["agent"], engine=row["engine"], home_user=row["home_user"],
            session_id=row["session_id"], resume_handle=row["resume_handle"])
        try:
            if now - datetime.fromisoformat(row["created_at"]) > WINDOW:
                logger.info("engine switch of chat %s: given up after %d days; the %s session stays",
                            row["chat_id"][:8], WINDOW.days, row["engine"])
                await _drop(lid)
                continue
            chat = await run_db(task_store.get_chat, row["chat_id"])
            if chat and {chat.get("session_id"), chat.get("codex_thread_id")} & set(scope.refs):
                # The chat names the old session again: it is no leftover.
                await _drop(lid)
                continue
            if chat and not await run_db(answered_well, chat, row["created_at"]):
                _later(lid)
                continue
            ready = await run_db(_place_ready, row["place"])
            if ready is None:
                await _drop(lid)
                continue
            if not ready:
                _later(lid)
                continue
            # Any row that still names the session (another chat, a task that
            # continues it or reports to it): retention's own reference set.
            if set(scope.refs) & await run_db(task_store.get_all_session_refs):
                logger.info("engine switch of chat %s: the %s session stays, something else names it",
                            row["chat_id"][:8], row["engine"])
                await _drop(lid)
                continue
            try:
                gone = await asyncio.wait_for(session_files.delete_files(row["place"], scope),
                                              timeout=DELETE_WAIT_S)
            except asyncio.TimeoutError:
                logger.info("engine switch of chat %s: %s did not answer the delete in %.0f s; "
                            "asked again later", row["chat_id"][:8], row["place"][:8], DELETE_WAIT_S)
                _later(lid)
                continue
            logger.info("engine switch of chat %s: %d files of its %s session removed from %s",
                        row["chat_id"][:8], gone, row["engine"],
                        "the platform" if placement.is_local(row["place"]) else row["place"][:8])
            await _drop(lid)
        except session_files.TransferError as e:
            logger.info("engine switch of chat %s: files not removed yet: %s", row["chat_id"][:8], e)
            _later(lid)
        except Exception:  # noqa: BLE001  # one row's failure never stops the sweep
            logger.warning("engine switch leftovers: row %s", lid[:8], exc_info=True)
            _later(lid)
