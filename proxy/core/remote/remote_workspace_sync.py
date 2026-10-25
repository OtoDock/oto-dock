"""Workspace sync between the platform and a satellite for remote agents (mixin).

On reconnect / session start the platform and satellite reconcile their copies of
an agent's workspace (push/pull/merge with versioned-sync conflict handling),
with large conflict-free pulls deferred to the background. Mixed into
RemoteExecutionLayer; split out of remote_execution.py.
"""

import asyncio
import contextlib
import functools
import logging
import os
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import psycopg

from auth import roles as _roles
from core import layout, placement
from services.infra import path_confinement, safe_fs

if TYPE_CHECKING:
    from core.remote.file_sync import FileEntry, MergePlan

logger = logging.getLogger("remote-layer")


# Initial-sync PULLS (satellite→platform) at or above this size are deferred to a
# background task so they don't block the CLI from starting.
# Only pulls defer — a deferred PUSH could clobber a concurrent agent edit, but a
# pull only refreshes the platform/dashboard view and never touches the agent's
# disk, so deferring it is clobber-free. Small pulls stay foreground (cheap, and
# keeps the dashboard immediately fresh).
_DEFER_PULL_MIN_BYTES = 4 * 1024 * 1024  # 4 MB


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


# The manifest walk and the diff are CPU work with a stat and a hash per
# file. On the default executor a fleet re-authenticating after a proxy
# restart parked every other ``to_thread`` on the box for seconds, and the
# walks slowed each other down in the GIL. They run on a pool of their own,
# two workers so that background walks and a person's merge never wait on
# the same worker; the rest of a merge (short store reads and writes, the
# transfers) stays where it was. ``diff_manifests`` still asks the author
# store per conflicting file from this pool, as it did from the default one.
_SYNC_CPU_WORKERS = 2
_sync_cpu: ThreadPoolExecutor | None = None


def _sync_cpu_executor() -> ThreadPoolExecutor:
    global _sync_cpu
    if _sync_cpu is None:
        from core import loop_watchdog
        _sync_cpu = ThreadPoolExecutor(
            max_workers=_SYNC_CPU_WORKERS, thread_name_prefix="sync-cpu",
        )
        loop_watchdog.watch_executor("sync-cpu", _sync_cpu)
    return _sync_cpu


async def _run_sync_cpu(fn, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _sync_cpu_executor(), functools.partial(fn, *args, **kwargs),
    )


# Background merges (a reconnect walk, the idle sweep) are bounded twice.
# The reconnect gate limits how many of them apply at once across the
# fleet; the background manifest gate keeps their manifest and diff work
# to one of the two sync-cpu workers, so a person's merge (a session start,
# the enable-click presync: neither takes a gate) waits behind at most one
# cold walk, never a wave. A reconnect walk sleeps a jitter first so a
# fleet re-authenticating together does not walk together. The gates are
# kept per event loop: a semaphore binds to the loop of its first waiter.
#
# Lock order: the reconnect gate, then the per-(machine, agent) sync lock,
# then the background manifest gate (held only through the manifest and
# the diff, inside the sync lock), then the per-file path lock and the
# transfer gate. A person's merge takes the sync lock without either gate,
# so a walk waiting on a gate never holds a person up.
_RECONNECT_SYNC_JITTER_S = 10.0
_RECONNECT_SYNC_CONCURRENCY = 4
_BACKGROUND_MANIFEST_CONCURRENCY = 1
# A machine's reconnect walk starts no sooner than this after its previous
# walk COMPLETED (a pass that reached every agent): a machine flapping three
# times a minute walks twice at most, one back after an hour walks at once.
# Longer would let a session re-adopted from grace read bytes a tombstone or
# a platform-ahead mark says are stale. A pass a drop cut short restarts no
# window.
_RECONNECT_DEBOUNCE_S = 30.0
# machine_id → when its last complete walk ended (``_monotonic``); entries
# older than the window are dropped at the next stamp.
_reconnect_completed_at: dict[str, float] = {}
_gates: dict[str, tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore]] = {}


