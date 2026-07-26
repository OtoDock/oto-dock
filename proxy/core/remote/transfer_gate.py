"""Global outbound-transfer gate (Feature F, 1.4.0).

Bounds how many LARGE proxy→satellite pushes run at once ACROSS ALL machines
and both push paths — the live fan-out (``workspace_fanout.fan_out_write``)
and the initial-sync push branch (``remote_workspace_sync``). Per-machine
pacing already exists (PUSH_WINDOW_CHUNKS ack windows + the two-lane
control-first writer); this caps the AGGREGATE so "N machines × one 1GB
file" can't hold N in-flight windows at once.

Config: ``OTODOCK_SYNC_FANOUT_CONCURRENCY`` (default 3; 0 = unlimited,
gate fully disabled) and ``OTODOCK_SYNC_FANOUT_MIN_MB`` (default 4):
pushes SMALLER than the threshold bypass the gate entirely — a small file
is a handful of 512KB frames already bounded by the bulk queue, and gating
it would head-of-line-block live edits behind bulk transfers. 4MB matches
``_DEFER_PULL_MIN_BYTES`` (the codebase's "big enough to move off the hot
path" constant); a 4MB push always completes within ONE ack window.

LOCK-ORDER INVARIANT — THE GATE IS INNERMOST. Established order::

    sync_lock(machine,agent) → _window(8) → path lock(agent,rel) → GATE
        → push windows/acks

A gate holder only awaits bulk-queue puts and ack futures (bounded by the
push timeout; deregister rejects pending futures) — it never acquires any
outer lock, so the wait-for graph is acyclic. Do NOT acquire the gate
around anything that takes a sync/path lock. ``asyncio.Semaphore`` wakes
waiters FIFO → starvation-free.

Registry-agnostic: ``slot`` takes an optional async ``on_state`` callback
(state string) so tracked fan-outs can surface 'queued'/'active' in the
transfer registry without this module importing it. Callbacks are
best-effort — they never block or fail a push.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger("claude-proxy.transfer-gate")

_sem: asyncio.Semaphore | None = None
_limit: int | None = None  # None = not yet initialized from config
_waiters: int = 0          # manual count; asyncio.Semaphore exposes none


def _get_limit() -> int:
    global _limit
    if _limit is None:
        import config
        _limit = max(0, getattr(config, "SYNC_FANOUT_CONCURRENCY", 3))
    return _limit


def _get_sem() -> asyncio.Semaphore | None:
    global _sem
    limit = _get_limit()
    if limit <= 0:
        return None
    if _sem is None:
        _sem = asyncio.Semaphore(limit)
    return _sem


def _min_bytes() -> int:
    import config
    return max(0, getattr(config, "SYNC_FANOUT_MIN_MB", 4)) * 1024 * 1024


def reset_for_tests() -> None:
    """Drop cached semaphore/limit so monkeypatched config takes effect."""
    global _sem, _limit, _waiters
    _sem = None
    _limit = None
    _waiters = 0


def is_gated(size_bytes: int) -> bool:
    """True when a push of this size contends for a global slot."""
    return _get_limit() > 0 and size_bytes >= _min_bytes()


async def _notify(on_state, state: str) -> None:
    if on_state is None:
        return
    try:
        await on_state(state)
    except Exception:
        logger.debug("transfer_gate on_state(%s) failed", state, exc_info=True)


@contextlib.asynccontextmanager
async def slot(
    machine_id: str, agent_slug: str, rel_path: str, size_bytes: int, *,
    on_state=None,
):
    """Acquire a global outbound slot for one machine's push.

    Below-threshold pushes and limit=0 bypass instantly (no callbacks, no
    log — behavior identical to pre-gate). Gated pushes log ONE INFO line
    when they actually wait and report 'queued' → 'active' via ``on_state``
    ('active' fires on the fast path too, so tracked rows always pass
    through a consistent lifecycle). Terminal done/failed states are the
    caller's job — the gate only owns admission.
    """
    global _waiters
    sem = _get_sem()
    if sem is None or size_bytes < _min_bytes():
        yield
        return
    if sem.locked():
        logger.info(
            "fan-out queued: %s/%s -> %s (%d ahead)",
            agent_slug, rel_path, machine_id[:8], _waiters,
        )
        await _notify(on_state, "queued")
    _waiters += 1
    try:
        await sem.acquire()
    finally:
        _waiters -= 1
    try:
        await _notify(on_state, "active")
        yield
    finally:
        sem.release()
