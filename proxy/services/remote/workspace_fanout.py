"""Active-session workspace fan-out — propagate a workspace write/delete to the
OTHER satellites currently running the same collaborative agent.

Background
----------
When users A and B run sessions for the same agent on DIFFERENT satellites, an
edit on A's machine only reached B's machine at B's next session start
(``core/remote/remote_workspace_sync.py::_initial_workspace_sync``). This module closes that
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
import contextlib
import errno
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import config
from core import layout
from core import placement
from services.infra import safe_fs
from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path
from storage.pg import run_db

logger = logging.getLogger("claude-proxy.workspace-fanout")

# A tracked push's progress rows move at most this often.
_PROGRESS_EVERY_S = 0.5


def _interactive_remote_sessions(agent_slug: str) -> list[tuple[str, str, str, str]]:
    """``(session_id, machine_id, username, role)`` for alive REMOTE interactive
    (PTY) sessions of this agent. TUI sessions live in their own registry
    (``core/session/interactive_session``), not the remote layer's — without
    them a machine running only a terminal session looked IDLE to both the
    fan-out and the fingerprint sweep, so the sweep merged every 60 s against
    a tree the live session was actively mutating (and scrubbed files the
    session had just written), while live pushes skipped the machine entirely.
    Local PTY sessions (``is_remote`` false) run on the platform tree itself
    — nothing to push, excluded like local headless sessions."""
    try:
        from core.session import interactive_session as _isess
    except Exception:
        return []
    out: list[tuple[str, str, str, str]] = []
    for sid, s in list(getattr(_isess, "_sessions", {}).items()):
        try:
            if s.agent_name == agent_slug and s.alive and not placement.is_local(s.target):
                out.append((sid, s.target, s.username or "", s.role or ""))
        except Exception:
            continue
    return out


def fanout_targets(
    agent_slug: str, rel_path: str, *, exclude_machine_id: str | None = None,
    shared_only: bool | None = None,
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

    Synchronous and registry-only so it is unit-testable in isolation, but for
    the one store read a ``users/`` path needs: ``shared_only`` is that answer
    when the caller resolved it off the loop (``shared_only_of``); ``None``
    reads it here (the sync callers outside the file-sync path).
    """
    # Shared-only agents have no per-user scope — users/ paths (stray dirs
    # from older installs at most) never fan out to any machine, mirroring
    # compute_manifest's exclude_user_dirs.
    if layout.is_personal(rel_path):
        if shared_only is None:
            from core.session.visibility import is_shared_only
            shared_only = is_shared_only(agent_slug)
        if shared_only:
            return []
    # External callers' trees never fan out (proxy host only).
    if rel_path.startswith("externals/"):
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
    shared_only: bool | None = None,
) -> bool:
    """Cheap (in-memory, NO DB) gate: is there ANY machine that might receive this
    file — so a caller can skip an expensive disk read without DB I/O?

    True if an active session is allowed it (``fanout_targets``), or — when
    ``include_idle`` — if ANY other connected machine exists (an idle candidate; the
    precise pairing + isolation filter runs later in ``idle_connected_targets``).
    Over-approximates idle (a connected machine that doesn't hold the agent still
    says "yes" → at worst one wasted read, NEVER a wrong push). ``shared_only``
    as in ``fanout_targets``.
    """
    if _targets(agent_slug, rel_path, exclude_machine_id, shared_only):
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


def _targets(agent_slug: str, rel_path: str, exclude_machine_id: str | None,
             shared_only: bool | None) -> list[str]:
    """``fanout_targets`` with the Shared-only answer passed only when a
    ``users/`` path resolved it (the keyword reaches the gate for those
    paths alone; every other call keeps the two-argument shape)."""
    if shared_only is None:
        return fanout_targets(agent_slug, rel_path, exclude_machine_id=exclude_machine_id)
    return fanout_targets(agent_slug, rel_path, exclude_machine_id=exclude_machine_id,
                          shared_only=shared_only)


async def shared_only_of(agent_slug: str, rel_path: str) -> bool | None:
    """The Shared-only answer a ``users/`` path needs, read on the DB lane;
    None for a path that never asks it."""
    if not layout.is_personal(rel_path):
        return None
    from core.session.visibility import is_shared_only
    return await run_db(is_shared_only, agent_slug)


