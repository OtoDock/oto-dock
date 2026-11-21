"""Removing a chat: the one sequence the delete route and the retention sweep
run. A chat's rows hang on more than the row: its pending wakes, a pump or
session still holding it, a queue that is delivering, the share snapshots and
previews that are not in the row's cascade. The route keeps its own refusals
(a live run, a running meeting, a chat still starting); the sweep skips such a
chat before it gets here (``services/infra/retention.py``)."""

import asyncio
import functools
import logging

from core.events import chat_writer
from storage import database as task_store
from storage.pg import run_db

logger = logging.getLogger("otodock")


async def close_chat_terminal(chat_id: str) -> None:
    """Close a deleted chat's live interactive terminal, so the person's
    session count and the subscription seat are freed with the chat instead
    of at the idle reap. The queued server prompts are dropped, never handed
    back: the chat is going. Bounded re-read: a respawn race can leave two
    live sessions on one chat for a moment. Best-effort, like the pooled
    close below."""
    from core.session import interactive_session
    for _ in range(4):
        live = interactive_session.find_live_for_chat(chat_id)
        if live is None:
            return
        try:
            await interactive_session.close_session(
                live.session_id, reason="chat_deleted", drop_queued=True)
        except Exception:  # noqa: BLE001  # closing a deleted chat's terminal, the next round re-reads
            # The next round re-reads: a close that raised may have left
            # this session registered, or a respawn's twin behind it.
            logger.warning("Chat delete: closing terminal %s failed",
                           live.session_id[:8], exc_info=True)


async def close_chat_session(chat: dict) -> None:
    """Close a deleted chat's live pooled session on whichever layer holds it
    (the engine switch calls this too, with a bare session_id/agent dict).

    Without this, deleting a chat leaves its warm CLI/daemon running headless
    (holding a subscription seat + satellite/local slot) until the idle
    reaper — or longer, when background work keeps extending the reaper's
    leash. Registry membership, not resolution, picks the layer: the chat's
    stored execution_path may be stale relative to where the session actually
    lives. Best-effort — an absent/dead session is fine."""
    session_id = chat.get("session_id") or ""
    if not session_id:
        return
    try:
        from core.session.session_manager import find_layer_for_session
        layer = find_layer_for_session(session_id)
        if layer is None:
            return
        await layer.close_session(session_id)
    except Exception:  # noqa: BLE001  # closing a deleted chat's live session on whatever layer holds it
        logger.warning("Chat delete: closing live session %s failed",
                       session_id[:8], exc_info=True)


async def remove_chat(chat: dict, *, untouched_before: str = "",
                      with_reply_reports: bool = False) -> bool:
    """Everything that goes with a chat, in order. ``untouched_before`` and
    ``with_reply_reports`` are the sweep's (``db_chats.delete_chat``). True
    when the rows went; False when the sweep's guard kept them, and then
    nothing else of the chat is touched."""
    chat_id = chat["id"]
    from core.events import input_queue
    from core.events.stream_pump import _active_pumps
    from services.scheduler import scheduler
    # A deleted chat must never be woken — cancel its pending continuations
    # (row + APScheduler job) before the row goes.
    for cont in await run_db(task_store.list_continuations_for_chat, chat_id):
        await scheduler.remove_dynamic_task(cont["id"])
    # From here to the row delete landing, nothing starts on the chat and
    # no delivery runs on it; its pump ends first (its rows and turn-end
    # job queue on the lane ahead of the delete), then the session goes.
    # The mark outlives a landed delete a while (a slow viewer's loop asks
    # after its ``done``) and is lifted by a failed one, whose queue is
    # reloaded from the rows the cascade never took.
    input_queue.mark_deleting(chat_id)
    try:
        pump = _active_pumps.get(chat_id)
        if pump is not None:
            await pump.end_for_delete()
        await close_chat_terminal(chat_id)
        await close_chat_session(chat)
        # The chat's share rows cascade with it; their snapshot copies do not
        # (SHARING.md): take them first, remove them after. The row delete
        # rides the chat's writer lane so it lands behind any row job of the
        # chat still queued (a turn ending right now) instead of racing it.
        from services.sharing import chat_snapshot
        from storage.sharing import share_store
        doomed = await run_db(share_store.list_target_shares, "chat", chat_id)
        removed, bound, cache = await chat_writer.submit(
            chat_id, functools.partial(
                task_store.delete_chat_bound, chat_id,
                untouched_before=untouched_before, with_reply_reports=with_reply_reports,
            ), label="delete")
    except BaseException:
        input_queue.unmark_deleting(chat_id, landed=False)
        raise
    kept = bool(untouched_before) and not removed
    input_queue.unmark_deleting(chat_id, landed=not kept)
    if kept:
        return False
    if bound and bound != (chat.get("session_id") or ""):
        # A start that bound its session after the caller read the row (a
        # warm-up's bind landing late) left it out of the close above.
        await close_chat_session({"session_id": bound})
    for share in doomed:
        await asyncio.to_thread(chat_snapshot.remove, share)
    from services.media import media_pipeline, preview_snapshots
    await asyncio.to_thread(media_pipeline.remove_cache_copies, cache)
    await asyncio.to_thread(preview_snapshots.delete_chat_dir, chat_id)
    return True


def delete_rows(chat_id: str, *, untouched_before: str = "",
                with_reply_reports: bool = False) -> tuple[bool, str]:
    """A chat's rows (``db_chats.delete_chat_bound``), then the copies of its
    cache-owned media, for a caller that removes a chat outside
    :func:`remove_chat` (a phone conversation, a chat whose start failed).
    ``(removed, session id)``. Blocking: call it in a worker thread."""
    from services.media import media_pipeline
    removed, bound, cache = task_store.delete_chat_bound(
        chat_id, untouched_before=untouched_before, with_reply_reports=with_reply_reports)
    media_pipeline.remove_cache_copies(cache)
    return removed, bound
