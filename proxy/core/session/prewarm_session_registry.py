"""Fast TTL reaper for unused pre-warmed sessions.

A pre-warm spawns a real session (CLI + MCP tree) holding a concurrency slot +
subscription BEFORE the user has sent anything. The WS-disconnect path closes an
orphaned pre-warm immediately, but a user who pre-warms then sits on another page
while still connected would otherwise hold that slot for the full idle window.
This registry reaps an *unclaimed* pre-warm after a short TTL.

Race-safety: ``claim`` and ``reap_stale`` both mutate the registry under one lock,
and an entry is removed exactly once — so the reuse path (claim) and the reaper can
never both act on the same session (no use-after-close). The reuse path calls
``claim(sid)`` as its FINAL gate before reusing a pre-warm; if it returns False the
entry was already reaped, so the caller must spawn fresh. The reaper closes through
the execution layer (which frees the subscription AND the concurrency slot).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("claude-proxy.prewarm")

# How long an unclaimed pre-warm may live before the reaper closes it. Short — the
# whole point is to release the slot quickly when a user pre-warms then doesn't send.
PREWARM_TTL_S = 120.0


@dataclass
class _Entry:
    agent: str
    user_sub: str
    role: str
    exec_path: str
    ts: float
    model: str = ""


_entries: dict[str, _Entry] = {}
_lock = asyncio.Lock()


async def register(session_id: str, *, agent: str, user_sub: str = "",
                   role: str = "manager", exec_path: str = "",
                   model: str = "") -> None:
    """Record a freshly pre-warmed (not-yet-claimed) session as reapable."""
    async with _lock:
        _entries[session_id] = _Entry(
            agent=agent, user_sub=user_sub, role=role or "manager",
            exec_path=exec_path, ts=time.monotonic(), model=model,
        )


async def claim(session_id: str) -> bool:
    """Atomically take a pre-warm out of the reapable set. Returns True if it was
    still present (the caller may reuse the session), False if it was already
    reaped / never registered (the caller must spawn fresh)."""
    async with _lock:
        return _entries.pop(session_id, None) is not None


async def claim_by_key(*, user_sub: str, agent: str, model: str,
                       role: str, exec_path: str) -> tuple[str, float] | None:
    """Atomically claim a pre-warm by its full match key — the recovery path
    for DETACHED pre-warms (wake-word detection fires an HTTP pre-warm with no
    WS connection to remember the sid on). Returns ``(session_id, age_s)`` or
    None. Model is part of the key because CLI sessions spawn with ``--model``
    and can't swap; role because the SecurityContext is baked at start."""
    async with _lock:
        for sid, e in _entries.items():
            if (e.user_sub == user_sub and e.agent == agent
                    and e.model == model and e.role == (role or "manager")
                    and e.exec_path == exec_path):
                _entries.pop(sid, None)
                return sid, time.monotonic() - e.ts
    return None


async def discard(session_id: str) -> None:
    """Drop a pre-warm from the registry without reaping — it's being closed or
    replaced by another path (WS disconnect, pre-warm replacement). Idempotent."""
    async with _lock:
        _entries.pop(session_id, None)


async def reap_stale(ttl: float = PREWARM_TTL_S) -> int:
    """Close pre-warms unclaimed for longer than ``ttl``. The close goes through the
    execution layer, which releases the subscription AND the concurrency slot.
    Returns the number reaped."""
    now = time.monotonic()
    async with _lock:
        stale = [(sid, e) for sid, e in _entries.items() if now - e.ts >= ttl]
        for sid, _ in stale:
            _entries.pop(sid, None)  # removed under the lock → no longer claimable
    reaped = 0
    for sid, e in stale:  # close OUTSIDE the lock (async, and these are ours alone)
        try:
            from core.session.session_manager import get_execution_layer
            layer = get_execution_layer(
                e.agent, execution_path=e.exec_path or None,
                user_sub=e.user_sub or None, role=e.role or "manager",
            )
            await layer.close_session(sid)
            reaped += 1
            logger.info("prewarm: reaped unused pre-warm %s (agent=%s, idle>%.0fs)",
                        sid[:8], e.agent, ttl)
        except Exception as ex:  # noqa: BLE001
            logger.warning("prewarm: failed to reap %s: %s", sid[:8], ex)
    return reaped


async def reap_loop(period: float = 30.0) -> None:
    """Background sweep — reap stale pre-warms every ``period`` seconds."""
    while True:
        await asyncio.sleep(period)
        try:
            await reap_stale()
        except Exception as ex:  # noqa: BLE001
            logger.error("prewarm reap loop error: %s", ex)
