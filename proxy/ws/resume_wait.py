"""A turn deferred on a resume probe its machine did not answer in time gets
its way out here.

``_resume_dead_session_for_chat`` runs on the socket's own dispatch, so it
waits the inline budget only; a machine that is connected but slow (a
starved daemon, a long read) answers later without ever dropping its socket,
so no reconnect pass runs for it. The turn's queued messages and stored
wakes would sit until a person touched the chat. One watcher per chat waits
on the outstanding probe (``settle_resume`` kept it out), or
``RESUME_REARM_WAIT_S`` at most, then arms the chat's queue again and
redelivers its pending wakes, exactly as the reconnect pass does for a
machine that comes back. A machine that drops meanwhile is that pass's.
"""

from __future__ import annotations

import logging

from core import placement
from core.background import spawn
from core.events import input_queue
from core.session.resume_probe import ProbeWaitEnd, await_resume_answer

logger = logging.getLogger("otodock")

# The chats with a watcher running: a second deferral on the same chat
# while one waits adds nothing.
_watching: set[str] = set()


def watch(layer, session_id: str, chat_id: str, machine_id: str, *,
          agent_name: str = "", username: str = "") -> None:
    """Start the watcher for ``chat_id``'s deferred turn (idempotent while
    one runs)."""
    if not chat_id or chat_id in _watching:
        return
    _watching.add(chat_id)
    spawn(_watch(layer, session_id, chat_id, machine_id, agent_name, username),
          name=f"resume_wait:{chat_id[:8]}", owner="ws")


async def _watch(layer, session_id: str, chat_id: str, machine_id: str,
                 agent_name: str, username: str) -> None:
    from services.scheduler.delivery import redeliver_pending_wakes
    try:
        end = await await_resume_answer(layer, session_id, agent_name=agent_name,
                                        username=username)
        if end is ProbeWaitEnd.DROPPED:
            logger.info("WS dashboard: chat=%s waited on its machine, which dropped; "
                        "the reconnect pass arms it", chat_id[:8])
            return
        logger.info("WS dashboard: chat=%s armed again, its machine %s", chat_id[:8],
                    "answered the resume probe" if end is ProbeWaitEnd.ANSWERED
                    else "still silent past the wait")
        input_queue.clear_waiting(chat_id, input_queue.WAITING_MACHINE)
        input_queue.arm(chat_id)
        await redeliver_pending_wakes(machine_id=machine_id or placement.LOCAL,
                                      session_ids=[session_id])
    except Exception:
        logger.exception("WS dashboard: the resume watcher of chat=%s failed", chat_id[:8])
    finally:
        _watching.discard(chat_id)
