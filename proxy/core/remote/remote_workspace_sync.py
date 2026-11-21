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
import random
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from auth import roles as _roles
from core import layout, placement
from core.remote.remote_workspace_apply import (  # noqa: F401
    _DEFER_PULL_MIN_BYTES,
    RemoteWorkspaceApplyMixin,
    _keep_mtime,
    _partition_deferred_pulls,
    _SyncRun,
)

logger = logging.getLogger("remote-layer")


class _NoManifest(Exception):
    """The machine's manifest did not come: no merge ran."""



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
# fleet (its slot is lent, ``_background_slot``, so a merge that waits on
# its machine does not keep it); the background manifest gate keeps their manifest and diff work
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


# A background merge borrows its fleet slot: one that still runs after the
# lend (a machine that answers slowly or not at all, a large catch-up) goes
# on without the slot, so the machines behind it are served. At most
# ``_UNSLOTTED_MERGES`` run so: when those places are taken a long merge
# keeps its slot until one frees. Worst case, 4 + 12 background merges at
# once, and that many machines that do not answer to stop the others'
# catch-up.
_GATE_LEND_S = 20.0
_UNSLOTTED_MERGES = 12
_unslotted = [0]


@contextlib.asynccontextmanager
async def _background_slot():
    """The reconnect gate's slot for one background merge, lent for
    ``_GATE_LEND_S``."""
    gate = _reconnect_gate()
    await gate.acquire()
    loop = asyncio.get_running_loop()
    state = {"slot": True, "unslotted": False, "timer": None}

    def _lapse() -> None:
        if not state["slot"]:
            return
        if _unslotted[0] >= _UNSLOTTED_MERGES:
            state["timer"] = loop.call_later(_GATE_LEND_S / 4, _lapse)
            return
        state["slot"], state["unslotted"] = False, True
        _unslotted[0] += 1
        gate.release()

    state["timer"] = loop.call_later(_GATE_LEND_S, _lapse)
    try:
        yield
    finally:
        timer = state["timer"]
        if timer is not None:
            timer.cancel()
        if state["slot"]:
            state["slot"] = False
            gate.release()
        if state["unslotted"]:
            _unslotted[0] -= 1


# The page a satellite is asked for (entries per frame, and the satellite
# also caps a frame's text), so a large tree's manifest never needs a frame
# past the socket's cap.
_MANIFEST_PAGE_SIZE = 4096


def manifest_request(agent_slug: str, *, report_skipped: bool = False) -> dict:
    """The paged ``request_manifest`` frame. ``report_skipped`` asks the
    satellite to list what it could not read as entries with ``skipped``
    and no hash."""
    msg: dict[str, Any] = {"type": "request_manifest", "agent_slug": agent_slug,
                           "page_size": _MANIFEST_PAGE_SIZE}
    if report_skipped:
        msg["report_skipped"] = True
    return msg


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


