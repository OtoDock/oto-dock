"""Active-session workspace fan-out — propagate a workspace write/delete to the
OTHER satellites currently running the same collaborative agent.

Background
----------
When users A and B run sessions for the same agent on DIFFERENT satellites, an
edit on A's machine only reached B's machine at B's next session start
(``core/remote/remote_execution.py::_initial_workspace_sync``). This module closes that
gap: every authorized platform-side workspace write — the per-turn ``file_changed``
applier (``core/remote/satellite_connection.py``), dashboard file-API edits + uploads,
Collabora saves (``api/media/wopi.py``) and file-tools writes (``push_back`` +
``hook_file_written``) — fans the new bytes out to every OTHER active session's
machine within the turn. ``propagate_write`` (below) is the shared "publish bytes
to the agent tree + fan out, atomically under the global lock" entry point for the
write paths whose bytes are produced OUTSIDE the ``file_changed`` applier.

Isolation
---------
Per-user / per-role isolation is enforced per target session via
``core/remote/file_sync.py::should_sync_to_target`` — the SAME push-direction predicate
``compute_manifest`` applies at session start. A user-paired (or agent-scope)
session only receives ``users/{own}`` + shared paths, never another user's data;
a non-owner session never receives ``config/``. This is exactly what makes routing
the dashboard / upload push helpers through the fan-out **fix** the historical
leak where they pushed to every machine of the agent unconditionally.

Source exclusion
----------------
``exclude_machine_id`` skips the originating satellite (it already has the bytes).
Pass ``None`` for platform-origin writes (dashboard / upload) — they have no source
machine, so the file goes to every *allowed* active machine.

Local sessions are a no-op — their workspace is a bwrap bind-mount of the platform
dir (same inode), so there is nothing to push and they never appear in the remote
layer's session registry.

Downstream of the write-back guard
-----------------------------------
The per-turn caller (``satellite_connection._apply_file_changed``) only invokes the
fan-out AFTER ``can_write_back`` authorizes the write — so the fan-out only ever
sees already-authorized paths and never has to re-filter ``.claude`` / ``.codex``
machinery. The push helpers (dashboard / upload) are likewise gated upstream by the
file-API role checks.

All functions are best-effort: a push failure to one machine is logged and never
raises (the file reconciles at that machine's next session start).
"""

from __future__ import annotations

import asyncio
import logging
import contextlib

logger = logging.getLogger("claude-proxy.workspace-fanout")


def _interactive_remote_sessions(agent_slug: str) -> list[tuple[str, str, str, str]]:
    """``(session_id, machine_id, username, role)`` for alive REMOTE interactive
    (PTY) sessions of this agent. TUI sessions live in their own registry
    (``core/session/interactive_session``), not the remote layer's — without
    them a machine running only a terminal session looked IDLE to both the
    fan-out and the fingerprint sweep, so the sweep merged every 60 s against
    a tree the live session was actively mutating (and scrubbed files the
    session had just written), while live pushes skipped the machine entirely.
    Local PTY sessions (``target == "local"``) run on the platform tree itself
    — nothing to push, excluded like local headless sessions."""
    try:
        from core.session import interactive_session as _isess
    except Exception:
        return []
    out: list[tuple[str, str, str, str]] = []
    for sid, s in list(getattr(_isess, "_sessions", {}).items()):
        try:
            if (s.agent_name == agent_slug and s.alive
                    and (s.target or "local") != "local"):
                out.append((sid, s.target, s.username or "", s.role or ""))
        except Exception:
            continue
    return out


