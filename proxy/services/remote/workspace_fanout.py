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

A session on a compute machine beside the platform is a no-op: its workspace is a
bwrap bind-mount of the platform dir (same inode), so there is nothing to push
(``_shared_compute_machine_ids``, ``files_synced``).

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
import errno
import logging
import stat
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import config
from core import layout, placement
from core.background import spawn
from services.infra import safe_fs
from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path
from storage.pg import run_db

logger = logging.getLogger("otodock.workspace-fanout")

# (machine, agent) → (expires, identity or None): who an idle machine is for
# an agent (``idle_connected_targets``), kept this long.
_IDLE_IDENTITY_S = 5.0
_idle_identities: dict[tuple[str, str], tuple[float, tuple[str | None, str] | None]] = {}


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
    except ImportError:
        return []
    out: list[tuple[str, str, str, str]] = []
    compute = _shared_compute_machine_ids()
    for sid, s in list(getattr(_isess, "_sessions", {}).items()):
        try:
            if (s.agent_name == agent_slug and s.alive and not placement.is_local(s.target)
                    and s.target not in compute):
                out.append((sid, s.target, s.username or "", s.role or ""))
        except AttributeError:
            continue
    return out


def _shared_compute_machine_ids() -> frozenset[str]:
    """The connected compute machines beside the platform: they read its
    folder itself, so no file is pushed to them. One on another host has its
    own copy and takes the pushes, as a paired machine."""
    try:
        from core.remote.satellite_connection import get_connection_manager
        cm = get_connection_manager()
        return frozenset(m for m in cm.compute_machine_ids() if cm.shares_folders(m))
    except ImportError:
        return frozenset()


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
    except ImportError:
        return []
    sessions = getattr(layer, "_sessions", None) or {}

    from core.remote.file_sync import should_sync_to_target
    from core.session.session_state import get_session_security

    machines: set[str] = set()
    for sid, info in list(sessions.items()):
        if info.agent_name != agent_slug or not getattr(info, "alive", False):
            continue
        if not getattr(info, "files_synced", True):
            continue  # a compute machine reads the platform's folder itself
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
    except ImportError:
        return set()
    sessions = getattr(layer, "_sessions", None) or {}
    out: set[str] = set()
    for info in list(sessions.values()):
        if (getattr(info, "agent_name", None) == agent_slug and getattr(info, "alive", False)
                and getattr(info, "files_synced", True)):
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
    except ImportError:
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


async def _machine_identity(layer, machine_id: str, agent_slug: str):
    """Who a machine is for an agent, from its pairing: ``(username, role)``,
    or None when it never ran the agent (no target: a partial tree is never
    seeded) or is paired no more. It changes rarely and costs several store
    reads, so it is kept a few seconds: a tree write asks once per machine, not
    once per file and machine."""
    from storage.files import sync_state_store
    now = time.monotonic()
    kept = _idle_identities.get((machine_id, agent_slug))
    if kept is not None and kept[0] > now:
        return kept[1]
    agents = await run_db(sync_state_store.agents_for_machine, machine_id)
    ident = (await layer.resolve_machine_sync_identity(machine_id, agent_slug)
             if agent_slug in agents else None)
    for key, (expires, _i) in list(_idle_identities.items()):
        if expires <= now:
            del _idle_identities[key]
    _idle_identities[(machine_id, agent_slug)] = (now + _IDLE_IDENTITY_S, ident)
    return ident


async def machine_may_hold(machine_id: str, agent_slug: str, rel_path: str) -> bool:
    """Whether ``machine_id`` may receive ``rel_path`` now: the fan-out's rule
    judged again, for a name that waited for a machine that was away. A session
    of the agent on it that may see the file, or its pairing's identity."""
    from core.remote.file_sync import should_sync_to_target
    from core.session.session_manager import _get_remote_layer
    shared_only = await shared_only_of(agent_slug, rel_path)
    if machine_id in fanout_targets(agent_slug, rel_path, shared_only=shared_only):
        return True
    if shared_only or rel_path.startswith("externals/"):
        return False
    ident = await _machine_identity(_get_remote_layer(), machine_id, agent_slug)
    return ident is not None and should_sync_to_target(rel_path, ident[0], ident[1])


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
        candidates = [mid for mid in connected
                      if mid != exclude_machine_id and mid not in active]
        if not candidates:
            return []

        idents = await asyncio.gather(*(_machine_identity(layer, mid, agent_slug) for mid in candidates))
        return [
            mid for mid, ident in zip(candidates, idents, strict=True)
            if ident is not None and should_sync_to_target(rel_path, ident[0], ident[1])
        ]
    except Exception:  # noqa: BLE001  # the idle fan-out is best effort, the active machines got the write
        logger.debug(
            "idle_connected_targets failed for %s/%s", agent_slug, rel_path,
            exc_info=True,
        )
        return []


