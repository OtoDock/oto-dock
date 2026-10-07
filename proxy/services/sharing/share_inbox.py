"""The ``share_inbox`` frame (SHARING.md "The section"): who hears that a
share changed, and the send.

A share's recipients are read at the moment of the event, from the rows as
they stand: a person share reaches its person; an agent share and a
department share reach the members of every agent they place the app in,
and every platform admin. The frame carries the recipient's pending count and the
agents whose Apps panel changed for them; the dashboard refetches rather
than applying it. Nothing is sent at connect (the panel fetches on open).
"""

from __future__ import annotations

import asyncio
import logging

from storage.automation import notification_store
from storage.sharing import share_store

logger = logging.getLogger("claude-proxy.shares")


def targets_for(share: dict) -> dict[str, set[str]]:
    """``{sub: agents}`` for everyone whose section or panel the share
    touches. Synchronous (store reads): call it on the DB executor."""
    out: dict[str, set[str]] = {}
    kind = share.get("grantee_kind")
    if kind == share_store.PERSON:
        placed = share.get("placed_agent")
        if share.get("grantee_sub"):
            out[share["grantee_sub"]] = {placed} if placed else set()
    elif kind in (share_store.AGENT, share_store.DEPARTMENT):
        agents = share_store.placed_agents_of(share)
        admins = set(notification_store.get_admin_user_subs())
        for sub in share_store.members_of_agents(agents) | admins:
            out.setdefault(sub, set()).update(agents)
    return out


async def send(targets: dict[str, set[str]] | dict[str, list[str]]) -> None:
    """Queue one ``share_inbox`` frame on every dashboard connection of each
    target that holds one right now, with that person's pending count read
    in one store job. Best effort: a failure is logged and the dashboard
    catches up at its next open."""
    from services.notifications import notification_manager as nm
    from ws import wire_events as wire
    connected = {sub: agents for sub, agents in targets.items() if nm.get_all_connections(sub)}
    if not connected:
        return
    try:
        counts = await asyncio.to_thread(share_store.pending_count_for, list(connected))
    except Exception:
        logger.exception("share_inbox: the pending counts could not be read")
        return
    for sub, agents in connected.items():
        frame = {"type": wire.SHARE_INBOX, "pending": counts.get(sub, 0),
                 "agents": sorted(a for a in agents if a)}
        for c in nm.get_all_connections(sub):
            try:
                c.queue.put_nowait(frame)
            except Exception as e:
                logger.debug("share_inbox to %s: %s", c.connection_id[:8], e)
