"""What each machine is owed: the names of the files the platform changed
and that machine does not hold yet.

A platform write never waits for a machine. It adds the file's name to the
lane of every machine that should get it and returns. A few workers per
machine take names while the machine is connected: a worker opens the
platform copy as it is then, hashes that descriptor and pushes it, or sends
the delete when the name's last change was one. Nothing is held per write
(no bytes, no descriptor), so a name changed twice is pushed once, a write
and a delete of one path keep their order, a push never carries bytes older
than the platform's copy, and a tree of any size costs short strings.

One machine's lane never waits on another machine. A machine that drops
keeps its names and gets them when it returns. Past
``config.SAT_PUSH_LANE_MAX_NAMES`` names of one agent, or ``NAMES_MAX_BYTES``
of names on one machine, the names are dropped and one background merge is
owed instead, again until one has run.

A name is judged when it is marked. One that waited, for a machine that was
away, is judged again before its push: that machine's pairing or its person's
role may have changed meanwhile.

Readers: ``pending`` (a read on that machine must serve the platform's copy
while a name waits), ``settled`` (the turn barrier, the asking machine's
wait) and ``drain`` (a planned stop's last wait, ``startup_drain._settle_pushes``, and tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.background import spawn

logger = logging.getLogger("otodock.push-lane")

# Pushes of one machine that run at once (larger ones also contend on the
# machine's transfer gate, every byte on its connection's credit).
WORKERS = 4
# A machine away this long loses the names it is owed: its next merge
# settles everything.
NAMES_KEEP_S = 3600.0
# What one machine's names may hold (a name costs ``_NAME_BYTES`` plus its
# path, and nothing bounds a path's length where a name is made).
NAMES_MAX_BYTES = 64 * 1024 * 1024
_NAME_BYTES = 388
# A name older than this is judged again before its push.
REJUDGE_AFTER_S = 30.0
# A push that raises (a store that does not answer) is tried this many times,
# with a pause that doubles, before the machine is left to the next merge.
PUSH_TRIES = 3
_PUSH_PAUSE_MAX_S = 30.0
# The pause before a merge that did not run is owed again.
MERGE_RETRY_S = 30.0

_STALE_TEXT = "The file is being changed on the machine. It was left as it is."
_FAILED_TEXT = "The file did not reach this machine. The next sync brings it."
_GONE_TEXT = "The file is no longer on the platform, so nothing was sent."
_NOT_ALLOWED_TEXT = "This machine may no longer receive this file."


@dataclass
class _Name:
    # The newest change this name carries, and the one that put it here: a
    # wait that began before a later change still counts the name.
    seq: int
    first: int
    cost: int = 0
    deleted: bool = False
    holds_turns: bool = False
    # (transfer id, size): the progress rows that end with this name here.
    transfers: list[tuple[str, int]] = field(default_factory=list)
    marked_at: float = field(default_factory=time.monotonic)
    tries: int = 0


@dataclass
class _Lane:
    machine_id: str
    names: dict[tuple[str, str], _Name] = field(default_factory=dict)
    # The names being pushed.
    inflight: dict[tuple[str, str], _Name] = field(default_factory=dict)
    seq: int = 0
    per_agent: dict[str, int] = field(default_factory=dict)
    name_bytes: int = 0
    merge_owed: set[str] = field(default_factory=set)
    merging: set[str] = field(default_factory=set)
    # The agents whose owed merge a turn went on without: no turn waits for it again.
    merge_told: set[str] = field(default_factory=set)
    workers: set[asyncio.Task] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    moved_at: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)

    def wake(self, *, moved: bool = True) -> None:
        """Something ended or moved: waiters look again. ``moved``: it counts
        as movement, which a merge that did not run does not."""
        if moved:
            self.moved_at = time.monotonic()
        self.changed.set()
        self.changed = asyncio.Event()


_lanes: dict[str, _Lane] = {}


def _manager():
    from core.remote.satellite_connection import get_connection_manager
    return get_connection_manager()


def _lane(machine_id: str) -> _Lane:
    lane = _lanes.get(machine_id)
    if lane is None:
        lane = _lanes[machine_id] = _Lane(machine_id)
    return lane


def seq(machine_id: str) -> int:
    """The count of changes noted for ``machine_id`` so far."""
    lane = _lanes.get(machine_id)
    return lane.seq if lane is not None else 0


def mark(machine_id: str, agent_slug: str, rel_path: str, *, deleted: bool = False,
         holds_turns: bool = False, transfer: tuple[str, int] | None = None) -> int:
    """``machine_id`` is owed ``rel_path`` of ``agent_slug`` as the platform
    holds it from now (``deleted``: its removal). Returns the change's
    number on that machine's lane. Never waits."""
    import config
    lane = _lane(machine_id)
    now = time.monotonic()
    lane.seq += 1
    lane.touched = now
    key = (agent_slug, rel_path)
    if agent_slug in lane.merge_owed:
        # The whole agent is settled by its merge: nothing is kept by name.
        if holds_turns:
            lane.merge_told.discard(agent_slug)
        if transfer is not None:
            _end_rows_soon(machine_id, [transfer], _FAILED_TEXT)
        _ensure_workers(lane)
        return lane.seq
    name = lane.names.get(key)
    if name is None:
        name = _Name(seq=lane.seq, first=lane.seq, cost=_NAME_BYTES + len(rel_path))
        _put(lane, key, name)
    name.seq = lane.seq
    name.marked_at = now
    name.deleted = deleted
    name.holds_turns = name.holds_turns or holds_turns
    if transfer is not None:
        name.transfers.append(transfer)
    if lane.name_bytes > NAMES_MAX_BYTES:
        for agent in list(lane.per_agent):
            _overflow(lane, agent)
    elif lane.per_agent[agent_slug] > config.SAT_PUSH_LANE_MAX_NAMES:
        _overflow(lane, agent_slug)
    _ensure_workers(lane)
    return lane.seq


