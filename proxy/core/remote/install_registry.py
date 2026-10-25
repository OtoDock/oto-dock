"""In-flight install registry — per-user fan-out + history replay.

Each entry is keyed by ``(machine_id, agent_slug)`` and represents a single
MCP-install operation on a satellite for one agent. The install is shared
across chats: a new chat or task run for the same (machine, agent) reuses
the same install slot and sees the same events.

Why a second registry alongside ``warmup_registry``? Install progress is a
satellite-level concept, not a chat-level one. A user can open the new-chat
page for agent X (no chat_id yet), trigger an install, navigate to a
different chat, come back, and still see the install progress — because
the install lives on the (machine, agent) key, not on any particular
chat_id.

Delivery model. Events do NOT fan out to per-WS-connection listeners. That
model raced and leaked: a backgrounded ``pre_warmup`` task attaches its WS's
``_send`` and then blocks inside ``start_session`` for the entire install,
so the dead ``_send`` stayed attached after that WS dropped (a transparent
reconnect) while the live tab — which never ran a warmup — never attached.
The result was "install bar invisible until refresh": the proxy fanned every
event to a closed socket while the user's foreground tab got nothing.

Instead, ``emit`` appends to bounded per-install history and hands the event
to a registered ``_broadcaster`` (set at startup to
``ws/satellite.py::push_install_event``), which pushes it via the same
per-user notify channel that satellite-update events already use — but only
to the install's *participants*: the user_subs that warmed this
(machine, agent) (recorded by ``register``). That scopes progress to exactly
the users engaging the satellite — a personal machine reaches only its
owner; an admin-shared machine reaches the viewer/editor actually using the
agent, not every admin. A freshly connected dashboard WS replays each
in-flight install's history on connect (``snapshot_inflight``) for installs
it participates in, so a tab opened mid-install catches up. This is immune
to the per-connection attach races and leaked listeners of the old model.

Lifecycle:
- ``register(machine, agent)`` is called by ``RemoteExecutionLayer.start_session``
  upfront (increments refcount), ``unregister`` at the end (decrements; pops
  the entry at refcount 0).
- ``emit`` appends to history and broadcasts.
- ``snapshot_inflight`` lets the dashboard replay history on connect.
- Sweeper task in ``app.py`` removes entries older than 600s as a backstop.

Module-level state:
  _inflight: dict[(machine_id, agent_slug), InflightInstall]   guarded by _lock
  _broadcaster: registered async (machine_id, event) -> None   delivery hook
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger("install-registry")

# Event history bounded so a long install can't grow unbounded. One install
# emits ~5 events per MCP; 10 MCPs ≈ 50 events. Replay sends the full history
# so a connecting client renders the same final state.
_HISTORY_MAX = 50

# Entries older than this are swept even if no one called unregister(). The
# sweeper is best-effort; production paths use the try/finally in
# RemoteExecutionLayer.start_session. Matches the sync_mcps timeout ceiling
# (mcp_sync.py:170 — 10 min).
_SWEEP_AGE_SECONDS = 600

# Per-user delivery hook. Set once at startup (app.py) to
# ws/satellite.py::push_install_event. Held as a registered callable rather
# than an import so this core module never imports the ws layer.
#
# ``fn(machine_id, event, recipients)`` pushes the event into the dashboard
# notify queues of each user_sub in ``recipients`` — the *participants* of
# this install (the users who warmed this (machine, agent); see register).
# That scopes install progress to exactly the users engaging the satellite:
# a personal machine reaches only its owner, an admin-shared machine reaches
# the viewer/editor/etc. actually using the agent — not every admin.
Broadcaster = Callable[[str, dict, list[str]], Awaitable[None]]
_broadcaster: Broadcaster | None = None


def set_broadcaster(fn: Broadcaster | None) -> None:
    """Register the per-user delivery hook invoked by ``emit`` for every
    install event. See the ``Broadcaster`` type comment for the contract.
    """
    global _broadcaster
    _broadcaster = fn


@dataclass
class InflightInstall:
    machine_id: str
    agent: str
    started_at: float = field(default_factory=time.monotonic)
    last_emit_ts: float = field(default_factory=time.monotonic)
    event_history: list[dict] = field(default_factory=list)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    # user_subs that warmed this (machine, agent) and should therefore see
    # the install bar — accumulated across every register() for the install's
    # lifetime (a viewer + the owner can drive the same shared install).
    # The broadcaster delivers each event to exactly these users' dashboard
    # tabs; the dashboard replays history on connect only to participants.
    # Empty for user-less paths (phone) → fire-and-drop with no delivery.
    participants: set[str] = field(default_factory=set)
    # Number of start_sessions currently driving this install. Multiple
    # concurrent warmups for the same (machine, agent) — e.g. two tabs —
    # share one install slot. First register creates the entry (refcount=1);
    # second register increments to 2; each unregister decrements. Entry
    # is popped when refcount hits 0.
    ref_count: int = 0


_inflight: dict[tuple[str, str], InflightInstall] = {}
_lock = asyncio.Lock()


def _key(machine_id: str, agent: str) -> tuple[str, str]:
    return (machine_id, agent)


async def register(machine_id: str, agent: str, user_sub: str = "") -> InflightInstall:
    """Begin (or join) an install for (machine, agent). Increments refcount.

    Called by ``RemoteExecutionLayer.start_session`` before invoking
    ``mcp_sync.sync_mcps_for_session`` — so the entry always exists before
    the first ``emit``. Idempotent: concurrent registers for the same key
    share one entry, each increments refcount.

    ``user_sub`` is the logged-in user driving this session; it is recorded
    as a *participant* so install progress is delivered to exactly the users
    engaging this (machine, agent). Empty (phone / user-less paths) adds no
    participant — the install then fires-and-drops with no dashboard delivery.

    If ``user_sub`` is a *new* participant joining an install that already
    has history (e.g. a second viewer opens the agent mid-install, or a user
    navigates in on an already-open WS that won't re-run connect replay), the
    existing history is replayed to just that user so their bar shows the
    current state from 0%, not from whenever they happened to join.
    """
    key = _key(machine_id, agent)
    catch_up: list[dict] | None = None
    async with _lock:
        rec = _inflight.get(key)
        if rec is None:
            rec = InflightInstall(machine_id=machine_id, agent=agent)
            _inflight[key] = rec
        rec.ref_count += 1
        if user_sub and user_sub not in rec.participants:
            rec.participants.add(user_sub)
            if rec.event_history:
                catch_up = list(rec.event_history)

    # Replay existing history to the newly-joined participant only (outside
    # the lock). Existing participants already received these live, so the
    # ``not in participants`` guard above prevents re-delivery to them.
    if catch_up and _broadcaster is not None:
        for ev in catch_up:
            try:
                await _broadcaster(machine_id, ev, [user_sub])
            except Exception:
                logger.exception(
                    "install_registry register catch-up replay raised "
                    "for machine=%s agent=%s", machine_id[:8], agent,
                )
    return rec


async def emit(machine_id: str, agent: str, event: dict) -> None:
    """Append an event to history and broadcast it to the owning user's
    dashboard connections via the registered broadcaster.

    The broadcaster is exception-safe at its own call sites (the notify-queue
    put swallows); we still guard here so one bad delivery can't abort an
    install. We append + release the lock BEFORE awaiting the broadcast to
    avoid holding the lock across network I/O.
    """
    key = _key(machine_id, agent)
    async with _lock:
        rec = _inflight.get(key)
        if rec is None:
            # Late emit after unregister — drop. Common for terminal events
            # whose unregister fires before the await completes.
            logger.warning(
                "install_registry.emit DROPPED (no entry): machine=%s agent=%s type=%s",
                machine_id[:8], agent, event.get("type", "?"),
            )
            return
        rec.event_history.append(event)
        if len(rec.event_history) > _HISTORY_MAX:
            rec.event_history = rec.event_history[-_HISTORY_MAX:]
        rec.last_emit_ts = time.monotonic()
        recipients = list(rec.participants)

    logger.debug(
        "install_registry.emit machine=%s agent=%s type=%s mcp=%s pct=%s recipients=%d",
        machine_id[:8], agent, event.get("type", "?"),
        event.get("mcp", "-"), event.get("pct", "-"), len(recipients),
    )
    if _broadcaster is not None and recipients:
        try:
            await _broadcaster(machine_id, event, recipients)
        except Exception:
            logger.exception(
                "install_registry broadcaster raised for machine=%s agent=%s",
                machine_id[:8], agent,
            )


async def unregister(machine_id: str, agent: str) -> None:
    """Decrement refcount; pop the entry when refcount hits 0.

    Sets ``completed`` on the entry so any waiter can observe terminal state.
    Safe if the entry is already gone.
    """
    key = _key(machine_id, agent)
    async with _lock:
        rec = _inflight.get(key)
        if rec is None:
            return
        rec.ref_count = max(0, rec.ref_count - 1)
        if rec.ref_count == 0:
            rec.completed.set()
            _inflight.pop(key, None)


def get(machine_id: str, agent: str) -> InflightInstall | None:
    """Synchronous read — used by the install heartbeat loop to check
    ``last_emit_ts`` and by callers that only need a race-tolerant peek.
    """
    return _inflight.get(_key(machine_id, agent))


def snapshot_inflight() -> list[InflightInstall]:
    """Return a snapshot list of all in-flight installs.

    Used by the dashboard WS on connect to replay ``event_history`` for the
    installs running on the connecting user's machines, so a tab opened
    mid-install renders the current state immediately.
    """
    return list(_inflight.values())


async def sweep_stale() -> int:
    """Remove entries older than _SWEEP_AGE_SECONDS regardless of state.

    Returns count removed (for logging). Called periodically from a
    background task in proxy/app.py. Belt-and-suspenders for any path
    that forgets to unregister.
    """
    now = time.monotonic()
    removed = 0
    async with _lock:
        for k in list(_inflight.keys()):
            rec = _inflight[k]
            if now - rec.started_at > _SWEEP_AGE_SECONDS:
                _inflight.pop(k, None)
                removed += 1
    if removed:
        logger.info("install_registry: swept %d stale entries", removed)
    return removed
