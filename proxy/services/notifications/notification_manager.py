"""Async notification orchestration layer.

Resolves notification targets, tracks per-connection visibility/idle state, routes delivery
to WebSocket (toast) or native push (FCM/Web Push), and manages notification scheduling.

## Routing model

Each WebSocket connection registers itself with a UUID ``connection_id`` and tracks its own
``active`` flag (driven by the frontend's visibilitychange + 5-min idle timer), an ``away``
flag (visible-but-input-idle — see ``ConnectionInfo``), and ``platform`` (``"web"`` or
``"android"``). A single user may have many simultaneous connections (laptop tab +
Android app + second monitor tab) — state is per-connection, not per-user.

Delivery is mutually exclusive:

* Any connection active (visible + recent input) → WS toast to **all** active connections, **no**
  native push. Inactive connections receive a silent inbox/badge update so their state stays
  current.
* No active connection → native push (FCM + Web Push) handles the alert. Connected-but-inactive
  WSes receive the silent update so the badge is correct when the user returns.

Every fire writes a ``notification_deliveries`` DB row, so the in-app inbox always reflects the
full history regardless of channel.
"""

import asyncio
import collections
import contextlib
import logging
import time
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime

from storage.automation import notification_store
import config
from core.session import session_kind
from auth.providers import acting_role_of
# Imported with the manager: its libraries (pywebpush, google-auth,
# requests) take about 0.3 s to load, which must never land on the loop at
# the first alert of a process.
from services.notifications import push_sender
from ws import chat_phase
from ws import wire_events as wire
from storage.pg import run_db

logger = logging.getLogger("claude-proxy.notifications")


# --- Per-connection state ---


@dataclass
class ConnectionInfo:
    """State for one WebSocket connection (one tab/device per user)."""
    connection_id: str
    queue: asyncio.Queue
    active: bool = True  # overridden by frontend immediately on ws.onopen
    platform: str = "web"  # "web" | "android"
    # Visible-but-input-idle: the dashboard is open on a screen nobody has
    # touched for ~5 min (the FE's `user_idle {away: true}`). Distinct from a
    # plain-idle HIDDEN tab: an away connection's toast/sound plays to an
    # empty chair, so end-of-turn alerts also take the FCM leg for it, while
    # a hidden tab keeps suppressing the buzz (same-machine multitasking).
    away: bool = False
    # Live-app frames (APPS.md "Live apps") ride a second queue: the
    # notify queue is drained only between the viewed chat's turns, and a
    # push must reach the screen while the agent's own turn is running.
    live_queue: "LiveQueue | None" = None
    # What the tab shows right now (`focus` frame: surface + ids); cleared
    # when the tab hides or the connection drops, never stored anywhere.
    focus: dict | None = None
    focus_at: float = 0.0
    # The platform-catalog feeds an app on this tab asked for, as (agent,
    # feed) pairs (`catalog_subscribe` frames): a delta reaches a connection
    # only through one of these, so a tab with no such app costs nothing.
    # Dropped with the connection; the client re-sends after a reconnect.
    catalog_feeds: set[tuple[str, str]] = field(default_factory=set)


# user_sub → list of all current WS connections for this user
_user_connections: dict[str, list[ConnectionInfo]] = {}

# Frames a live queue holds before it starts evicting: a burst beyond this
# is a page nobody can follow.
LIVE_QUEUE_MAX = 256

_FOCUS_SURFACES = frozenset({"chat", "home", "app"})
_FOCUS_ID_MAX = 64
# Catalog subscriptions one connection may hold (apps × feeds on one tab).
CATALOG_SUBS_MAX = 64


NOTIFY_QUEUE_MAX = 1024

# What a full notify queue sheds, least valuable first. A frame of these
# kinds is superseded by the next of its key, a reconnect's snapshot or the
# next poll of the list it refreshes.
_NOTIFY_EVICT_ORDER = (
    wire.CHAT_STATUS, wire.CHAT_READ,
    wire.INSTALL_PROGRESS, wire.INSTALL_HEARTBEAT, wire.TRANSFER_PROGRESS,
    wire.WARMUP_HEARTBEAT, wire.SATELLITE_UPDATE_SYNC,
    wire.FILE_UPDATED, wire.CHAT_ROWS, wire.GOAL_UPDATE,
)
# Never evicted: the internal kinds carry turns and prompts, a notification
# is the inbox's alert, the end-of-turn ping has no second channel, and a
# terminal lifecycle frame is replayed only by a reconnect.
_NOTIFY_KEEP = frozenset({
    wire.NOTIFY_SERVER_KICK, wire.NOTIFY_BG_NUDGE, wire.NOTIFY_BG_COMMAND_NUDGE,
    wire.NOTIFY_LIVENESS_CLEAR, wire.NOTIFY_CHAT_UI_FRAME, wire.NOTIFY_TASK_RESULT_PROMPT,
    wire.NOTIFY_CONTINUATION_PROMPT, wire.NOTIFY_LOCATION_REQUEST,
    wire.NOTIFICATION, wire.NOTIFICATION_SILENT, wire.NOTIFICATION_COUNT, wire.SHARE_INBOX,
    wire.TURN_COMPLETE, wire.BG_AGENT_DONE,
    wire.INSTALL_DONE, wire.INSTALL_FAILED, wire.MCP_INSTALL_FAILED,
    wire.TRANSFER_DONE, wire.TRANSFER_STATE, wire.TRANSFER_MACHINE_STATE,
    wire.SATELLITE_UPDATED, wire.SATELLITE_UPDATE_FAILED,
})
# The broadcast copies the drain discards (the acting socket holds its own
# copy, ws/dashboard_server_events._DIRECT_COPY_ONLY): never queued.
_NOTIFY_REFUSED = frozenset({wire.TITLE_UPDATED, wire.ENGINE_SWITCHED})
_NOTIFY_STATUS = frozenset({wire.CHAT_STATUS, wire.CHAT_READ})
_NOTIFY_DROP_LOG_S = 60.0


def _notify_coalesce_key(frame: dict) -> tuple | None:
    """The key under which a queued frame is replaced by a newer one of the
    same kind, or None for a frame that is never coalesced."""
    t = frame.get("type")
    if t in _NOTIFY_STATUS or t == wire.WARMUP_HEARTBEAT:
        return (t, frame.get("chat_id"))
    if t in (wire.INSTALL_PROGRESS, wire.INSTALL_HEARTBEAT):
        return (t, frame.get("machine_id"), frame.get("agent"))
    if t == wire.TRANSFER_PROGRESS:
        return (t, frame.get("transfer_id") or frame.get("id"))
    if t == wire.SATELLITE_UPDATE_SYNC:
        return (t,)
    return None


class NotifyQueue(asyncio.Queue):
    """The per-connection notify queue, bounded.

    Drained only between the viewed chat's turns, so a long turn beside a
    chatty broadcast source could grow it without limit. Three rules keep
    it small before anything is lost: a status, read-receipt, progress or
    heartbeat frame replaces the queued one of its key (in place, so order
    holds); the two broadcast copies the drain discards are refused; and
    past ``NOTIFY_QUEUE_MAX`` the oldest frame of the least valuable kind
    present is evicted (``_NOTIFY_EVICT_ORDER``), never one of
    ``_NOTIFY_KEEP``. When only kept frames wait, a new evictable frame is
    dropped and a new kept frame is queued past the bound. ``put`` never
    blocks. An evicted status frame marks the queue ``stale``: the
    connection's drain then purges the queued status frames and sends a
    fresh ``chat_status_snapshot`` so the client's dots converge.
    """

    def __init__(self, bound: int = NOTIFY_QUEUE_MAX) -> None:
        super().__init__()
        self.bound = bound
        self.stale = False
        self.dropped = 0
        self._dropped_since_log = 0
        self._logged_at = 0.0

    def put_nowait(self, item: dict) -> None:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind in _NOTIFY_REFUSED:
            return
        key = _notify_coalesce_key(item) if kind else None
        if key is not None:
            for i, queued in enumerate(self._queue):
                if _notify_coalesce_key(queued) == key:
                    self._queue[i] = item
                    return
        if self.qsize() >= self.bound and kind not in _NOTIFY_KEEP:
            victim = self._victim()
            if victim is None:
                self._drop(item)
                return
            evicted = self._queue[victim]
            del self._queue[victim]
            self._drop(evicted)
        super().put_nowait(item)

    async def put(self, item: dict) -> None:
        self.put_nowait(item)

    def _victim(self) -> int | None:
        for kind in _NOTIFY_EVICT_ORDER:
            for i, queued in enumerate(self._queue):
                if queued.get("type") == kind:
                    return i
        for i, queued in enumerate(self._queue):
            if queued.get("type") not in _NOTIFY_KEEP:
                return i
        return None

    def _drop(self, frame: dict) -> None:
        self.dropped += 1
        self._dropped_since_log += 1
        if frame.get("type") in _NOTIFY_STATUS:
            self.stale = True
        now = time.monotonic()
        if now - self._logged_at >= _NOTIFY_DROP_LOG_S:
            logger.warning(
                "notify queue full (%d): dropped %d frame(s) in the last minute, the newest a %s",
                self.bound, self._dropped_since_log, frame.get("type"),
            )
            self._logged_at = now
            self._dropped_since_log = 0

    def purge_status(self) -> int:
        """Drop every queued status frame (a snapshot replaces them) and
        clear ``stale``. Returns how many went."""
        kept = [f for f in self._queue if f.get("type") not in _NOTIFY_STATUS]
        gone = len(self._queue) - len(kept)
        self._queue.clear()
        self._queue.extend(kept)
        self.stale = False
        return gone