def _put(lane: _Lane, key: tuple[str, str], name: _Name) -> None:
    lane.names[key] = name
    lane.per_agent[key[0]] = lane.per_agent.get(key[0], 0) + 1
    lane.name_bytes += name.cost


def _take(lane: _Lane, key: tuple[str, str]) -> _Name:
    name = lane.names.pop(key)
    left = lane.per_agent.get(key[0], 1) - 1
    if left > 0:
        lane.per_agent[key[0]] = left
    else:
        lane.per_agent.pop(key[0], None)
    lane.name_bytes -= name.cost
    return name


def _overflow(lane: _Lane, agent_slug: str) -> None:
    """Too many names for one machine: they go, and one background merge of
    the pair settles what they named."""
    rows: list[tuple[str, int]] = []
    for key in [k for k in lane.names if k[0] == agent_slug]:
        rows.extend(_take(lane, key).transfers)
    lane.merge_owed.add(agent_slug)
    lane.merge_told.discard(agent_slug)
    logger.warning(
        "machine %s is owed more than its bound of names of %s: one merge settles them",
        lane.machine_id[:8], agent_slug,
    )
    if rows:
        _end_rows_soon(lane.machine_id, rows, _FAILED_TEXT)
    lane.wake()


WRITE, DELETE, MERGE = "write", "delete", "merge"


def owed(machine_id: str, agent_slug: str, rel_path: str) -> str:
    """What ``machine_id`` is still owed for ``rel_path``: ``""`` (nothing),
    ``WRITE`` (the platform's copy is on its way: a read on that machine
    serves it and never pulls the machine's older bytes over it), ``DELETE``
    (its removal is on its way: a read there finds no file and never pulls
    the machine's copy back) or ``MERGE`` (the whole agent is settled by a
    merge it is owed)."""
    lane = _lanes.get(machine_id)
    if lane is None:
        return ""
    key = (agent_slug, rel_path)
    name = lane.names.get(key)
    if name is not None:
        return DELETE if name.deleted else WRITE
    flight = lane.inflight.get(key)
    if flight is not None:
        return DELETE if flight.deleted else WRITE
    if agent_slug in lane.merge_owed or agent_slug in lane.merging:
        return MERGE
    return ""