async def mark_fan_out_targets(
    agent_slug: str, rel_path: str, *, exclude_machine_id: str | None = None,
) -> dict[str, tuple[int, int]] | None:
    """Mark every active fan-out target of a platform write in flight, with
    the platform copy's stat, and return them (machine to stat) for the
    fan-out's ``marked``: empty when no machine is a target. Called inside
    the path lock right after the write: from its release until the fan-out
    marks the targets itself (after its store read, the descriptor open and
    the hash, or after the save it serves has answered), a read on a target
    with no pull record would pull the older bytes over the write. Never
    raises: a failure marks nothing and answers None (logged); the fan-out
    still runs and marks its targets itself."""
    try:
        shared_only = await shared_only_of(agent_slug, rel_path)
        machines = _targets(agent_slug, rel_path, exclude_machine_id, shared_only)
        if not machines:
            return {}
        st = await asyncio.to_thread(
            safe_fs.lstat_beneath, config.AGENTS_DIR,
            f"{agent_slug}/{normalize_rel_path(rel_path)}")
        from core.remote import remote_file_flow
        from core.remote.satellite_file_transfer import push_ceiling_s
        stat = (st.st_size, st.st_mtime_ns)
        in_flight_s = push_ceiling_s(st.st_size)
        for mid in machines:
            remote_file_flow.note_platform_ahead(mid, agent_slug, rel_path, *stat,
                                                 in_flight_s=in_flight_s)
        return {mid: stat for mid in machines}
    except Exception:  # noqa: BLE001  # marking fan-out targets spans db, disk and machines: the save stands
        logger.warning("marking the fan-out targets of %s/%s failed", agent_slug, rel_path,
                       exc_info=True)
        return None


async def fan_out_write(
    agent_slug: str, rel_path: str, source: bytes | Path, *,
    exclude_machine_id: str | None = None, include_idle: bool = False,
    transfer_kind: str = "sync", transfer_id: str | None = None,
    origin_user_sub: str = "", only_machine_id: str | None = None,
    marked: dict[str, tuple[int, int]] | None = None,
    holds_turns: bool | None = None,
) -> None:
    """Hand the file at ``rel_path`` to every OTHER machine running
    ``agent_slug`` that is allowed to receive it (``only_machine_id``: to
    that one machine, when it is one of them), and return. Never waits for
    a machine and never raises: each target's lane
    (``core/remote/machine_push_lane``) pushes the platform copy as it is
    when its turn comes. ``only_machine_id`` names the machine that asked:
    its push is waited for, for as long as it moves.

    ``marked``: the machines the caller marked in flight before it
    (``mark_fan_out_targets``). Each one that is no target now gets the
    failure's rules instead of a window no push will end
    (``remote_file_flow.end_in_flight``).

    ``holds_turns``: whether a turn on a target waits for this file (the
    turn barrier). A write a person or a platform tool made does (the
    default with ``include_idle``), a fan-out that started on a machine does
    not."""
    handed: list[str] = []
    try:
        handed = await _fan_out_write(
            agent_slug, rel_path, source, exclude_machine_id=exclude_machine_id,
            include_idle=include_idle, transfer_kind=transfer_kind,
            transfer_id=transfer_id, origin_user_sub=origin_user_sub,
            only_machine_id=only_machine_id,
            holds_turns=include_idle if holds_turns is None else holds_turns,
        )
    except Exception:  # noqa: BLE001  # the fan-out is best effort: the write stands, the next merge settles
        logger.warning("fan_out_write %s/%s failed", agent_slug, rel_path, exc_info=True)
    finally:
        if marked:
            from core.remote import remote_file_flow
            for mid, stat in marked.items():
                if mid not in handed:
                    remote_file_flow.end_in_flight(mid, agent_slug, rel_path, stat)


