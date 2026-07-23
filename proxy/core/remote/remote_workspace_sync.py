"""Workspace sync between the platform and a satellite for remote agents (mixin).

On reconnect / session start the platform and satellite reconcile their copies of
an agent's workspace (push/pull/merge with versioned-sync conflict handling),
with large conflict-free pulls deferred to the background. Mixed into
RemoteExecutionLayer; split out of remote_execution.py.
"""

import contextlib
import logging
import uuid

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



class RemoteWorkspaceSyncMixin:
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
        from storage import remote_store as _rs, database as _db

        machine = await _asyncio.to_thread(_rs.get_remote_machine, machine_id)
        if not machine:
            return None
        owner_sub = machine.get("registered_by", "") or ""
        is_admin_paired = machine.get("pairing_scope", "") == "admin"
        # Platform-admin check — ONLY consulted for admin-PAIRED machines
        # (admin-shared, role "admin"). User-paired machines always resolve
        # the owner's per-agent role, platform admins included (see docstring).
        owner_is_admin = is_admin_paired and bool(owner_sub) and (
            (await _asyncio.to_thread(_db.get_user, owner_sub)) or {}
        ).get("role") == "admin"
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
        serializes each agent against a concurrent session warmup. Best-effort.
        """
        import asyncio as _asyncio
        from storage import sync_state_store

        agents = await _asyncio.to_thread(sync_state_store.agents_for_machine, machine_id)
        if not agents:
            return

        synced = 0
        for agent_slug in sorted(agents):
            ident = await self.resolve_machine_sync_identity(machine_id, agent_slug)
            if ident is None:
                continue  # machine unpaired / vanished
            target_username, target_role = ident
            try:
                await self._initial_workspace_sync(
                    machine_id, agent_slug,
                    target_username=target_username, target_role=target_role,
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
        from storage import sync_state_store

        cm = self._cm
        for machine_id in list(cm.get_connected_machines()):
            conn = cm.get_connection(machine_id)
            if conn is None or not conn.agent_fingerprints:
                continue
            try:
                tracked = await _asyncio.to_thread(
                    sync_state_store.agents_for_machine, machine_id,
                )
            except Exception:
                continue
            for slug, fp in list(conn.agent_fingerprints.items()):
                if slug not in tracked:
                    continue  # not a proxy-tracked agent on this machine
                if conn.synced_fingerprints.get(slug) == fp:
                    continue  # unchanged since the last completed sync
                if machine_id in workspace_fanout._active_machine_ids(slug):
                    continue  # an active session covers it via the per-turn path
                task = _asyncio.create_task(
                    self._idle_fingerprint_sync_one(machine_id, slug, fp)
                )
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
            await self._initial_workspace_sync(
                machine_id, agent_slug,
                target_username=target_username, target_role=target_role,
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
        ``_initial_workspace_sync._apply`` MINUS capture/notify (deferred pulls are
        ``capture_side is None`` by construction): per file, under the global
        per-(agent, rel_path) lock, stream the satellite's bytes to the platform
        agent dir, then advance base/file_author + drop any tombstone, and refresh
        an open dashboard view. Best-effort — a failure (incl. a WS drop →
        ``pull_file_to_path`` returns False) is logged and reconciles at the next
        warmup / reconnect-sync. Does NOT hold the per-(machine, agent) warmup lock,
        so a back-to-back session's warmup isn't blocked behind it."""
        import asyncio as _asyncio
        import config as _cfg
        from core.remote import remote_file_flow
        from services.notifications import notification_manager
        from services.path_policy_v2 import PathRef
        from storage import (
            file_author_store, file_tombstones_store, sync_state_store,
        )

        agent_dir = _cfg.AGENTS_DIR / agent_slug
        pulled = 0
        for action in actions:
            rp = action.rel_path
            try:
                lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rp)
                async with lock:
                    dest = (agent_dir / rp).resolve()
                    try:
                        dest.relative_to(agent_dir.resolve())
                    except ValueError:
                        logger.warning("deferred-sync pull traversal blocked: %s", rp)
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
        progress_cb=None,
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
        Best-effort: any error is caught by the caller and the session proceeds.
        """
        import asyncio as _asyncio
        import config as _cfg
        from core.remote import file_sync, remote_file_flow
        from services.path_policy_v2 import PathRef
        from services.notifications import notification_manager
        from storage import (
            sync_state_store, file_tombstones_store, file_author_store,
            recover_bin_store,
        )

        agent_dir = _cfg.AGENTS_DIR / agent_slug
        if not agent_dir.exists():
            return

        # Shared-only agents have NO per-user scope at all — their users/
        # subtree (stray dirs from older installs at most) never syncs in
        # either direction, to ANY machine class.
        from core.session.visibility import is_shared_only
        exclude_users = await _asyncio.to_thread(is_shared_only, agent_slug)

        # One warmup per (machine, agent) at a time — concurrent warmups would
        # double-apply and race the base.
        async with self._cm.get_sync_lock(machine_id, agent_slug):
            local_entries = await _asyncio.to_thread(
                file_sync.compute_manifest, agent_dir,
                target_username=target_username, target_role=target_role,
                exclude_user_dirs=exclude_users,
            )
            try:
                ack = await self._cm.send_command(
                    machine_id,
                    {"type": "request_manifest", "agent_slug": agent_slug},
                    timeout=30.0,
                )
            except Exception as e:
                logger.warning(
                    "request_manifest failed for %s: %s — skipping initial sync",
                    agent_slug, e,
                )
                return
            remote_entries = ack.get("files", []) or []
            remote_size = {
                e.get("path", ""): int(e.get("size", 0) or 0)
                for e in remote_entries
            }

            # Merge inputs: per-machine base, live tombstones, clock offset, and a
            # lazy platform-author resolver for cross-user conflict detection.
            base = await _asyncio.to_thread(
                sync_state_store.load_for_machine_agent, machine_id, agent_slug,
            )
            tombstones = await _asyncio.to_thread(
                file_tombstones_store.load_for_agent, agent_slug,
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
                if (target_role in ("manager", "admin") and _cwb_username)
                else set(file_sync.DEFAULT_PUSH_ONLY_PREFIXES)
            )
            plan = await _asyncio.to_thread(
                file_sync.diff_manifests,
                local_entries, remote_entries,
                base=base, tombstones=tombstones, clock_offset=clock_offset,
                author_of=_author_of, satellite_user=satellite_user,
                push_only_dirs=push_only_dirs,
                target_username=target_username, target_role=target_role,
                session_username=session_username,
                exclude_user_dirs=exclude_users,
            )

            if not plan.actions and not plan.to_scrub:
                logger.debug("Initial workspace sync for %s: in-sync", agent_slug)
                return

            # Defer large, conflict-free PULLS to a background
            # task so the CLI starts without waiting for satellite-side bulk content
            # (e.g. a big media file the satellite holds). Pulls never touch the
            # agent's disk → safe to defer. Everything else — ALL pushes, deletes,
            # scrubs, noops, conflict-capture pulls, and small pulls — stays
            # foreground (awaited before start_session).
            foreground_actions, deferred_pulls = _partition_deferred_pulls(
                plan.actions, remote_size,
            )

            local_mtime = {e.path: e.mtime for e in local_entries}
            # Created EAGERLY (not lazily on first capture): the windowed
            # apply below runs actions concurrently, and a lazy nonlocal
            # create could double-mkdtemp and leak the loser.
            import tempfile as _tempfile
            from pathlib import Path as _Path
            recover_tmp = _Path(_tempfile.mkdtemp(prefix="oto-recover-"))
            to_notify: list[tuple[str, str]] = []
            n = {"push": 0, "pull": 0, "delete": 0, "scrub": 0, "capture": 0,
                 "done": 0}

            # W2 (sync-performance): throttled progress ticks → progress_cb
            # (the warmup wires it to the dashboard install bar; background
            # sweeps pass None). Always fire the FINAL tick so the bar lands
            # on 100% even under throttle.
            import time as _time_mod
            _total = len(foreground_actions)
            _last_tick = {"t": 0.0}

            async def _progress_tick(final: bool = False) -> None:
                if progress_cb is None or _total == 0:
                    return
                now = _time_mod.monotonic()
                if not final and now - _last_tick["t"] < 0.5:
                    return
                _last_tick["t"] = now
                try:
                    await progress_cb(n["done"], _total)
                except Exception:
                    logger.debug("sync progress_cb failed", exc_info=True)

            async def _pull_satellite_bytes(rp: str) -> bytes | None:
                """Pull the satellite's current copy of ``rp`` → bytes, or None on a
                pull/read failure."""
                dest = recover_tmp / uuid.uuid4().hex
                ok = await self._cm.pull_file_to_path(
                    machine_id, PathRef("agent_tree", rp), dest, agent_slug=agent_slug,
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

            def _read_platform_bytes(rp: str) -> bytes | None:
                try:
                    base_dir = agent_dir.resolve()
                    dest = (agent_dir / rp).resolve()
                    dest.relative_to(base_dir)
                    return dest.read_bytes()
                except (OSError, ValueError):
                    return None

            async def _capture_loser(action) -> None:
                """Copy the LOSING side's current bytes to the recover-bin (a
                cross-user conflict). Best-effort — the op proceeds regardless."""
                if action.capture_side == "satellite":
                    data = await _pull_satellite_bytes(action.rel_path)
                elif action.capture_side == "platform":
                    data = await _asyncio.to_thread(_read_platform_bytes, action.rel_path)
                else:
                    return
                if data is None:
                    return
                entry = await _asyncio.to_thread(
                    recover_bin_store.capture, agent_slug, action.rel_path,
                    data, action.capture_reason,
                )
                if entry is not None:
                    n["capture"] += 1
                    if action.notify_user:
                        to_notify.append((action.notify_user, entry["original_name"]))

            async def _apply(action) -> None:
                rp = action.rel_path
                # Only a pull ADDS/UPDATES a file on the PLATFORM tree, so only a
                # pull refreshes an open dashboard (set in the pull branch below).
                pulled_to_platform = False
                # Serialize each file against the live write-back path + other
                # warmups on the SAME file (cross-machine) via the global lock.
                lock = await remote_file_flow._acquire_global_path_lock(agent_slug, rp)
                async with lock:
                    if action.op == "push":
                        if action.capture_side:
                            await _capture_loser(action)
                        try:
                            content = (agent_dir / rp).read_bytes()
                        except OSError as e:
                            logger.warning("Cannot read %s for sync: %s", rp, e)
                            return
                        ok = await self._cm.push_file(
                            machine_id, PathRef("agent_tree", rp), content,
                            agent_slug=agent_slug,
                        )
                        if not ok:
                            logger.warning("Push failed during initial sync: %s", rp)
                            return
                        n["push"] += 1
                        if action.base_hash:
                            await _asyncio.to_thread(
                                sync_state_store.record_one, machine_id, agent_slug,
                                rp, action.base_hash, local_mtime.get(rp, 0.0),
                            )
                        if action.drop_tombstone:
                            await _asyncio.to_thread(file_tombstones_store.drop, agent_slug, rp)

                    elif action.op == "pull":
                        if action.capture_side:
                            await _capture_loser(action)
                        dest = (agent_dir / rp).resolve()
                        try:
                            dest.relative_to(agent_dir.resolve())
                        except ValueError:
                            logger.warning("initial-sync pull traversal blocked: %s", rp)
                            return
                        ok = await self._cm.pull_file_to_path(
                            machine_id, PathRef("agent_tree", rp), dest,
                            agent_slug=agent_slug,
                        )
                        if not ok:
                            logger.warning("Pull failed during initial sync: %s", rp)
                            return
                        n["pull"] += 1
                        pulled_to_platform = True
                        if action.base_hash:
                            try:
                                base_mtime = dest.stat().st_mtime
                            except OSError:
                                base_mtime = 0.0
                            await _asyncio.to_thread(
                                sync_state_store.record_one, machine_id, agent_slug,
                                rp, action.base_hash, base_mtime,
                            )
                            # The platform now holds the satellite user's bytes.
                            if satellite_user:
                                await _asyncio.to_thread(
                                    file_author_store.record, agent_slug, rp, satellite_user,
                                )
                        if action.drop_tombstone:
                            await _asyncio.to_thread(file_tombstones_store.drop, agent_slug, rp)

                    elif action.op == "delete_satellite":
                        # Capture the satellite's only copy BEFORE deleting. A pull
                        # failure means we do NOT delete (never cause unrecoverable
                        # loss from a transient blip) — the tombstone persists and
                        # we retry next sync.
                        data = await _pull_satellite_bytes(rp)
                        if data is None:
                            logger.warning(
                                "delete-sync: pull failed for %s/%s — NOT deleting",
                                agent_slug, rp,
                            )
                            return
                        entry = await _asyncio.to_thread(
                            recover_bin_store.capture, agent_slug, rp, data, "deleted",
                        )
                        if entry is not None:
                            n["capture"] += 1
                        try:
                            await self._cm.send_fire_and_forget(
                                machine_id,
                                {"type": "file_push", "agent_slug": agent_slug,
                                 "action": "delete", "path": rp},
                            )
                            n["delete"] += 1
                        except Exception:
                            logger.exception("delete-sync failed for %s (backup kept)", rp)
                        if action.clear_base:
                            await _asyncio.to_thread(
                                sync_state_store.clear_one, machine_id, agent_slug, rp,
                            )

                    elif action.op == "delete_platform":
                        # The satellite DELETED a file it had converged on, out-of-turn,
                        # with its tree still alive (delete-attribution). Capture
                        # the platform bytes to the recover-bin (7-day undo) FIRST, then
                        # delete the platform copy, tombstone it (so OTHER idle satellites
                        # drop it too), clear base+author, and fan the delete out. Under
                        # the global per-(agent, path) lock already held here.
                        if action.capture_side:
                            await _capture_loser(action)
                        dest = (agent_dir / rp).resolve()
                        try:
                            dest.relative_to(agent_dir.resolve())
                        except ValueError:
                            logger.warning("delete_platform traversal blocked: %s", rp)
                            return
                        try:
                            if dest.is_file():
                                dest.unlink()
                        except OSError as e:
                            logger.warning("delete_platform unlink failed for %s: %s", rp, e)
                            return
                        from core.remote import file_sync as _fs
                        _fs.prune_empty_parents(dest, agent_dir)
                        n["delete"] += 1
                        import time as _t
                        await _asyncio.to_thread(
                            file_tombstones_store.record, agent_slug, rp,
                            _t.time(), origin="satellite-idle-delete",
                        )
                        if action.clear_base:
                            await _asyncio.to_thread(
                                sync_state_store.clear_one, machine_id, agent_slug, rp,
                            )
                        await _asyncio.to_thread(
                            file_author_store.clear, agent_slug, rp,
                        )
                        from services.remote import workspace_fanout as _wf
                        await _wf.fan_out_delete(
                            agent_slug, rp, exclude_machine_id=machine_id,
                        )
                        pulled_to_platform = True  # platform tree changed → dashboard refresh

                    elif action.op == "noop":
                        if action.base_hash:
                            await _asyncio.to_thread(
                                sync_state_store.record_one, machine_id, agent_slug,
                                rp, action.base_hash, local_mtime.get(rp, 0.0),
                            )
                        if action.clear_base:
                            await _asyncio.to_thread(
                                sync_state_store.clear_one, machine_id, agent_slug, rp,
                            )
                        if action.drop_tombstone:
                            await _asyncio.to_thread(file_tombstones_store.drop, agent_slug, rp)

                if pulled_to_platform:
                    # A pull (satellite file landed) OR a delete_platform (satellite
                    # out-of-turn delete propagated) just changed the platform tree →
                    # refresh any open dashboard workspace view,
                    # best-effort, outside the lock (it doesn't need it). push
                    # (platform→satellite) + delete_satellite (platform already
                    # lacks the file) don't change the platform tree, so they
                    # don't notify the dashboard.
                    await notification_manager.broadcast_file_updated(
                        agent_slug, rp, source="disk",
                    )

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
            _window = _asyncio.Semaphore(8)

            async def _apply_windowed(action) -> None:
                async with _window:
                    try:
                        await _apply(action)
                    except Exception:
                        logger.exception(
                            "initial-sync action failed for %s/%s",
                            agent_slug, action.rel_path,
                        )
                    finally:
                        n["done"] += 1
                        await _progress_tick()

            await _asyncio.gather(
                *(_apply_windowed(a) for a in foreground_actions)
            )
            await _progress_tick(final=True)

            # Isolation scrubs (another user's data / agent-scope creds that leaked
            # onto the satellite) — delete there, never captured, never base-tracked.
            for rp in plan.to_scrub:
                try:
                    await self._cm.send_fire_and_forget(
                        machine_id,
                        {"type": "file_push", "agent_slug": agent_slug,
                         "action": "delete", "path": rp},
                    )
                    n["scrub"] += 1
                except Exception:
                    logger.exception("isolation scrub delete failed for %s", rp)

            if recover_tmp is not None:
                import shutil as _shutil
                _shutil.rmtree(recover_tmp, ignore_errors=True)

            logger.info(
                "Initial workspace sync for %s: pushed=%d pulled=%d deleted=%d "
                "scrubbed=%d captured=%d",
                agent_slug, n["push"], n["pull"], n["delete"], n["scrub"], n["capture"],
            )

            if to_notify:
                await self._notify_conflict_losers(agent_slug, to_notify)

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
                    machine_id, agent_slug, deferred_pulls, satellite_user,
                )
            )
            self._deferred_sync_tasks.add(task)
            task.add_done_callback(self._deferred_sync_tasks.discard)

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