def _gate(name: str, limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    held = _gates.get(name)
    if held is None or held[0] is not loop or held[1] != limit:
        held = (loop, limit, asyncio.Semaphore(limit))
        _gates[name] = held
    return held[2]


def _reconnect_gate() -> asyncio.Semaphore:
    return _gate("reconnect", _RECONNECT_SYNC_CONCURRENCY)


def _background_manifest_gate() -> asyncio.Semaphore:
    return _gate("manifest", _BACKGROUND_MANIFEST_CONCURRENCY)


# The page a 0.5.130 satellite is asked for (entries per frame; the
# satellite also caps a frame's text), so a large tree's manifest never needs
# a frame past the socket's cap.
_MANIFEST_PAGE_SIZE = 4096


def manifest_request(cm, machine_id: str, agent_slug: str, *,
                     report_skipped: bool = False) -> dict:
    """The ``request_manifest`` frame for ``machine_id``: paged when the
    satellite pages (asking an older one would be ignored, and an older
    proxy never asks, so the wire stays compatible both ways).
    ``report_skipped`` asks a 0.5.145 satellite to list what it could not
    read as entries with ``skipped`` and no hash (only the merge reads
    those; the caller checks the satellite's version)."""
    msg: dict[str, Any] = {"type": "request_manifest", "agent_slug": agent_slug}
    if cm.satellite_supports_paged_manifest(machine_id):
        msg["page_size"] = _MANIFEST_PAGE_SIZE
    if report_skipped:
        msg["report_skipped"] = True
    return msg


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


def _reconnect_jitter_s() -> float:
    return random.uniform(0.0, _RECONNECT_SYNC_JITTER_S)


def _monotonic() -> float:
    return time.monotonic()


def _reconnect_hold_s(machine_id: str) -> float:
    """What is left of ``machine_id``'s debounce window, 0 outside it."""
    done_at = _reconnect_completed_at.get(machine_id)
    if done_at is None:
        return 0.0
    return max(0.0, done_at + _RECONNECT_DEBOUNCE_S - _monotonic())


def _note_reconnect_pass_done(machine_id: str) -> None:
    now = _monotonic()
    for mid, at in list(_reconnect_completed_at.items()):
        if now - at >= _RECONNECT_DEBOUNCE_S:
            del _reconnect_completed_at[mid]
    _reconnect_completed_at[machine_id] = now


@dataclass
class _ReconnectWalk:
    sleeping: bool = True
    rerun: bool = False


# One reconnect walk per machine. ``register`` kicks one on every
# re-authentication, and a flapping or replaced machine would pile them up:
# a kick that finds a walk still in its jitter returns (that walk runs
# soon); one that finds a walk past it asks for one more pass.
_reconnect_walks: dict[str, _ReconnectWalk] = {}
# (machine, agent) pairs with an idle-sweep merge pending, so a merge that
# waits on the gate past the next sweep is not spawned twice.
_idle_sync_inflight: set[tuple[str, str]] = set()


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


if TYPE_CHECKING:
    from core.remote.remote_host import RemoteLayerHost as _Host
else:
    _Host = object


class RemoteWorkspaceSyncMixin(_Host):
    # --- Workspace sync ---

    async def resolve_machine_sync_identity(
        self, machine_id: str, agent_slug: str,
    ) -> "tuple[str | None, str] | None":
        """Resolve ``(target_username, target_role)`` for syncing ``agent_slug`` to a
        paired machine FROM ITS PAIRING — no active session needed. The single
        source of truth for "who is this machine, for this agent", shared by the
        reconnect catch-up (below) and the connected-idle fan-out
        (``services/workspace_fanout``):

          * admin-PAIRED machine → ``(None, role)`` — admin-shared, NO per-user
            filter, so it receives EVERY user's folder (mirrors
            ``should_sync_to_target``'s ``username is None`` branch); owned by
            a platform admin → role ``"admin"`` on every agent;
          * user-paired machine  → ``(owner_username, owner's per-agent role)``
            — for EVERYONE, platform admins included. An admin who is
            per-agent viewer/editor used to get role ``"admin"`` here, which
            granted their personal machine config/ push + write-back; that
            override let a satellite that lost its copy delete-attribute the
            agent's prompt (Jul 2026 incident). Machine-scope sync authority
            comes from the pairing + per-agent role, never the platform role.

        ``None`` ⇒ the machine isn't paired / has vanished (skip it). The role gates
        ``config/`` on the push side and ``can_write_back`` on the satellite→platform
        pull side, so a user machine only ever syncs back its role-allowed folders.
        """
        import asyncio as _asyncio

        from storage import database as _db, remote_store as _rs

        machine = await _asyncio.to_thread(_rs.get_remote_machine, machine_id)
        if not machine:
            return None
        owner_sub = machine.get("registered_by", "") or ""
        is_admin_paired = placement.machine_is_admin_paired(machine)
        # Platform-admin check — ONLY consulted for admin-PAIRED machines
        # (admin-shared, role "admin"). User-paired machines always resolve
        # the owner's per-agent role, platform admins included (see docstring).
        owner_is_admin = is_admin_paired and bool(owner_sub) and _roles.is_admin(
            ((await _asyncio.to_thread(_db.get_user, owner_sub)) or {}).get("role"))
        # Per-user isolation: user-paired → scope to the owner; admin-paired → None.
        if is_admin_paired:
            target_username: str | None = None
        else:
            target_username = (
                await _asyncio.to_thread(_db.get_username_by_sub, owner_sub)
                if owner_sub else None
            ) or ""
        if owner_is_admin:
            target_role = "admin"
        else:
            roles = (
                await _asyncio.to_thread(_db.get_user_agent_roles, owner_sub)
                if owner_sub else {}
            )
            target_role = (roles or {}).get(agent_slug, "")
        return (target_username, target_role)

    async def presync_machine_agent(self, machine_id: str, agent_slug: str) -> None:
        """W3 (sync-performance): warm a (machine, agent) workspace right at
        ENABLE-CLICK (admin assign / user per-agent target) instead of making
        the FIRST chat pay the whole initial transfer inline. Identity comes
        from the machine pairing (no session exists yet); an offline machine
        no-ops (the reconnect catch-up covers it); a concurrent warmup is
        serialized by the per-(machine, agent) sync lock. Best-effort by
        contract — callers fire-and-forget."""
        if not self._cm.is_connected(machine_id):
            return
        ident = await self.resolve_machine_sync_identity(machine_id, agent_slug)
        if ident is None:
            return
        target_username, target_role = ident
        await self._initial_workspace_sync(
            machine_id, agent_slug,
            target_username=target_username, target_role=target_role,
        )

    async def sync_all_agents_on_reconnect(self, machine_id: str) -> None:
        """Catch every agent previously synced to this machine up to the platform
        when the satellite RE-AUTHENTICATES — applying deletes (tombstones) + drift
        that landed while it was offline, WITHOUT waiting for a session warmup.

        No session here, so the (user, role) context comes from the machine's
        pairing (``resolve_machine_sync_identity``): a user-paired machine syncs as
        its owner (with the owner's per-agent role); an admin-paired machine syncs
        admin-shared. The per-(machine, agent) lock inside ``_initial_workspace_sync``
        serializes each agent against a concurrent session warmup. One walk per
        machine (a second kick coalesces into it), a jitter before the walk
        (or the rest of the machine's debounce window, ``_RECONNECT_DEBOUNCE_S``,
        after its previous complete walk, whichever is longer), the reconnect
        gate around each agent's merge. Best-effort.
        """
        from storage.files import sync_state_store

        walk = _reconnect_walks.get(machine_id)
        if walk is not None:
            if not walk.sleeping:
                walk.rerun = True
            return
        walk = _reconnect_walks[machine_id] = _ReconnectWalk()
        try:
            agents = await asyncio.to_thread(
                sync_state_store.agents_for_machine, machine_id,
            )
            if not agents:
                return
            await asyncio.sleep(max(_reconnect_jitter_s(), _reconnect_hold_s(machine_id)))
            walk.sleeping = False
            while True:
                if await self._reconnect_pass(machine_id, agents):
                    _note_reconnect_pass_done(machine_id)
                if not walk.rerun:
                    return
                # The pass a kick earned waits for the window too; kicks
                # meanwhile coalesce into it.
                walk.rerun = False
                walk.sleeping = True
                await asyncio.sleep(_reconnect_hold_s(machine_id))
                walk.sleeping = False
        finally:
            _reconnect_walks.pop(machine_id, None)

    async def _reconnect_pass(self, machine_id: str, agents) -> bool:
        """One pass over a machine's tracked agents; stops when the machine
        is no longer connected (its next re-authentication kicks a new walk).
        True when it reached every agent."""
        synced = 0
        for agent_slug in sorted(agents):
            if not self._cm.is_connected(machine_id):
                return False
            ident = await self.resolve_machine_sync_identity(machine_id, agent_slug)
            if ident is None:
                continue  # machine unpaired / vanished
            target_username, target_role = ident
            try:
                async with _reconnect_gate():
                    await self._initial_workspace_sync(
                        machine_id, agent_slug,
                        target_username=target_username, target_role=target_role,
                        background=True,
                    )
                synced += 1
            except Exception:
                logger.exception(
                    "reconnect-sync failed for %s/%s", machine_id[:8], agent_slug,
                )
        if synced:
            logger.info(
                "Reconnect workspace sync for %s: caught up %d agent(s)",
                machine_id[:8], synced,
            )
        return True

    async def run_idle_fingerprint_sweep(self) -> None:
        """Periodic backstop: for each CONNECTED-IDLE (machine, agent) whose
        satellite-reported STAT fingerprint changed since the last completed sync,
        run the merge — catching OUT-OF-TURN satellite-side changes (a file dropped /
        edited in the agents folder with no active session) that the per-turn
        ``detect_changes`` + fan-out never see. Fingerprint-gated, so a quiet machine
        costs nothing; the merge only runs when something actually changed. Scoped to
        agents the proxy already tracks (a ``sync_state`` base) — a brand-new agent
        dir gets its first sync at session start, not here. Each merge is spawned as a
        task that takes the per-(machine, agent) warmup lock (so it can't collide with
        a session warmup / reconnect-sync). Best-effort; called from the 60s registry
        sweep in ``app.py``."""
        import asyncio as _asyncio

        from services.remote import workspace_fanout
        from storage.files import sync_state_store

        cm = self._cm
        for machine_id in list(cm.get_connected_machines()):
            conn = cm.get_connection(machine_id)
            if conn is None or not conn.agent_fingerprints:
                continue
            try:
                tracked = await _asyncio.to_thread(
                    sync_state_store.agents_for_machine, machine_id,
                )
            except psycopg.Error:
                continue
            for slug, fp in list(conn.agent_fingerprints.items()):
                if slug not in tracked:
                    continue  # not a proxy-tracked agent on this machine
                if conn.synced_fingerprints.get(slug) == fp:
                    continue  # unchanged since the last completed sync
                if machine_id in workspace_fanout._active_machine_ids(slug):
                    continue  # an active session covers it via the per-turn path
                key = (machine_id, slug)
                if key in _idle_sync_inflight:
                    continue  # its merge is still pending (on the gate)
                _idle_sync_inflight.add(key)
                task = _asyncio.create_task(
                    self._idle_fingerprint_sync_one(machine_id, slug, fp)
                )
                task.add_done_callback(lambda t, key=key: _idle_sync_inflight.discard(key))  # type: ignore[misc]  # mypy infers no type for a lambda with a default argument
                self._deferred_sync_tasks.add(task)
                task.add_done_callback(self._deferred_sync_tasks.discard)

    async def _idle_fingerprint_sync_one(
        self, machine_id: str, agent_slug: str, fp: str,
    ) -> None:
        """Run one fingerprint-triggered idle merge + advance the synced baseline on
        success. The merge re-validates with real content hashes, so a fingerprint
        false-positive just finds in-sync. We record the fp that TRIGGERED this run
        (not the latest reported) so a change arriving mid-merge re-triggers next
        sweep rather than being skipped. Best-effort."""
        try:
            ident = await self.resolve_machine_sync_identity(machine_id, agent_slug)
            if ident is None:
                return
            target_username, target_role = ident
            async with _reconnect_gate():
                await self._initial_workspace_sync(
                    machine_id, agent_slug,
                    target_username=target_username, target_role=target_role,
                    background=True,
                )
            conn = self._cm.get_connection(machine_id)
            if conn is not None:
                conn.synced_fingerprints[agent_slug] = fp
        except Exception:
            logger.exception(
                "idle fingerprint sync failed for %s/%s", machine_id[:8], agent_slug,
            )

    async def _run_deferred_pulls(
        self, machine_id: str, agent_slug: str, actions: list, satellite_user: str,
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
        so a back-to-back session's warmup isn't blocked behind it."""
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

    async def _initial_workspace_sync(
        self, machine_id: str, agent_slug: str,
        *, target_username: str | None = None,
        target_role: str = "",
        session_username: str = "",
        session_knowledge_rw: bool = False,
        progress_cb=None,
        background: bool = False,
    ) -> None:
        """Versioned last-write-wins sync of the agent_dir with a satellite at
        session start.

        Runs a 3-way merge (platform vs satellite vs the per-machine ``base`` from
        ``sync_state``) so the NEWER version of each file wins — never
        proxy-always-wins. Deletes propagate only via tombstones; a genuine
        cross-user concurrent conflict captures the loser to the recover-bin and
        notifies them; everything else converges silently. See
        ``core/remote/file_sync.py``.

        Serialized per-(machine, agent) against concurrent warmups, and per-file
        against live write-backs via the global path lock. ``target_username`` /
        ``target_role`` carry per-user/role isolation (resolved by the caller).
        ``session_username`` is the SESSION's authenticated human (SecurityContext
        slug, ``""`` for service sessions) — the write-back identity on
        admin-shared machines, where ``target_username`` is ``None`` by design
        (it is the machine-pairing isolation filter, not a person). Without it,
        the owner-tier config/ write-back could never fire on the highest-trust
        target, and the merge scrubbed satellite-created config files that the
        live-path ``file_changed`` applier (which reads the SecurityContext)
        would have accepted.
        ``session_knowledge_rw`` is the session's manager-provenance flag
        (``SecurityContext.knowledge_rw``) — threaded into the merge's pull
        gate so session-start reconciliation and the per-turn applier agree
        on knowledge/ writes from agent-scope task fires (agent-scope tasks
        DO run on admin-paired satellites, where this gate is the ONLY
        boundary — no bwrap there). Machine-pairing merges pass False.
        ``background`` marks a merge nobody is waiting on (the reconnect walk,
        the idle sweep): its manifest and diff take the one-slot background
        gate before the sync-cpu pool, so a person's merge never queues
        behind a wave of walks.
        Best-effort: any error is caught by the caller and the session proceeds.
        """
        import asyncio as _asyncio

        import config as _cfg

        try:
            agent_dir = _cfg.get_agent_dir(agent_slug)
        except ValueError:
            logger.warning("workspace sync skipped: %r names no agent folder", agent_slug)
            return
        if not agent_dir.exists():
            return

        # Shared-only agents have NO per-user scope at all — their users/
        # subtree (stray dirs from older installs at most) never syncs in
        # either direction, to ANY machine class.
        from core.session.visibility import is_shared_only
        exclude_users = await _asyncio.to_thread(is_shared_only, agent_slug)

        # One warmup per (machine, agent) at a time — concurrent warmups would
        # double-apply and race the base.
        cpu_gate = _background_manifest_gate() if background else contextlib.nullcontext()
        async with self._cm.get_sync_lock(machine_id, agent_slug):
            run = await self._sync_inputs(
                machine_id, agent_slug, agent_dir, exclude_users, cpu_gate,
                target_username=target_username, target_role=target_role,
                session_username=session_username,
                session_knowledge_rw=session_knowledge_rw, progress_cb=progress_cb,
            )
            if run is None:
                return
            deferred_pulls = await self._sync_apply(run)

        # Kick the deferred large pulls in the background now that the
        # per-(machine, agent) sync lock is released (so a back-to-back warmup
        # isn't blocked behind them). Fire-and-forget; the task self-removes from
        # the tracking set on completion.
        if deferred_pulls:
            logger.info(
                "Initial workspace sync for %s: deferring %d large pull(s) to "
                "background", agent_slug, len(deferred_pulls),
            )
            task = _asyncio.create_task(
                self._run_deferred_pulls(
                    machine_id, agent_slug, deferred_pulls, run.satellite_user,
                )
            )
            self._deferred_sync_tasks.add(task)
            task.add_done_callback(self._deferred_sync_tasks.discard)

    async def _sync_inputs(
        self, machine_id: str, agent_slug: str, agent_dir: Path, exclude_users: bool,
        cpu_gate: contextlib.AbstractAsyncContextManager, *,
        target_username: str | None, target_role: str, session_username: str,
        session_knowledge_rw: bool,
        progress_cb: Callable[[float, int], Awaitable[None]] | None,
    ) -> _SyncRun | None:
        """The merge's inputs, under the sync lock: both manifests, the base,
        the tombstones and holds, the plan with its stale-hold pass; None when
        the satellite's manifest did not come or the trees are in sync."""
        import asyncio as _asyncio

        from core.remote import file_sync, remote_file_flow
        from storage.files import (
            file_author_store,
            file_tombstones_store,
            sync_state_store,
        )

        async with cpu_gate:
            local_entries = await _run_sync_cpu(
                file_sync.compute_manifest,
                agent_dir,
                target_username=target_username, target_role=target_role,
                exclude_user_dirs=exclude_users,
                # Cap the plan at what THIS machine accepts: pre-0.5.103
                # satellites hard-reject >100MB, so bigger files must never
                # enter their merge plan (doomed pushes / permanent churn).
                max_file_size=self._cm.effective_sync_cap(machine_id),
                # Marker-confirmed build-dir exclusions — ONLY for machines
                # whose satellite ACKed the table (0.5.110+): both walks
                # must apply the same rules or the diff churns.
                ignore_rules=self._cm.effective_ignore_rules(machine_id),
            )
        # What the manifest can be trusted with is the asked connection's
        # (a satellite updating mid-merge answers on a new one: a check
        # after the reply would judge this manifest by that version).
        report_skipped = self._cm.satellite_supports_skipped_manifest(machine_id)
        absence_unknown = self._cm.satellite_absence_unreliable(machine_id)
        restore_holds = self._cm.satellite_restores_holds(machine_id)
        try:
            ack = await self._cm.send_command(
                machine_id,
                manifest_request(self._cm, machine_id, agent_slug,
                                 report_skipped=report_skipped),
                timeout=30.0,
            )
        except RuntimeError as e:
            logger.warning(
                "request_manifest failed for %s: %s — skipping initial sync",
                agent_slug, e,
            )
            return None
        remote_entries = ack.get("files", []) or []
        if not report_skipped:
            # Only an asked satellite may answer an entry without a hash.
            remote_entries = [e for e in remote_entries
                              if not (isinstance(e, dict) and e.get("skipped"))]
        remote_size = {
            e.get("path", ""): int(e.get("size", 0) or 0)
            for e in remote_entries
        }
        # What a push may expect to replace on the machine: the
        # manifest's own stat of the file (its mtime travels as seconds,
        # the satellite allows the rounding), "live" for one it did not
        # list.
        remote_stat = {
            e.get("path", ""): {"size": int(e.get("size", 0) or 0),
                                "mtime_ns": int(round(float(e.get("mtime", 0) or 0) * 1e9))}
            for e in remote_entries if e.get("hash")
        }

        # Merge inputs: per-machine base, live tombstones, clock offset, and a
        # lazy platform-author resolver for cross-user conflict detection.
        base = await _asyncio.to_thread(
            sync_state_store.load_for_machine_agent, machine_id, agent_slug,
        )
        tombstones = await _asyncio.to_thread(
            file_tombstones_store.load_for_agent, agent_slug,
        )
        holds = await _asyncio.to_thread(
            file_tombstones_store.load_holds_for_agent, agent_slug,
        )
        clock_offset = self._cm.get_clock_offset(machine_id)
        satellite_user = target_username or ""  # the satellite-side writer (slug)

        def _author_of(p: str):
            return file_author_store.get(agent_slug, p)

        # config/ is push-only by default, but an OWNER-tier session
        # (manager/admin with a real username) curates config/ on its remote
        # machine — let those edits sync BACK, matching the live-path
        # can_write_back rule (config/ is STATIC, not regenerated per session,
        # so there is nothing to clobber). An empty set drops config/ from
        # push-only; .claude/.codex stay push-only (segments).
        #
        # The write-back identity is TWO-SOURCED: on a user-paired machine
        # it is the machine owner (target_username — sessions there are the
        # owner's); on an ADMIN-SHARED machine target_username is None BY
        # DESIGN (it's the isolation filter, not a person), so the identity
        # is the session's authenticated human (session_username). An
        # orphaned-owner machine (target_username == "") stays fail-closed —
        # no substitution.
        _cwb_username = (
            target_username if target_username is not None else session_username
        )
        push_only_dirs = (
            set()
            if (_roles.can_manage(target_role) and _cwb_username)
            else set(file_sync.DEFAULT_PUSH_ONLY_PREFIXES)
        )
        # Knowledge-library mirrors: read-only attachments are
        # platform-authoritative on every machine (push-only + scrubbed
        # everywhere — the projector owns mirror content); writable
        # attachments participate in the normal merge, with their
        # (source, subdir) pairs feeding can_write_back's segment-wise
        # subtree rule. Prefixes are PER LIBRARY subtree — the trailing
        # slash keeps the prefix match segment-safe (an RO library
        # ``marketing`` never covers ``marketing-extra``).
        from storage.knowledge import db_knowledge_libraries
        _attachments = await _asyncio.to_thread(
            db_knowledge_libraries.attachments_for_consumer, agent_slug,
        )
        _lib_ro = {
            (f"{layout.KNOWLEDGE}/shared/{a['source_agent']}/{a['subdir']}/"
             if a["subdir"] else f"{layout.KNOWLEDGE}/shared/{a['source_agent']}/")
            for a in _attachments if not a["writable"]
        }
        writable_libraries = frozenset(
            (a["source_agent"], a["subdir"] or "")
            for a in _attachments if a["writable"]
        )
        push_only_dirs |= _lib_ro
        async with cpu_gate:
            plan = await _run_sync_cpu(
                file_sync.diff_manifests,
                local_entries, remote_entries,
                base=base, tombstones=tombstones, clock_offset=clock_offset,
                author_of=_author_of, satellite_user=satellite_user,
                push_only_dirs=push_only_dirs,
                target_username=target_username, target_role=target_role,
                session_username=session_username,
                exclude_user_dirs=exclude_users,
                writable_libraries=writable_libraries,
                scrub_dirs=_lib_ro,
                knowledge_rw=session_knowledge_rw,
                absence_unknown=absence_unknown,
                holds=holds,
                restore_holds=restore_holds,
            )

        # A pull that settles a save the machine refused as stale in the
        # machine's favour keeps the save in the recover bin first,
        # whatever the merge's own rule says for the path (a person's own
        # file, a same-user divergence): the save was explicit.
        for action in plan.actions:
            if (action.op == "pull" and not action.capture_side
                    and remote_file_flow.stale_hold(machine_id, agent_slug, action.rel_path)):
                action.capture_side = "platform"
                action.capture_reason = "conflict"
                action.notify_user = action.notify_user or _author_of(action.rel_path)

        # Sync-delta telemetry (alert-only, never blocks or filters the
        # sync): a huge planned pull of NEW satellite files usually means
        # a build tree the sync-ignore table doesn't cover yet.
        try:
            from core.remote import sync_delta_alerts
            _new_pulls = [a for a in plan.actions
                          if a.op == "pull" and a.rel_path not in base]
            if _new_pulls:
                _subs: dict[str, int] = {}
                _nbytes = 0
                for a in _new_pulls:
                    _subs[sync_delta_alerts._top_subtree(a.rel_path)] = (
                        _subs.get(
                            sync_delta_alerts._top_subtree(a.rel_path), 0) + 1)
                    _nbytes += remote_size.get(a.rel_path, 0)
                await sync_delta_alerts.maybe_alert(
                    machine_id, agent_slug, source="reconcile",
                    new_files=len(_new_pulls), new_bytes=_nbytes,
                    subtree_counts=_subs)
        except ImportError:
            logger.debug("sync-delta telemetry failed", exc_info=True)

        if not plan.actions and not plan.to_scrub:
            logger.debug("Initial workspace sync for %s: in-sync", agent_slug)
            return None

        return _SyncRun(
            machine_id=machine_id, agent_slug=agent_slug, agent_dir=agent_dir,
            progress_cb=progress_cb, plan=plan, local_entries=local_entries,
            remote_size=remote_size, remote_stat=remote_stat,
            satellite_user=satellite_user,
        )

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
        for rp in run.plan.to_scrub:
            try:
                await self._cm.send_fire_and_forget(
                    run.machine_id,
                    {"type": "file_push", "agent_slug": run.agent_slug,
                     "action": "delete", "path": rp},
                )
                run.n["scrub"] += 1
            except Exception:
                logger.exception("isolation scrub delete failed for %s", rp)

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
        """The push op; False: a push never changes the platform tree."""
        import asyncio as _asyncio

        from core.remote import file_sync, remote_file_flow
        from services.path_policy_v2 import PathRef
        from storage.files import file_tombstones_store, sync_state_store

        captured = None
        if action.capture_side:
            captured = await self._sync_capture_loser(run, action)
        # Pass the PATH — push_file streams from disk (memory
        # O(chunk) even at 1GB; a vanished file returns False
        # into the existing failure path). It runs while the
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
            ok = await self._cm.push_file(
                run.machine_id, PathRef("agent_tree", rp),
                run.agent_dir / rp,
                agent_slug=run.agent_slug, progress_cb=_bytes,
                content_hash=action.base_hash or None,
                expect=run.remote_stat.get(rp, "live"), stale_out=stale,
            )
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

        if action.capture_side:
            await self._sync_capture_loser(run, action)
        # The named path, never resolved: the pull opens it
        # by components and refuses a link at any one.
        if not file_sync.is_canonical_rel_path(rp):
            logger.warning("initial-sync pull traversal blocked: %s", rp)
            return False
        dest = path_confinement.join_under(run.agent_dir, rp)
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

    async def _sync_delete_satellite(self, run: _SyncRun, action, rp: str) -> bool:
        """The delete_satellite op; False: the platform tree is unchanged."""
        import asyncio as _asyncio

        from storage.files import recover_bin_store, sync_state_store

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
            recover_bin_store.capture, run.agent_slug, rp, data, "deleted",
        )
        if entry is not None:
            run.n["capture"] += 1
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
        """Notify each user whose parallel edit lost a cross-user conflict — their
        version is recoverable from the workspace Recover bin. Coalesced per user.
        The notification deep-links to the Recover button (no download)."""
        import asyncio as _asyncio
        from collections import defaultdict

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
            count = len(names)
            if count == 1:
                title, body = (
                    "Recover your file",
                    f"Your version of “{names[0]}” was replaced by a newer edit "
                    f"from another user, but is recoverable.",
                )
            else:
                preview = ", ".join(names[:3])
                more = f" +{count - 3} more" if count > 3 else ""
                title, body = (
                    "Recover your files",
                    f"{count} of your files were replaced by newer edits from "
                    f"other users, but are recoverable: {preview}{more}.",
                )
            try:
                await notification_manager.fire_notification(
                    title=title, body=body, severity="info",
                    scope="user", target=sub,
                    source="file_conflict", agent_slug=agent_slug,
                )
            except Exception:
                logger.exception("conflict notify failed for %s", slug)