class LiveQueue:
    """The per-connection queue for live-app frames. A state frame for an
    app replaces the one still waiting for that app (the higher rev wins,
    so state occupies one slot per app), an open frame is never evicted,
    and a full queue drops its oldest push first: a burst of pushes nobody
    could follow yields to the frames that still matter. ``put`` is
    synchronous and never blocks the writer; ``get`` parks one reader."""

    def __init__(self, maxsize: int = LIVE_QUEUE_MAX) -> None:
        self._items: collections.deque[dict] = collections.deque()
        self._maxsize = maxsize
        self._waiter: asyncio.Future | None = None

    def __len__(self) -> int:
        return len(self._items)

    def put(self, frame: dict) -> bool:
        ftype = frame.get("type")
        if ftype == wire.APP_STATE:
            app_id = frame.get("app_id")
            for i, item in enumerate(self._items):
                if item.get("type") == wire.APP_STATE and item.get("app_id") == app_id:
                    if int(frame.get("rev") or 0) >= int(item.get("rev") or 0):
                        self._items[i] = frame
                    self._wake()
                    return True
        if ftype == wire.CATALOG and frame.get("snapshot") is not None:
            # A catalog snapshot supersedes whatever waits for the same feed:
            # the queued snapshot and every delta before it.
            key = (frame.get("agent"), frame.get("feed"))
            kept = [item for item in self._items
                    if not (item.get("type") == wire.CATALOG
                            and (item.get("agent"), item.get("feed")) == key)]
            self._items = collections.deque(kept)
        if len(self._items) >= self._maxsize:
            # Evict in order of what matters least: the oldest push, then the
            # oldest catalog delta, then the oldest of anything but an open
            # (an open frame is never the victim); refuse when only opens wait.
            victim = None
            for kind in (wire.APP_PUSH, wire.CATALOG):
                victim = next(
                    (i for i, item in enumerate(self._items) if item.get("type") == kind),
                    None,
                )
                if victim is not None:
                    break
            if victim is None:
                victim = next(
                    (i for i, item in enumerate(self._items) if item.get("type") != wire.OPEN_APP),
                    None,
                )
            if victim is None:
                return False
            if ftype == wire.APP_PUSH and self._items[victim].get("type") != wire.APP_PUSH:
                return False
            del self._items[victim]
        self._items.append(frame)
        self._wake()
        return True

    def get_nowait(self) -> dict | None:
        return self._items.popleft() if self._items else None

    async def get(self) -> dict:
        while not self._items:
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        return self._items.popleft()

    def _wake(self) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)


def register_user_connection(
    user_sub: str,
    connection_id: str,
    queue: asyncio.Queue,
    platform: str = "web",
    live_queue: asyncio.Queue | None = None,
) -> None:
    """Track a new WS connection for this user.

    Frontend sends the actual visibility state (``user_active``/``user_idle``) immediately on
    ``ws.onopen`` — the ``active=True`` default closes the window between register and the first
    visibility message.
    """
    conns = _user_connections.setdefault(user_sub, [])
    # Defensive: don't double-register the same connection_id.
    conns[:] = [c for c in conns if c.connection_id != connection_id]
    conns.append(ConnectionInfo(
        connection_id=connection_id,
        queue=queue,
        active=True,
        platform=platform,
        live_queue=live_queue,
    ))


def unregister_user_connection(user_sub: str, connection_id: str) -> None:
    """Drop the connection from tracking. Removes the user entry if empty."""
    conns = _user_connections.get(user_sub)
    if not conns:
        return
    conns[:] = [c for c in conns if c.connection_id != connection_id]
    if not conns:
        _user_connections.pop(user_sub, None)


def set_connection_active(
    user_sub: str, connection_id: str, active: bool, away: bool = False,
) -> None:
    """Update the active/away flags for one connection (driven by the
    user_active / user_idle WS messages; ``away`` rides ``user_idle`` and is
    forced off for an active connection)."""
    for c in _user_connections.get(user_sub, []):
        if c.connection_id == connection_id:
            c.active = active
            c.away = away and not active
            # A hidden tab shows nothing; a visible tab nobody touches (a
            # wall display) keeps its focus.
            if not active and not away:
                c.focus = None
                c.focus_at = 0.0
            return


def set_connection_focus(user_sub: str, connection_id: str, focus) -> None:
    """Record what one tab shows (the dashboard's ``focus`` frame). Anything
    but a known surface with short ids is treated as no focus."""
    conn = get_connection(user_sub, connection_id)
    if conn is None:
        return
    clean: dict | None = None
    if isinstance(focus, dict) and focus.get("surface") in _FOCUS_SURFACES:
        clean = {"surface": focus["surface"]}
        for key in ("app_id", "chat_id"):
            value = focus.get(key)
            if isinstance(value, str) and 0 < len(value) <= _FOCUS_ID_MAX:
                clean[key] = value
        if clean["surface"] == "app" and "app_id" not in clean:
            clean = None
    conn.focus = clean
    conn.focus_at = time.monotonic() if clean else 0.0


def connection_focus(user_sub: str, connection_id: str) -> dict | None:
    """The focus of one connection (a chat turn reads the connection that
    sent it, so two devices never confuse each other)."""
    conn = get_connection(user_sub, connection_id)
    return dict(conn.focus) if conn and conn.focus else None


def user_focus(user_sub: str) -> dict | None:
    """The newest focus across the user's connections (a duplex turn or a
    phone call has no connection of its own; a wall display counts)."""
    best: ConnectionInfo | None = None
    for c in _user_connections.get(user_sub, []):
        if c.focus and (best is None or c.focus_at > best.focus_at):
            best = c
    return dict(best.focus) if best else None


def push_live(user_sub: str, frame: dict, *, connection_id: str | None = None) -> int:
    """Queue a live-app frame on every connection of ``user_sub`` (active or
    not: a wall display idles into ``away`` and must still update), or on
    the one named. A full queue drops its oldest frame. Returns how many
    connections took the frame."""
    count = 0
    for c in _user_connections.get(user_sub, []):
        if connection_id is not None and c.connection_id != connection_id:
            continue
        q = c.live_queue
        if q is None:
            continue
        if q.put(frame):
            count += 1
        else:
            logger.debug("live queue full for %s: push dropped", c.connection_id[:8])
    return count


def set_catalog_subscription(user_sub: str, connection_id: str, agent: str, feed: str,
                             on: bool) -> bool:
    """Add or drop one (agent, feed) pair on a connection. False when the
    connection is unknown or full."""
    conn = get_connection(user_sub, connection_id)
    if conn is None:
        return False
    key = (agent or "", feed or "")
    if not on:
        conn.catalog_feeds.discard(key)
        return True
    if key in conn.catalog_feeds:
        return True
    if len(conn.catalog_feeds) >= CATALOG_SUBS_MAX:
        return False
    conn.catalog_feeds.add(key)
    return True


def _catalog_match(conn: ConnectionInfo, agent: str, feed: str) -> bool:
    # An agent-less frame (the viewer's own feeds across agents) matches a
    # subscription to that feed under any agent.
    if not conn.catalog_feeds:
        return False
    if agent:
        return (agent, feed) in conn.catalog_feeds
    return any(f == feed for _, f in conn.catalog_feeds)


def catalog_subscribed(user_sub: str, agent: str, feed: str) -> bool:
    return any(_catalog_match(c, agent, feed) for c in _user_connections.get(user_sub, []))