async def _fan_out_write(
    agent_slug: str, rel_path: str, source: bytes | Path, *,
    exclude_machine_id: str | None, include_idle: bool,
    transfer_kind: str, transfer_id: str | None,
    origin_user_sub: str, only_machine_id: str | None, holds_turns: bool,
) -> list[str]:
    """``fan_out_write``'s body; returns the machines the name was handed to.

    ``source`` says what was written: the bytes (small in-memory payloads —
    WOPI, hooks: the caller wrote them to the platform copy first) or the
    platform copy's ``Path``. Either way a lane pushes the platform copy
    itself, opened beneath the agents root when its turn comes: nothing is
    held per write, so a 1GB file fanning out to N machines costs N names.

    Targets active-session machines (``fanout_targets``); when ``include_idle`` is set
    — platform-origin dashboard/upload writes — ALSO connected-but-idle machines that
    hold the agent (``idle_connected_targets``), so a dashboard edit reaches a
    connected satellite even with no live session and its next session start stays
    light.

    Progress tracking (Feature E): when ``transfer_id`` is supplied (dashboard
    uploads — always tracked) or the payload exceeds one chunk, the transfer
    is registered in ``core/remote/transfer_registry`` with one row per
    target machine, and each lane moves and ends its own row. Registry calls
    are best-effort — a registry failure never affects the push.
    """
    shared_only = await shared_only_of(agent_slug, rel_path)
    machines = _targets(agent_slug, rel_path, exclude_machine_id, shared_only)
    if include_idle:
        idle = await idle_connected_targets(
            agent_slug, rel_path, exclude_machine_id=exclude_machine_id, shared_only=shared_only,
        )
        if idle:
            machines = list(set(machines) | set(idle))
    if only_machine_id is not None:
        machines = [m for m in machines if m == only_machine_id]
    from core.remote import machine_push_lane, transfer_registry
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
    try:
        if not config.is_safe_agent_name(agent_slug):
            raise PathOutsideRoot(agent_slug)
        rel = f"{agent_slug}/{normalize_rel_path(rel_path)}"
        if from_path and safe_fs.rel_under(cast("Path", source), config.AGENTS_DIR) != rel:
            raise safe_fs.EscapeRefused(errno.EXDEV, "not the platform copy", str(source))
    except (OSError, PathOutsideRoot) as exc:
        logger.warning(
            "fan_out_write %s/%s: source refused (%s)", agent_slug, rel_path,
            type(exc).__name__,
        )
        await _empty_terminal(0)
        return []
    size = 0
    if not from_path:
        size = len(cast("bytes", source))
    else:
        # The copy the lanes will push: a regular file reached beneath the
        # agents root with no component followed, or nothing is handed on.
        try:
            st = await asyncio.to_thread(safe_fs.lstat_beneath, config.AGENTS_DIR, rel)
            if not stat.S_ISREG(st.st_mode):
                raise safe_fs.EscapeRefused(errno.EINVAL, "not a regular file", rel)
        except OSError as exc:
            logger.warning(
                "fan_out_write %s/%s: source refused (%s)", agent_slug, rel_path,
                type(exc).__name__,
            )
            await _empty_terminal(0)
            return []
        size = st.st_size
    if not machines:
        await _empty_terminal(size)
        return []

    tid: str | None = None
    if transfer_id is not None or size > MAX_CHUNK_SIZE:
        tid = await transfer_registry.begin(
            agent_slug, rel_path, kind=transfer_kind, bytes_total=size,
            machine_ids=list(machines), transfer_id=transfer_id,
            origin_user_sub=origin_user_sub,
        )
    asked = 0
    for mid in machines:
        asked = machine_push_lane.mark(
            mid, agent_slug, rel_path, holds_turns=holds_turns,
            transfer=(tid, size) if tid else None)
    if only_machine_id is not None and machines:
        # The machine that asked: its copy is waited for while it moves.
        await machine_push_lane.settled(only_machine_id, agent_slug, upto=asked,
                                        idle_s=float(config.PUSH_STALL_S))
    return machines