def pending(machine_id: str, agent_slug: str, rel_path: str) -> bool:
    return bool(owed(machine_id, agent_slug, rel_path))


def _waiting(lane: _Lane, agent_slug: str, since: int, upto: int, turns_only: bool) -> bool:
    for names in (lane.names, lane.inflight):
        for (agent, _rel), name in names.items():
            if (agent == agent_slug and since < name.seq and name.first <= upto
                    and (name.holds_turns or not turns_only)):
                return True
    if turns_only and agent_slug in lane.merge_told:
        return False
    return agent_slug in lane.merge_owed or agent_slug in lane.merging


async def settled(machine_id: str, agent_slug: str, *, since: int = 0, upto: int | None = None,
                  turns_only: bool = False, idle_s: float = 60.0,
                  cancel: asyncio.Event | None = None) -> bool:
    """Wait until ``machine_id`` holds every name of ``agent_slug`` changed
    after ``since`` and up to ``upto`` (now, when None). ``turns_only``: only
    the names a turn waits for. True when settled. False when the machine is
    not connected (it is told when it returns), when neither this wait nor its
    lane has moved for ``idle_s`` (a push that moves is waited for however long
    it takes, and a lane long quiet still gets the whole bound), or when
    ``cancel`` was set. Never raises."""
    lane = _lanes.get(machine_id)
    if lane is None:
        return True
    limit = lane.seq if upto is None else upto
    cm = _manager()
    began = time.monotonic()
    while _waiting(lane, agent_slug, since, limit, turns_only):
        if not cm.is_connected(machine_id) or (cancel is not None and cancel.is_set()):
            return False
        left = max(lane.moved_at, began) + idle_s - time.monotonic()
        if left <= 0:
            return False
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(lane.changed.wait(), min(left, 5.0))
    return True


def release_turns(machine_id: str, agent_slug: str) -> None:
    """A turn went on without what ``machine_id`` is owed of ``agent_slug``,
    and was told: the next turn does not pay for the same files again. A file
    written after this holds turns as any new write does."""
    lane = _lanes.get(machine_id)
    if lane is None:
        return
    for names in (lane.names, lane.inflight):
        for (agent, _rel), name in names.items():
            if agent == agent_slug:
                name.holds_turns = False
    if agent_slug in lane.merge_owed or agent_slug in lane.merging:
        lane.merge_told.add(agent_slug)


def machine_connected(machine_id: str) -> None:
    """The machine signed in: what it is owed goes out."""
    from core.remote import remote_file_flow
    remote_file_flow.note_signed_in(machine_id)
    lane = _lanes.get(machine_id)
    if lane is not None:
        lane.moved_at = time.monotonic()
        _ensure_workers(lane)


def machine_dropped(machine_id: str) -> None:
    """The machine's link dropped: its names wait, whoever waits is released."""
    lane = _lanes.get(machine_id)
    if lane is not None:
        lane.wake()


def prune(connected: set[str]) -> None:
    """Forget the lanes of machines away for ``NAMES_KEEP_S``."""
    now = time.monotonic()
    for machine_id, lane in list(_lanes.items()):
        if (machine_id in connected or any(not t.done() for t in lane.workers)
                or now - lane.touched < NAMES_KEEP_S):
            continue
        _lanes.pop(machine_id, None)
        lane.wake()
        from core.remote import remote_file_flow
        remote_file_flow.forget_merges(machine_id)


def reset() -> None:
    """Forget every lane (tests)."""
    for lane in list(_lanes.values()):
        for task in list(lane.workers):
            task.cancel()
    _lanes.clear()


async def drain(timeout: float = 30.0) -> bool:
    """Wait until no connected machine is owed a name and no push runs
    (every planned stop, ``startup_drain._settle_pushes``, and tests). True
    when it drained in time."""
    cm = _manager()
    deadline = time.monotonic() + timeout
    while True:
        busy = [lane for lane in _lanes.values()
                if lane.inflight or lane.merging
                or (cm.is_connected(lane.machine_id) and (lane.names or lane.merge_owed))]
        if not busy:
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(busy[0].changed.wait(), min(left, 0.2))