def push_catalog(user_sub: str, agent: str, feed: str, frame: dict) -> int:
    """Queue a catalog frame on the user's connections that asked for the
    feed. Returns how many took it."""
    count = 0
    for c in _user_connections.get(user_sub, []):
        if c.live_queue is None or not _catalog_match(c, agent, feed):
            continue
        if c.live_queue.put(frame):
            count += 1
    return count


def set_connection_platform(user_sub: str, connection_id: str, platform: str) -> None:
    """Update the platform for one connection (from the client_info message)."""
    for c in _user_connections.get(user_sub, []):
        if c.connection_id == connection_id:
            c.platform = platform
            return


def get_active_connections(user_sub: str) -> list[ConnectionInfo]:
    return [c for c in _user_connections.get(user_sub, []) if c.active]


def get_all_connections(user_sub: str) -> list[ConnectionInfo]:
    return list(_user_connections.get(user_sub, []))


def has_active_connection(user_sub: str) -> bool:
    """True if at least one connection is currently active (visible + recent input)."""
    return any(c.active for c in _user_connections.get(user_sub, []))


def get_connection(user_sub: str, connection_id: str) -> ConnectionInfo | None:
    for c in _user_connections.get(user_sub, []):
        if c.connection_id == connection_id:
            return c
    return None


# --- Audience cache: who sees a shared-only or task chat's live state ---
#
# The agent's members plus every platform admin are read on every turn edge
# of every shared-only chat and agent-scope task run, from synchronous
# callers on the loop AND from the tailers' worker threads. The list is
# served from memory: an entry lives _AUDIENCE_TTL_S and is then refreshed
# behind the read (an executor job on the loop, a synchronous read on a
# worker thread). A person whose access an admin takes away is dropped at
# once through the offboarding event and every attach path and platform role
# change invalidates the entries it touches, so the TTL bounds only the
# changes that raise neither (an identity provider's role change at login, a
# row written outside the routes).
# Every read takes the entry's generation before it reads the store, so a
# read that started before a drop or an invalidation is discarded when it
# lands. An agent with no entry yet is a placeholder until its first read
# lands: a fan-out on the loop is answered with nobody meanwhile, every other
# read reads the store.

_AUDIENCE_TTL_S = 30.0


@dataclass
class _AudienceEntry:
    subs: tuple[str, ...]
    fetched_at: float
    getter: object          # the store function that filled it (a stand-in misses)
    generation: int = 0     # bumped by every drop and invalidation
    stale: bool = False
    refreshing: bool = False
    placeholder: bool = False   # no list read yet: it answers fan-outs only


_audience: dict[str, _AudienceEntry] = {}
_audience_tasks: set[asyncio.Task] = set()


def agent_audience(agent: str, *, fan_out: bool = False) -> list[str]:
    """Every user who sees ``agent``'s shared-only and task chats, from the
    cache, as a fresh list. Callable from any thread. A miss (an agent with
    no entry yet) reads the store on the caller's thread, except a
    ``fan_out`` read on the loop: a caller that only delivers frames is
    answered with nobody at once while one executor job fills the entry, and
    the next frame reaches the audience. A check that refuses on the answer
    never passes ``fan_out``."""
    if not agent:
        return []
    getter = notification_store.get_agent_user_subs
    entry = _audience.get(agent)
    if entry is None or entry.getter is not getter:
        entry = _audience[agent] = _placeholder(getter)
    elif not entry.stale and time.monotonic() - entry.fetched_at < _AUDIENCE_TTL_S:
        return list(entry.subs)
    return list(_refresh_audience(agent, entry, getter, fan_out=fan_out))


def _placeholder(getter) -> _AudienceEntry:
    return _AudienceEntry((), 0.0, getter, stale=True, placeholder=True)


def _refresh_audience(agent: str, entry: _AudienceEntry, getter, *,
                      fan_out: bool) -> tuple[str, ...]:
    """A stale or placeholder entry. On the loop thread a stale entry is
    served and refreshed in one executor job (one in flight per agent), and
    a placeholder is served so to a fan-out only. On any other thread, and
    for a placeholder an exact caller reads, the store is read right there.
    The generation is taken before the read: an invalidation that lands
    during it keeps the entry stale."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None or (entry.placeholder and not fan_out):
        generation = entry.generation
        try:
            subs = tuple(getter(agent))
        except Exception:
            if entry.placeholder and _audience.get(agent) is entry:
                _audience.pop(agent, None)
            raise
        _store_audience(agent, generation, subs, getter)
        return subs
    _start_audience_refresh(loop, agent, entry, getter)
    return entry.subs


def _start_audience_refresh(loop, agent: str, entry: _AudienceEntry, getter) -> None:
    if not entry.refreshing:
        entry.refreshing = True
        task = loop.create_task(_refresh_audience_job(agent, entry.generation, getter))
        _audience_tasks.add(task)
        task.add_done_callback(_audience_tasks.discard)


async def _refresh_audience_job(agent: str, generation: int, getter) -> None:
    from storage.pg import run_db
    try:
        subs = tuple(await run_db(getter, agent))
    except Exception:
        logger.warning("audience refresh of %s failed; serving the last list",
                       agent, exc_info=True)
        entry = _audience.get(agent)
        if entry is not None:
            entry.refreshing = False
        return
    _store_audience(agent, generation, subs, getter)


def _store_audience(agent: str, generation: int, subs: tuple[str, ...], getter) -> None:
    """Land a refreshed list unless the entry moved on while the read was in
    flight (a drop, an invalidation): then it stays stale and the next read
    refreshes again."""
    entry = _audience.get(agent)
    if entry is None or entry.getter is not getter:
        return
    entry.refreshing = False
    if entry.generation != generation:
        return
    _audience[agent] = _AudienceEntry(subs, time.monotonic(), getter, generation)


def invalidate_audience(agent: str = "") -> None:
    """Mark one agent's entry, or every entry, for a refresh on its next
    read. An agent with no entry yet gets one filled when this runs on the
    loop (its creation, a community install), so its first turn edge finds
    its audience."""
    entries = [_audience.get(agent)] if agent else list(_audience.values())
    for entry in entries:
        if entry is not None:
            entry.stale = True
            entry.generation += 1
    if agent and entries[0] is None:
        _fill_new_audience(agent)


def _fill_new_audience(agent: str) -> None:
    """One executor job fills a missing entry; off the loop the first read
    fills it instead."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    getter = notification_store.get_agent_user_subs
    entry = _audience.setdefault(agent, _placeholder(getter))
    if entry.placeholder:
        _start_audience_refresh(loop, agent, entry, getter)


def _drop_from_audience(sub: str, agents) -> None:
    for agent in agents:
        entry = _audience.get(agent)
        if entry is None:
            continue
        _audience[agent] = _AudienceEntry(
            tuple(s for s in entry.subs if s != sub), entry.fetched_at,
            entry.getter, entry.generation + 1, stale=True,
            placeholder=entry.placeholder)


async def _on_offboard(event) -> None:
    """The offboarding subscriber: a person who lost access to an agent
    leaves its audience before the admin's request completes; a lost row
    with access kept (a platform admin unassigned) only refreshes the entry;
    a lower role changes nothing; a deleted person leaves every entry."""
    from services.agents import offboarding
    if event.reason == offboarding.DELETED:
        _drop_from_audience(event.sub, list(_audience))
    elif event.reason == offboarding.REMOVED:
        _drop_from_audience(event.sub, [a.agent for a in event.agents if a.lost_access])
        for loss in event.agents:
            if not loss.lost_access:
                invalidate_audience(loss.agent)


def _register_audience_invalidation() -> None:
    from services.agents import offboarding
    offboarding.subscribe("audience-cache", _on_offboard, priority=5)


def _read_all_audiences() -> dict[str, tuple[str, ...]]:
    from storage.agents import agent_store
    return {a["slug"]: tuple(notification_store.get_agent_user_subs(a["slug"]))
            for a in agent_store.get_all_agents()}