def fanout_targets(
    agent_slug: str, rel_path: str, *, exclude_machine_id: str | None = None,
) -> list[str]:
    """machine_ids of active remote sessions of ``agent_slug`` ALLOWED to receive
    ``rel_path`` (per-session isolation), with the source machine excluded and
    machines deduped.

    Public + cheap (in-memory registry scan, no I/O) so callers can use it as a
    gate before an expensive disk read (e.g. the ``file_changed`` applier only
    re-reads the file to fan out when there are targets), and so tests can assert
    the selection logic directly.

    A machine is a target if **any** of its active sessions for this agent passes
    ``should_sync_to_target`` — the file lands once on that machine's disk, and at
    least one session there may legitimately see it. This makes both trust classes
    correct without a machine-ownership DB lookup:
      * user-paired (one owner session) → only that owner's allowed paths;
      * admin-shared (many users' sessions) → pushed where any active user is allowed.

    Synchronous and registry-only (no I/O) so it is unit-testable in isolation.
    """
    # Shared-only agents have no per-user scope — users/ paths (stray dirs
    # from older installs at most) never fan out to any machine, mirroring
    # compute_manifest's exclude_user_dirs.
    if rel_path.startswith("users/"):
        from core.session.visibility import is_shared_only
        if is_shared_only(agent_slug):
            return []

    try:
        from core.session.session_manager import _get_remote_layer
        layer = _get_remote_layer()
    except Exception:
        return []
    sessions = getattr(layer, "_sessions", None) or {}

    from core.remote.file_sync import should_sync_to_target
    from core.session.session_state import get_session_security

    machines: set[str] = set()
    for sid, info in list(sessions.items()):
        if info.agent_name != agent_slug or not getattr(info, "alive", False):
            continue
        mid = info.machine_id
        if mid in machines:
            continue  # already cleared by another session on the same machine
        if exclude_machine_id is not None and mid == exclude_machine_id:
            continue
        sec = get_session_security(sid)
        if sec is None:
            # No authenticated context → fail-closed, don't push. The file
            # reconciles at that session's next start via _initial_workspace_sync.
            continue
        # Always pass a CONCRETE username (a real slug, or "" for agent-scope) —
        # never None, which would disable the per-user filter and could leak
        # another user's data onto a user-paired / agent-scope target.
        username = getattr(sec, "username", "") or ""
        role = getattr(sec, "role", "") or ""
        if should_sync_to_target(rel_path, username, role):
            machines.add(mid)
    # Remote interactive (PTY) sessions — same isolation predicate, identity
    # from the session registry (set from the spawn's SecurityContext).
    for _sid, mid, username, role in _interactive_remote_sessions(agent_slug):
        if mid in machines:
            continue
        if exclude_machine_id is not None and mid == exclude_machine_id:
            continue
        if should_sync_to_target(rel_path, username, role):
            machines.add(mid)
    return list(machines)


def _active_machine_ids(agent_slug: str) -> set[str]:
    """machine_ids with ANY alive session for ``agent_slug`` (no isolation filter).

    The set the connected-idle fan-out EXCLUDES — those machines already receive the
    per-active-session fan-out (and the live ``file_changed`` applier). In-memory, no
    I/O. "Idle" is PER AGENT: a machine running agent-X but not agent-Y is idle for
    agent-Y and a legitimate idle target for agent-Y's files.
    """
    try:
        from core.session.session_manager import _get_remote_layer
        layer = _get_remote_layer()
    except Exception:
        return set()
    sessions = getattr(layer, "_sessions", None) or {}
    out: set[str] = set()
    for info in list(sessions.values()):
        if getattr(info, "agent_name", None) == agent_slug and getattr(info, "alive", False):
            out.add(info.machine_id)
    # Remote interactive (PTY) sessions count as ACTIVE too: they have no
    # per-turn scan, but the fingerprint sweep treating their machine as idle
    # meant a merge every 60 s against a moving tree (see
    # _interactive_remote_sessions). Their write-back is the PTY periodic scan.
    for _sid, mid, _u, _r in _interactive_remote_sessions(agent_slug):
        out.add(mid)
    return out


