"""In-flight warmup registry — fan-out + history replay across WS reconnects.

Each entry is keyed by `chat_id` and holds the set of listeners (each is an
async ``send(payload: dict)`` callable bound to a dashboard WS) plus a
bounded history of emitted events.

Why this exists: warmup on a fresh remote satellite can take ~90s while MCPs
install. During that window the dashboard's WS may drop (mobile network flap,
app backgrounded) and reconnect with a new connection. Without this registry
the eventual ``warmup_ready`` event would be written to the dead socket. With
it, the new WS sends ``resume_chat`` for the in-flight chat_id, the handler
re-attaches as a listener, the history is replayed, and the next ``emit()``
fans out to the new socket.

Module-level state:
  _inflight: dict[chat_id, InflightWarmup]   guarded by _lock

Sweeper task in app.py removes entries with completed.is_set() or older than
600s. Belt-and-suspenders for any path that forgets to unregister.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger("warmup-registry")

# Event history bounded so a long install can't grow unbounded. One install
# emits ~5 events per MCP; 10 MCPs ≈ 50 events. Replay sends the full history
# so the reconnecting client renders the same final state.
_HISTORY_MAX = 50

# Entries older than this are swept even if no one called unregister(). The
# sweeper is best-effort; production paths use the try/finally in
# _handle_warmup. Matches the sync_mcps timeout ceiling (mcp_sync.py:152).
_SWEEP_AGE_SECONDS = 600

SendFn = Callable[[dict], Awaitable[None]]


@dataclass
class InflightWarmup:
    chat_id: str
    user_sub: str
    agent: str
    started_at: float = field(default_factory=time.monotonic)
    last_emit_ts: float = field(default_factory=time.monotonic)
    listeners: set = field(default_factory=set)
    event_history: list[dict] = field(default_factory=list)
    completed: asyncio.Event = field(default_factory=asyncio.Event)


_inflight: dict[str, InflightWarmup] = {}
_lock = asyncio.Lock()


async def register(chat_id: str, user_sub: str, agent: str) -> InflightWarmup:
    """Register a new in-flight warmup. Idempotent — returns existing entry
    if one is already in flight for this chat_id (rare: race between
    _handle_warmup and an immediate _handle_resume_chat).
    """
    async with _lock:
        rec = _inflight.get(chat_id)
        if rec is None:
            rec = InflightWarmup(chat_id=chat_id, user_sub=user_sub, agent=agent)
            _inflight[chat_id] = rec
        return rec


async def attach_listener(chat_id: str, send_fn: SendFn) -> InflightWarmup | None:
    """Attach a WS send-callable as a listener. Returns the entry (so caller
    can replay event_history) or None if no in-flight warmup exists.
    """
    async with _lock:
        rec = _inflight.get(chat_id)
        if rec is None:
            return None
        rec.listeners.add(send_fn)
        return rec


async def detach_listener(chat_id: str, send_fn: SendFn) -> None:
    """Remove a listener. Safe if already detached or entry gone."""
    async with _lock:
        rec = _inflight.get(chat_id)
        if rec is not None:
            rec.listeners.discard(send_fn)


async def emit(chat_id: str, event: dict) -> None:
    """Fan out an event to all attached listeners, append to history.

    Each listener's send_fn is already exception-safe (the dashboard _send
    wrapper swallows). We snapshot the listener set under the lock then
    release it before awaiting the sends to avoid holding the lock across
    network I/O.
    """
    async with _lock:
        rec = _inflight.get(chat_id)
        if rec is None:
            # Late emit after unregister — drop. Common for terminal events
            # whose unregister fires before the await completes.
            return
        rec.event_history.append(event)
        if len(rec.event_history) > _HISTORY_MAX:
            rec.event_history = rec.event_history[-_HISTORY_MAX:]
        rec.last_emit_ts = time.monotonic()
        listeners = list(rec.listeners)

    for send_fn in listeners:
        try:
            await send_fn(event)
        except Exception:
            logger.exception("listener send_fn raised for chat=%s", chat_id[:8])


async def unregister(chat_id: str) -> None:
    """Remove the in-flight warmup entry. Sets completed event so the
    sweeper can also clean it up if a caller forgets. Safe if entry gone.
    """
    async with _lock:
        rec = _inflight.pop(chat_id, None)
        if rec is not None:
            rec.completed.set()


def get(chat_id: str) -> InflightWarmup | None:
    """Synchronous read — used in fast-paths like the resume_chat handler
    to decide whether to attach. Race-free reads aren't necessary; a stale
    None just means the caller falls through to the normal session path.
    """
    return _inflight.get(chat_id)


def inflight_chat_ids() -> list[str]:
    """Chat ids with a warmup in flight right now — the warming set for the
    ``/v1/chats/active`` widget seed. Synchronous snapshot (same contract as
    ``get()``: a stale read only costs one transient row). Callers must
    re-derive per-viewer visibility from the CHAT ROW — ``user_sub`` on the
    entry is the WARMER, not the audience (a shared-agent chat warmed by one
    user must surface for every assigned user)."""
    return list(_inflight.keys())


async def sweep_stale() -> int:
    """Remove entries that are completed or older than _SWEEP_AGE_SECONDS.

    Returns count removed (for logging). Called periodically from a
    background task in proxy/app.py.
    """
    now = time.monotonic()
    removed = 0
    async with _lock:
        for cid in list(_inflight.keys()):
            rec = _inflight[cid]
            if rec.completed.is_set() or now - rec.started_at > _SWEEP_AGE_SECONDS:
                _inflight.pop(cid, None)
                removed += 1
    if removed:
        logger.info("warmup_registry: swept %d stale entries", removed)
    return removed