def _schedule_audience_warmup() -> None:
    """Fill the cache for every agent in one executor job at boot, so the
    first turn edge of an agent finds its entry."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    getter = notification_store.get_agent_user_subs

    async def job() -> None:
        from storage.pg import run_db
        try:
            lists = await run_db(_read_all_audiences)
        except Exception:
            logger.warning("audience warm-up failed; entries fill on first use", exc_info=True)
            return
        now = time.monotonic()
        for agent, subs in lists.items():
            if agent not in _audience:
                _audience[agent] = _AudienceEntry(subs, now, getter)

    task = loop.create_task(job())
    _audience_tasks.add(task)
    task.add_done_callback(_audience_tasks.discard)


def reset_audience_cache() -> None:
    """Forget every entry and every tracked refresh (tests: a later test
    never waits on a job left on a loop that ended)."""
    _audience.clear()
    _audience_tasks.clear()


def chat_status_targets(owner_sub: str, agent: str) -> list[str]:
    """Real users who should see live/read state for a chat owned by
    ``owner_sub``: the owner — or, for a synthetic owner, every user of the
    agent (admins included). Synthetic owners are the shared-only chat owner
    (``agent::<slug>``) AND the scheduler's agent-scope task-chat owner
    (``task::<slug>``) — the latter fans out so scheduled-run pulses reach the
    sidebar's task mode. The synthetic paths read the audience cache as a
    fan-out (every caller delivers frames)."""
    from core.session.visibility import is_shared_chat_owner, is_task_chat_owner
    if is_shared_chat_owner(owner_sub) or is_task_chat_owner(owner_sub):
        try:
            return agent_audience(agent, fan_out=True) if agent else []
        except Exception:
            return []
    return [owner_sub] if owner_sub else []


def broadcast_chat_status(owner_sub: str, chat_id: str, status: str, agent: str = "") -> None:
    """Tell every dashboard connection that should see ``chat_id`` that it
    started or ended a turn, so the sidebar live-dot is correct even for chats
    generating in the BACKGROUND. Emitted by the pump on every turn start/end
    (viewed, detached, or headless) and by interactive sessions on turn-open
    transitions. ``status`` is a ``ws.chat_phase.WIRE_PHASES`` word —
    ``streaming`` or ``ready`` — and nothing else (a programming error, not a
    runtime condition: every emitter passes the constant). ``owner_sub`` is
    the chat row's owner; pass ``agent`` so shared-only chats (synthetic
    ``agent::`` owner) fan out to the agent's users instead of nobody.
    Best-effort."""
    if status not in chat_phase.WIRE_PHASES:
        raise ValueError(f"chat_status carries streaming or ready, not {status!r}")
    if not chat_id:
        return
    targets = chat_status_targets(owner_sub, agent)
    for sub in targets:
        for c in _user_connections.get(sub, []):
            try:
                c.queue.put_nowait({"type": wire.CHAT_STATUS, "chat_id": chat_id, "status": status})
            except Exception as e:
                logger.debug("chat_status broadcast: %s", e)
    # The catalog's ``sessions`` feed rides the live queue (drained during a
    # turn), never the notify queue above (drained between turns); the same
    # targets, so the fan-out is computed once.
    try:
        from api.apps import catalog
        catalog.chat_status_changed(targets, chat_id, status, agent)
    except Exception as e:
        logger.debug("catalog sessions delta: %s", e)


def broadcast_chat_read(owner_sub: str, chat_id: str, agent: str = "") -> None:
    """Tell every dashboard connection that should see ``chat_id`` that its
    unread marker cleared (someone opened the chat) — same fan-out as
    ``broadcast_chat_status``, so a shared-only chat clears on every user's
    sidebar and a user's other tabs stay in sync. Best-effort."""
    if not chat_id:
        return
    for sub in chat_status_targets(owner_sub, agent):
        for c in _user_connections.get(sub, []):
            try:
                c.queue.put_nowait({"type": wire.CHAT_READ, "chat_id": chat_id})
            except Exception as e:
                logger.debug("chat_read broadcast: %s", e)


def broadcast_engine_switched(
    owner_sub: str, chat_id: str, execution_path: str, model: str,
    agent: str = "",
) -> None:
    """Tell every dashboard connection that should see ``chat_id`` that it now
    runs on a different execution engine (the cross-engine resume switch), so
    a second tab's locked model dropdown re-homes instead of lying about the
    old engine. Same fan-out as ``broadcast_chat_status``; the acting socket
    also gets a direct ack, so receivers must treat the frame as idempotent.
    Best-effort."""
    if not chat_id:
        return
    for sub in chat_status_targets(owner_sub, agent):
        for c in _user_connections.get(sub, []):
            try:
                c.queue.put_nowait({
                    "type": wire.ENGINE_SWITCHED, "chat_id": chat_id,
                    "execution_path": execution_path, "model": model,
                })
            except Exception as e:
                logger.debug("engine_switched broadcast: %s", e)


def broadcast_goal_update(user_sub: str, chat_id: str, goal: dict | None) -> None:
    """Tell ALL of a user's dashboard connections that ``chat_id``'s codex
    thread goal changed OUTSIDE a turn (codex accounts goal progress at turn
    stop, so the final update — often the completion — lands after
    turn/completed with no pump to carry it). ``goal=None`` clears the panel.
    The frontend's per-chat frame gate scopes it to the viewing client.
    Best-effort, sync (mirrors broadcast_chat_status)."""
    if not chat_id or not user_sub:
        return
    for c in _user_connections.get(user_sub, []):
        try:
            c.queue.put_nowait({"type": wire.GOAL_UPDATE, "chat_id": chat_id, "goal": goal})
        except Exception as e:
            logger.debug("goal_update broadcast: %s", e)


def broadcast_chat_rows(user_sub: str, chat_id: str, agent: str = "") -> None:
    """Tell a user's dashboard connections that new HISTORY rows were persisted
    for an interactive chat (transcript tail batch). The frontend uses it to
    live-refresh an open rich-history view (the terminal ⇄ transcript toggle);
    everything else ignores it. Deliberately payload-free (no row content) —
    the viewer refetches through the normal authorized GET. Best-effort, sync
    (mirrors broadcast_chat_title)."""
    if not chat_id or not user_sub:
        return
    for c in _user_connections.get(user_sub, []):
        try:
            c.queue.put_nowait(
                {"type": wire.CHAT_ROWS, "chat_id": chat_id, "agent": agent})
        except Exception as e:
            logger.debug("chat_rows broadcast: %s", e)


def broadcast_chat_title(user_sub: str, chat_id: str, title: str, agent: str = "") -> None:
    """Tell every dashboard connection that should see ``chat_id`` that it was
    (re)titled, so the sidebar + Active-now rows update without waiting for a
    navigation refetch. Used by the transcript tailer to surface the backfilled
    title of an interactive chat (which can't title at send-time). Same
    ``title_updated`` event the headless send-path emits per-socket; the
    frontend's onTitleUpdated refetches the list. Fan-out mirrors
    ``broadcast_chat_status`` — pass ``agent`` so a shared-only chat's synthetic
    ``agent::`` owner reaches the agent's users instead of nobody. Best-effort."""
    if not chat_id or not user_sub:
        return
    for sub in chat_status_targets(user_sub, agent):
        for c in _user_connections.get(sub, []):
            try:
                c.queue.put_nowait({"type": wire.TITLE_UPDATED, "chat_id": chat_id, "title": title})
            except Exception as e:
                logger.debug("title_updated broadcast: %s", e)
    # The catalog's ``sessions`` feed carries the title too (a chat created
    # untitled is filled in when the title lands).
    try:
        from api.apps import catalog
        catalog.chat_meta_changed(chat_id, {"title": title}, owner=user_sub, agent=agent or None)
    except Exception as e:
        logger.debug("catalog sessions title delta: %s", e)


# --- Per-chat turn origin (which device sent the last user prompt) ---

# chat_id → (connection_id, platform). Drives end-of-turn alert routing:
# browser-origin chats ping THAT browser (even with the tab hidden) and never
# fire Android FCM; app-origin chats fire FCM when the app is backgrounded.
# In-memory — after a proxy restart fire_ephemeral falls back to the legacy
# activity-based rule until the chat's next user send.
_chat_turn_origin: dict[str, tuple[str, str]] = {}
# chat_id → the user whose connection sent the last prompt. In a shared chat
# that is not always the user the session was warmed for; an agent opening
# an app on "the user's screen" must mean the human who is actually there.
_chat_turn_user: dict[str, str] = {}


def set_chat_turn_origin(user_sub: str, chat_id: str, connection_id: str) -> None:
    """Record which connection (device) initiated the chat's current turn.
    Called at user-send time (warmup first prompt / chat / queued message)."""
    if not chat_id or not connection_id:
        return
    conn = get_connection(user_sub, connection_id)
    if conn:
        _chat_turn_origin[chat_id] = (connection_id, conn.platform)
        _chat_turn_user[chat_id] = user_sub


def chat_turn_origin(chat_id: str) -> tuple[str, str, str] | None:
    """``(connection_id, platform, user_sub)`` of the chat's last sender
    while that connection is still registered, else None."""
    origin = _chat_turn_origin.get(chat_id)
    user_sub = _chat_turn_user.get(chat_id, "")
    if not origin or not user_sub:
        return None
    if get_connection(user_sub, origin[0]) is None:
        return None
    return origin[0], origin[1], user_sub