def has_fanout_candidates(
    agent_slug: str, rel_path: str, *,
    include_idle: bool = False, exclude_machine_id: str | None = None,
) -> bool:
    """Cheap (in-memory, NO DB) gate: is there ANY machine that might receive this
    file — so a caller can skip an expensive disk read without DB I/O?

    True if an active session is allowed it (``fanout_targets``), or — when
    ``include_idle`` — if ANY other connected machine exists (an idle candidate; the
    precise pairing + isolation filter runs later in ``idle_connected_targets``).
    Over-approximates idle (a connected machine that doesn't hold the agent still
    says "yes" → at worst one wasted read, NEVER a wrong push).
    """
    if fanout_targets(agent_slug, rel_path, exclude_machine_id=exclude_machine_id):
        return True
    if not include_idle:
        return False
    try:
        from core.remote.satellite_connection import get_connection_manager
        cm = get_connection_manager()
    except Exception:
        return False
    active = _active_machine_ids(agent_slug)
    for mid in cm.get_connected_machines():
        if mid != exclude_machine_id and mid not in active:
            return True
    return False


async def idle_connected_targets(
    agent_slug: str, rel_path: str, *, exclude_machine_id: str | None = None,
) -> list[str]:
    """machine_ids of CONNECTED-but-IDLE machines (no active session for this agent)
    that ALREADY hold ``agent_slug`` and may receive ``rel_path`` under their PAIRING
    scope — admin-paired ⇒ admin-shared (the WHOLE agent folder, every user); user-
    paired ⇒ the owner's role-gated scope. Keeps a connected satellite current with
    dashboard edits WITHOUT a live session, so its next session start is light.

    Resolves each machine's ``(username, role)`` from its pairing via
    ``RemoteExecutionLayer.resolve_machine_sync_identity`` — DB I/O, hence async and
    kept OUT of the cheap ``has_fanout_candidates`` / ``fanout_targets`` gate. Only
    machines that ALREADY hold the agent (a converged ``sync_state`` base) are
    targeted — never seed a partial tree on a machine that has never run the agent
    (its first full sync happens at session start). Fully defensive: any error →
    ``[]`` (fall back to the active-only fan-out).
    """
    try:
        from core.remote.file_sync import should_sync_to_target
        from core.remote.satellite_connection import get_connection_manager
        from core.session.session_manager import _get_remote_layer
        from storage import sync_state_store

        # Same shared-only users/ exclusion as fanout_targets.
        if rel_path.startswith("users/"):
            from core.session.visibility import is_shared_only
            if await asyncio.to_thread(is_shared_only, agent_slug):
                return []

        cm = get_connection_manager()
        layer = _get_remote_layer()
        if layer is None:
            return []
        connected = cm.get_connected_machines()
        if not connected:
            return []
        active = _active_machine_ids(agent_slug)
        out: list[str] = []
        for mid in connected:
            if mid == exclude_machine_id or mid in active:
                continue  # excluded source, or already covered by the active fan-out
            agents = await asyncio.to_thread(sync_state_store.agents_for_machine, mid)
            if agent_slug not in agents:
                continue  # machine has never run this agent → don't seed a partial tree
            ident = await layer.resolve_machine_sync_identity(mid, agent_slug)
            if ident is None:
                continue
            target_username, target_role = ident
            if should_sync_to_target(rel_path, target_username, target_role):
                out.append(mid)
        return out
    except Exception:
        logger.debug(
            "idle_connected_targets failed for %s/%s", agent_slug, rel_path,
            exc_info=True,
        )
        return []


