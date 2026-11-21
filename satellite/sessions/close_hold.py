"""A session's close in flight on this machine, by its id: a start of the
same id waits for it (the satellite's twin of the platform's
``core/session/session_closing``).

A terminal's close is never acknowledged, and a headless close may outlast
the platform's wait for it; both end by removing what the session left by
its id (its home, its prompt file, its session files). A start of the same
id that ran first would lose them a moment after it made them. The close
records itself here, and ``pty_open`` and ``start_session`` await it before
they make anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

# The longest a start waits for a close of its id. A close that hangs past
# it is logged and the start goes on rather than never starting.
WAIT_S = 60.0

_closing: dict[str, set[asyncio.Event]] = {}


@contextlib.asynccontextmanager
async def closing(session_id: str) -> AsyncIterator[None]:
    """The close of ``session_id``, held while it runs."""
    done = asyncio.Event()
    _closing.setdefault(session_id, set()).add(done)
    try:
        yield
    finally:
        done.set()
        held = _closing.get(session_id)
        if held is not None:
            held.discard(done)
            if not held:
                _closing.pop(session_id, None)


async def close_now(session_id: str, session, why: str) -> None:
    """Run ``session``'s own close under its id's hold, for a session
    nothing else closes: a terminal whose engine ended by itself (the
    platform sends no close for one), a start that raised after it made
    things by the id. Its home and the sign-in in it, its secret files and
    its agents go as at any close. Never raises."""
    try:
        async with closing(session_id):
            await session.close()
    except Exception:
        logger.exception("the close of %s failed (%s)", session_id[:8], why)


def runs_unclosed(session_id: str, *registries) -> bool:
    """Whether a session of this machine runs under ``session_id`` (in one
    of ``registries``) and no close of it runs now."""
    return any(session_id in r for r in registries) and not _closing.get(session_id)


async def settled(session_id: str) -> None:
    """Return once no close of ``session_id`` runs (at most ``WAIT_S``)."""
    held = tuple(_closing.get(session_id, ()))
    if not held:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in held)), timeout=WAIT_S)
    except asyncio.TimeoutError:
        logger.warning("a close of %s still runs after %.0f s; the start goes on", session_id[:8], WAIT_S)