async def idle_connected_targets(
    agent_slug: str, rel_path: str, *, exclude_machine_id: str | None = None,
    shared_only: bool | None = None,
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
        from storage.files import sync_state_store

        # Same shared-only users/ exclusion as fanout_targets.
        if layout.is_personal(rel_path):
            if shared_only is None:
                shared_only = await shared_only_of(agent_slug, rel_path)
            if shared_only:
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
    agent_slug: str, rel_path: str, source: "bytes | Path", *,
    exclude_machine_id: str | None = None, include_idle: bool = False,
    transfer_kind: str = "sync", transfer_id: str | None = None,
    origin_user_sub: str = "",
) -> None:
    """Push a file at ``rel_path`` to every OTHER machine running ``agent_slug``
    that is allowed to receive it. Best-effort; never raises.

    ``source`` is either the file's bytes (small in-memory payloads — WOPI,
    hooks) or its platform filesystem ``Path`` — the streaming mode all
    large-file callers use: ``push_file`` reads one chunk at a time from disk, so a
    1GB file fanning out to N machines costs O(N × chunk) memory, never
    N × the file.

    Targets active-session machines (``fanout_targets``); when ``include_idle`` is set
    — platform-origin dashboard/upload writes — ALSO connected-but-idle machines that
    hold the agent (``idle_connected_targets``), so a dashboard edit reaches a
    connected satellite even with no live session and its next session start stays
    light. Pushes run concurrently; per-push failures are logged, not raised.

    Progress tracking (Feature E): when ``transfer_id`` is supplied (dashboard
    uploads — always tracked) or the payload exceeds one chunk, the transfer
    is registered in ``core/remote/transfer_registry`` with one row per
    target machine and live byte progress (``push_file``'s progress_cb), so
    the workspace toolbar popup shows per-machine state. Registry calls are
    best-effort — a registry failure never affects the push.
    """
    shared_only = await shared_only_of(agent_slug, rel_path)
    machines = _targets(agent_slug, rel_path, exclude_machine_id, shared_only)
    if include_idle:
        idle = await idle_connected_targets(
            agent_slug, rel_path, exclude_machine_id=exclude_machine_id, shared_only=shared_only,
        )
        if idle:
            machines = list(set(machines) | set(idle))
    from core.remote import transfer_registry
    from core.remote.file_sync import MAX_CHUNK_SIZE

    async def _empty_terminal(size: int) -> None:
        # The caller promised the client a tracked push (the cheap candidate
        # gate said yes) but nothing will be pushed: register it with no rows
        # so the registry emits the terminal the client is waiting for,
        # instead of returning silently.
        if transfer_id is not None:
            await transfer_registry.begin(
                agent_slug, rel_path, kind=transfer_kind, bytes_total=size,
                machine_ids=[], transfer_id=transfer_id,
                origin_user_sub=origin_user_sub,
            )

    # A source Path is the platform copy of ``rel_path`` or nothing: neither
    # the size probe below nor a push ever touches another file.
    from_path = not isinstance(source, (bytes, bytearray))
    rel = ""
    if from_path:
        try:
            if not config.is_safe_agent_name(agent_slug):
                raise PathOutsideRoot(agent_slug)
            rel = f"{agent_slug}/{normalize_rel_path(rel_path)}"
            if safe_fs.rel_under(source, config.AGENTS_DIR) != rel:
                raise safe_fs.EscapeRefused(errno.EXDEV, "not the platform copy", str(source))
        except (OSError, PathOutsideRoot) as exc:
            logger.warning(
                "fan_out_write %s/%s: source refused (%s)", agent_slug, rel_path,
                type(exc).__name__,
            )
            await _empty_terminal(0)
            return
    if not machines:
        size = 0
        if not from_path:
            size = len(source)
        else:
            with contextlib.suppress(OSError):
                size = (await asyncio.to_thread(
                    safe_fs.lstat_beneath, config.AGENTS_DIR, rel)).st_size
        await _empty_terminal(size)
        return

    # The bytes every push reads are that platform copy, opened ONCE beneath
    # the agents root with no link followed; ``push_file`` receives the
    # descriptor's own path, so a swap of any name meanwhile changes nothing
    # it sends, and the merge base recorded below is the hash of what was
    # read from that same descriptor (D3: taken before the pushes, never
    # after). Every push sends that hash as the frame's hash: a writer that
    # changes the inode in place after the hash makes the satellite refuse
    # the push, so no base names bytes the target never got.
    src_fd: int | None = None
    base_mtime = 0.0
    if from_path:
        try:
            src_fd, st = await asyncio.to_thread(
                safe_fs.open_regular_for_read, config.AGENTS_DIR, rel)
        except OSError as exc:
            logger.warning(
                "fan_out_write %s/%s: source refused (%s)", agent_slug, rel_path,
                type(exc).__name__,
            )
            await _empty_terminal(0)
            return
        size, base_mtime = st.st_size, st.st_mtime
        platform_stat: tuple[int, int] | None = (st.st_size, st.st_mtime_ns)
        content_hash = await asyncio.to_thread(_hash_fd, src_fd)
        source = Path(safe_fs.fd_path(src_fd))
    else:
        size = len(source)
        content_hash = "sha256:" + hashlib.sha256(source).hexdigest()
        # The platform copy these bytes were written to (the callers write
        # it first, under the path lock they still hold).
        try:
            pst = await asyncio.to_thread(
                safe_fs.lstat_beneath, config.AGENTS_DIR,
                f"{agent_slug}/{normalize_rel_path(rel_path)}")
            platform_stat = (pst.st_size, pst.st_mtime_ns)
        except (OSError, PathOutsideRoot):
            platform_stat = None
    try:
        await _fan_out_pushes(
            agent_slug, rel_path, source, machines, size, content_hash, base_mtime,
            transfer_kind=transfer_kind, transfer_id=transfer_id,
            origin_user_sub=origin_user_sub, max_chunk=MAX_CHUNK_SIZE,
            platform_stat=platform_stat,
        )
    finally:
        if src_fd is not None:
            os.close(src_fd)