# --- Target resolution ---


def resolve_targets(scope: str, target: str | None) -> list[str]:
    """Resolve notification scope + target to a list of user_sub IDs."""
    if scope == "user":
        return [target] if target else []
    elif scope == "agent":
        if not target:
            return []
        return notification_store.get_agent_user_subs(target)
    elif scope == "global":
        return notification_store.get_all_user_subs()
    elif scope == "admin":
        return notification_store.get_admin_user_subs()
    return []


# --- Agent identity in titles ---


def agent_label(agent_slug: str | None) -> str:
    """Human label for an agent in notification text: the display name when
    the slug resolves, else the slug itself (never empty for a truthy slug).
    Cached-dict read (``agent_store.get_agent``), safe on any thread."""
    if not agent_slug:
        return ""
    try:
        from storage.agents import agent_store
        row = agent_store.get_agent(agent_slug)
    except Exception:
        row = None
    return (row or {}).get("display_name") or agent_slug


def push_title(title: str, agent_slug: str | None) -> str:
    """Title for the NATIVE push channel (FCM / Web Push), where there is no
    card header to say which agent a notification came from: prefix the
    agent's display name ("Alpha · Task done"). In-app surfaces keep the raw
    title — the panel/toast render an agent header instead. Skipped when the
    title already names the agent (display name OR slug, case-insensitive),
    so end-of-turn titles like "Alpha finished" are never double-labelled."""
    label = agent_label(agent_slug)
    if not label:
        return title
    lowered = (title or "").lower()
    if label.lower() in lowered or (agent_slug or "").lower() in lowered:
        return title
    return f"{label} · {title}"


# --- Notification firing ---


async def fire_notification(
    title: str,
    body: str,
    severity: str = "info",
    scope: str = "user",
    target: str | None = None,
    source: str = "mcp",
    source_id: str | None = None,
    notification_id: str | None = None,
    agent_slug: str | None = None,
    chat_id: str | None = None,
    href: str = "",
) -> list[dict]:
    """Fire a notification to all resolved targets.

    Creates a delivery record per user (so the inbox is always populated regardless of channel)
    then routes to WS / native push per the mutually-exclusive policy in ``_deliver_to_user``.
    ``href`` is a dashboard path the row opens instead of the agent/chat deep link.
    """
    user_subs = await run_db(resolve_targets, scope, target)
    if not user_subs:
        logger.warning(
            f"No targets resolved for notification: scope={scope}, target={target}"
        )
        return []

    def _create_rows() -> list[tuple[str, dict]]:
        # One job for every inbox row, each its own transaction as before:
        # a recipient deleted since the targets were resolved costs their
        # row alone, never the others'.
        out: list[tuple[str, dict]] = []
        for user_sub in user_subs:
            try:
                out.append((user_sub, notification_store.create_delivery(
                    user_sub=user_sub, title=title, body=body, severity=severity,
                    scope=scope, source=source, notification_id=notification_id,
                    agent_slug=agent_slug, chat_id=chat_id, href=href,
                )))
            except Exception:
                logger.exception("notification delivery row for %s failed", user_sub[:8])
        return out

    created = await run_db(_create_rows)
    deliveries = [d for _, d in created]
    slot = _fan_out_slot()

    async def _one(user_sub: str, delivery: dict) -> None:
        async with slot:
            try:
                await _deliver_to_user(user_sub, delivery)
            except Exception:
                logger.exception("notification delivery to %s failed", user_sub[:8])

    await asyncio.gather(*(_one(u, d) for u, d in created), return_exceptions=True)

    # Update fired count if this came from a stored notification definition
    # (immediate-fire from create, scheduled fire, or manual /fire endpoint).
    # Then hard-delete the definition row if it's a one-time notification —
    # matches one-time task behaviour. Delivery records in
    # notification_deliveries are independent and remain in the user's inbox.
    if notification_id:
        await asyncio.to_thread(
            notification_store.update_notification_fired, notification_id
        )
        notif_row = await asyncio.to_thread(
            notification_store.get_notification, notification_id
        )
        if notif_row and notif_row.get("notification_type") == "one_time":
            if config.SCHEDULER_MODE != "standalone":
                unregister_notification(notification_id)
            await asyncio.to_thread(
                notification_store.delete_notification, notification_id
            )
            logger.debug(f"Cleaned up fired one-time notification: {notification_id}")

    # The title is user-facing content (a window alert names the account and
    # its email): the log keeps the row id, not the text.
    logger.info(
        f"Notification fired: id={notification_id or 'ephemeral'}, severity={severity}, "
        f"scope={scope}, targets={len(deliveries)}"
    )
    return deliveries


def _install_id() -> str:
    """This proxy's stable install id (the relay identity), tagged into every push
    so the Android app can route a notification to the matching installation. Empty
    string if unavailable — old apps ignore it and route to the active install.
    A store read: ``_install_id_async`` serves it from a per-process cache."""
    try:
        from services.billing.relay_client import get_install_id
        return get_install_id()
    except Exception:
        return ""


_install_id_value = ""
_install_id_flights: dict[int, asyncio.Future] = {}


async def _install_id_async() -> str:
    """``_install_id()`` read once per process on ``run_db`` (the id never
    changes once minted; an empty answer is retried next time), the first
    callers of a loop sharing one read."""
    global _install_id_value
    if _install_id_value:
        return _install_id_value
    loop = asyncio.get_running_loop()
    flight = _install_id_flights.get(id(loop))
    if flight is None:
        # The read is its own task and every caller, the first included,
        # awaits it shielded: a cancelled caller cancels nobody's read.
        async def _read() -> str:
            global _install_id_value
            value = await run_db(_install_id)
            _install_id_value = value
            return value

        flight = _install_id_flights[id(loop)] = loop.create_task(_read())
        flight.add_done_callback(lambda f: (_install_id_flights.pop(id(loop), None),
                                            f.cancelled() or f.exception()))
    return await asyncio.shield(flight)


# The fan-outs' concurrency: recipients delivered at once, per loop (a
# semaphore binds to the loop it first waits on). The pushes inside a
# delivery take ``push_sender._push_slot()``, never this one.
_FAN_OUT_CONCURRENCY = 8
_fan_out_slots: dict[int, asyncio.Semaphore] = {}


def _fan_out_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _fan_out_slots.get(id(loop))
    if slot is None:
        _fan_out_slots.clear()
        slot = _fan_out_slots[id(loop)] = asyncio.Semaphore(_FAN_OUT_CONCURRENCY)
    return slot


