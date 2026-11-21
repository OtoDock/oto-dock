"""Applying a workspace sync's plan: each action (push, pull, the two deletes),
the windowed run over them, the losers' capture and notice. ``RemoteWorkspaceSyncMixin``
keeps what decides the plan and has this mixin as its base.
"""

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import config
from core import agent_folder
from services.infra import path_confinement, safe_fs

if TYPE_CHECKING:
    from core.remote.file_sync import FileEntry, MergePlan
    from core.remote.remote_host import RemoteLayerHost as _Host
else:
    _Host = object

logger = logging.getLogger("remote-layer")


# Initial-sync PULLS (satellite→platform) at or above this size are deferred to a
# background task so they don't block the CLI from starting.
# Only pulls defer — a deferred PUSH could clobber a concurrent agent edit, but a
# pull only refreshes the platform/dashboard view and never touches the agent's
# disk, so deferring it is clobber-free. Small pulls stay foreground (cheap, and
# keeps the dashboard immediately fresh).
_DEFER_PULL_MIN_BYTES = 4 * 1024 * 1024  # 4 MB
# A scrub's deletes are queued in a loop: every so many the writer gets its turn,
# and the loop waits while the connection holds more than a healthy reader takes
# in the slow-reader grace, or a large scrub would cut the machine it cleans.
_SCRUB_YIELD_FRAMES = 256
_SCRUB_HOLD_FRAMES = 5000


def _partition_deferred_pulls(actions: list, remote_size: dict) -> tuple[list, list]:
    """Split merge actions into ``(foreground, deferred_pulls)`` for Part 2
    (Option A). A pull is deferred iff it is conflict-free (``capture_side is
    None``) AND the satellite copy is ``>= _DEFER_PULL_MIN_BYTES``; every other
    action — all pushes, deletes, scrubs, noops, conflict-capture pulls, and
    small pulls — stays foreground (run before the CLI starts)."""
    deferred = [
        a for a in actions
        if a.op == "pull" and a.capture_side is None
        and remote_size.get(a.rel_path, 0) >= _DEFER_PULL_MIN_BYTES
    ]
    if not deferred:
        return list(actions), []
    deferred_ids = {id(a) for a in deferred}
    foreground = [a for a in actions if id(a) not in deferred_ids]
    return foreground, deferred


def _keep_mtime(agent_slug: str, rel_path: str, mtime: float) -> None:
    """Give a just-pulled platform copy the mtime its machine gave it (a
    restore: the pull's own time would make old bytes look newest). Opened
    beneath the agents root with no component followed; best-effort."""
    import config as _cfg
    from services.infra import safe_fs
    try:
        fd = safe_fs.open_beneath(_cfg.AGENTS_DIR, f"{agent_slug}/{rel_path}", os.O_RDONLY)
    except OSError:
        return
    try:
        os.utime(fd, (mtime, mtime))
    except OSError as e:
        logger.debug("keep_mtime: %s/%s kept the pull's time: %s", agent_slug, rel_path, e)
    finally:
        os.close(fd)


def _platform_bytes(agent_dir, rel_path: str) -> bytes | None:
    """The platform copy of ``rel_path`` for a loser capture, read beneath
    the agents root with no component followed (a link at the name or on
    the way is refused, never read through); None when there is nothing a
    capture may copy."""
    from core.remote.file_sync import MAX_FILE_SIZE
    agent = Path(agent_dir)
    try:
        return safe_fs.read_bytes_beneath(
            agent.parent, f"{agent.name}/{rel_path}", max_size=MAX_FILE_SIZE,
        )
    except OSError:
        return None


