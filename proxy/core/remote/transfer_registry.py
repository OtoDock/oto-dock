"""In-flight workspace transfer registry — per-user fan-out + connect replay.

Feeds the workspace progress UI (Feature E, 1.4.0): each entry is one tracked
file transfer — a dashboard upload's background fan-out, or a live sync push
of a big file — keyed by ``transfer_id``, with ONE ROW PER TARGET MACHINE
(state ``queued|active|done|failed`` + byte progress).

Modeled on ``install_registry`` (same registered-broadcaster delivery through
the per-user dashboard notify queues) with two differences:

* **Snapshot-state replay** instead of event-history replay: transfer
  progress is idempotent state, so a connecting dashboard receives ONE
  ``transfer_state`` event per in-flight item (``snapshot_event``) rather
  than a replayed history.
* **Recipients are resolved once at ``begin()``** using the exact
  per-user-isolation pattern of ``broadcast_file_updated``: all users of the
  agent, filtered per-user with ``should_sync_to_target(rel_path, username,
  role)``. A personal ``users/<u>/…`` upload reaches only its owner
  (including vs admins); shared ``workspace/`` reaches every member;
  ``config/`` reaches owner-tier only. The cached frozenset also gates
  connect replay — the server never emits a transfer to a non-recipient.

Every function is fully defensive (never raises into a transfer path): a
broken registry must never abort a push.

Lifecycle: ``begin`` (rows born 'queued') → per-machine ``set_state``
('active' at push start — Feature F moves this into its gate's on_state
callback — then terminal 'done'/'failed') + throttled ``progress`` ticks →
all-terminal emits ``transfer_done`` → ``sweep_stale`` evicts after a short
done-linger (or fails out silently-wedged items).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

logger = logging.getLogger("transfer-registry")

# Progress events per (item, machine) at most every this many seconds; state
# transitions and the terminal 100% tick always emit. Window acks already make
# ticks coarse on WAN (8MB per ack) — this only matters on fast LANs.
PROGRESS_MIN_INTERVAL = 0.5

# Terminal items linger this long before eviction so the dashboard shows the
# done/failed state briefly (the client prunes at ~5s — slightly earlier, so a
# replayed item never outlives its server record).
DONE_LINGER_S = 6.0

# No event for this long on a non-terminal item → the push wedged or its
# terminal set_state was lost; fail the remaining rows and evict. push_file's
# window-ack timeout is 30-60s, so 120s of silence is decisive.
STALE_AGE_S = 120.0

# ``fn(event, recipients)`` pushes the event into each recipient's dashboard
# notify queues (ws/satellite.py::push_transfer_event, registered at startup).
Broadcaster = Callable[[dict, list[str]], Awaitable[None]]
_broadcaster: Broadcaster | None = None


def set_broadcaster(fn: Broadcaster | None) -> None:
    global _broadcaster
    _broadcaster = fn


@dataclass
class MachineRow:
    machine_id: str
    name: str
    state: str = "queued"  # queued | active | done | failed
    bytes_sent: int = 0
    bytes_total: int = 0
    error: str = ""
    _last_progress_emit: float = 0.0


@dataclass
class TransferItem:
    transfer_id: str
    agent_slug: str
    rel_path: str
    kind: str  # "upload" | "sync"
    bytes_total: int
    origin_user_sub: str
    recipients: frozenset[str]
    machines: dict[str, MachineRow]
    started_at: float = field(default_factory=time.monotonic)
    started_at_iso: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_event_ts: float = field(default_factory=time.monotonic)
    done_at: float | None = None

    @property
    def filename(self) -> str:
        return self.rel_path.rsplit("/", 1)[-1]


_items: dict[str, TransferItem] = {}
_lock = asyncio.Lock()


def _machine_row_event(row: MachineRow) -> dict:
    return {
        "machine_id": row.machine_id,
        "name": row.name,
        "state": row.state,
        "bytes_sent": row.bytes_sent,
        "bytes_total": row.bytes_total,
        **({"error": row.error} if row.error else {}),
    }


def snapshot_event(item: TransferItem) -> dict:
    """The connect-replay event: full current state of one item."""
    return {
        "type": "transfer_state",
        "transfer_id": item.transfer_id,
        "agent_slug": item.agent_slug,
        "rel_path": item.rel_path,
        "filename": item.filename,
        "kind": item.kind,
        "bytes_total": item.bytes_total,
        "started_at": item.started_at_iso,
        "machines": [_machine_row_event(r) for r in item.machines.values()],
        **({"done": True} if item.done_at is not None else {}),
    }


async def _emit(event: dict, recipients: frozenset[str]) -> None:
    if _broadcaster is None or not recipients:
        return
    try:
        await _broadcaster(event, list(recipients))
    except Exception:
        logger.exception("transfer_registry broadcaster raised")


def _resolve_recipients(agent_slug: str, rel_path: str) -> frozenset[str]:
    """All users of the agent allowed to see ``rel_path`` — the exact
    ``broadcast_file_updated`` pattern (blocking; call via to_thread)."""
    from core.remote.file_sync import should_sync_to_target
    from services.notifications.notification_manager import resolve_targets
    from storage import database as task_store

    out: set[str] = set()
    for user_sub in resolve_targets("agent", agent_slug):
        u = task_store.get_user(user_sub) or {}
        if u.get("role") == "admin":
            role = "admin"
        else:
            role = (task_store.get_user_agent_roles(user_sub) or {}).get(
                agent_slug, "viewer",
            )
        username = task_store.get_username_by_sub(user_sub) or ""
        if should_sync_to_target(rel_path, username, role):
            out.add(user_sub)
    return frozenset(out)


def _machine_name(machine_id: str) -> str:
    try:
        from storage import remote_store
        m = remote_store.get_remote_machine(machine_id) or {}
        return m.get("name") or machine_id[:8]
    except Exception:
        return machine_id[:8]


async def begin(
    agent_slug: str,
    rel_path: str,
    *,
    kind: str,
    bytes_total: int,
    machine_ids: list[str],
    transfer_id: str | None = None,
    origin_user_sub: str = "",
) -> str | None:
    """Create a tracked transfer with one 'queued' row per machine and emit
    ``transfer_started``. Returns the transfer_id, or None if creation failed
    (callers treat None as 'untracked' and push anyway)."""
    try:
        tid = transfer_id or str(uuid.uuid4())
        recipients = await asyncio.to_thread(
            _resolve_recipients, agent_slug, rel_path,
        )
        names = {
            mid: await asyncio.to_thread(_machine_name, mid)
            for mid in machine_ids
        }
        item = TransferItem(
            transfer_id=tid,
            agent_slug=agent_slug,
            rel_path=rel_path,
            kind=kind,
            bytes_total=bytes_total,
            origin_user_sub=origin_user_sub,
            recipients=recipients,
            machines={
                mid: MachineRow(
                    machine_id=mid, name=names[mid], bytes_total=bytes_total,
                )
                for mid in machine_ids
            },
        )
        async with _lock:
            _items[tid] = item
        await _emit({
            "type": "transfer_started",
            "transfer_id": tid,
            "agent_slug": agent_slug,
            "rel_path": rel_path,
            "filename": item.filename,
            "kind": kind,
            "bytes_total": bytes_total,
            "started_at": item.started_at_iso,
            "machines": [_machine_row_event(r) for r in item.machines.values()],
        }, recipients)
        if not machine_ids:
            # Tracked transfer with NO target: the cheap has_fanout_candidates
            # gate over-approximates (any connected machine says "yes"), so
            # the upload response already promised remote_push=true and the
            # client waits for a terminal. Complete at birth — with zero rows
            # set_state would never fire transfer_done and the popup/chip hung
            # on "Processing…" / "Syncing to machines…" forever (found live
            # 2026-09-02 on an install with idle satellites).
            async with _lock:
                item.done_at = time.monotonic()
            await _emit({
                "type": "transfer_done",
                "transfer_id": tid,
                "agent_slug": agent_slug,
                "ok": True,
            }, recipients)
        return tid
    except Exception:
        logger.exception(
            "transfer_registry.begin failed for %s/%s", agent_slug, rel_path,
        )
        return None


async def set_state(
    transfer_id: str, machine_id: str, state: str, *, error: str = "",
) -> None:
    """Transition one machine row; emits ``transfer_machine_state``. When all
    rows turn terminal, stamps ``done_at`` and emits ``transfer_done``.
    Idempotent on repeated terminal sets; never raises."""
    try:
        done_event = None
        async with _lock:
            item = _items.get(transfer_id)
            if item is None:
                return
            row = item.machines.get(machine_id)
            if row is None:
                return
            if row.state in ("done", "failed") and state in ("done", "failed"):
                return  # already terminal — idempotent
            row.state = state
            row.error = error
            if state == "done":
                row.bytes_sent = row.bytes_total
            item.last_event_ts = time.monotonic()
            recipients = item.recipients
            if (
                item.done_at is None
                and all(r.state in ("done", "failed") for r in item.machines.values())
            ):
                item.done_at = time.monotonic()
                done_event = {
                    "type": "transfer_done",
                    "transfer_id": transfer_id,
                    "agent_slug": item.agent_slug,
                    "ok": all(r.state == "done" for r in item.machines.values()),
                }
        await _emit({
            "type": "transfer_machine_state",
            "transfer_id": transfer_id,
            "agent_slug": item.agent_slug,
            "machine_id": machine_id,
            "state": state,
            **({"error": error} if error else {}),
        }, recipients)
        if done_event is not None:
            await _emit(done_event, recipients)
    except Exception:
        logger.exception("transfer_registry.set_state failed")


async def progress(
    transfer_id: str, machine_id: str, bytes_sent: int, bytes_total: int,
) -> None:
    """Update a row's byte progress; throttled emit (state flips to 'active'
    implicitly if a tick lands on a still-queued row). Never raises."""
    try:
        async with _lock:
            item = _items.get(transfer_id)
            if item is None:
                return
            row = item.machines.get(machine_id)
            if row is None:
                return
            row.bytes_sent = bytes_sent
            row.bytes_total = bytes_total or row.bytes_total
            if row.state == "queued":
                row.state = "active"
            item.last_event_ts = time.monotonic()
            now = time.monotonic()
            final = bytes_sent >= (bytes_total or row.bytes_total)
            if not final and now - row._last_progress_emit < PROGRESS_MIN_INTERVAL:
                return
            row._last_progress_emit = now
            recipients = item.recipients
        await _emit({
            "type": "transfer_progress",
            "transfer_id": transfer_id,
            "agent_slug": item.agent_slug,
            "machine_id": machine_id,
            "bytes_sent": bytes_sent,
            "bytes_total": bytes_total or row.bytes_total,
        }, recipients)
    except Exception:
        logger.exception("transfer_registry.progress failed")


def get(transfer_id: str) -> TransferItem | None:
    return _items.get(transfer_id)


def snapshot_inflight() -> list[TransferItem]:
    """All current items (in-flight + lingering) for connect replay — the
    caller filters by ``user_sub in item.recipients``."""
    return list(_items.values())


async def sweep_stale() -> int:
    """Evict terminal items past DONE_LINGER_S; fail-out + evict silently
    wedged items past STALE_AGE_S. Returns count removed."""
    now = time.monotonic()
    removed = 0
    to_fail: list[tuple[TransferItem, list[str]]] = []
    async with _lock:
        for tid in list(_items.keys()):
            item = _items[tid]
            if item.done_at is not None:
                if now - item.done_at > DONE_LINGER_S:
                    _items.pop(tid, None)
                    removed += 1
            elif now - item.last_event_ts > STALE_AGE_S:
                stale_rows = [
                    r.machine_id for r in item.machines.values()
                    if r.state not in ("done", "failed")
                ]
                _items.pop(tid, None)
                removed += 1
                to_fail.append((item, stale_rows))
    for item, rows in to_fail:
        for mid in rows:
            await _emit({
                "type": "transfer_machine_state",
                "transfer_id": item.transfer_id,
                "agent_slug": item.agent_slug,
                "machine_id": mid,
                "state": "failed",
                "error": "transfer stalled",
            }, item.recipients)
        await _emit({
            "type": "transfer_done",
            "transfer_id": item.transfer_id,
            "agent_slug": item.agent_slug,
            "ok": False,
        }, item.recipients)
    if removed:
        logger.info("transfer_registry: swept %d entries", removed)
    return removed
