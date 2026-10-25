"""The delivery ladder's fresh-spawn rung: a wake for a dead LOCAL chat whose
session left no conversation to resume (its file was removed, or never
written before the process died) starts a fresh session on the chat, with
the chat's history digest ahead of the wake, instead of waiting stored until
a person opens the chat.

Only a local answer is definite (the file is checked on this host); a remote
machine's "no" can be a failed probe, so a remote chat keeps the
skip-and-store, and its reconnect's sweep resumes it with full context. An
engine that rebuilds its history from the database never needs the rung (the
one-shot resumes it under the same id).

Order (each step owns its undo): the new id is minted before the admission
reserve, so the reserve, the superseded check and the release all name it;
the chat's claim is taken before anything on the chat changes; the chat is
repointed and its digest consumed under the claim; the echo turn's pump
registration releases it. A chat busy at the claim closes the new session
and the wake is stored, with the chat untouched.
"""

from __future__ import annotations

import logging

from core import placement
from storage import database as task_store
from ws import wire_events as wire

logger = logging.getLogger("otodock")


def applies(session_id: str, target: str, chat_row: dict | None) -> bool:
    """The wake's chat is LOCAL and still runs ``session_id`` (the caller
    has ruled out an engine that rebuilds its history from the database)."""
    if target != placement.LOCAL or not chat_row:
        return False
    return (chat_row.get("session_id") or "") == session_id


def applies_without_session(session_id: str, chat_row: dict | None) -> bool:
    """A chat with no session at all (a move summarised it): nothing exists
    to resume anywhere, so the answer is definite on every place. Asked only
    by a caller that opts in (the platform's resume after the agent's own
    move)."""
    return bool(chat_row) and not session_id and not (chat_row or {}).get("session_id")


async def run_echo(layer, new_sid: str, old_sid: str, chat_id: str, agent: str,
                   result_prompt: str, *, person: str) -> str | None:
    """The wake's turn on the started fresh session: "" when it ran, None
    when the chat was busy (the new session is closed; the caller stores the
    wake). ``old_sid`` empty: the chat had no session, and its pending seed
    (a summarised move's) stays the one its fresh turn consumes."""
    from core.events import input_queue
    from core.events.stream_pump import _active_pumps
    from core.session.history_seed import consume_pending_seed
    from services.engines import subscription_pool
    from services.scheduler.lanes import _run_echo_turn_pumped
    from storage.pg import run_db

    q = input_queue.get(chat_id)
    claim = q.try_claim()
    try:
        if claim is None or _active_pumps.get(chat_id) is not None:
            await _close(layer, new_sid)
            return None
        seed = wire.SEED_RESUME_FAILED
        if not old_sid:
            seed = ((await run_db(task_store.get_chat, chat_id)) or {}).get(
                "pending_history_seed") or ""
        await run_db(task_store.update_chat, chat_id, session_id=new_sid,
                     codex_thread_id=None, context_used=0, pending_history_seed=seed)
        if old_sid:
            subscription_pool.release_subscription(old_sid)
        prompt, _notice = await run_db(consume_pending_seed, chat_id, result_prompt)
        logger.info("Wake on chat %s: session %s left no "
                    "conversation, continued on a fresh session %s",
                    chat_id[:8], old_sid[:8] or "(none)", new_sid[:8])
        out = await _run_echo_turn_pumped(layer, new_sid, chat_id, agent, prompt,
                                          person=person, claim=claim)
        if out is None:
            # Not run after all (a starter that takes no claim registered a
            # pump meanwhile): the chat goes back to its own session (or to
            # none), and its next turn restores the history instead.
            await run_db(task_store.update_chat, chat_id, session_id=old_sid or None,
                         pending_history_seed=seed)
            await _close(layer, new_sid)
        return out
    finally:
        q.release(claim)


async def _close(layer, session_id: str) -> None:
    try:
        await layer.close_session(session_id)
    except Exception:  # noqa: BLE001  # closing a fresh session the wake did not use
        logger.warning("Wake: closing the unused fresh session %s failed",
                       session_id[:8],
                       exc_info=True)