async def drain_fan_outs(timeout: float = 30.0) -> bool:
    """Wait until every connected machine holds what the fan-outs handed it
    (tests)."""
    from core.remote import machine_push_lane
    return await machine_push_lane.drain(timeout)


async def fan_out_delete(
    agent_slug: str, rel_path: str, *,
    exclude_machine_id: str | None = None, include_idle: bool = False,
) -> None:
    """Hand a delete of ``rel_path`` to every OTHER machine running
    ``agent_slug`` that is allowed to receive it. Never waits for a machine
    and never raises.

    Targets active-session machines; when ``include_idle`` is set — a dashboard
    delete — ALSO connected-but-idle machines that hold the agent, so the file is
    removed there immediately rather than only via the tombstone at the idle
    machine's next sync. The delete takes its place in each machine's lane
    with the path's writes, so a write still waiting never lands after it.
    The lane sends the same ``file_push`` / ``action: "delete"`` envelope the
    dashboard file-API delete has always used (``path_kind`` defaults to
    ``agent_tree`` on the satellite) and drops the machine's merge base.
    """
    try:
        shared_only = await shared_only_of(agent_slug, rel_path)
        machines = _targets(agent_slug, rel_path, exclude_machine_id, shared_only)
        if include_idle:
            idle = await idle_connected_targets(
                agent_slug, rel_path, exclude_machine_id=exclude_machine_id,
                shared_only=shared_only,
            )
            if idle:
                machines = list(set(machines) | set(idle))
        from core.remote import machine_push_lane
        for mid in machines:
            machine_push_lane.mark(mid, agent_slug, rel_path, deleted=True)
    except Exception as e:  # noqa: BLE001  # the delete's fan-out is best effort: the tombstone settles it
        logger.warning("fan_out_delete %s failed: %s", rel_path, e)


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


# The fan-out of a Collabora save runs after the save answered, one queued
# per path at most: a save while one waits for the fan-out lock adds its
# marked machines to it, and that one pushes the platform copy as it is when
# it starts. Keyed (agent, path, excluded machine).
_queued_save_fan_outs: dict[tuple[str, str, str | None], dict] = {}
_save_fan_out_tasks: set[asyncio.Task] = set()


def _schedule_save_fan_out(agent_slug: str, rel_path: str,
                           exclude_machine_id: str | None,
                           marked: dict[str, tuple[int, int]] | None) -> None:
    """Start the fan-out of a save that marked ``marked``, or fold the marks
    into the one already queued. Tracked as an in-flight push of the
    machines it marked (``upload_inflight``): a headless turn on one of them
    waits for it before its prompt goes out (the agent reads the saved
    bytes); a turn on another machine does not. ``marked`` None (the marks
    failed): the fan-out runs all the same, bound for no known machine, so
    it holds every machine's turn."""
    key = (agent_slug, rel_path, exclude_machine_id)
    queued = _queued_save_fan_outs.get(key)
    if queued is not None:
        if marked is None:
            from core.remote import upload_inflight
            upload_inflight.unbind(queued["task"])
        else:
            queued["marked"].update(marked)
            queued["machines"].update(marked)
        return
    if marked is not None and not marked:
        return  # no machine runs a session that may see the path
    entry: dict = {"marked": dict(marked or {}), "machines": set(marked or ())}
    from core.remote import upload_inflight
    task = upload_inflight.track(agent_slug, _save_fan_out(key, entry),
                                 machines=None if marked is None else entry["machines"])
    entry["task"] = task
    _save_fan_out_tasks.add(task)
    task.add_done_callback(_save_fan_out_tasks.discard)
    # The task runs only once this coroutine yields: registered after it
    # exists, so a failed start never leaves a queue nobody drains.
    _queued_save_fan_outs[key] = entry