@dataclass
class _SyncRun:
    """One merge past its inputs: the plan and the manifests' facts the
    actions read, the counters, the recover bin and the one window every
    action shares (the fields past ``satellite_user``: ``_sync_apply``'s)."""
    machine_id: str
    agent_slug: str
    agent_dir: Path
    progress_cb: Callable[[float, int], Awaitable[None]] | None
    plan: "MergePlan"
    local_entries: "list[FileEntry]"
    remote_size: dict[str, int]
    remote_stat: dict[str, dict]
    satellite_user: str
    local_mtime: dict[str, float] = field(init=False)
    local_size: dict[str, int] = field(init=False)
    recover_tmp: Path | None = None
    to_notify: list[tuple[str, str]] = field(init=False)
    n: dict[str, int] = field(init=False)
    total: int = field(init=False)
    last_tick: dict[str, float] = field(init=False)
    partial: dict[str, float] = field(init=False)
    window: asyncio.Semaphore = field(init=False)

    def planned_stat(self, rp: str) -> tuple[int, float] | None:
        """The platform copy the plan saw, None when it saw none."""
        return (self.local_size[rp], self.local_mtime[rp]) if rp in self.local_size else None


def _platform_moved(path: Path, planned: tuple[int, float] | None) -> bool:
    """A save landed after the manifest: the platform copy is no longer the one
    the plan saw (``_SyncRun.planned_stat``). The plan judged other bytes, so a
    pull over them would lose them with no capture. Both copies stay as they
    are and the next merge's rule sees both."""
    try:
        st = os.lstat(path)
    except OSError:
        return planned is not None
    return (st.st_size, st.st_mtime) != planned