async def fire_ephemeral(
    user_sub: str, title: str, body: str, chat_id: str | None = None,
    *, interactive: bool = False, cli_attached: bool = False,
) -> None:
    """Fire an ephemeral turn-complete signal, routed to the device that
    STARTED the turn (recorded by ``set_chat_turn_origin`` at send time):

    * **Browser-origin chat** → a ``turn_complete`` WS frame to that browser
      connection — the frontend pings even when the tab is hidden or the chat
      is backgrounded — and normally no Android FCM. If the browser connection
      is gone, the alert is dropped (a browser chat never buzzes the phone).
      EXCEPTION: an ``away`` origin (dashboard visibly open, no input for
      ~5 min — the toast just played to an empty chair) falls through to the
      presence/FCM path IN ADDITION to the frame, so the phone hears about it
      unless another connection is actively used.
    * **App-origin chat** → nothing while the app is foregrounded (the open
      view shows the result); FCM when the app is backgrounded/disconnected.
      The Android side keeps the shade entry visible until the app next comes
      to the foreground — see ``NotificationService.handleEphemeral``.
    * **No recorded origin** (server/scheduler turn, or the proxy restarted
      since the send) → legacy activity rule: an actively-engaged connection
      means the in-app ping covers it; otherwise FCM.

    ``interactive=True`` (terminal turn ends, which have no pump and often no
    origin) overlays a presence rule on the FCM/silent fallthroughs: while ANY
    dashboard connection is active (visible + recent input), the alert takes
    the in-app path — a ``turn_complete`` frame to every active connection —
    instead of buzzing the phone. An active Android connection counts as
    presence (the foreground app ignores the frame and shows the result).
    Without active presence the FCM leg runs, and (interactive only, not
    cli_attached — that turn just rendered on the live terminal) every idle
    WEB connection still gets the frame so an open-but-unattended dashboard
    toasts alongside the push.

    ``cli_attached=True`` (an ``otodock``-CLI terminal owns the session) also
    IGNORES the recorded origin: a CLI attachment is not dashboard presence,
    and a stale origin left by a superseded dashboard viewer must never
    swallow the push. Without active dashboard presence the FCM always fires,
    even while the CLI terminal is open on the remote machine.
    """
    frame = {
        "type": wire.TURN_COMPLETE, "chat_id": chat_id,
        "title": title, "body": body,
    }
    origin = None
    origin_away = False
    if chat_id and not cli_attached:
        origin = _chat_turn_origin.get(chat_id)
    # One INFO line per alert with the routing decision — the only way to
    # diagnose "I never got the end-of-turn notification" from the proxy log
    # (2026-09-03): which device started the turn, whether it is still
    # connected/active, and which leg the alert took.
    _origin_desc = (
        f"{origin[1]}:{origin[0][:8]}" if origin else ("cli" if cli_attached else "none"))

    def _route(leg: str) -> None:
        logger.info(
            "turn_complete: chat=%s user=%s origin=%s interactive=%s → %s",
            (chat_id or "")[:8], user_sub[:8], _origin_desc, interactive, leg,
        )

    if origin:
        conn_id, platform = origin
        conn = get_connection(user_sub, conn_id)
        if platform != "android":
            if not conn:
                _route("dropped (origin browser connection gone)")
                return
            await _safe_put(conn.queue, frame)
            if not conn.away:
                _route("frame to the origin browser")
                return
            # Origin dashboard visibly open but input-idle: the frame above
            # keeps the local toast/sound, and the alert ALSO falls through
            # to the presence/FCM path so the phone hears about it.
            origin_away = True
        elif conn and conn.active:
            _route("silent (Android app in the foreground shows the result)")
            return
        # app-origin backgrounded / away browser-origin → overlay / FCM below
    if interactive or cli_attached:
        active = get_active_connections(user_sub)
        if active:
            for c in active:
                await _safe_put(c.queue, frame)
            _route(f"frame to {len(active)} active connection(s)")
            return
        if interactive and not cli_attached:
            # No active presence → the FCM leg below runs; idle-but-open web
            # dashboards still get the frame so they toast alongside the push
            # (the user may be looking after all). Skipped for cli_attached —
            # that turn just rendered on the live otodock terminal. The away
            # origin conn was already framed above; android conns alert via
            # FCM (their FE ignores the frame anyway).
            for c in get_all_connections(user_sub):
                if c.platform != "web":
                    continue
                if origin_away and c.connection_id == origin[0]:
                    continue
                await _safe_put(c.queue, frame)
    elif (not origin or origin_away) and has_active_connection(user_sub):
        _route("silent (an active dashboard connection covers it)")
        return  # legacy rule: an engaged device's in-app ping covers it

    try:
        from storage.automation import notification_store as ns
        # Deep link for the tap — same route rules as _deliver_to_user's
        # click_url. Ephemeral pushes historically carried NO link, so tapping
        # an end-of-turn notification just foregrounded the app on whatever
        # chat it already showed (live-observed 2026-07-11).
        click_url = "/"
        if chat_id:
            # Task chats open the chat page with task mode toggled on; the
            # /runs/{id} resolver redirect stays the agent-less fallback.
            if session_kind.is_task_chat_id(chat_id):
                click_url = f"/runs/{session_kind.run_id_of_chat(chat_id)}"
            try:
                from storage import database as task_store
                _chat = await asyncio.to_thread(task_store.get_chat, chat_id)
                _agent = (_chat or {}).get("agent")
                if _agent:
                    click_url = (f"/chat/{_agent}/{chat_id}?tasks=1"
                                 if session_kind.is_task_chat_id(chat_id)
                                 else f"/chat/{_agent}/{chat_id}")
            except Exception:
                pass  # link is best-effort — the push itself still matters
        subscriptions = await run_db(ns.get_push_subscriptions, user_sub)
        android = [s for s in subscriptions if s["platform"] == "android"]
        _tokens = len(android)
        payload = {
            "title": title,
            "body": body,
            "severity": "info",
            "ephemeral": True,
            "click_url": click_url,
            "install_id": await _install_id_async(),
        }
        slot = push_sender._push_slot()

        async def _push(sub: dict) -> bool:
            async with slot:
                return await push_sender.send_fcm(sub["subscription_data"], payload)

        _ok = sum(1 for r in await asyncio.gather(*(_push(s) for s in android),
                                                   return_exceptions=True) if r is True)
        _route(f"native push — android tokens={_tokens} sent={_ok}"
               + ("" if _tokens else " (no Android push registration for this user)"))
    except Exception as e:
        logger.warning(f"Failed to send ephemeral push to {user_sub}: {e}")


def _build_delivery_payload(delivery: dict) -> dict:
    """Shape the delivery dict for the WS event body. Shared by both notification and
    notification_silent events so the frontend can treat the payload identically."""
    return {
        "id": delivery["id"],
        "notification_id": delivery.get("notification_id"),
        "title": delivery["title"],
        "body": delivery["body"],
        "severity": delivery["severity"],
        "scope": delivery["scope"],
        "source": delivery["source"],
        "delivered_at": delivery["delivered_at"],
        "agent_slug": delivery.get("agent_slug"),
        "chat_id": delivery.get("chat_id"),
        "href": delivery.get("href") or "",
    }


async def _safe_put(queue: asyncio.Queue, message: dict) -> bool:
    """Try ``queue.put(...)`` and swallow + log any exception. Returns True on success."""
    try:
        await queue.put(message)
        return True
    except Exception as e:
        logger.debug(f"WS queue put failed: {e}")
        return False


_FILE_UPDATED_WINDOW_S = 0.25
_FILE_UPDATED_MAX_FRAMES = 64


@dataclass
class _FileBatch:
    """One agent's pending ``file_updated`` calls: per path, the merged
    source (``disk`` when any call said so), the OR of the pin flags and the
    users every call excluded (the intersection: an agent's write must
    reach the person who saved the same file)."""
    paths: dict[str, dict] = field(default_factory=dict)
    handle: asyncio.TimerHandle | None = None


_file_batches: dict[str, _FileBatch] = {}
_file_flushes: set[asyncio.Task] = set()


async def broadcast_file_updated(
    agent_slug: str, rel_path: str, *, source: str = "disk",
    exclude_user_sub: str = "", pin: bool = False,
) -> None:
    """Push a lightweight ``file_updated`` event to the active dashboard
    connections of users assigned to ``agent_slug`` who are allowed to see
    ``rel_path`` (per-user isolation), so an open Collabora preview / workspace
    file-tree refreshes.

    NOT a notification — no inbox row, no toast, no sound, no DB write. ``source``
    distinguishes a Collabora save (``"collabora"`` — already live-merged among
    the humans editing it, so a peer's open Collabora session doesn't need a
    reload) from an agent / disk write (``"disk"`` — which a live Collabora
    session doesn't know about). The CLIENT decides whether to reload based on
    ``source`` + its own dirty state, and ignores the event for files it doesn't
    have open. ``exclude_user_sub`` skips the writer (no point refreshing their
    own save). Best-effort; never raises.

    Calls are coalesced per agent: the first opens a window of
    ``_FILE_UPDATED_WINDOW_S``, the ones that follow join it, and the flush
    resolves the audience once for the whole batch (a sync burst of N files
    costs one audience read, not N). The call returns once it is queued;
    ``flush_file_updates`` runs a pending window now."""
    if not agent_slug or not rel_path:
        return
    try:
        batch = _file_batches.get(agent_slug)
        if batch is None:
            batch = _file_batches[agent_slug] = _FileBatch()
            batch.handle = asyncio.get_running_loop().call_later(
                _FILE_UPDATED_WINDOW_S, _flush_file_batch, agent_slug)
        excluded = {exclude_user_sub} if exclude_user_sub else set()
        entry = batch.paths.get(rel_path)
        if entry is None:
            batch.paths[rel_path] = {"source": source, "pin": bool(pin), "exclude": excluded}
        else:
            entry["source"] = "disk" if "disk" in (entry["source"], source) else source
            entry["pin"] = entry["pin"] or bool(pin)
            entry["exclude"] &= excluded
    except Exception:
        logger.debug("broadcast_file_updated failed for %s/%s", agent_slug, rel_path, exc_info=True)


def _flush_file_batch(agent_slug: str) -> None:
    batch = _file_batches.pop(agent_slug, None)
    if batch is None:
        return
    if batch.handle is not None:
        batch.handle.cancel()
    task = asyncio.get_running_loop().create_task(_send_file_updates(agent_slug, batch.paths))
    _file_flushes.add(task)
    task.add_done_callback(_file_flushes.discard)


async def flush_file_updates() -> None:
    """Flush every pending window now and wait for the sends (tests, the
    shutdown)."""
    for agent_slug in list(_file_batches):
        _flush_file_batch(agent_slug)
    while _file_flushes:
        await asyncio.gather(*list(_file_flushes), return_exceptions=True)


