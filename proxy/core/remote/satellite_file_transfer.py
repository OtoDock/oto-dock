"""The satellite's ``file_changed`` lane and shared-workspace conflicts (mixin).

A machine's file changes pass admission, run on one lane per machine and apply
to the platform's copy, with multi-user conflict detection and recoverable
backups on the live write-back path. The transfer verbs (streaming push, stat,
the pull stream) are ``satellite_file_verbs``, this mixin's base. Mixed into
SatelliteConnectionManager.
"""

import asyncio
import base64
import contextlib
import hashlib
import logging
import stat
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from core.background import spawn
from core.remote.satellite_file_verbs import (  # noqa: F401
    SatelliteFileVerbsMixin,
    _PullStream,
    push_ceiling_s,
)
from services.infra import path_confinement, safe_fs

logger = logging.getLogger("otodock.satellite")


# --- Multi-user shared-workspace conflict detection (versioned-sync) ---

# Only files at or under this size are conflict-tracked AND get a recoverable
# byte backup on the live write-back path — we never read/hash a large file on
# the per-turn apply path. Larger files still fan out + propagate; they just
# aren't captured live (an idle satellite still captures them up to the
# recover-bin cap at session-start).
CONFLICT_BACKUP_MAX_BYTES = 2 * 1024 * 1024


@dataclass
class _AdmittedFrame:
    """A ``file_changed`` frame past admission: the authenticated context and
    the fields the applier keys on, all from that context or the frame."""
    sec: object
    agent_slug: str
    rel_path: str
    action: str
    session_id: str
    msg: dict


@dataclass
class _FileChangedLane:
    """One machine's applier lane: the frames in flight (queued or running)
    and the semaphore that bounds how many run at once. A task keeps the
    record it started on, so a replaced connection's appliers finish on
    their own lane and never touch a fresh one's count."""
    sem: asyncio.Semaphore
    inflight: int = 0
    last_shed_warning: float | None = None
    shed_since_warning: int = 0


# When a machine last pushed a change of an agent's tree (monotonic), by
# (machine_id, agent_slug): a platform-side judge (CHECKS.md ``judge_on:
# platform``) waits until the post-turn frames go quiet. Stamped only for
# admitted frames (the agent is the session's authenticated one), bounded at
# ``SAT_FILE_CHANGED_STAMP_MAX`` entries, and evicted with a machine's
# connection unless the machine sits in the reconnect grace (the satellite
# replays its frames on reconnect and the judge must keep waiting).
LAST_FILE_CHANGED: OrderedDict[tuple[str, str], float] = OrderedDict()


def last_file_changed_at(machine_id: str, agent_slug: str) -> float:
    return LAST_FILE_CHANGED.get((machine_id, agent_slug), 0.0)


def _stamp_file_changed(machine_id: str, agent_slug: str) -> None:
    import config
    key = (machine_id, agent_slug)
    if key in LAST_FILE_CHANGED:
        del LAST_FILE_CHANGED[key]
    LAST_FILE_CHANGED[key] = time.monotonic()
    while len(LAST_FILE_CHANGED) > config.SAT_FILE_CHANGED_STAMP_MAX:
        LAST_FILE_CHANGED.popitem(last=False)


def evict_file_changed_stamps(machine_id: str) -> None:
    for key in [k for k in LAST_FILE_CHANGED if k[0] == machine_id]:
        del LAST_FILE_CHANGED[key]


# The frames of a machine and agent that are queued or being applied: kept
# beside the lanes (a lane's record is replaced at every reconnect while
# its appliers finish on the old one). A judge that waits for a turn's
# files to go quiet reads it: a frame still waiting for a slot, or a large
# file still being pulled, is not quiet however old its stamp is.
_IN_FLIGHT: dict[tuple[str, str], int] = {}


def file_changed_in_flight(machine_id: str, agent_slug: str) -> int:
    return _IN_FLIGHT.get((machine_id, agent_slug), 0)


def _in_flight(machine_id: str, agent_slug: str, step: int) -> None:
    key = (machine_id, agent_slug)
    left = _IN_FLIGHT.get(key, 0) + step
    if left > 0:
        _IN_FLIGHT[key] = left
    else:
        _IN_FLIGHT.pop(key, None)