class RemoteWorkspaceApplyMixin(_Host):
    async def _apply_scrubs(self, run) -> None:
        for n, rp in enumerate(run.plan.to_scrub, 1):
            if n % _SCRUB_YIELD_FRAMES == 0:
                await self._scrub_room(run.machine_id)
            try:
                await self._cm.send_fire_and_forget(
                    run.machine_id,
                    {"type": "file_push", "agent_slug": run.agent_slug,
                     "action": "delete", "path": rp},
                )
                run.n["scrub"] += 1
            except Exception:
                logger.exception("isolation scrub delete failed for %s", rp)

    async def _scrub_room(self, machine_id: str) -> None:
        """Until the machine's queue is short again, or that connection is gone."""
        await asyncio.sleep(0)
        conn = self._cm.get_connection(machine_id)
        while (conn is not None and conn.queued_frames > _SCRUB_HOLD_FRAMES  # noqa: ASYNC110  # nothing signals a queue's length in frames
               and self._cm.get_connection(machine_id) is conn):
            await asyncio.sleep(0.05)

    async def _sync_apply(self, run: _SyncRun) -> list:
        """The plan's foreground actions through the window, the scrubs, the
        recover bin's cleanup and the losers' notices; the deferred pulls,
        for the caller to kick once the sync lock is released."""
        import asyncio as _asyncio

        # Defer large, conflict-free PULLS to a background
        # task so the CLI starts without waiting for satellite-side bulk content
        # (e.g. a big media file the satellite holds). Pulls never touch the
        # agent's disk → safe to defer. Everything else — ALL pushes, deletes,
        # scrubs, noops, conflict-capture pulls, and small pulls — stays
        # foreground (awaited before start_session).
        foreground_actions, deferred_pulls = _partition_deferred_pulls(
            run.plan.actions, run.remote_size,
        )

        run.local_mtime = {e.path: e.mtime for e in run.local_entries}
        run.local_size = {e.path: e.size for e in run.local_entries}
        # Created EAGERLY (not lazily on first capture): the windowed
        # apply below runs actions concurrently, and a lazy nonlocal
        # create could double-mkdtemp and leak the loser.
        import tempfile as _tempfile
        from pathlib import Path as _Path
        run.recover_tmp = _Path(_tempfile.mkdtemp(prefix="oto-recover-"))
        run.to_notify = []
        run.n = {"push": 0, "pull": 0, "delete": 0, "scrub": 0, "capture": 0,
                 "stale": 0, "done": 0}

        # W2 (sync-performance): throttled progress ticks → progress_cb
        # (the warmup wires it to the dashboard install bar; background
        # sweeps pass None). Always fire the FINAL tick so the bar lands
        # on 100% even under throttle.
        run.total = len(foreground_actions)
        run.last_tick = {"t": 0.0}
        # The share of each push still on its way: a large file moves
        # the row by its bytes, not only when it lands.
        run.partial = {}

        # W1 (sync-performance): WINDOWED PIPELINING. The satellite applies
        # frames serially on its event loop either way — but awaiting each
        # per-file ack before SENDING the next serialized the WIRE, so a
        # 1,500-file first sync cost RTT × N (the 25-minute first-turn
        # report). A bounded window keeps the wire full while preserving
        # every correctness property: per-path ordering rides the global
        # path lock INSIDE _apply, failures still log-and-continue (same
        # as the old loop), and counters/lists only mutate on the loop
        # thread. Big chunked pushes may interleave with small files —
        # the writer lanes already order control frames ahead.
        run.window = _asyncio.Semaphore(8)

        await _asyncio.gather(
            *(self._sync_apply_windowed(run, a) for a in foreground_actions)
        )
        await self._sync_progress_tick(run, final=True)

        # Isolation scrubs (another user's data / agent-scope creds that leaked
        # onto the satellite) — delete there, never captured, never base-tracked.
        await self._apply_scrubs(run)

        if run.recover_tmp is not None:
            import shutil as _shutil
            _shutil.rmtree(run.recover_tmp, ignore_errors=True)

        logger.info(
            "Initial workspace sync for %s: pushed=%d pulled=%d deleted=%d "
            "scrubbed=%d captured=%d stale=%d",
            run.agent_slug, run.n["push"], run.n["pull"], run.n["delete"],
            run.n["scrub"], run.n["capture"], run.n["stale"],
        )

        if run.to_notify:
            await self._notify_conflict_losers(run.agent_slug, run.to_notify)
        return deferred_pulls

    async def _sync_progress_tick(self, run: _SyncRun, final: bool = False) -> None:
        """A throttled tick to the run's progress_cb; a final tick always fires."""
        import time as _time_mod

        if run.progress_cb is None or run.total == 0:
            return
        now = _time_mod.monotonic()
        if not final and now - run.last_tick["t"] < 0.5:
            return
        run.last_tick["t"] = now
        try:
            done = run.n["done"] + (0.0 if final else sum(run.partial.values()))
            await run.progress_cb(done, run.total)
        except Exception:  # noqa: BLE001  # a caller's progress callback must not fail the sync
            logger.debug("sync progress_cb failed", exc_info=True)

    async def _sync_pull_bytes(self, run: _SyncRun, rp: str) -> bytes | None:
        """Pull the satellite's current copy of ``rp`` → bytes, or None on a
        pull/read failure."""
        from services.path_policy_v2 import PathRef

        # _sync_apply makes the run's recovery dir before any action runs.
        dest = cast("Path", run.recover_tmp) / uuid.uuid4().hex
        ok = await self._cm.pull_file_to_path(
            run.machine_id, PathRef("agent_tree", rp), dest, agent_slug=run.agent_slug,
        )
        if not ok:
            return None
        try:
            return dest.read_bytes()
        except OSError:
            return None
        finally:
            with contextlib.suppress(OSError):
                dest.unlink()

    async def _sync_capture_loser(
        self, run: _SyncRun, action,
    ) -> tuple[dict, tuple | None] | None:
        """Copy the LOSING side's current bytes to the recover-bin (a
        cross-user conflict). Best-effort — the op proceeds regardless.
        Returns the entry and the notice it queued, for a push the
        machine then refuses to undo (``_uncapture``)."""
        import asyncio as _asyncio

        from storage.files import recover_bin_store

        if action.capture_side == "satellite":
            data = await self._sync_pull_bytes(run, action.rel_path)
        elif action.capture_side == "platform":
            data = await _asyncio.to_thread(_platform_bytes, run.agent_dir, action.rel_path)
        else:
            return None
        if data is None:
            return None
        entry = await _asyncio.to_thread(
            recover_bin_store.capture, run.agent_slug, action.rel_path,
            data, action.capture_reason,
        )
        if entry is None:
            return None
        run.n["capture"] += 1
        notice = None
        if action.notify_user:
            notice = (action.notify_user, entry["original_name"])
            run.to_notify.append(notice)
        return entry, notice

    async def _sync_uncapture(self, run: _SyncRun, captured) -> None:
        """The push the capture preceded did not replace the machine's
        copy (a stale answer): nothing was lost, the entry goes and
        its owner is not told their version was replaced."""
        import asyncio as _asyncio

        from storage.files import recover_bin_store

        if captured is None:
            return
        entry, notice = captured
        await _asyncio.to_thread(recover_bin_store.delete, entry["entry_id"])
        run.n["capture"] -= 1
        if notice is not None and notice in run.to_notify:
            run.to_notify.remove(notice)

    async def _sync_hold_stale(self, run: _SyncRun, rp: str, stale: dict) -> None:
        """Mark the platform copy ahead of a machine that refused its push as stale."""
        import asyncio as _asyncio

        from core.remote import remote_file_flow

        # The machine is changing the file: the platform copy is
        # marked ahead there with the machine's stat as the
        # baseline, so a read serves the platform copy and the next
        # merge compares both.
        try:
            pst = await _asyncio.to_thread((run.agent_dir / rp).lstat)
        except OSError:
            return
        remote_file_flow.note_platform_ahead(
            run.machine_id, run.agent_slug, rp, pst.st_size, pst.st_mtime_ns,
            machine=remote_file_flow.stale_machine_stat(stale), stale=True,
        )
        logger.info(
            "Push of %s held: the file %s on the machine; the next merge "
            "compares both copies", rp,
            "changed since the manifest" if stale.get("reason") == "changed"
            else "is being written",
        )

    async def _sync_apply_action(self, run: _SyncRun, action) -> None:
        """One action under the global path lock; the dashboard refresh after it
        only when the op changed the platform tree."""
        from core.remote import remote_file_flow
        from services.notifications import notification_manager

        rp = action.rel_path
        # Only a pull ADDS/UPDATES a file on the PLATFORM tree, so only a
        # pull refreshes an open dashboard (set in the pull branch below).
        pulled_to_platform = False
        # Serialize each file against the live write-back path + other
        # warmups on the SAME file (cross-machine) via the global lock.
        lock = await remote_file_flow._acquire_global_path_lock(run.agent_slug, rp)
        async with lock:
            if action.op == "push":
                pulled_to_platform = await self._sync_push(run, action, rp)
            elif action.op == "pull":
                pulled_to_platform = await self._sync_pull(run, action, rp)
            elif action.op == "delete_satellite":
                pulled_to_platform = await self._sync_delete_satellite(run, action, rp)
            elif action.op == "delete_platform":
                pulled_to_platform = await self._sync_delete_platform(run, action, rp)
            elif action.op == "noop":
                pulled_to_platform = await self._sync_noop(run, action, rp)

        if pulled_to_platform:
            # A pull (satellite file landed) OR a delete_platform (satellite
            # out-of-turn delete propagated) just changed the platform tree →
            # refresh any open dashboard workspace view,
            # best-effort, outside the lock (it doesn't need it). push
            # (platform→satellite) + delete_satellite (platform already
            # lacks the file) don't change the platform tree, so they
            # don't notify the dashboard.
            await notification_manager.broadcast_file_updated(
                run.agent_slug, rp, source="disk",
            )

    async def _sync_push(self, run: _SyncRun, action, rp: str) -> bool:
        """The push op; False: a push never changes the platform tree. The
        platform copy is opened beneath the agent's folder with no link
        followed (the root its manifest walked: the folder by its realpath)
        and pushed from that descriptor: the plan listed a regular file, and
        whatever stands at the name now (a link, a pipe, nothing) is never
        read through or waited on. Such a name is nothing to push, and the
        next merge reads it again."""
        import asyncio as _asyncio

        try:
            src_fd, _st = await _asyncio.to_thread(
                safe_fs.open_regular_for_read, agent_folder.agent_root(run.agent_slug), rp)
        except OSError as e:
            logger.info("Push of %s left out during initial sync: %s", rp, e)
            return False
        try:
            return await self._sync_push_copy(run, action, rp, src_fd)
        finally:
            os.close(src_fd)

    async def _sync_push_copy(self, run: _SyncRun, action, rp: str, src_fd: int) -> bool:
        """``_sync_push`` from the platform copy's open descriptor."""
        import asyncio as _asyncio

        from core.remote import file_sync, remote_file_flow
        from services.path_policy_v2 import PathRef
        from storage.files import file_tombstones_store, sync_state_store

        captured = None
        if action.capture_side:
            captured = await self._sync_capture_loser(run, action)
        # Pass the descriptor's PATH: push_file streams from it (memory
        # O(chunk) even at 1GB). It runs while the
        # file moves (the connection's credit paces it), and
        # its byte progress drives the row and keeps the
        # session starting. The transfer gate (innermost,
        # see its lock-order invariant) bounds the machine's
        # concurrent LARGE pushes; sub-threshold files
        # bypass it so a
        # tiny-file warmup storm never queues behind a big
        # fan-out. The push is pinned to the platform hash
        # the merge planned on (the base it records): bytes
        # changed since the manifest fail the push instead,
        # and the failure drops the cached hash of the path.
        from core.remote import transfer_gate

        async def _bytes(sent: int, total: int, _rp=rp) -> None:
            run.partial[_rp] = sent / max(total, 1)
            await self._sync_progress_tick(run)

        stale: dict = {}
        async with transfer_gate.slot(
            run.machine_id, run.agent_slug, rp,
            run.local_size.get(rp, 0),
        ):
            try:
                ok = await self._cm.push_file(
                    run.machine_id, PathRef("agent_tree", rp),
                    Path(safe_fs.fd_path(src_fd)),
                    agent_slug=run.agent_slug, progress_cb=_bytes,
                    content_hash=action.base_hash or None,
                    expect=run.remote_stat.get(rp, "live"), stale_out=stale,
                )
            except Exception as exc:  # noqa: BLE001  # a push that raised missed its machine: the failure's rule
                logger.warning("Push of %s raised during initial sync: %s", rp, exc)
                ok = False
        if not ok and stale:
            # Nothing replaced, the hash kept: the platform
            # copy is as the manifest saw it, the machine's
            # too (so the capture before the push undoes).
            run.n["stale"] += 1
            await self._sync_uncapture(run, captured)
            await self._sync_hold_stale(run, rp, stale)
            return False
        if not ok:
            # A push pinned to a stale cached hash fails at
            # every sync until the next merge hashes it again.
            file_sync.forget_hash(run.agent_dir / rp)
            logger.warning("Push failed during initial sync: %s", rp)
            # The merge finishes all the same, so nothing else tells a read
            # that the machine still lacks this: with no mark it would pull
            # the older bytes over the platform's, and both sides would agree.
            # With the link down the probe gets no answer, and what the
            # manifest listed is the baseline that tells a later edit there.
            # Should that edit win, this save is kept first (``stale``).
            listed = run.remote_stat.get(rp)
            with contextlib.suppress(OSError):
                pst = await _asyncio.to_thread(os.fstat, src_fd)
                await remote_file_flow.note_push_failed(
                    self._cm, run.machine_id, run.agent_slug, rp, pst.st_size, pst.st_mtime_ns,
                    fallback={"exists": True, **listed} if listed else {"exists": False},
                    stale=True)
            return False
        # The machine holds the platform copy: no read there
        # may take it for a missed push (lane D's marker).
        remote_file_flow.clear_platform_ahead(run.machine_id, run.agent_slug, rp)
        run.n["push"] += 1
        if action.base_hash:
            await _asyncio.to_thread(
                sync_state_store.record_one, run.machine_id, run.agent_slug,
                rp, action.base_hash, run.local_mtime.get(rp, 0.0),
            )
        if action.drop_tombstone:
            await _asyncio.to_thread(file_tombstones_store.drop, run.agent_slug, rp)
        return False

    async def _sync_pull(self, run: _SyncRun, action, rp: str) -> bool:
        """The pull op; True once the satellite's bytes landed on the platform."""
        import asyncio as _asyncio

        from core.remote import file_sync, remote_file_flow
        from services.path_policy_v2 import PathRef
        from storage.files import (
            file_author_store,
            file_tombstones_store,
            sync_state_store,
        )

        # The named path, never resolved: the pull opens it
        # by components and refuses a link at any one.
        if not file_sync.is_canonical_rel_path(rp):
            logger.warning("initial-sync pull traversal blocked: %s", rp)
            return False
        dest = path_confinement.join_under(run.agent_dir, rp)
        if await _asyncio.to_thread(_platform_moved, dest, run.planned_stat(rp)):
            logger.info("Pull of %s left to the next merge: the platform copy moved", rp)
            remote_file_flow.note_merge_left_one(run.machine_id, run.agent_slug, rp)
            return False
        if action.capture_side:
            await self._sync_capture_loser(run, action)
        ok = await self._cm.pull_file_to_path(
            run.machine_id, PathRef("agent_tree", rp), dest,
            agent_slug=run.agent_slug,
        )
        if not ok:
            logger.warning("Pull failed during initial sync: %s", rp)
            return False
        run.n["pull"] += 1
        pulled_to_platform = True
        # The machine's copy is the platform's now: no mark
        # of an older platform copy ahead there stands.
        remote_file_flow.clear_platform_ahead(run.machine_id, run.agent_slug, rp)
        if action.keep_mtime is not None:
            await _asyncio.to_thread(
                _keep_mtime, run.agent_slug, rp, action.keep_mtime)
        if action.base_hash:
            try:
                base_mtime = dest.stat().st_mtime
            except OSError:
                base_mtime = 0.0
            await _asyncio.to_thread(
                sync_state_store.record_one, run.machine_id, run.agent_slug,
                rp, action.base_hash, base_mtime,
            )
            # The platform now holds the satellite user's bytes.
            if run.satellite_user:
                await _asyncio.to_thread(
                    file_author_store.record, run.agent_slug, rp, run.satellite_user,
                )
        if action.drop_tombstone:
            await _asyncio.to_thread(file_tombstones_store.drop, run.agent_slug, rp)
        return pulled_to_platform

    async def _run_deferred_pulls(
        self, machine_id: str, agent_slug: str, actions: list, satellite_user: str,
        planned: dict[str, tuple[int, float] | None],
    ) -> None:
        """Background runner for large conflict-free PULLS deferred off the warmup
        path (Part 2, Option A). Mirrors the foreground pull branch of
        ``_sync_pull`` MINUS capture/notify (deferred pulls are
        ``capture_side is None`` by construction): per file, under the global
        per-(agent, rel_path) lock, stream the satellite's bytes to the platform
        agent dir, then advance base/file_author + drop any tombstone, and refresh
        an open dashboard view. Best-effort — a failure (incl. a WS drop →
        ``pull_file_to_path`` returns False) is logged and reconciles at the next
        warmup / reconnect-sync. Does NOT hold the per-(machine, agent) warmup lock,
        so a back-to-back session's warmup isn't blocked behind it. ``planned``:
        each path's platform copy as the plan saw it (``_platform_moved``); a
        large pull runs long after the manifest."""
        import asyncio as _asyncio

        import config as _cfg
        from core.remote import file_sync, remote_file_flow
        from services.notifications import notification_manager
        from services.path_policy_v2 import PathRef
        from storage.files import (
            file_author_store,
            file_tombstones_store,
            sync_state_store,
        )

        try:
            agent_dir = _cfg.get_agent_dir(agent_slug)
        except ValueError:
            logger.warning("deferred pulls skipped: %r names no agent folder", agent_slug)
            return
        pulled = 0
        for action in actions:
            rp = action.rel_path
            try:
                lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rp)
                async with lock:
                    # The named path, never resolved: the pull opens it by
                    # components and refuses a link at any one.
                    if not file_sync.is_canonical_rel_path(rp):
                        logger.warning("deferred-sync pull traversal blocked: %s", rp)
                        continue
                    dest = path_confinement.join_under(agent_dir, rp)
                    if await _asyncio.to_thread(_platform_moved, dest, planned.get(rp)):
                        logger.info("Pull of %s left to the next merge: the platform copy moved", rp)
                        remote_file_flow.note_merge_left_one(machine_id, agent_slug, rp)
                        continue
                    ok = await self._cm.pull_file_to_path(
                        machine_id, PathRef("agent_tree", rp), dest,
                        agent_slug=agent_slug,
                    )
                    if not ok:
                        logger.warning(
                            "deferred-sync pull failed for %s/%s", agent_slug, rp,
                        )
                        continue
                    pulled += 1
                    if action.keep_mtime is not None:
                        await _asyncio.to_thread(
                            _keep_mtime, agent_slug, rp, action.keep_mtime)
                    if action.base_hash:
                        try:
                            base_mtime = dest.stat().st_mtime
                        except OSError:
                            base_mtime = 0.0
                        await _asyncio.to_thread(
                            sync_state_store.record_one, machine_id, agent_slug,
                            rp, action.base_hash, base_mtime,
                        )
                        if satellite_user:
                            await _asyncio.to_thread(
                                file_author_store.record, agent_slug, rp, satellite_user,
                            )
                    if action.drop_tombstone:
                        await _asyncio.to_thread(
                            file_tombstones_store.drop, agent_slug, rp,
                        )
                # Refresh an open dashboard workspace view (best-effort, outside the
                # lock — it doesn't need it).
                await notification_manager.broadcast_file_updated(
                    agent_slug, rp, source="disk",
                )
            except Exception:
                logger.exception(
                    "deferred-sync pull errored for %s/%s", agent_slug, rp,
                )
        if pulled:
            logger.info(
                "Deferred workspace sync for %s: pulled %d large file(s) in background",
                agent_slug, pulled,
            )

    async def _sync_delete_satellite(self, run: _SyncRun, action, rp: str) -> bool:
        """The delete_satellite op; False: the platform tree is unchanged."""
        import asyncio as _asyncio

        from storage.files import (
            file_tombstones_store,
            recover_bin_store,
            sync_state_store,
        )

        # A delete nobody asked for (the end-of-turn scan inferred it, or the
        # sweep aged the file out): a copy the bin cannot hold stays on the
        # machine, and when the tombstone ends the merge adopts it back.
        tomb = await _asyncio.to_thread(file_tombstones_store.get, run.agent_slug, rp)
        unasked = (tomb or {}).get("origin") in file_tombstones_store.UNASKED_ORIGINS
        if unasked and (run.remote_size.get(rp, 0) > config.RECOVER_BIN_MAX_BYTES
                        or not recover_bin_store.is_recoverable_path(rp)):
            logger.warning("delete-sync: %s/%s stays on the machine: the Recover bin cannot take it",
                           run.agent_slug, rp)
            return False
        # Capture the satellite's only copy BEFORE deleting. A pull
        # failure means we do NOT delete (never cause unrecoverable
        # loss from a transient blip) — the tombstone persists and
        # we retry next sync.
        data = await self._sync_pull_bytes(run, rp)
        if data is None:
            logger.warning(
                "delete-sync: pull failed for %s/%s — NOT deleting",
                run.agent_slug, rp,
            )
            return False
        entry = await _asyncio.to_thread(
            recover_bin_store.capture, run.agent_slug, rp, data, "deleted", evict=not unasked,
        )
        if entry is not None:
            run.n["capture"] += 1
        elif unasked and data and not await _asyncio.to_thread(
                recover_bin_store.already_binned, run.agent_slug, rp, data):
            logger.warning("delete-sync: %s/%s stays on the machine: its copy did not reach the Recover bin",
                           run.agent_slug, rp)
            return False
        try:
            await self._cm.send_fire_and_forget(
                run.machine_id,
                {"type": "file_push", "agent_slug": run.agent_slug,
                 "action": "delete", "path": rp},
            )
            run.n["delete"] += 1
        except Exception:
            logger.exception("delete-sync failed for %s (backup kept)", rp)
        if action.clear_base:
            await _asyncio.to_thread(
                sync_state_store.clear_one, run.machine_id, run.agent_slug, rp,
            )
        return False

    async def _sync_delete_platform(self, run: _SyncRun, action, rp: str) -> bool:
        """The delete_platform op; True once the platform copy is gone."""
        import asyncio as _asyncio

        from core.remote import file_sync
        from storage.files import (
            file_author_store,
            file_tombstones_store,
            sync_state_store,
        )

        # The satellite DELETED a file it had converged on, out-of-turn,
        # with its tree still alive (delete-attribution). Capture
        # the platform bytes to the recover-bin (7-day undo) FIRST, then
        # delete the platform copy, tombstone it (so OTHER idle satellites
        # drop it too), clear base+author, and fan the delete out. Under
        # the global per-(agent, path) lock already held here.
        if action.capture_side:
            await self._sync_capture_loser(run, action)
        if not file_sync.is_canonical_rel_path(rp):
            logger.warning("delete_platform traversal blocked: %s", rp)
            return False
        # The name is removed beneath the agent's folder: a
        # link at it goes (its target stays). A missing parent
        # means the file is gone already. A link above it or a
        # directory at it refuses, and a refused delete claims
        # nothing: no tombstone, the base and the author kept,
        # no fan-out, no dashboard refresh.
        try:
            await _asyncio.to_thread(
                file_sync.apply_incoming_file, run.agent_dir, rp, "delete",
            )
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("delete_platform refused for %s: %s", rp, e)
            return False
        run.n["delete"] += 1
        import time as _t
        await _asyncio.to_thread(
            file_tombstones_store.record, run.agent_slug, rp,
            _t.time(), origin="satellite-idle-delete",
        )
        if action.clear_base:
            await _asyncio.to_thread(
                sync_state_store.clear_one, run.machine_id, run.agent_slug, rp,
            )
        await _asyncio.to_thread(
            file_author_store.clear, run.agent_slug, rp,
        )
        from services.remote import workspace_fanout as _wf
        await _wf.fan_out_delete(
            run.agent_slug, rp, exclude_machine_id=run.machine_id,
        )
        pulled_to_platform = True  # platform tree changed → dashboard refresh
        return pulled_to_platform

    async def _sync_noop(self, run: _SyncRun, action, rp: str) -> bool:
        """The noop op: the base and tombstone bookkeeping only; False."""
        import asyncio as _asyncio

        from storage.files import file_tombstones_store, sync_state_store

        if action.base_hash:
            await _asyncio.to_thread(
                sync_state_store.record_one, run.machine_id, run.agent_slug,
                rp, action.base_hash, run.local_mtime.get(rp, 0.0),
            )
        if action.clear_base:
            await _asyncio.to_thread(
                sync_state_store.clear_one, run.machine_id, run.agent_slug, rp,
            )
        if action.drop_tombstone:
            await _asyncio.to_thread(file_tombstones_store.drop, run.agent_slug, rp)
        return False

    async def _sync_apply_windowed(self, run: _SyncRun, action) -> None:
        """One action inside the run's window; a failure logs, the tick counts it."""
        async with run.window:
            try:
                await self._sync_apply_action(run, action)
            except Exception:
                logger.exception(
                    "initial-sync action failed for %s/%s",
                    run.agent_slug, action.rel_path,
                )
            finally:
                # The push's share leaves as its file counts done, so
                # the row never steps back.
                run.partial.pop(action.rel_path, None)
                run.n["done"] += 1
                await self._sync_progress_tick(run)

    async def _notify_conflict_losers(
        self, agent_slug: str, to_notify: list[tuple[str, str]],
    ) -> None:
        """Notify each user whose edit lost a conflict — their version is
        recoverable from the workspace Recover bin. Coalesced per user. The
        notification deep-links to the Recover button (no download)."""
        import asyncio as _asyncio
        from collections import defaultdict

        from core.remote.file_sync import replaced_notice
        from services.notifications import notification_manager
        from storage import database

        by_user: dict[str, list[str]] = defaultdict(list)
        for slug, name in to_notify:
            if name not in by_user[slug]:
                by_user[slug].append(name)
        for slug, names in by_user.items():
            sub = await _asyncio.to_thread(database.get_user_sub_by_username, slug)
            if not sub:
                continue  # loser slug no longer maps to a user → nobody to notify
            title, body = replaced_notice(names)
            try:
                await notification_manager.fire_notification(
                    title=title, body=body, severity="info",
                    scope="user", target=sub,
                    source="file_conflict", agent_slug=agent_slug,
                )
            except Exception:
                logger.exception("conflict notify failed for %s", slug)