async def _send_file_updates(agent_slug: str, paths: dict[str, dict]) -> None:
    """One batch's frames: the audience and each active user's role and
    username read in one job, then one ``file_updated`` per path to each
    user's active sockets (a batch past ``_FILE_UPDATED_MAX_FRAMES`` sends
    that many per path and one agent-level frame with an empty ``rel_path``,
    which every tree view refreshes on and no per-path consumer matches),
    and each path handed to the catalog once."""
    try:
        import base64

        from core.remote.file_sync import should_sync_to_target
        from storage import database as task_store

        def _audience() -> tuple[list[str], dict[str, tuple[str, str]]]:
            subs = resolve_targets("agent", agent_slug) or []
            active = [u for u in subs if any(c.active for c in _user_connections.get(u, []))]
            return subs, {u: (acting_role_of(u, agent_slug),
                              task_store.get_username_by_sub(u) or "") for u in active}

        _, by_user = await run_db(_audience)

        def _file_id(rel_path: str) -> str:
            # base64url of the AGENTS_DIR-relative path == api.media.wopi.encode_file_id,
            # so the client can match this event to an open Collabora preview by its
            # file_id without a path round-trip.
            return base64.urlsafe_b64encode(f"{agent_slug}/{rel_path}".encode()).decode().rstrip("=")

        from api.apps import catalog
        reached: set[str] = set()
        # The pins and the Collabora saves first (a pins list and an open
        # preview match their path; a tree view matches any frame), then
        # the disk writes in order: the cap falls on the writes alone.
        ordered = sorted(paths.items(),
                         key=lambda kv: not (kv[1]["pin"] or kv[1]["source"] != "disk"))
        for n, (rel_path, entry) in enumerate(ordered):
            msg = {
                "type": wire.FILE_UPDATED,
                "agent_slug": agent_slug,
                "rel_path": rel_path,
                "file_id": _file_id(rel_path),
                "source": entry["source"],
            }
            if entry["pin"]:
                # Dock pin membership changed for this path (file pinned or
                # unpinned) — clients refresh the pins list, not just content.
                msg["pin"] = True
            told: list[str] = []
            for user_sub, (role, username) in by_user.items():
                if user_sub in entry["exclude"]:
                    continue
                # Per-user isolation: never tell a user about a path they can't see
                # (another user's users/{u}/ file, or config/ for a non-owner) —
                # the same predicate the workspace fan-out applies.
                if not should_sync_to_target(rel_path, username, role):
                    continue
                told.append(user_sub)
                if n < _FILE_UPDATED_MAX_FRAMES:
                    for c in _user_connections.get(user_sub, []):
                        if c.active:
                            await _safe_put(c.queue, msg)
                else:
                    reached.add(user_sub)
            # The catalog's ``file_changes`` feed, ONE delta per change: frames to
            # the users told above (on the live queue, so an open app hears it
            # mid-turn), and the agent's app servers and handlers take it once,
            # whether or not anyone is watching.
            try:
                catalog.file_changed(told, agent_slug, rel_path,
                                     file_id=msg["file_id"] if entry["source"] == "collabora" else "",
                                     source=entry["source"])
            except Exception as e:
                logger.debug("catalog file_changes delta: %s", e)
        if reached:
            summary = {"type": wire.FILE_UPDATED, "agent_slug": agent_slug,
                       "rel_path": "", "file_id": "", "source": "disk"}
            for user_sub in reached:
                for c in _user_connections.get(user_sub, []):
                    if c.active:
                        await _safe_put(c.queue, summary)
    except Exception:
        logger.debug("file_updated batch for %s failed", agent_slug, exc_info=True)


async def _deliver_to_user(user_sub: str, delivery: dict) -> None:
    """Route a delivery via WS toast OR native push — never both.

    * Active connections → WS ``notification`` event (toast + sound on that device).
    * Inactive connections → WS ``notification_silent`` event (inbox + badge only, no alert).
    * No active connection at all → native push (FCM + Web Push) handles the alert.

    The DB row was already written in ``fire_notification`` before this is called, so the inbox
    always has the entry regardless of which path runs.
    """
    payload = _build_delivery_payload(delivery)
    active_conns = get_active_connections(user_sub)
    all_conns = get_all_connections(user_sub)
    inactive_conns = [c for c in all_conns if not c.active]
    # The catalog's ``notifications`` feed (apps showing the viewer's inbox).
    try:
        from api.apps import catalog
        catalog.notification_delivered(user_sub, delivery)
    except Exception as e:
        logger.debug("catalog notifications delta: %s", e)

    if active_conns:
        # WS toast to every actively-engaged device.
        for c in active_conns:
            ok = await _safe_put(c.queue, {"type": wire.NOTIFICATION, "delivery": payload})
            if ok:
                logger.debug(
                    f"Notification delivered via WS (active) to {user_sub} "
                    f"conn={c.connection_id[:8]} platform={c.platform}: {delivery['title']}"
                )
        # Silent inbox update to any inactive WS so its badge stays in sync.
        for c in inactive_conns:
            await _safe_put(c.queue, {"type": wire.NOTIFICATION_SILENT, "delivery": payload})
        return

    # No active connection — native push is the alert channel.
    push_attempted = False
    try:
        _agent = delivery.get("agent_slug")
        _cid = delivery.get("chat_id")
        if delivery.get("href"):
            click_url = delivery["href"]
        elif delivery.get("source") == "file_conflict" and _agent:
            # File-conflict notifications deep-link to the workspace Recover bin
            # (no chat_id); AgentChat reads ?recover=1 and opens the modal.
            click_url = f"/chat/{_agent}?recover=1"
        elif session_kind.is_task_chat_id(_cid):
            # Task notifications deep-link to the chat page with task mode on;
            # without an agent slug, the /runs/{run_id} resolver redirects.
            click_url = (f"/chat/{_agent}/{_cid}?tasks=1" if _agent
                         else f"/runs/{session_kind.run_id_of_chat(_cid)}")
        elif _agent and _cid:
            click_url = f"/chat/{_agent}/{_cid}"
        else:
            click_url = "/"
        await push_sender.send_to_user(user_sub, {
            # Native push has no agent header → the display name rides the
            # title (in-app rows keep the raw title + a header, see push_title).
            "title": push_title(delivery["title"], _agent),
            "body": delivery["body"],
            "delivery_id": delivery["id"],
            "severity": delivery["severity"],
            "click_url": click_url,
            "install_id": await _install_id_async(),
        })
        push_attempted = True
        logger.debug(
            f"Notification delivered via native push to {user_sub} "
            f"(no active connection): {delivery['title']}"
        )
    except Exception as e:
        logger.warning(f"Push delivery failed to {user_sub}: {e}")

    # Silent inbox update to all connected-but-inactive WSes so the badge/inbox stay current
    # when the user returns to the dashboard.
    for c in all_conns:
        await _safe_put(c.queue, {"type": wire.NOTIFICATION_SILENT, "delivery": payload})

    if not all_conns and not push_attempted:
        logger.debug(f"No delivery channel for {user_sub} — entry remains in DB inbox only")


# --- Scheduling ---

_scheduler_ref = None  # Set at startup


def start() -> None:
    """Initialize notification system. Called at proxy startup after scheduler.start()."""
    _register_audience_invalidation()
    _schedule_audience_warmup()
    if config.SCHEDULER_MODE == "standalone":
        logger.info(
            "Notification manager started "
            "(scheduling handled by standalone scheduler)"
        )
        return

    from services.scheduler import scheduler as sched_module
    global _scheduler_ref
    _scheduler_ref = sched_module.get_scheduler()

    # DB tables initialized in app.py via pg_schema.init_schema()

    # Schedule all enabled notifications
    _schedule_all_notifications()

    logger.info("Notification manager started")


def _schedule_all_notifications() -> None:
    """Register all enabled notifications with APScheduler."""
    if not _scheduler_ref:
        logger.warning("Scheduler not available, skipping notification scheduling")
        return

    notifications = notification_store.list_notifications(enabled_only=True)
    scheduled = 0
    for n in notifications:
        if _register_notification(n):
            scheduled += 1

    if scheduled:
        logger.info(f"Scheduled {scheduled} notifications")