class _FleetSlot:
    """One of the fleet's applier slots, held around platform work only:
    ``released`` gives it back for a wait on the applier's machine (a large
    file's pull) and takes one again after. Taken inside the path lock, so
    a holder of a slot never waits for a path lock."""

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem
        self._held = False

    async def __aenter__(self) -> "_FleetSlot":
        await self._sem.acquire()
        self._held = True
        return self

    async def __aexit__(self, *exc) -> None:
        if self._held:
            self._held = False
            self._sem.release()

    @contextlib.asynccontextmanager
    async def released(self):
        if self._held:
            self._held = False
            self._sem.release()
        try:
            yield
        finally:
            await self._sem.acquire()
            self._held = True


def _machine_of_session(session_id: str) -> str:
    """The machine the remote layer holds ``session_id`` on, "" when it holds
    no such session (a local session, or one not yet re-adopted)."""
    from core.session.session_manager import live_session
    live = live_session(session_id)
    return live.machine_id if live is not None else ""


def _inline_matches(msg: dict) -> bool:
    """Whether an inline write's bytes are the ones its ``hash`` names.
    Blocking (up to the inline cap of base64 and a sha256)."""
    import hashlib
    try:
        data = base64.b64decode(msg.get("content_b64") or "")
    except (ValueError, TypeError):
        return False
    return msg.get("hash") == "sha256:" + hashlib.sha256(data).hexdigest()


def _admit_file_changed(machine_id: str, msg: dict) -> "_AdmittedFrame | None":
    """The gate every ``file_changed`` frame passes before it touches any
    state: the frame names an agent, a path and an action; the claimed
    session has a registered SecurityContext; a context bound to a machine
    is bound to THIS machine; and the context's agent is the frame's. A
    frame that fails costs a dictionary lookup and a log line: no stamp, no
    query, no task. Identity is never read off the payload.
    """
    agent_slug = msg.get("agent_slug", "")
    rel_path = msg.get("path", "")
    action = msg.get("action", "")
    if not agent_slug or not rel_path or not action:
        return None
    session_id = msg.get("session_id", "")
    # A compute machine beside the platform writes the platform's folder
    # itself: a change it reports is one the platform already has, and its
    # process is never a writer of the tree through this door. One on
    # another host reports its sessions' changes as a paired machine does,
    # each judged below with its session's own role.
    from core.remote.satellite_connection import get_connection_manager
    if get_connection_manager().shares_folders(machine_id):
        logger.warning("file_changed: dropped a frame from compute machine %s (%s): "
                       "its files are the platform's own", machine_id[:8], rel_path[:80])
        return None

    from core.remote import file_sync as core_file_sync
    from core.session.session_state import get_session_security
    sec = get_session_security(session_id) if session_id else None
    # Bind the claimed session to the SENDING machine (same rule as
    # transcript_lines / pty_inject_result): a compromised satellite must
    # not inherit another machine's session role by quoting its session id.
    # The machine a session runs on is the remote layer's record of it (a
    # task's or a meeting participant's context names no machine on its
    # placement); a context with a placement machine is judged on it, and a
    # session the remote layer does not hold whose context names none is a
    # mismatch for a frame from a machine.
    _sess_target = _machine_of_session(session_id) or (sec.placement.machine_id if sec else "")
    if sec is not None and _sess_target != machine_id:
        logger.warning(
            "file_changed: session %s is bound to machine %s but the "
            "frame came from %s; dropping the claimed role",
            str(session_id)[:8], str(_sess_target)[:8] or "none", str(machine_id)[:8],
        )
        sec = None
    if sec is None:
        # Engine-internal machinery paths (.claude/.codex/.credentials) with
        # NO registered context are the engine's last write landing after
        # the session's close (Claude stamps `.claude/.last-cleanup` on exit,
        # and a mode-switch close unregisters the context a few ms before):
        # said at info so the close race stays visible
        # without a WARNING. A missing context on a NORMAL path is exactly
        # the anomaly the WARNING exists to surface.
        if core_file_sync.is_engine_machinery_path(rel_path):
            logger.info(
                "write-back skipped (engine machinery, no session context): "
                "session=%s path=%s action=%s",
                (session_id[:8] if session_id else "?"), rel_path, action,
            )
        else:
            logger.warning(
                "write-back denied: session=%s role=no-ctx path=%s action=%s",
                (session_id[:8] if session_id else "?"), rel_path, action,
            )
        return None
    # Agent identity is part of "never trust the satellite payload": the
    # session's AUTHENTICATED agent must be the frame's. A mismatch means a
    # buggy/compromised satellite tried to write into a DIFFERENT agent's
    # tree: reject. This also keeps the lock / fan-out / conflict / stamp
    # keys honest, since fan-out targets are selected by agent_slug.
    sec_agent = getattr(sec, "agent", "") or ""
    if sec_agent != agent_slug:
        logger.warning(
            "write-back agent mismatch: session=%s sec_agent=%s payload=%s path=%s",
            (session_id[:8] if session_id else "?"), sec_agent, agent_slug, rel_path,
        )
        return None
    return _AdmittedFrame(sec=sec, agent_slug=agent_slug, rel_path=rel_path,
                          action=action, session_id=session_id, msg=msg)