async def _save_fan_out(key: tuple[str, str, str | None], entry: dict) -> None:
    agent_slug, rel_path, exclude_machine_id = key
    from core.remote.remote_file_flow import acquire_fanout_lock
    try:
        # The fan-out lock alone, never the path lock while holding it: the
        # writers that take both take the path lock first.
        fanout_lock = await acquire_fanout_lock(agent_slug, rel_path)
        async with fanout_lock:
            # A save from here on schedules its own fan-out: this one opens
            # the platform copy after this point, so every save folded into
            # it is in what it pushes.
            if _queued_save_fan_outs.get(key) is entry:
                del _queued_save_fan_outs[key]
            await fan_out_write(
                agent_slug, rel_path, config.AGENTS_DIR / agent_slug / rel_path,
                exclude_machine_id=exclude_machine_id, marked=entry["marked"],
                holds_turns=True,
            )
    except Exception:
        logger.exception("the fan-out of a save of %s/%s failed", agent_slug, rel_path)
    finally:
        if _queued_save_fan_outs.get(key) is entry:
            del _queued_save_fan_outs[key]


async def drain_save_fan_outs() -> None:
    """Wait until no save's fan-out is queued or running (tests)."""
    while _save_fan_out_tasks:
        await asyncio.gather(*list(_save_fan_out_tasks), return_exceptions=True)


async def propagate_write(
    agent_slug: str, rel_path: str, content: bytes, *,
    exclude_machine_id: str | None = None, writer: str | None = None,
    precheck: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Atomically write ``content`` to the platform agent tree under the
    global per-(agent, rel_path) lock, so it never interleaves with the
    ``file_changed`` applier / ``push_back`` / ``pull_through``, and fan it
    out to every machine running ``agent_slug`` (``exclude_machine_id``
    aside) AFTER the call returns.

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

    The save answers once its bytes are stored: a machine that stalls (its
    socket drops only after 30 s) would otherwise make Collabora's upload
    time out and retry into a refusal of its own save. Inside the path lock
    the targets are marked in flight (a read there serves the platform copy
    from the moment the save answers); the fan-out then runs under the
    fan-out lock alone (``_schedule_save_fan_out``).

    Failure semantics: the atomic disk write RAISES on failure (the caller treats
    it as fatal — e.g. Collabora's PutFile → 500). Nothing after it fails
    the save (logged): a 500 once the bytes landed would have Collabora retry
    into a refusal. The fan-out is best-effort (``fan_out_write`` swallows
    per-push failures and marks a missed machine ahead), so a satellite being
    offline never fails the write; it reconciles at its next read, reconnect
    or session start.

    ``precheck`` (the WOPI save check) is awaited inside the path lock,
    before the write: a write queued for the lock meanwhile cannot land
    between the check and this write. When it answers False nothing is
    written or fanned out and False is returned; otherwise True.

    NOTE: ``push_back`` does NOT call this — it takes the global path lock for
    its own-machine push, takes the fan-out lock inside it, releases the path
    lock, and then calls ``fan_out_write`` directly under the fan-out lock alone
    (since 1.7.1), so a slow target never holds the next writer of the path.
    """
    from core.remote.remote_file_flow import _acquire_global_path_lock
    lock = await _acquire_global_path_lock(agent_slug, rel_path)
    async with lock:
        if precheck is not None and not await precheck():
            return False
        await _atomic_write_agent_file(agent_slug, rel_path, content)
        # Versioned-sync bookkeeping: the path is live again (retire any tombstone)
        # and ``writer`` (the editing user's slug, if known) becomes its author for
        # cross-user conflict attribution. Best-effort.
        try:
            from storage.files import file_author_store, file_tombstones_store
            await asyncio.to_thread(file_tombstones_store.drop, agent_slug, rel_path)
            if writer:
                await asyncio.to_thread(file_author_store.record, agent_slug, rel_path, writer)
        except Exception:  # noqa: BLE001  # a landed save's bookkeeping must not fail the save
            logger.warning("the bookkeeping of a save of %s/%s failed", agent_slug, rel_path,
                           exc_info=True)
        marked = await mark_fan_out_targets(agent_slug, rel_path,
                                            exclude_machine_id=exclude_machine_id)
    try:
        _schedule_save_fan_out(agent_slug, rel_path, exclude_machine_id, marked)
    except Exception:
        logger.exception("scheduling the fan-out of a save of %s/%s failed", agent_slug, rel_path)
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
                spawn(library_projector.propagate_mirror_write(
                        agent_slug, _src, _sub),
                      name="propagate_mirror_write", owner="workspace_fanout")
        else:
            spawn(library_projector.propagate_source_write(
                    agent_slug, rel_path[len("knowledge/"):]),
                  name="propagate_source_write", owner="workspace_fanout")
    return True