async def fan_out_write(
    agent_slug: str, rel_path: str, content: bytes, *,
    exclude_machine_id: str | None = None, include_idle: bool = False,
) -> None:
    """Push ``content`` for ``rel_path`` to every OTHER machine running ``agent_slug``
    that is allowed to receive it. Best-effort; never raises.

    Targets active-session machines (``fanout_targets``); when ``include_idle`` is set
    — platform-origin dashboard/upload writes — ALSO connected-but-idle machines that
    hold the agent (``idle_connected_targets``), so a dashboard edit reaches a
    connected satellite even with no live session and its next session start stays
    light. The caller supplies the current platform bytes (e.g. re-read post-apply) so
    large-file writes fan out the complete file. Pushes run concurrently; per-push
    failures are logged, not raised.
    """
    machines = fanout_targets(
        agent_slug, rel_path, exclude_machine_id=exclude_machine_id,
    )
    if include_idle:
        idle = await idle_connected_targets(
            agent_slug, rel_path, exclude_machine_id=exclude_machine_id,
        )
        if idle:
            machines = list(set(machines) | set(idle))
    if not machines:
        return
    from core.remote.satellite_connection import get_connection_manager
    from services.path_policy_v2 import PathRef
    cm = get_connection_manager()
    ref = PathRef("agent_tree", rel_path)
    results = await asyncio.gather(
        *(cm.push_file(mid, ref, content, agent_slug=agent_slug) for mid in machines),
        return_exceptions=True,
    )
    # Advance each successfully-pushed machine's merge base so it stays converged:
    # a later live edit there isn't mis-flagged as clobbering an unseen change, and
    # the next session-start merge sees in-sync. Hash/stat computed once,
    # only when there are targets (already gated above).
    acked = [
        mid for mid, res in zip(machines, results)
        if not isinstance(res, Exception) and res is not False
    ]
    if acked:
        import hashlib
        import config as _cfg
        from storage import sync_state_store
        content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        try:
            base_mtime = (_cfg.AGENTS_DIR / agent_slug / rel_path).stat().st_mtime
        except OSError:
            base_mtime = 0.0
        for mid in acked:
            try:
                await asyncio.to_thread(
                    sync_state_store.record_one, mid, agent_slug, rel_path,
                    content_hash, base_mtime,
                )
            except Exception:
                logger.debug("fan_out_write base-advance failed for %s", mid[:8])
    for mid, res in zip(machines, results):
        if isinstance(res, Exception):
            logger.warning(
                "fan_out_write %s -> %s failed: %s", rel_path, mid[:8], res,
            )
        elif res is False:
            logger.debug(
                "fan_out_write %s -> %s not acked (offline?)", rel_path, mid[:8],
            )


async def fan_out_delete(
    agent_slug: str, rel_path: str, *,
    exclude_machine_id: str | None = None, include_idle: bool = False,
) -> None:
    """Broadcast a delete for ``rel_path`` to every OTHER machine running
    ``agent_slug`` that is allowed to receive it. Fire-and-forget; never raises.

    Targets active-session machines; when ``include_idle`` is set — a dashboard
    delete — ALSO connected-but-idle machines that hold the agent, so the file is
    removed there immediately rather than only via the tombstone at the idle
    machine's next sync. Uses the same ``file_push`` / ``action: "delete"`` envelope
    the dashboard file-API delete has always used (``path_kind`` defaults to
    ``agent_tree`` on the satellite).
    """
    machines = fanout_targets(
        agent_slug, rel_path, exclude_machine_id=exclude_machine_id,
    )
    if include_idle:
        idle = await idle_connected_targets(
            agent_slug, rel_path, exclude_machine_id=exclude_machine_id,
        )
        if idle:
            machines = list(set(machines) | set(idle))
    if not machines:
        return
    from core.remote.satellite_connection import get_connection_manager
    from storage import sync_state_store
    cm = get_connection_manager()
    for mid in machines:
        try:
            await cm.send_fire_and_forget(mid, {
                "type": "file_push",
                "agent_slug": agent_slug,
                "action": "delete",
                "path": rel_path,
            })
            # The machine no longer holds this file → drop its merge base so the
            # next session-start merge doesn't treat it as a divergence. (The
            # tombstone — written at the delete source — drives the actual delete
            # for idle machines.)
            await asyncio.to_thread(
                sync_state_store.clear_one, mid, agent_slug, rel_path,
            )
        except Exception as e:
            logger.warning(
                "fan_out_delete %s -> %s failed: %s", rel_path, mid[:8], e,
            )