def _register_notification(notif: dict) -> bool:
    """Register a single notification with APScheduler. Returns True if registered."""
    if not _scheduler_ref:
        return False

    from apscheduler.triggers.date import DateTrigger

    from services.scheduler import scheduler_triggers

    job_id = f"notif_{notif['id']}"
    ntype = notif.get("notification_type", "one_time")

    # Resolve the row's TZ — user_tz (browser-snapshotted at create) overrides
    # platform default. Invalid IANA → fall back to platform silently.
    tz_name = notif.get("user_tz") or config.get_platform_timezone()
    try:
        notif_tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        logger.warning(f"Invalid user_tz {tz_name!r} on notification {notif['id']}; using platform TZ")
        notif_tz = config.get_platform_tz()

    try:
        if ntype == "recurring" and notif.get("schedule"):
            trigger = scheduler_triggers.build_cron_trigger(notif["schedule"], notif_tz)
        elif ntype == "recurring" and notif.get("interval_seconds") is not None:
            # Anchor start_date at `created_at + interval_seconds`. Mirrors the
            # task-side anchor (see scheduler_triggers.build_interval_trigger).
            # Normalise a datetime created_at → ISO before handing to the helper.
            created_at = notif.get("created_at")
            if created_at is not None and not isinstance(created_at, str):
                created_at = created_at.isoformat()
            trigger = scheduler_triggers.build_interval_trigger(
                notif["interval_seconds"], created_at, notif_tz,
            )
        elif ntype == "one_time" and notif.get("run_at"):
            run_date = datetime.fromisoformat(notif["run_at"])
            if run_date.tzinfo is None:
                # Naive datetimes are assumed to be in the row's TZ (user_tz
                # snapshot or platform fallback). Agents write local time.
                run_date = run_date.replace(tzinfo=notif_tz)
            if run_date < datetime.now(run_date.tzinfo):
                logger.debug(f"Skipping past notification: {notif['id']}")
                return False
            trigger = DateTrigger(run_date=run_date)
        else:
            # Immediate or no schedule — don't register with scheduler
            return False

        _scheduler_ref.add_job(
            _fire_scheduled_notification,
            trigger=trigger,
            args=[notif["id"]],
            id=job_id,
            name=f"notif: {notif.get('title', notif['id'])}",
            misfire_grace_time=300,
            coalesce=True,
            replace_existing=True,
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to schedule notification {notif['id']}: {e}")
        return False


def unregister_notification(notification_id: str) -> None:
    """Remove a notification from APScheduler."""
    if config.SCHEDULER_MODE == "standalone" or not _scheduler_ref:
        return
    job_id = f"notif_{notification_id}"
    with contextlib.suppress(Exception):  # job may not exist
        _scheduler_ref.remove_job(job_id)


async def _fire_scheduled_notification(notification_id: str) -> None:
    """APScheduler callback: fire a scheduled notification."""
    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id
    )
    if not notif or not notif.get("enabled"):
        return

    await fire_notification(
        title=notif["title"],
        body=notif["body"],
        severity=notif["severity"],
        scope=notif["scope"],
        target=notif.get("target"),
        source=notif["source"],
        source_id=notif.get("source_id"),
        notification_id=notification_id,
        agent_slug=notif.get("agent_slug"),
        chat_id=notif.get("chat_id"),
    )


def schedule_new_notification(notif: dict) -> bool:
    """Schedule a newly created notification. Called after API creates it."""
    if config.SCHEDULER_MODE == "standalone":
        return False  # Standalone scheduler picks it up via DB sync
    return _register_notification(notif)


async def pause_notification(notification_id: str) -> bool:
    """Set enabled=FALSE in DB and unregister from APScheduler (embedded mode).

    Returns True if the notification exists. Idempotent.
    Standalone mode picks up the change via DB sync (≤ SCHEDULER_SYNC_INTERVAL).
    Defence-in-depth: ``_fire_scheduled_notification`` already short-circuits
    on ``enabled=FALSE``, so a stale fire during the sync window is harmless.
    """
    ok = await asyncio.to_thread(
        notification_store.set_notification_enabled, notification_id, False,
    )
    if ok and config.SCHEDULER_MODE != "standalone":
        unregister_notification(notification_id)
    return ok


async def resume_notification(notification_id: str) -> bool:
    """Set enabled=TRUE in DB and re-register with APScheduler (embedded mode).

    Returns True if the notification exists. For one-time notifications whose
    ``run_at`` is in the past, ``_register_notification`` returns False — the
    row stays enabled but no job is scheduled. The user can press 'Fire Now'
    in the UI to fire manually.
    Standalone mode picks up the change via DB sync.
    """
    ok = await asyncio.to_thread(
        notification_store.set_notification_enabled, notification_id, True,
    )
    if not ok or config.SCHEDULER_MODE == "standalone":
        return ok
    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id,
    )
    if notif and notif.get("enabled"):
        _register_notification(notif)
    return ok


async def delete_notification(notification_id: str) -> bool:
    """Hard-delete: unregister from APScheduler then delete the DB row.

    Returns True if the row was deleted. Standalone mode picks up the row
    removal via DB sync; the cleanup loop drops the orphaned APScheduler job.
    """
    if config.SCHEDULER_MODE != "standalone":
        unregister_notification(notification_id)
    return await asyncio.to_thread(
        notification_store.delete_notification, notification_id,
    )


_NOTIF_TIMING_FIELDS = {"schedule", "run_at", "interval_seconds", "user_tz"}


async def update_notification(notification_id: str, fields: dict) -> tuple[bool, str | None]:
    """Apply a partial update + reschedule if timing changed.

    Validates cron / ISO datetime, normalises mutually exclusive timing
    fields, auto-derives ``notification_type`` (recurring when schedule is
    set, one_time when run_at is set), updates DB, then re-registers the
    APScheduler job in embedded mode.

    Returns ``(ok, error_message)``. ``error_message`` is non-empty when
    validation failed (caller maps to HTTP 400).
    """
    from services.scheduler import scheduler_triggers

    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id,
    )
    if not notif:
        return False, None  # caller maps to 404

    payload = dict(fields)

    # Validate user_tz first — drives naive run_at parsing below.
    edit_tz_name: str
    if "user_tz" in payload and payload["user_tz"] is not None:
        try:
            zoneinfo.ZoneInfo(payload["user_tz"])
        except Exception as e:
            return False, f"Invalid user_tz: {e}"
        edit_tz_name = payload["user_tz"]
    else:
        edit_tz_name = notif.get("user_tz") or config.get_platform_timezone()
    try:
        edit_tz = zoneinfo.ZoneInfo(edit_tz_name)
    except Exception:
        edit_tz = config.get_platform_tz()

    # Validate cron string against the post-edit TZ (build_cron_trigger, so
    # the standard-cron day-of-week remap validates too — never from_crontab)
    if "schedule" in payload and payload["schedule"] is not None:
        try:
            scheduler_triggers.build_cron_trigger(payload["schedule"], edit_tz)
        except Exception as e:
            return False, f"Invalid cron schedule: {e}"

    # Validate ISO datetime — naive interpreted in post-edit TZ (user_tz or
    # platform fallback). Aligns with _register_notification's behaviour.
    if "run_at" in payload and payload["run_at"] is not None:
        try:
            run_date = datetime.fromisoformat(payload["run_at"])
            if run_date.tzinfo is None:
                payload["run_at"] = run_date.replace(tzinfo=edit_tz).isoformat()
        except Exception as e:
            return False, f"Invalid run_at: {e}"

    # Validate interval bounds when present (lazy import to keep this module
    # cheap to import for the standalone scheduler path).
    if "interval_seconds" in payload and payload["interval_seconds"] is not None:
        from services.scheduler.scheduler import _validate_interval_seconds
        err = _validate_interval_seconds(payload["interval_seconds"])
        if err:
            return False, err

    # Mutual exclusivity + auto-derive notification_type
    if payload.get("schedule"):
        payload["interval_seconds"] = None
        payload["run_at"] = None
        payload["notification_type"] = "recurring"
    elif payload.get("interval_seconds"):
        payload["schedule"] = None
        payload["run_at"] = None
        payload["notification_type"] = "recurring"
    elif payload.get("run_at"):
        payload["schedule"] = None
        payload["interval_seconds"] = None
        payload["notification_type"] = "one_time"

    timing_changed = any(k in payload for k in _NOTIF_TIMING_FIELDS)

    ok = await asyncio.to_thread(
        notification_store.update_notification, notification_id, payload,
    )
    if not ok:
        return False, None

    # Re-register only when timing changed and we're in embedded mode.
    if timing_changed and config.SCHEDULER_MODE != "standalone":
        refreshed = await asyncio.to_thread(
            notification_store.get_notification, notification_id,
        )
        if refreshed and refreshed.get("enabled"):
            unregister_notification(notification_id)
            _register_notification(refreshed)
    return True, None