def _hash_fd(fd: int) -> str:
    h = hashlib.sha256()
    for chunk in safe_fs.iter_fd(fd):
        h.update(chunk)
    return "sha256:" + h.hexdigest()


async def _fan_out_pushes(
    agent_slug: str, rel_path: str, source: "bytes | Path", machines: list[str],
    size: int, content_hash: str, base_mtime: float, *,
    transfer_kind: str, transfer_id: str | None, origin_user_sub: str, max_chunk: int,
    platform_stat: tuple[int, int] | None = None,
) -> None:
    from core.remote import transfer_registry
    from core.remote.satellite_connection import get_connection_manager
    from services.path_policy_v2 import PathRef
    cm = get_connection_manager()
    ref = PathRef("agent_tree", rel_path)
    MAX_CHUNK_SIZE = max_chunk

    tid: str | None = None
    if transfer_id is not None or size > MAX_CHUNK_SIZE:
        tid = await transfer_registry.begin(
            agent_slug, rel_path, kind=transfer_kind, bytes_total=size,
            machine_ids=list(machines), transfer_id=transfer_id,
            origin_user_sub=origin_user_sub,
        )

    from core.remote import transfer_gate

    async def _push_one(mid: str) -> bool:
        cb = None
        on_state = None
        if tid:
            # A push reports every acked chunk: the row moves at most every
            # _PROGRESS_EVERY_S (its terminal goes out below regardless).
            last = {"t": 0.0}

            async def cb(sent: int, total: int, _mid=mid):
                now = time.monotonic()
                if sent < total and now - last["t"] < _PROGRESS_EVERY_S:
                    return
                last["t"] = now
                await transfer_registry.progress(tid, _mid, sent, total)

            # The gate drives the queued→active lifecycle for tracked rows
            # (rows are born 'queued' in the registry).
            async def on_state(state: str, _mid=mid):
                await transfer_registry.set_state(tid, _mid, state)

        # The machine's outbound gate (Feature F): only ≥threshold pushes contend;
        # a machine waiting on a slot shows 'queued' in the progress popup.
        async with transfer_gate.slot(
            mid, agent_slug, rel_path, size, on_state=on_state,
        ):
            if platform_stat is not None:
                # The in-flight mark counts from the slot, not the queue.
                remote_file_flow.note_platform_ahead(mid, agent_slug, rel_path, *platform_stat,
                                                     in_flight_s=in_flight_s)
            ok = await cm.push_file(
                mid, ref, source, agent_slug=agent_slug, progress_cb=cb,
                content_hash=content_hash,
            )
        if tid:
            if ok:
                await transfer_registry.progress(tid, mid, size, size)
                await transfer_registry.set_state(tid, mid, "done")
            else:
                await transfer_registry.set_state(
                    tid, mid, "failed",
                    error="push failed (machine offline?) — retries at next sync",
                )
        return ok

    # Until its push lands, each target holds older bytes than the platform
    # copy: a read there meanwhile serves the platform copy (this fan-out
    # runs outside the path lock, so a read could otherwise pull the older
    # bytes over it). Marked now, and again when the push gets its transfer
    # slot, for as long as the push may run (its ceiling): a mark that
    # outlives a push which never answers turns into a failure's rules.
    from core.remote import remote_file_flow
    from core.remote.satellite_file_transfer import push_ceiling_s
    in_flight_s = push_ceiling_s(size)
    if platform_stat is not None:
        for mid in machines:
            remote_file_flow.note_platform_ahead(mid, agent_slug, rel_path, *platform_stat,
                                                 in_flight_s=in_flight_s)
    results = await asyncio.gather(
        *(_push_one(mid) for mid in machines),
        return_exceptions=True,
    )
    if tid:
        # A raised (not returned-False) push never hit _push_one's outcome
        # code — close its row defensively so the item can complete.
        for mid, res in zip(machines, results):
            if isinstance(res, Exception):
                await transfer_registry.set_state(
                    tid, mid, "failed", error=f"push error: {res}",
                )
    # Advance each successfully-pushed machine's merge base so it stays converged:
    # a later live edit there isn't mis-flagged as clobbering an unseen change, and
    # the next session-start merge sees in-sync. The hash and the mtime were
    # taken from the source before the pushes (a Path source: from the very
    # descriptor the pushes read), so the base names the bytes each target got.
    acked = [
        mid for mid, res in zip(machines, results)
        if not isinstance(res, Exception) and res is not False
    ]
    if acked:
        from storage.files import sync_state_store
        if isinstance(source, (bytes, bytearray)):
            try:
                base_mtime = (await asyncio.to_thread(
                    safe_fs.lstat_beneath, config.AGENTS_DIR,
                    f"{agent_slug}/{normalize_rel_path(rel_path)}")).st_mtime
            except (OSError, PathOutsideRoot):
                base_mtime = 0.0
        if content_hash:
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
    # Each target that missed the bytes holds an older copy than the
    # platform's: a read there must not pull it over them
    # (remote_file_flow's platform-ahead marker). An acked push clears it.
    missed = [mid for mid, res in zip(machines, results) if isinstance(res, Exception) or res is False]
    for mid in acked:
        remote_file_flow.clear_platform_ahead(mid, agent_slug, rel_path, stat=platform_stat)
    if missed and platform_stat is not None:
        await asyncio.gather(*(
            remote_file_flow.note_push_failed(cm, mid, agent_slug, rel_path, *platform_stat)
            for mid in missed
        ), return_exceptions=True)


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
    shared_only = await shared_only_of(agent_slug, rel_path)
    machines = _targets(agent_slug, rel_path, exclude_machine_id, shared_only)
    if include_idle:
        idle = await idle_connected_targets(
            agent_slug, rel_path, exclude_machine_id=exclude_machine_id, shared_only=shared_only,
        )
        if idle:
            machines = list(set(machines) | set(idle))
    if not machines:
        return
    from core.remote.satellite_connection import get_connection_manager
    from storage.files import sync_state_store
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
    beneath the agents root: the string guards first, then ``atomic_writer``
    (a ``.partial`` temp renamed within the parent's handle, no component
    followed, a link at the name replaced and never written through). Runs
    in a thread. Raises ``ValueError`` (a bad slug or rel) / ``OSError`` (I/O,
    a refusal of the helpers) on failure; the caller decides whether that's
    fatal (the Collabora save, its one caller). A failed write leaves
    no temp behind (quota is never leaked by an orphan)."""
    try:
        if not config.is_safe_agent_name(agent_slug):
            raise PathOutsideRoot(agent_slug)
        rel = f"{agent_slug}/{normalize_rel_path(rel_path)}"
    except PathOutsideRoot as exc:
        raise ValueError(str(exc)) from None
    await asyncio.to_thread(
        safe_fs.atomic_write_beneath, config.AGENTS_DIR, rel, content, mkdirs=True,
    )


async def propagate_write(
    agent_slug: str, rel_path: str, content: bytes, *,
    exclude_machine_id: str | None = None, writer: str | None = None,
    precheck: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Atomically write ``content`` to the platform agent tree AND fan it out to
    every machine running ``agent_slug`` (``exclude_machine_id`` aside) — all under the global
    per-(agent, rel_path) lock so it never interleaves with the ``file_changed``
    applier / ``push_back`` / ``pull_through``.

    The propagation entry point of a Collabora save
    (``api/media/wopi.py::wopi_put_file``), whose bytes are produced OUTSIDE
    the ``file_changed`` applier, so the proxy gets the final bytes directly,
    not a pull from a satellite. Collabora already live-merged concurrent
    human editors, so ``content`` IS the merged result. A file-tools write
    goes through ``hook_file_written`` instead: ``push_back`` and its
    fan-out on a remote session, nothing to fan out on a local one.

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

    ``precheck`` (the WOPI save check) is awaited inside the path lock,
    before the write: a write queued for the lock meanwhile cannot land
    between the check and this write. When it answers False nothing is
    written or fanned out and False is returned; otherwise True.

    NOTE: ``push_back`` does NOT call this — it takes the global path lock for
    its own-machine push, takes the fan-out lock inside it, releases the path
    lock, and then calls ``fan_out_write`` directly under the fan-out lock alone
    (since 1.7.1), so a slow target never holds the next writer of the path.
    """
    from core.remote.remote_file_flow import _acquire_global_path_lock, acquire_fanout_lock
    lock = await _acquire_global_path_lock(agent_slug, rel_path)
    async with lock:
        if precheck is not None and not await precheck():
            return False
        await _atomic_write_agent_file(agent_slug, rel_path, content)
        # Versioned-sync bookkeeping: the path is live again (retire any tombstone)
        # and ``writer`` (the editing user's slug, if known) becomes its author for
        # cross-user conflict attribution. Best-effort.
        from storage.files import file_tombstones_store
        from storage.files import file_author_store
        await asyncio.to_thread(file_tombstones_store.drop, agent_slug, rel_path)
        if writer:
            await asyncio.to_thread(file_author_store.record, agent_slug, rel_path, writer)
        # The fan-out lock is taken inside the path lock (the lock order every
        # writer keeps: path lock, fan-out lock, transfer gate), so fan-outs
        # of one path never interleave, whoever starts them.
        fanout_lock = await acquire_fanout_lock(agent_slug, rel_path)
        async with fanout_lock:
            await fan_out_write(
                agent_slug, rel_path, content, exclude_machine_id=exclude_machine_id,
            )
    # Knowledge-library projection (outside the lock — the projector takes
    # its own per-source lock): Collabora/file-tools writes into a promoted
    # source's knowledge propagate to consumer mirrors; RW mirror edits flow
    # back to the source. RO mirrors never mint edit tokens, so no gate here.
    if rel_path.startswith("knowledge/"):
        from services.knowledge import library_projector
        parsed = library_projector.parse_library_rel(rel_path)
        if parsed is not None:
            _src, _sub = parsed
            if _sub:
                asyncio.create_task(
                    library_projector.propagate_mirror_write(
                        agent_slug, _src, _sub))
        else:
            asyncio.create_task(
                library_projector.propagate_source_write(
                    agent_slug, rel_path[len("knowledge/"):]))
    return True