class RemoteWorkspaceSyncMixin(RemoteWorkspaceApplyMixin):
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
        if not machine or placement.machine_is_compute(machine):
            # A compute machine works on the platform's own folder: nothing
            # syncs to it.
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

    async def sync_agent_in_background(self, machine_id: str, agent_slug: str) -> bool:
        """One background merge of ``agent_slug`` with ``machine_id``, as the
        reconnect walk runs it for each agent: the merge a machine is owed
        when it was handed more file names than its lane keeps
        (``core/remote/machine_push_lane``). The identity comes from the
        machine's pairing. A machine that is not connected is left to its
        reconnect walk. False when the merge is still owed: the machine is
        away or no manifest came."""
        if not self._cm.is_connected(machine_id):
            return False
        ident = await self.resolve_machine_sync_identity(machine_id, agent_slug)
        if ident is None:
            return True
        target_username, target_role = ident
        async with _background_slot():
            return await self._initial_workspace_sync(
                machine_id, agent_slug,
                target_username=target_username, target_role=target_role,
                background=True,
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
                async with _background_slot():
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
            async with _background_slot():
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

    async def _initial_workspace_sync(
        self, machine_id: str, agent_slug: str,
        *, target_username: str | None = None,
        target_role: str = "",
        session_username: str = "",
        session_knowledge_rw: bool = False,
        progress_cb=None,
        background: bool = False,
        shared_by_many: bool = False,
    ) -> bool:
        """Versioned last-write-wins sync of the agent_dir with a satellite at
        session start. True when it finished, trees in sync included; False when
        it did not run because the machine's manifest did not come.

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
        ``shared_by_many`` marks a compute machine with its own copy, which
        many people's sessions share: ``target_username`` is the session's
        person and the other people's folders there are left to their own
        merges (``file_sync.diff_manifests``).
        Best-effort: any error is caught by the caller and the session proceeds.
        """
        import asyncio as _asyncio

        import config as _cfg

        try:
            agent_dir = _cfg.get_agent_dir(agent_slug)
        except ValueError:
            logger.warning("workspace sync skipped: %r names no agent folder", agent_slug)
            return True
        if not agent_dir.exists():
            return True

        # Shared-only agents have NO per-user scope at all — their users/
        # subtree (stray dirs from older installs at most) never syncs in
        # either direction, to ANY machine class.
        from core.session.visibility import is_shared_only
        exclude_users = await _asyncio.to_thread(is_shared_only, agent_slug)

        # One warmup per (machine, agent) at a time — concurrent warmups would
        # double-apply and race the base.
        cpu_gate = _background_manifest_gate() if background else contextlib.nullcontext()
        async with self._cm.get_sync_lock(machine_id, agent_slug):
            try:
                run = await self._sync_inputs(
                    machine_id, agent_slug, agent_dir, exclude_users, cpu_gate,
                    target_username=target_username, target_role=target_role,
                    session_username=session_username,
                    session_knowledge_rw=session_knowledge_rw, progress_cb=progress_cb,
                    shared_by_many=shared_by_many,
                )
            except _NoManifest:
                return False
            deferred_pulls = await self._sync_apply(run) if run is not None else []
            from core.remote import remote_file_flow
            remote_file_flow.note_merged(machine_id, agent_slug)
        if run is None:
            return True

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
                    {a.rel_path: run.planned_stat(a.rel_path) for a in deferred_pulls},
                )
            )
            self._deferred_sync_tasks.add(task)
            task.add_done_callback(self._deferred_sync_tasks.discard)
        return True

    async def _sync_inputs(
        self, machine_id: str, agent_slug: str, agent_dir: Path, exclude_users: bool,
        cpu_gate: contextlib.AbstractAsyncContextManager, *,
        target_username: str | None, target_role: str, session_username: str,
        session_knowledge_rw: bool,
        progress_cb: Callable[[float, int], Awaitable[None]] | None,
        shared_by_many: bool = False,
    ) -> _SyncRun | None:
        """The merge's inputs, under the sync lock: both manifests, the base,
        the tombstones and holds, the plan with its stale-hold pass; None when
        the trees are in sync. ``_NoManifest`` when the satellite's manifest did
        not come."""
        import asyncio as _asyncio

        import config as app_config
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
                # Marker-confirmed build-dir exclusions, the table the
                # satellite applies to its own walk: both walks must apply
                # the same rules or the diff churns.
                ignore_rules=file_sync.validate_ignore_rules(app_config.SYNC_IGNORE_RULES),
            )
        try:
            ack = await self._cm.send_command(
                machine_id,
                manifest_request(agent_slug, report_skipped=True),
                timeout=30.0,
            )
        except RuntimeError as e:
            logger.warning(
                "request_manifest failed for %s: %s — skipping initial sync",
                agent_slug, e,
            )
            raise _NoManifest from e
        remote_entries = ack.get("files", []) or []
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
                holds=holds,
                restore_holds=self._cm.satellite_restores_holds(machine_id),
                shared_by_many=shared_by_many,
            )

        remote_file_flow.note_merge_left(machine_id, agent_slug, plan.left_unread)

        # A pull that settles a save the machine refused as stale in the
        # machine's favour keeps the save in the recover bin first,
        # whatever the merge's own rule says for the path (a person's own
        # file, a same-user divergence): the save was explicit. A save the
        # machine has held since is no longer one (``stale_hold``).
        platform_hash: dict[str, str] = {}
        for action in plan.actions:
            if action.op != "pull" or action.capture_side:
                continue
            if not platform_hash:
                platform_hash = {e.path: e.hash for e in local_entries}
            if remote_file_flow.stale_hold(
                    machine_id, agent_slug, action.rel_path,
                    platform_hash=platform_hash.get(action.rel_path, ""),
                    base_hash=(base.get(action.rel_path) or (None,))[0]):
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