# -- the workers -----------------------------------------------------------


def _ensure_workers(lane: _Lane) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    if not _manager().is_connected(lane.machine_id):
        return
    # A worker that found nothing has ended, though its entry may still be
    # here until the loop runs its callback: only the running ones count, and
    # only one between two names is free for the next.
    running = sum(1 for task in lane.workers if not task.done())
    idle = max(0, running - len(lane.inflight) - len(lane.merging))
    waiting = len(lane.names) + len(lane.merge_owed)
    for _ in range(min(WORKERS - running, waiting - idle)):
        task = spawn(_work(lane), name="push_lane", owner="machine_push_lane")
        lane.workers.add(task)
        task.add_done_callback(lane.workers.discard)


def _next(lane: _Lane) -> tuple[str, str] | None:
    for key in lane.names:
        if key not in lane.inflight:
            return key
    return None


async def _work(lane: _Lane) -> None:
    cm = _manager()
    while cm.is_connected(lane.machine_id):
        key = _next(lane)
        if key is None:
            agent = next((a for a in lane.merge_owed if a not in lane.merging), None)
            if agent is None:
                return
            await _merge(lane, agent)
            continue
        name = _take(lane, key)
        lane.inflight[key] = name
        keep = False
        try:
            keep = not await _push(cm, lane, key, name)
        except asyncio.CancelledError:
            keep = True
            raise
        except Exception:  # noqa: BLE001  # one name's push must not end the machine's lane
            logger.warning("push of %s/%s to machine %s failed", key[0], key[1],
                           lane.machine_id[:8], exc_info=True)
            name.tries += 1
            keep = name.tries < PUSH_TRIES
            if keep:
                await asyncio.sleep(min(_PUSH_PAUSE_MAX_S, 2.0 ** name.tries))
            else:
                await _gave_up(lane, key, name)
        finally:
            lane.inflight.pop(key, None)
            if keep:
                _keep(lane, key, name)
            lane.wake()


def _keep(lane: _Lane, key: tuple[str, str], name: _Name) -> None:
    """A name its push did not settle waits again. A newer change of it
    takes over what it carried: its rows, and the turns that wait for it."""
    newer = lane.names.get(key)
    if newer is not None:
        newer.first = min(newer.first, name.first)
        newer.holds_turns = newer.holds_turns or name.holds_turns
        newer.transfers[:0] = name.transfers
    elif key[0] in lane.merge_owed:
        if name.transfers:
            _end_rows_soon(lane.machine_id, name.transfers, _FAILED_TEXT)
    else:
        _put(lane, key, name)


async def _gave_up(lane: _Lane, key: tuple[str, str], name: _Name) -> None:
    """A name whose push kept raising: nothing brings the file now but a
    merge, so a read on that machine must serve the platform's copy and never
    pull the machine's older bytes over it (the failure's rule, with no
    stat to ask the machine for)."""
    import config
    from core.remote import remote_file_flow
    from services.infra import safe_fs
    agent_slug, rel_path = key
    await _end_rows(lane.machine_id, name.transfers, error=_FAILED_TEXT)
    if name.deleted:
        return
    try:
        st = await asyncio.to_thread(safe_fs.lstat_beneath, config.AGENTS_DIR,
                                     f"{agent_slug}/{rel_path}")
    except (OSError, ValueError):
        return
    remote_file_flow.note_platform_ahead(lane.machine_id, agent_slug, rel_path,
                                         st.st_size, st.st_mtime_ns, machine=None)