async def _atomic_write_agent_file(
    agent_slug: str, rel_path: str, content: bytes,
) -> None:
    """Write ``content`` to ``AGENTS_DIR/<agent_slug>/<rel_path>`` atomically
    (``.partial`` + ``os.replace``), path-traversal-checked. Runs in a thread.
    Raises ``ValueError`` (traversal) / ``OSError`` (I/O) on failure — the caller
    decides whether that's fatal (Collabora save) or best-effort (file-tools)."""
    def _write() -> None:
        import os
        import config
        base = (config.AGENTS_DIR / agent_slug).resolve()
        dest = (base / rel_path).resolve()
        dest.relative_to(base)  # raises ValueError on `..` traversal
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".partial")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, dest)
        except OSError:
            # EDQUOT / ENOSPC: drop the orphan .partial (manifest-invisible →
            # would leak quota) and re-raise so the caller surfaces the failed
            # write instead of falsely converging sync state.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
    await asyncio.to_thread(_write)


async def propagate_write(
    agent_slug: str, rel_path: str, content: bytes, *,
    exclude_machine_id: str | None = None, writer: str | None = None,
) -> None:
    """Atomically write ``content`` to the platform agent tree AND fan it out to
    every OTHER satellite running ``agent_slug`` — all under the global
    per-(agent, rel_path) lock so it never interleaves with the ``file_changed``
    applier / ``push_back`` / ``pull_through``.

    The shared propagation entry point for platform-side writes whose bytes are
    produced OUTSIDE the ``file_changed`` applier (so the proxy gets the final
    bytes directly, not a pull from a satellite):
      * **Collabora save** — ``api/media/wopi.py::wopi_put_file`` (Collabora already
        live-merged concurrent human editors → ``content`` IS the merged result);
      * **file-tools on a LOCAL session** — ``api/hooks/hooks.py::hook_file_written``
        local branch (the Docker MCP wrote the platform agent dir directly; we
        re-publish those bytes atomically + fan them out to remote satellites).

    Deliberately NO conflict-detect / recover-bin: by the time these callers run,
    the pre-overwrite bytes are already gone (there is no proxy-side pre-write
    hook), so loser attribution is unrecoverable. Last-writer-wins still converges
    because the global lock serializes this against every other platform writer.
    Conflict detection + recovery stays on the ``file_changed`` path
    (``core/remote/satellite_connection.py``) — the only writer that pre-captures.

    Failure semantics: the atomic disk write RAISES on failure (the caller treats
    it as fatal — e.g. Collabora's PutFile → 500). The fan-out is best-effort
    (``fan_out_write`` swallows per-push failures), so a satellite being offline
    never fails the write; it reconciles at that satellite's next session start.

    NOTE: ``push_back`` does NOT call this — it already holds the global lock for
    its own-machine push, so it calls ``fan_out_write`` directly (re-acquiring the
    same non-reentrant lock here would deadlock).
    """
    from core.remote.remote_file_flow import _acquire_global_path_lock
    lock = await _acquire_global_path_lock(agent_slug, rel_path)
    async with lock:
        await _atomic_write_agent_file(agent_slug, rel_path, content)
        # Versioned-sync bookkeeping: the path is live again (retire any tombstone)
        # and ``writer`` (the editing user's slug, if known) becomes its author for
        # cross-user conflict attribution. Best-effort.
        from storage import file_tombstones_store, file_author_store
        await asyncio.to_thread(file_tombstones_store.drop, agent_slug, rel_path)
        if writer:
            await asyncio.to_thread(file_author_store.record, agent_slug, rel_path, writer)
        await fan_out_write(
            agent_slug, rel_path, content, exclude_machine_id=exclude_machine_id,
        )