class SatelliteFileTransferMixin(SatelliteFileVerbsMixin):

    def _fc_lane(self, machine_id: str) -> _FileChangedLane:
        import config
        lane = self._fc_lanes.get(machine_id)
        if lane is None:
            lane = self._fc_lanes[machine_id] = _FileChangedLane(
                sem=asyncio.Semaphore(config.SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE))
        return lane

    def _reset_file_changed_lane(self, machine_id: str) -> None:
        """A fresh lane for a (re)connecting or leaving machine: appliers still
        running hold the old record and finish on it."""
        self._fc_lanes.pop(machine_id, None)

    def _dispatch_file_changed(self, machine_id: str, msg: dict) -> bool:
        """Admit, bound and schedule one ``file_changed`` frame from the WS
        receive loop. False when the frame was dropped or shed.

        Bounds: at most ``SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE`` frames
        of one machine queued or running (0 = no shedding; past it the frame
        is shed with a rate-limited WARNING: file sync is best-effort and the
        next merge reconciles), ``SAT_FILE_CHANGED_CONCURRENCY_PER_MACHINE``
        running per machine, and ``SAT_FILE_CHANGED_CONCURRENCY_GLOBAL``
        doing platform work across machines. The order is the machine's own
        lane, then the path lock, then a fleet slot inside it, given back
        while the applier waits on its machine (``_FleetSlot``): a machine
        that sends slowly holds its own lane, never the fleet's slots.
        """
        import config
        frame = _admit_file_changed(machine_id, msg)
        if frame is None:
            return False
        lane = self._fc_lane(machine_id)
        # Every admitted frame stamps the quiet window, shed ones included: a
        # platform-side judge must not read a turn as quiet while its mirror
        # is incomplete.
        _stamp_file_changed(machine_id, frame.agent_slug)
        ceiling = config.SAT_FILE_CHANGED_MAX_INFLIGHT_PER_MACHINE
        if ceiling > 0 and lane.inflight >= ceiling:
            lane.shed_since_warning += 1
            now = time.monotonic()
            if lane.last_shed_warning is None or now - lane.last_shed_warning >= 10.0:
                logger.warning(
                    "file_changed: machine %s has %d frame(s) in flight; %d frame(s) "
                    "shed (the next merge reconciles them)",
                    machine_id[:8], lane.inflight, lane.shed_since_warning,
                )
                lane.last_shed_warning = now
                lane.shed_since_warning = 0
            return False
        lane.inflight += 1
        _in_flight(machine_id, frame.agent_slug, +1)
        spawn(self._run_applier(machine_id, lane, frame),
              name="run_applier", owner="satellite_file_transfer")
        return True

    async def _run_applier(self, machine_id: str, lane: _FileChangedLane,
                           frame: _AdmittedFrame) -> None:
        try:
            async with lane.sem:
                await self._apply_admitted(machine_id, frame)
        except Exception:
            logger.exception(
                "_apply_file_changed: failed for %s/%s", frame.agent_slug, frame.rel_path,
            )
        finally:
            lane.inflight -= 1
            _in_flight(machine_id, frame.agent_slug, -1)
            # A long-queued frame keeps the judge's quiet window open until
            # it is applied.
            _stamp_file_changed(machine_id, frame.agent_slug)

    async def _apply_file_changed(self, machine_id: str, msg: dict) -> None:
        """Admit and apply one ``file_changed`` frame: the direct entry (the
        receive loop goes through ``_dispatch_file_changed``, which admits
        once and runs the body under the lanes)."""
        frame = _admit_file_changed(machine_id, msg)
        if frame is None:
            return
        _stamp_file_changed(machine_id, frame.agent_slug)
        await self._apply_admitted(machine_id, frame)

    async def _apply_admitted(self, machine_id: str, frame: _AdmittedFrame) -> None:
        """Apply an admitted satellite-side file change to the platform's
        agent_dir, then fan it out to every OTHER satellite running the same
        agent.

        - Small files (≤ 1 MB) carry ``content_b64`` inline → write directly.
        - Large files (> 1 MB) carry only ``size`` + ``hash`` → pull explicitly,
          then write.
        - Deletes have no body — apply atomically. A delete that repeats a
          recorded one (``_delete_recorded``) changes nothing.

        Idempotent if the platform already has the same hash. Errors are
        logged and swallowed — file sync is best-effort during a turn; if
        it fails the user will see stale state until the next session start.

        The role gate (``can_write_back``) runs here, after admission; a
        knowledge-mirror path costs one query for the consumer's RW pairs.
        Apply + conflict-detect run UNDER the global per-(agent, rel_path)
        lock so concurrent same-file writers converge on a consistent byte
        sequence (last writer wins, never a torn interleave). The fan-out
        runs AFTER the lock is released, under the per-path fan-out lock
        taken inside it, with the applied bytes (an inline write) or the
        file's path (a pulled one) as its source: same-path fan-outs stay in
        apply order, a slow target no longer holds the next writer of the
        path back, and each machine's lane hashes the very descriptor it
        pushes, so a later same-path write cannot change what the recorded
        merge base names. Every store call runs on the DB lane (``run_db``).
        """
        import config as _cfg
        from core.remote import file_sync as core_file_sync
        from storage.pg import run_db

        sec = frame.sec
        agent_slug, rel_path, action = frame.agent_slug, frame.rel_path, frame.action
        session_id, msg = frame.session_id, frame.msg

        # Role-aware write-back guard (SECURITY). The satellite→platform write
        # direction must obey the same per-role write matrix as native tools.
        # The role is the AUTHENTICATED session's, NEVER the payload's.
        # Fail-closed. This is the ONLY filesystem-write gate for Codex remote
        # sessions (Codex has no per-tool permission hooks).
        _role = getattr(sec, "role", "") or ""
        _uname = getattr(sec, "username", "") or ""
        # MOUNT identity for the per-user-dir rule (Shared-only human chats
        # blank it); the REAL username above keeps owner config/knowledge
        # curation working. None (a ctx without the property) falls back to
        # username inside can_write_back.
        _mount = getattr(sec, "mount_username", None)
        # Knowledge-library mirror paths need the consumer's RW-attachment
        # (source, subdir) pairs for can_write_back's segment-wise subtree
        # rule (one indexed SELECT, only for mirror paths — plain writes
        # never pay it, and a frame that failed admission never reached it).
        _lib_src = core_file_sync.library_mirror_source(rel_path)
        _writable_libs: frozenset[tuple[str, str]] | None = None
        if _lib_src is not None:
            from storage.knowledge import db_knowledge_libraries
            _writable_libs = await run_db(
                db_knowledge_libraries.writable_pairs_for, agent_slug)
        if not core_file_sync.can_write_back(
                rel_path, _role, _uname, mount_username=_mount,
                writable_libraries=_writable_libs,
                knowledge_rw=bool(getattr(sec, "knowledge_rw", False))):
            # Engine-internal machinery paths (.claude/.codex/.credentials)
            # are denied for EVERY role by design, and engines rewrite their
            # own runtime state each turn (codex: models_cache.json) — that
            # denial is routine, not a signal. Keep WARNING for everything
            # else: a role/scope denial on a NORMAL path is exactly the
            # anomaly this log exists to surface.
            if core_file_sync.is_engine_machinery_path(rel_path):
                logger.debug(
                    "write-back skipped (engine machinery): session=%s path=%s action=%s",
                    (session_id[:8] if session_id else "?"), rel_path, action,
                )
            else:
                logger.warning(
                    "write-back denied: session=%s role=%s path=%s action=%s",
                    (session_id[:8] if session_id else "?"), _role, rel_path, action,
                )
            return

        # Satellite-side delete inside a library mirror. The satellite only
        # sends ``action=delete`` for a live watcher event (never inferred
        # from absence), so on a WRITABLE attachment it is the agent's
        # explicit intent — route it through the projector's explicit-delete
        # channel (capture at the source, delete at the source, propagate to
        # every mirror; 2026-09-03). A read-only mirror keeps the old rule:
        # ignored, the projector heals the file back from the source.
        # Applying the delete locally would just churn a tombstone against
        # the heal. Content writes above already passed the RW gate.
        if _lib_src is not None and action == "delete":
            _parts = rel_path.split("/")
            _sub_rel = "/".join(_parts[3:]) if len(_parts) >= 4 else ""
            _propagated = False
            if _sub_rel:
                from services.knowledge import library_projector
                try:
                    _propagated = await library_projector.propagate_mirror_delete(
                        agent_slug, _lib_src, _sub_rel)
                except Exception:
                    logger.exception(
                        "library-mirror delete propagation failed: %s/%s",
                        agent_slug, rel_path)
            if _propagated:
                logger.info(
                    "library-mirror delete propagated to source %s: %s/%s",
                    _lib_src, agent_slug, rel_path,
                )
            else:
                logger.info(
                    "library-mirror delete ignored (heals from source): %s/%s",
                    agent_slug, rel_path,
                )
            return
        try:
            agent_dir = _cfg.get_agent_dir(agent_slug)
        except ValueError:
            logger.warning("file_changed from %s refused: %r names no agent folder",
                           machine_id[:8], agent_slug)
            return

        # Sync-delta telemetry (alert-only, post-gate): count authorized
        # satellite-authored writes in a rolling window — a per-turn flood
        # usually means a build tree the sync-ignore table doesn't cover.
        if action == "write":
            try:
                from core.remote import sync_delta_alerts
                _sz = msg.get("size") or (
                    len(msg.get("content_b64") or "") * 3 // 4)
                _crossed = sync_delta_alerts.record_turn_write(
                    machine_id, agent_slug, rel_path, int(_sz or 0))
                if _crossed is not None:
                    spawn(sync_delta_alerts.maybe_alert(
                        machine_id, agent_slug, source="per-turn",
                        new_files=_crossed[0], new_bytes=_crossed[1],
                        subtree_counts=_crossed[2]),
                          name="maybe_alert", owner="satellite_file_transfer")
            except (ImportError, ValueError, TypeError):
                logger.debug("sync-delta turn counter failed", exc_info=True)

        # All platform-side writes to this agent file serialize on the global
        # per-(agent, rel_path) lock — across sessions and machines — so
        # pull_through / push_back / this applier / the fan-out never interleave
        # a torn write. agent_slug is guaranteed non-empty past admission.
        from core.remote import remote_file_flow
        from services.remote import workspace_fanout
        conflict_notify: tuple[str, str] | None = None  # (loser_slug, filename)
        fanout_source: bytes | Path | None = None
        fanout_lock = None
        fanout_marked: dict[str, tuple[int, int]] | None = None
        lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rel_path)
        try:
            async with lock, _FleetSlot(self._fc_global_sem) as slot:
                from storage.files import (
                    file_author_store,
                    file_tombstones_store,
                    recover_bin_store,
                    sync_state_store,
                )

                if action == "delete" and await self._delete_recorded(
                        machine_id, agent_dir, agent_slug, rel_path):
                    # The same delete again (a second session on the machine,
                    # or the echo of a delete the platform made): the record
                    # keeps the time and age the first one set, and nothing is
                    # sent on.
                    logger.info("file_changed from %s: the delete of %s/%s is already recorded",
                                machine_id[:8], agent_slug, rel_path)
                    return
                if action == "write" and "content_b64" in msg and msg.get("hash") and \
                        not await asyncio.to_thread(_inline_matches, msg):
                    # The frame's hash becomes the merge base: an inline write
                    # applies only when it is the hash of the bytes it carries
                    # (checked under the lock, so writes keep their order). A
                    # file that changed between a machine's hash and its read
                    # is reported again at its next turn's end.
                    logger.warning("file_changed from %s: %s not applied, its bytes are not "
                                   "the ones its hash names", machine_id[:8], rel_path)
                    return
                # Pre-capture the to-be-removed/overwritten bytes. Size-gated, so a
                # large file is never read on the apply path (None → not captured).
                pre_bytes, pre_hash = await self._capture_pre_overwrite(
                    agent_dir, rel_path,
                )

                if not await self._apply_file_changed_inner(
                        agent_dir, msg, machine_id, agent_slug, action, slot=slot):
                    # The write never landed (its pull failed, its path was
                    # refused, the frame had no body): the platform is as it
                    # was, so no base and no author are recorded, nothing is
                    # captured or sent on, and the next merge compares both
                    # copies, as a merge's own failed pull leaves them.
                    return

                # Keep the versioned-sync state current under this same lock:
                # so the next session-start merge sees no phantom conflict, deletes
                # propagate to idle satellites (tombstone), and a genuine cross-user
                # live overwrite captures the loser + notifies them.
                if action == "delete":
                    await run_db(
                        file_tombstones_store.record, agent_slug, rel_path,
                        time.time(), origin="live-delete",
                    )
                    if pre_bytes is not None:
                        await run_db(
                            recover_bin_store.capture, agent_slug, rel_path,
                            pre_bytes, "deleted",
                        )
                    await run_db(
                        sync_state_store.clear_one, machine_id, agent_slug, rel_path,
                    )
                    await run_db(file_author_store.clear, agent_slug, rel_path)
                else:
                    new_hash = msg.get("hash", "") or ""
                    # Clobber check: the platform copy changed since THIS machine's
                    # last-converged base → the satellite is overwriting an edit it
                    # never saw. The satellite wins (the live write applies), so the
                    # platform's prior bytes are the loser → strict capture.
                    base_row = await run_db(
                        sync_state_store.get_one, machine_id, agent_slug, rel_path,
                    )
                    base_hash = base_row[0] if base_row else None
                    if (pre_hash is not None and new_hash
                            and new_hash != pre_hash and pre_hash != base_hash):
                        author = await run_db(file_author_store.get, agent_slug, rel_path)
                        cap_side, cap_reason, notify_user = core_file_sync._divergence_capture(
                            rel_path, lambda _p: author, _uname, platform_wins=False,
                        )
                        if cap_side == "platform" and pre_bytes is not None:
                            entry = await run_db(
                                recover_bin_store.capture, agent_slug, rel_path,
                                pre_bytes, cap_reason,
                            )
                            if entry is not None and notify_user:
                                conflict_notify = (notify_user, entry["original_name"])
                    # Advance base + author to the satellite's just-applied write.
                    base_mtime = await asyncio.to_thread(self._mtime_of, agent_dir, rel_path)
                    await run_db(
                        sync_state_store.record_one, machine_id, agent_slug,
                        rel_path, new_hash, base_mtime,
                    )
                    await run_db(file_author_store.record, agent_slug, rel_path, _uname)

                # Fan out the applied change to every OTHER satellite of this
                # agent (isolation-filtered inside). The one store read a
                # personal path needs runs on the DB lane, never here.
                if action == "delete":
                    await workspace_fanout.fan_out_delete(
                        agent_slug, rel_path, exclude_machine_id=machine_id,
                    )
                elif workspace_fanout.fanout_targets(
                    agent_slug, rel_path, exclude_machine_id=machine_id,
                    shared_only=await workspace_fanout.shared_only_of(agent_slug, rel_path),
                ):
                    # The fan-out lock is taken here (inside the path lock, so
                    # fan-outs of this path keep apply order) and the push runs
                    # after the release: the applied bytes for an inline write,
                    # the file's path for a pulled one (push_file streams per
                    # chunk, so memory stays O(chunk) even for a 1GB file).
                    if "content_b64" in msg:
                        fanout_source = await asyncio.to_thread(
                            base64.b64decode, msg.get("content_b64") or "")
                    else:
                        fanout_source = await self._workspace_path_checked(agent_dir, rel_path)
                    if fanout_source is not None:
                        # The targets are marked in flight before the path
                        # lock is released: a read there with no pull record
                        # would otherwise pull the older bytes back first.
                        fanout_marked = await workspace_fanout.mark_fan_out_targets(
                            agent_slug, rel_path, exclude_machine_id=machine_id)
                        fanout_lock = await remote_file_flow.acquire_fanout_lock(
                            agent_slug, rel_path)
                        await fanout_lock.acquire()
        except Exception:
            logger.exception(
                "_apply_file_changed: failed for %s/%s", agent_slug, rel_path,
            )
            if fanout_lock is not None and fanout_lock.locked():
                fanout_lock.release()
            return

        if fanout_lock is not None:
            try:
                # The lock is taken only for a resolved source (above).
                await workspace_fanout.fan_out_write(
                    agent_slug, rel_path, cast("bytes | Path", fanout_source),
                    exclude_machine_id=machine_id,
                    marked=fanout_marked,
                )
            except Exception:
                logger.exception(
                    "_apply_file_changed: fan-out failed for %s/%s", agent_slug, rel_path,
                )
            finally:
                fanout_lock.release()

        if conflict_notify is not None:
            # Notify after releasing the lock — it doesn't need it.
            await self._notify_live_conflict(agent_slug, *conflict_notify)

        # Refresh any open dashboard workspace view — the live write/delete just
        # changed the platform tree. Best-effort, outside the
        # lock (it doesn't need it). Both actions notify: a write adds/updates the
        # file, a delete makes it vanish from the refetched tree. NOT excluding the
        # satellite's user — that user is the one watching the dashboard for the
        # file to appear/disappear.
        from services.notifications import notification_manager
        await notification_manager.broadcast_file_updated(
            agent_slug, rel_path, source="disk",
        )

        # Knowledge-library projection (best-effort, post-lock): a write into
        # a promoted source's knowledge/ updates every consumer mirror; an
        # authorized RW mirror write flows back to its source. Both no-op in
        # one SELECT when the agent has no library rows.
        from services.knowledge import library_projector
        if _lib_src is not None:
            _parts = rel_path.split("/")
            if len(_parts) >= 4:
                spawn(library_projector.propagate_mirror_write(
                    agent_slug, _lib_src, "/".join(_parts[3:]),
                ), name="propagate_mirror_write", owner="satellite_file_transfer")
        elif rel_path.startswith("knowledge/"):
            spawn(library_projector.propagate_source_write(
                agent_slug, rel_path[len("knowledge/"):],
                deleted=(action == "delete"),
            ), name="propagate_source_write", owner="satellite_file_transfer")

    async def _capture_pre_overwrite(
        self, agent_dir, rel_path: str,
    ) -> tuple[bytes | None, str | None]:
        """Read + hash the current on-disk bytes of a workspace file BEFORE it is
        overwritten, for conflict detection — but ONLY if it exists and is
        ≤ ``CONFLICT_BACKUP_MAX_BYTES``. Returns ``(bytes, "sha256:<hex>")`` or
        ``(None, None)``. Never reads a large file on the apply path. The read
        opens beneath the agents root with no component followed: a link at
        the name or on the way is refused, never read through.
        """
        def _read() -> tuple[bytes | None, str | None]:
            agent = Path(agent_dir)
            try:
                data = safe_fs.read_bytes_beneath(
                    agent.parent, f"{agent.name}/{rel_path}",
                    max_size=CONFLICT_BACKUP_MAX_BYTES,
                )
            except OSError:
                return (None, None)
            return (data, "sha256:" + hashlib.sha256(data).hexdigest())
        return await asyncio.to_thread(_read)

    async def _workspace_path_checked(self, agent_dir, rel_path: str) -> Path | None:
        """The named path of a workspace file (post-apply) for streaming
        fan-out, never resolved: None when it is missing, not a regular
        file, or reached through a link at any component (checked beneath
        the agents root, nothing followed)."""
        def _check() -> Path | None:
            agent = Path(agent_dir)
            try:
                st = safe_fs.lstat_beneath(agent.parent, f"{agent.name}/{rel_path}")
                dest = path_confinement.join_under(agent, rel_path)
            except (ValueError, OSError):
                return None
            return dest if stat.S_ISREG(st.st_mode) else None
        return await asyncio.to_thread(_check)

    async def _delete_recorded(self, machine_id: str, agent_dir, agent_slug: str,
                               rel_path: str) -> bool:
        """Whether a reported delete repeats one the platform recorded: no
        name at the path (a folder gone with it counts), a live record that
        is not a hold, and no base of this machine for the path (its own
        first report, the platform's delete or a merge cleared it). A file
        that came back is held, and a machine that held a copy the platform
        knew has a base: each is a delete of its own."""
        from storage.files import file_tombstones_store, sync_state_store
        from storage.pg import run_db
        agent = Path(agent_dir)
        name = await asyncio.to_thread(
            safe_fs.kind_beneath, agent.parent, f"{agent.name}/{rel_path}")
        if name.kind != "missing":
            return False
        row = await run_db(file_tombstones_store.get, agent_slug, rel_path)
        if row is None or row["origin"] == file_tombstones_store.HOLD_ORIGIN:
            return False
        return await run_db(sync_state_store.get_one, machine_id, agent_slug, rel_path) is None

    def _mtime_of(self, agent_dir, rel_path: str) -> float:
        """Current mtime of a platform file (epoch seconds), or 0.0 — used to stamp
        the merge base after a live write-back."""
        try:
            return (Path(agent_dir) / rel_path).stat().st_mtime
        except OSError:
            return 0.0

    async def _notify_live_conflict(
        self, agent_slug: str, loser_slug: str, filename: str,
    ) -> None:
        """Notify a user whose edit just lost a live conflict — their version
        is in the workspace Recover bin (no download link; the dashboard
        deep-links to the Recover button). Best-effort — never raises."""
        try:
            from core.remote.file_sync import replaced_notice
            from services.notifications import notification_manager
            from storage import database
            from storage.pg import run_db
            loser_sub = await run_db(database.get_user_sub_by_username, loser_slug)
            if not loser_sub:
                return  # prior writer no longer maps to a user → nobody to notify
            title, body = replaced_notice([filename])
            await notification_manager.fire_notification(
                title=title, body=body,
                severity="info", scope="user", target=loser_sub,
                source="file_conflict", agent_slug=agent_slug,
            )
        except Exception:
            logger.exception(
                "live conflict notify failed for %s/%s", agent_slug, filename,
            )

    async def _apply_file_changed_inner(
        self, agent_dir, msg: dict, machine_id: str, agent_slug: str, action: str,
        *, slot: "_FleetSlot | None" = None,
    ) -> bool:
        """Inner half of _apply_file_changed (lock already held if applicable).
        ``slot``: the caller's fleet slot, given back while a large file is
        pulled from the machine. False when a write did not land: a pull that
        failed, a path refused, a frame with no body. Each says so in the log."""
        from core.remote import file_sync as core_file_sync

        if action == "delete":
            await asyncio.to_thread(
                core_file_sync.apply_incoming_file,
                agent_dir, msg["path"], "delete", None,
            )
            return True

        # Routing: an inline write carries the content key (possibly "" — a
        # ZERO-BYTE file is a real write, not an absence); a large-file
        # notification carries no content key but a size, and is pulled.
        if "content_b64" in msg:
            await asyncio.to_thread(
                core_file_sync.apply_incoming_file,
                agent_dir, msg["path"], "write", msg.get("content_b64") or "",
            )
            return True

        if msg.get("size", 0) > 0:
            # Large file — stream the body straight to disk (chunked pull),
            # never holding the whole file in memory, at the named path,
            # never resolved: the pull opens it by components and refuses a
            # link at any one.
            from services.path_policy_v2 import PathRef
            if not core_file_sync.is_canonical_rel_path(msg["path"]):
                logger.warning(
                    "file_changed pull traversal blocked: %s", msg["path"],
                )
                return False
            dest = path_confinement.join_under(agent_dir, msg["path"])
            async with (slot.released() if slot is not None else contextlib.nullcontext()):
                ok = await self.pull_file_to_path(
                    machine_id,
                    PathRef("agent_tree", msg["path"]),
                    dest,
                    agent_slug=agent_slug,
                )
            if not ok:
                logger.warning(
                    "file_changed pull failed for %s", msg.get("path"),
                )
            return ok
        logger.warning("file_changed from %s: a write of %s carries no body and no size, not applied",
                       machine_id[:8], msg.get("path"))
        return False