async def _merge(lane: _Lane, agent_slug: str) -> None:
    """The background merge an overflow owes this machine and agent. One that
    did not run (no manifest came, the machine left) or failed is owed again
    after a pause: the names it stands for are gone."""
    from core.session.session_manager import _get_remote_layer
    lane.merging.add(agent_slug)
    lane.merge_owed.discard(agent_slug)
    ran = False
    try:
        layer = _get_remote_layer()
        ran = bool(await layer.sync_agent_in_background(lane.machine_id, agent_slug))
        if not ran:
            logger.info("the merge owed to machine %s for %s did not run", lane.machine_id[:8],
                        agent_slug)
    except Exception:  # noqa: BLE001  # a merge spans db, disk and the machine: it is owed again
        logger.warning("the merge owed to machine %s for %s failed", lane.machine_id[:8],
                       agent_slug, exc_info=True)
    try:
        if not ran and _manager().is_connected(lane.machine_id):
            await asyncio.sleep(MERGE_RETRY_S)
    finally:
        if ran:
            lane.merge_told.discard(agent_slug)
        else:
            lane.merge_owed.add(agent_slug)
        lane.merging.discard(agent_slug)
        lane.wake(moved=ran)


async def _push(cm, lane: _Lane, key: tuple[str, str], name: _Name) -> bool:
    """Bring ``key`` to the lane's machine as the platform holds it now.
    True when the name is done (pushed, skipped, refused or failed with the
    failure's rule applied). False when the machine left meanwhile and the
    name must wait for it."""
    import config
    from core.remote import file_sync, remote_file_flow, transfer_gate
    from core.remote.satellite_file_verbs import push_ceiling_s
    from services.infra import safe_fs
    from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path
    from services.path_policy_v2 import PathRef
    from storage.files import sync_state_store
    from storage.pg import run_db

    machine_id = lane.machine_id
    agent_slug, rel_path = key
    rows = name.transfers
    if name.deleted:
        conn = cm.get_connection(machine_id)
        await cm.send_fire_and_forget(machine_id, {
            "type": "file_push", "agent_slug": agent_slug, "action": "delete", "path": rel_path,
        })
        if conn is None or cm.get_connection(machine_id) is not conn:
            return False
        # The machine no longer holds this file → drop its merge base so the
        # next merge doesn't treat it as a divergence. (The tombstone,
        # written at the delete's source, drives the delete for a machine
        # that was away.)
        await run_db(sync_state_store.clear_one, machine_id, agent_slug, rel_path)
        await _end_rows(machine_id, rows)
        return True

    if time.monotonic() - name.marked_at > REJUDGE_AFTER_S:
        from services.remote import workspace_fanout
        if not await workspace_fanout.machine_may_hold(machine_id, agent_slug, rel_path):
            logger.info("push of %s to %s dropped: the machine may no longer hold it",
                        rel_path, machine_id[:8])
            await _end_rows(machine_id, rows, error=_NOT_ALLOWED_TEXT)
            return True

    try:
        if not config.is_safe_agent_name(agent_slug):
            raise PathOutsideRoot(agent_slug)
        rel = f"{agent_slug}/{normalize_rel_path(rel_path)}"
        src_fd, st = await asyncio.to_thread(safe_fs.open_regular_for_read, config.AGENTS_DIR, rel)
    except (OSError, PathOutsideRoot) as exc:
        # The copy is gone or is no regular file beneath the root: nothing
        # is pushed, and absence is never read as a delete (the merge decides).
        logger.debug("push lane: %s/%s not pushed (%s)", agent_slug, rel_path, type(exc).__name__)
        await _end_rows(machine_id, rows, error=_GONE_TEXT)
        return True
    try:
        size = st.st_size
        platform_stat = (st.st_size, st.st_mtime_ns)
        content_hash = await asyncio.to_thread(file_sync._hash_fd, src_fd)
        base = await run_db(sync_state_store.get_one, machine_id, agent_slug, rel_path)
        if base and base[0] == content_hash:
            # A merge, a push or the machine's own change brought it there.
            remote_file_flow.clear_platform_ahead(machine_id, agent_slug, rel_path,
                                                  stat=platform_stat)
            await _end_rows(machine_id, rows, size=size)
            return True

        last = {"t": 0.0}

        async def progress(sent: int, total: int) -> None:
            lane.moved_at = time.monotonic()
            if not rows or (sent < total and time.monotonic() - last["t"] < 0.5):
                return
            last["t"] = time.monotonic()
            from core.remote import transfer_registry
            for tid, _size in rows:
                await transfer_registry.progress(tid, machine_id, sent, total)

        async def on_state(state: str) -> None:
            from core.remote import transfer_registry
            for tid, _size in rows:
                await transfer_registry.set_state(tid, machine_id, state)

        stale: dict = {}
        in_flight_s = push_ceiling_s(size)
        async with transfer_gate.slot(machine_id, agent_slug, rel_path, size,
                                      on_state=on_state if rows else None):
            # Until the push lands the machine holds older bytes: a read
            # there serves the platform copy. Marked from the slot, for as
            # long as the push may run.
            remote_file_flow.note_platform_ahead(machine_id, agent_slug, rel_path,
                                                 *platform_stat, in_flight_s=in_flight_s)
            # The push expects the copy last pulled, probed or pushed there
            # (the record goes first; the ack re-records the committed copy).
            record = remote_file_flow.take_push_record(machine_id, agent_slug, rel_path)
            if record is None:
                # A push the link cut took the record with it and left it on
                # the mark. Blind, this push would overwrite an edit made there
                # meanwhile, as soon as the machine returns.
                standing = remote_file_flow._platform_ahead.get((machine_id, agent_slug, rel_path))
                record = standing["machine"] if standing else None
            conn = cm.get_connection(machine_id)
            try:
                ok = await cm.push_file(
                    machine_id, PathRef("agent_tree", rel_path), Path(safe_fs.fd_path(src_fd)),
                    agent_slug=agent_slug, progress_cb=progress, content_hash=content_hash,
                    expect=remote_file_flow.push_expectation_of(record), stale_out=stale,
                )
            except Exception as exc:  # noqa: BLE001  # a push that raised missed its machine: the failure's rule
                logger.warning("push of %s to %s failed: %s", rel_path, machine_id[:8], exc)
                ok = False
        if ok:
            # The base names the bytes this machine got: the hash and the
            # mtime were read from the descriptor the push sent.
            with contextlib.suppress(Exception):
                await run_db(sync_state_store.record_one, machine_id, agent_slug, rel_path,
                             content_hash, st.st_mtime)
            remote_file_flow.clear_platform_ahead(machine_id, agent_slug, rel_path,
                                                  stat=platform_stat)
            await _end_rows(machine_id, rows, size=size)
            return True
        if stale:
            # The machine is changing the file: its copy stays, the next
            # merge compares both. The ack's stat is the mark's baseline.
            remote_file_flow.note_platform_ahead(
                machine_id, agent_slug, rel_path, *platform_stat,
                machine=remote_file_flow.stale_machine_stat(stale), stale=True)
            logger.info("push of %s to %s held: the file is being changed there",
                        rel_path, machine_id[:8])
            await _end_rows(machine_id, rows, error=_STALE_TEXT)
            return True
        if conn is None or cm.get_connection(machine_id) is not conn:
            # The link went with the push in flight: marked ahead with the
            # record it had (nothing is asked of a machine that is away), and
            # the name waits for its return. The connection itself is what is
            # compared: a machine back already reads as connected, and its
            # name would fall to the failure rule.
            remote_file_flow.note_platform_ahead(machine_id, agent_slug, rel_path,
                                                 *platform_stat, machine=record)
            return False
        await remote_file_flow.note_push_failed(cm, machine_id, agent_slug, rel_path,
                                                *platform_stat, fallback=record)
        logger.debug("push of %s to %s not acked", rel_path, machine_id[:8])
        await _end_rows(machine_id, rows, error=_FAILED_TEXT)
        return True
    finally:
        os.close(src_fd)


async def _end_rows(machine_id: str, rows: list[tuple[str, int]], *, error: str = "",
                    size: int | None = None) -> None:
    if not rows:
        return
    from core.remote import transfer_registry
    for tid, row_size in rows:
        try:
            await transfer_registry.end_row(tid, machine_id, row_size if size is None else size,
                                            error=error)
        except Exception:  # noqa: BLE001  # a progress row's end must never fail the push
            logger.debug("transfer row %s not ended", tid, exc_info=True)


def _end_rows_soon(machine_id: str, rows: list[tuple[str, int]], error: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    spawn(_end_rows(machine_id, list(rows), error=error), name="end_rows",
          owner="machine_push_lane")
