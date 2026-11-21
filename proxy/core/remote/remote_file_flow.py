"""Remote workspace sync — bridges platform Docker MCPs to satellite files.

Background
----------
Docker MCPs on the platform (file-tools, camoufox) read and write files that
belong to an agent's workspace. On local-sandboxed sessions those files live
on the platform host directly. On a **remote** session the files live on the
satellite, but the Docker MCPs always run on the platform — so we have to
materialize the satellite's copy on the platform side for them to read.

This module bridges that gap by writing pulled files into the actual platform
``agents/<slug>/...`` workspace dir (NOT a separate cache). That means:

- The dashboard's workspace listing reflects what the satellite agent sees in
  real time, not just at end-of-turn.
- ``file-tools`` posts an agents-relative path to ``/v1/hooks/file*``; the
  hooks translate that per-target so the file resolves correctly whether the
  session is local or remote-satellite.
- File survival across the session is automatic — the file is in the same
  place as any locally-generated file.

Host-path additions
-------------------
``pull_through_host_path`` + ``push_back_host_path`` extend the same
pull-cache-push pattern to absolute paths on the satellite (e.g. the
user's ``~/Desktop/foo.png``). These live in ``AGENTS_DIR/.remote-host-cache/``
with a record in the server's own folder (``SESSIONS_DIR/host-cache/``) so
write-back targets the original abs_path. The
per-(machine_id, abs_path) write lock prevents concurrent Docker-MCP
edits from clobbering each other.

Public API
----------
- ``pull_through(session_id, rel_path)`` — fetch the satellite copy and write
  to platform's ``AGENTS_DIR/<slug>/<rel_path>``. Returns the host path. Used
  by hook callbacks (``/v1/hooks/file``, ``/v1/hooks/document-preview``,
  ``/v1/hooks/resolve-path``). Only for a path the session may write back
  (``write_back_allowed``); any other is served from the platform copy.
- ``push_back(session_id, rel_path)`` — flush a platform-side write back to
  the satellite. Used by ``/v1/hooks/file-written`` after a Docker MCP edits
  a file. Pending readers on the same path block until the push completes
  (write-barrier).
- ``pull_through_host_path(session_id, abs_path)`` — lazy-pull
  a satellite-host absolute path into a session-scoped temp dir; a record in
  the server's own folder (``SESSIONS_DIR/host-cache/``) keeps the
  (machine_id, abs_path) for push-back.
- ``push_back_host_path(session_id, cache_path)`` — push the
  cache file's bytes back to the satellite at the recorded abs_path.
- ``is_host_cache_path(host_path)`` — does the path live in
  the satellite-host cache subtree?
- ``is_remote_session(session_id)`` — is the session tracked by the remote
  layer?
- ``cleanup_session(session_id)`` — drop per-session locks/state on close.
- ``_acquire_global_path_lock(agent_slug, rel_path)`` — exposed so other proxy
  code paths that mutate the same workspace file (the ``file_changed`` event
  handler in ``satellite_connection.py`` and the active-session fan-out in
  ``services/remote/workspace_fanout.py``) serialize against pull/push.

The workspace write lock is module-global, keyed by ``(agent_slug, rel_path)``,
so pull/push, the ``file_changed`` applier, and the fan-out all serialize on the
same key ACROSS sessions and machines — two satellites running the same
collaborative agent can't clobber each other on a shared workspace file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from core.remote.remote_file_cache import (  # noqa: F401
    PUSH_ACK_STALE,
    _acquire_global_path_lock,
    _acquire_machine_host_lock,
    _back_off,
    _cache_rel,
    _cached_regular,
    _fanout_locks,
    _get_or_create_lock,
    _global_path_locks,
    _host_cache_paths,
    _host_cache_root,
    _lose_baseline,
    _make_cache_folder,
    _mark_current,
    _normalize_abs_path_for_key,
    _open_platform_copy,
    _PathLock,
    _platform_ahead,
    _probe_baseline,
    _probe_stat,
    _pull_stat_records,
    _read_host_meta,
    _read_old_host_meta,
    _record_agent_stat,
    _repush,
    _repush_tasks,
    _same_machine_copy,
    _standing_platform_ahead,
    _stats_match,
    _workspace_path,
    _write_host_meta,
    acquire_fanout_lock,
    capture_losing_platform_copy,
    clear_platform_ahead,
    delete_stands,
    end_in_flight,
    forget_merges,
    host_cache_entry,
    host_cache_session_root,
    host_meta_matches,
    is_host_cache_path,
    note_merge_left,
    note_merge_left_one,
    note_merged,
    note_platform_ahead,
    note_push_failed,
    note_signed_in,
    platform_copy_unsettled,
    push_expectation_of,
    record_committed_stat,
    record_held,
    record_pushed_stat,
    refused_save_stands,
    stale_hold,
    stale_machine_stat,
    take_push_record,
)

logger = logging.getLogger("otodock.remote-file-flow")


async def pull_through_host_path(
    session_id: str, abs_path: str,
) -> Path | None:
    """Lazy-pull a satellite-host absolute path into the proxy cache.

    Returns the cache path on success, ``None`` on failure. Caller (the
    resolve-path hook) returns the cache path to the Docker MCP, which
    reads it locally. Writes to the cache trigger ``push_back_host_path``
    via the ``/v1/hooks/file-written`` hook.
    """
    info = _get_remote_session_info(session_id)
    if info is None:
        return None
    cache_path, meta_path = _host_cache_paths(session_id, info.machine_id, abs_path)
    try:
        await asyncio.to_thread(_make_cache_folder, cache_path.parent)
    except OSError as e:
        logger.warning("pull_through_host_path: the cache folder of session %s refused: %s",
                       session_id[:8], e)
        return None

    lock = await _acquire_machine_host_lock(info.machine_id, abs_path)
    async with lock:
        from core.remote.satellite_connection import get_connection_manager
        from services.path_policy_v2 import PathRef
        cm = get_connection_manager()
        ref = PathRef("satellite_host", abs_path)

        # Revalidation fast path: cached copy + recorded pull-time stat +
        # matching fresh probe → skip the transfer entirely.
        fresh = await _probe_stat(cm, info.machine_id, ref)
        if fresh is not None and await asyncio.to_thread(_cached_regular, cache_path):
            recorded = (await asyncio.to_thread(_read_host_meta, meta_path) or {}).get("stat")
            if _stats_match(recorded, fresh):
                return cache_path

        ok = await cm.pull_file_to_path(info.machine_id, ref, cache_path)
        if not ok:
            return None
        # The file itself was already committed atomically (.partial + fsync
        # + rename) by pull_file_to_path. Write the record so a later
        # push-back targets the original abs_path — plus the PRE-pull stat
        # for the next read's revalidation (absent when the probe was
        # unavailable → next read pulls, today's behavior).
        meta = {"machine_id": info.machine_id, "abs_path": abs_path}
        if fresh is not None and fresh.get("exists"):
            meta["stat"] = fresh
        await asyncio.to_thread(_write_host_meta, meta_path, meta)
        return cache_path


async def push_back_host_path(session_id: str, cache_path: str) -> bool:
    """Push a satellite-host cache file's bytes back to the satellite.

    Looks up the (machine_id, abs_path) in the record written at pull time
    (``_host_cache_paths``: the server's own folder, never the cache). Acquires
    the same per-(machine_id, abs_path) lock to serialize concurrent edits.
    Returns True on satellite ack.

    Only a file of this session's own cache counts, by the path as written
    (``<cache>/<session>/<digest>/<name>``, never resolved), only toward its
    own machine, only to the machine path that folder was made for, and only
    where the session may write now. The cached file is opened with no link
    followed and pushed from that descriptor.
    """
    from core.remote import remote_file_cache
    info = _get_remote_session_info(session_id)
    entry = host_cache_entry(cache_path, session_id)
    if info is None or entry is None:
        logger.warning(
            "push_back_host_path: %s is not in session %s's own cache, refused",
            cache_path, session_id[:8],
        )
        return False
    digest, name = entry
    cp = Path(cache_path)
    meta_path = remote_file_cache._host_meta_root() / session_id / f"{digest}.json"
    meta = await asyncio.to_thread(_read_host_meta, meta_path)
    adopted = meta is None
    if adopted:
        meta = await asyncio.to_thread(_read_old_host_meta, cp)
    abs_path = host_meta_matches(meta or {}, info.machine_id, digest, name)
    if not abs_path:
        logger.warning(
            "push_back_host_path: no record of session %s's machine names %s, refused",
            session_id[:8], cache_path,
        )
        return False
    meta = cast(dict, meta)
    if not _may_write_host_path(session_id, abs_path):
        logger.warning("push_back_host_path: session %s may not write %s on its machine, refused",
                       session_id[:8], abs_path)
        return False
    if adopted:
        await asyncio.to_thread(_write_host_meta, meta_path, meta)
    machine_id = info.machine_id

    lock = await _acquire_machine_host_lock(machine_id, abs_path)
    async with lock:
        from core.remote.satellite_connection import get_connection_manager
        from services.infra import safe_fs
        from services.path_policy_v2 import PathRef
        cm = get_connection_manager()
        try:
            src_fd, _st = await asyncio.to_thread(
                safe_fs.open_regular_for_read, remote_file_cache._host_cache_root().parent,
                _cache_rel(cp))
        except OSError as e:
            logger.warning("push_back_host_path: %s not pushed: %s", cache_path, e)
            return False
        try:
            # The descriptor's PATH: push_file streams from it.
            ok = await cm.push_file(
                machine_id, PathRef("satellite_host", abs_path), Path(safe_fs.fd_path(src_fd)),
            )
        finally:
            os.close(src_fd)
        if ok and "stat" in meta:
            # The write changed the satellite file's mtime — the recorded
            # pull-time stat is stale. Invalidate it so the next read
            # re-pulls instead of fast-pathing onto a pre-write comparison.
            meta.pop("stat", None)
            try:
                await asyncio.to_thread(_write_host_meta, meta_path, meta)
            except OSError as e:
                logger.warning(
                    "push_back_host_path: record stat invalidation failed "
                    "for %s: %s", cache_path, e,
                )
        return ok


def _may_write_host_path(session_id: str, abs_path: str) -> bool:
    """Whether the session may write ``abs_path`` on its machine now: the
    path policy's own answer for a write (a pull admits paths only read)."""
    from core.session.session_state import get_session_security
    from services import path_policy_v2
    ctx = get_session_security(session_id)
    if ctx is None:
        return False
    answer = path_policy_v2.resolve_path_for_session(
        path_policy_v2.context_from_security(ctx), abs_path, writing=True)
    ref = answer.path_ref
    return bool(answer.allowed and ref is not None and ref.kind == "satellite_host"
                and _normalize_abs_path_for_key(ref.value) == _normalize_abs_path_for_key(abs_path))


@dataclass
class _SessionState:
    """Per-session bookkeeping for the file-flow subsystem.

    NOTE: the per-file write lock is NOT here — it's the module-global
    ``_global_path_locks`` keyed by ``(agent_slug, rel_path)`` so writers across
    different sessions / machines serialize. ``pending_push`` stays per-session
    because it's the *same-turn* read-after-write barrier for sequential
    Docker-MCP tool calls within one session.
    """

    # When a push_back is in flight we set this event; subsequent pulls on
    # the same rel_path wait for it to be cleared. Realizes the
    # "Docker MCP A's write is visible to Docker MCP B's read" write-barrier
    # contract for sequential same-turn tool calls.
    pending_push: dict[str, asyncio.Event] = field(default_factory=dict)


# session_id → _SessionState
_sessions: dict[str, _SessionState] = {}
_sessions_lock = asyncio.Lock()


async def _state(session_id: str) -> _SessionState:
    async with _sessions_lock:
        st = _sessions.get(session_id)
        if st is None:
            st = _SessionState()
            _sessions[session_id] = st
        return st


@dataclass
class _SurvivorSessionInfo:
    """Minimal registry stand-in for a remote session that survived a proxy
    restart satellite-side (old session_id, sidecars alive, JWT valid) but is
    absent from the rebuilt in-memory layer registry. Carries exactly the two
    fields the file flows consume — transport goes through the machine-level
    connection manager, which repopulates on satellite reconnect."""

    machine_id: str
    agent_name: str


# Sessions whose registry-miss fallback was already logged (once per session
# per process — the file flows re-look-up on every call).
_fallback_logged: set[str] = set()


def _get_remote_session_info(session_id: str):
    """Look up the RemoteSessionInfo for a session, or None if not remote.

    Delegates through the session_manager registry to avoid a direct import
    cycle with core.remote.remote_execution. On a registry miss, falls back
    to the disk-persisted SecurityContext: ``target_machine_id`` is only ever
    set for remote sessions, and a properly closed session has its context
    popped (pop persisted) before this module's cleanup runs — so the
    fallback cannot resurrect closed sessions, only post-restart survivors.
    """
    try:
        from core.session.session_manager import _get_remote_layer
        layer = _get_remote_layer()
        info = layer._sessions.get(session_id)
    except ImportError:
        info = None
    if info is not None:
        # A compute machine's session works on the platform's folder: the
        # platform's own flows serve it, as a local session's.
        return info if getattr(info, "files_synced", True) else None
    from core.session.session_state import get_session_security
    ctx = get_session_security(session_id)
    # A machine whose files are its own synced copy: a compute machine works
    # on the platform's folder, where the platform's own flows serve it.
    machine_id = ctx.placement.machine_id if ctx is not None and ctx.placement.files_synced else ""
    if not machine_id:
        return None
    if session_id not in _fallback_logged:
        _fallback_logged.add(session_id)
        logger.info(
            "Registry miss for session %s — serving remote file flows from "
            "the persisted security context (machine %s)",
            session_id[:8], machine_id[:8],
        )
    return _SurvivorSessionInfo(machine_id=machine_id, agent_name=ctx.agent)


def is_remote_session(session_id: str) -> bool:
    """True iff the session is tracked by RemoteExecutionLayer."""
    return _get_remote_session_info(session_id) is not None


def remote_machine_id(session_id: str) -> str:
    """The satellite machine id behind a remote session, '' when local."""
    info = _get_remote_session_info(session_id)
    return info.machine_id if info is not None else ""


def remote_session_agent(session_id: str) -> str:
    """The agent a remote session runs on its machine, '' when local."""
    info = _get_remote_session_info(session_id)
    return info.agent_name if info is not None else ""


async def stat_probe(session_id: str, rel_path: str) -> dict | None:
    """Best-effort ``file_stat`` of an agent-tree path on the session's
    satellite: ``{"exists", "size", "mtime_ns"}`` — ``exists`` is
    False for a directory as well as for an absent path. ``None`` when the
    session is not remote, the path is non-canonical, or the probe failed.
    Callers then fall back to
    ``pull_through`` (which re-probes and never trusts a missing answer).

    A pure read: no lock, no platform-side write, no mkdir — the safe way to
    ask "is this a file over there?" BEFORE ``pull_through`` creates the
    parent chain for a path that may turn out to be a typo or a directory.
    """
    info = _get_remote_session_info(session_id)
    if info is None:
        return None
    from core.remote.file_sync import is_canonical_rel_path
    if not is_canonical_rel_path(rel_path):
        return None
    from core.remote.satellite_connection import get_connection_manager
    from services.path_policy_v2 import PathRef
    cm = get_connection_manager()
    return await _probe_stat(
        cm, info.machine_id, PathRef("agent_tree", rel_path),
        agent_slug=info.agent_name,
    )


async def list_remote_files(session_id: str, rel_prefix: str) -> list[str] | None:
    """Agent-tree-relative paths of the regular files under ``rel_prefix``
    ('' = the whole tree) on the session's satellite, from ONE
    ``request_manifest`` round trip — the initial-sync frame, so symlinks,
    ``.partial`` staging files, runtime state and oversized files are already
    excluded satellite-side. Sorted. ``None`` when the session is not remote
    or the satellite could not answer (not connected, timeout, error) —
    callers fall back to the platform copy.
    """
    entries = await list_remote_entries(session_id, rel_prefix)
    return None if entries is None else [e["path"] for e in entries]


async def list_remote_entries(session_id: str, rel_prefix: str, *,
                              agent: str = "", report_skipped: bool = False) -> list[dict] | None:
    """``list_remote_files`` with each file's size as the machine listed it:
    ``[{"path", "size"}]``, sorted by path, same ``None`` cases. ``agent``
    names the agent the caller authorized the path against: a session held
    for another agent answers ``None``. ``report_skipped`` adds what the
    satellite could not read, as ``{"path", "size": 0, "skipped": <why>}``
    entries, a folder's with ``"dir": True`` (``""`` = the whole tree): a
    caller that removes what the machine lacks keeps those, and the
    unreadable folder's contents."""
    info = _get_remote_session_info(session_id)
    if info is None or (agent and info.agent_name != agent):
        return None
    from core.remote.remote_workspace_sync import manifest_request
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    try:
        ack = await cm.send_command(
            info.machine_id,
            manifest_request(info.agent_name, report_skipped=report_skipped),
            timeout=30.0,
        )
    except RuntimeError as e:
        logger.info(
            "list_remote_files: request_manifest failed for %s on %s: %s",
            info.agent_name, info.machine_id[:8], e,
        )
        return None
    want = rel_prefix.strip("/") + "/" if rel_prefix.strip("/") else ""
    out: list[dict] = []
    for entry in (ack.get("files") or []) if isinstance(ack, dict) else []:
        path = entry.get("path", "") if isinstance(entry, dict) else ""
        if not isinstance(path, str) or path.endswith(".partial"):
            continue
        skipped = entry.get("skipped")
        if skipped and not report_skipped:
            continue
        if not isinstance(skipped, str) or not skipped:
            if not path:
                continue
            if want and not path.startswith(want):
                continue
            size = entry.get("size")
            out.append({"path": path, "size": size if isinstance(size, int) and size >= 0 else 0})
            continue
        is_dir = entry.get("dir") is True
        # An unreadable folder above the prefix covers the whole listing.
        inside = not want or path.startswith(want) or (
            is_dir and (not path or want.startswith(path.rstrip("/") + "/")))
        if not inside:
            continue
        row = {"path": path, "size": 0, "skipped": skipped}
        if is_dir:
            row["dir"] = True
        out.append(row)
    return sorted(out, key=lambda e: e["path"])


async def pull_into(session_id: str, rel_path: str, dest: Path, *,
                    agent: str = "", max_bytes: int | None = None) -> bool:
    """Stream one agent-tree file from the session's satellite to ``dest``,
    a platform-owned path OUTSIDE the agents tree (a private snapshot). Never
    the platform copy: no path lock, no pull-stat record, no platform-ahead
    handling, nothing for the sync to reconcile. ``max_bytes`` fails the
    pull at the chunk that would pass it. False when the session is not
    remote or held for another agent than ``agent``, the path is not
    canonical or the pull failed."""
    info = _get_remote_session_info(session_id)
    if info is None or (agent and info.agent_name != agent):
        return False
    from core.remote.file_sync import is_canonical_rel_path
    if not is_canonical_rel_path(rel_path):
        return False
    from core.remote.satellite_connection import get_connection_manager
    from services.path_policy_v2 import PathRef
    try:
        return await get_connection_manager().pull_file_to_path(
            info.machine_id, PathRef("agent_tree", rel_path), dest,
            agent_slug=info.agent_name, max_bytes=max_bytes,
        )
    except (OSError, RuntimeError):
        logger.warning("pull_into: pull of %s failed", rel_path, exc_info=True)
        return False


async def write_back_allowed(session_id: str, agent: str, rel_path: str) -> bool:
    """May this session's machine copy of ``rel_path`` replace the platform's?
    The turn-end write-back's own rule (``file_sync.can_write_back``), judged
    on the session's authenticated context exactly as the ``file_changed``
    applier judges it. No context, or one held for another agent: False."""
    from core.session.session_state import get_session_security
    sec = get_session_security(session_id)
    if sec is None or sec.agent != agent:
        return False
    from core.remote import file_sync
    writable_libs: frozenset[tuple[str, str]] | None = None
    if file_sync.library_mirror_source(rel_path) is not None:
        from storage.knowledge import db_knowledge_libraries
        from storage.pg import run_db
        writable_libs = await run_db(db_knowledge_libraries.writable_pairs_for, agent)
    return file_sync.can_write_back(
        rel_path, sec.role or "", sec.username or "",
        mount_username=sec.mount_username,
        writable_libraries=writable_libs,
        knowledge_rw=bool(sec.knowledge_rw),
    )


def _platform_copy(agent_slug: str, rel_path: str) -> Path | None:
    """The platform's copy of a canonical agent-tree path when it is a
    regular file reached with no link on the way, else None. Blocking."""
    import stat

    import config
    from services.infra import safe_fs
    try:
        agent_dir = config.get_agent_dir(agent_slug)
        st = safe_fs.lstat_beneath(config.AGENTS_DIR, f"{agent_slug}/{rel_path}")
    except (OSError, ValueError):
        return None
    return agent_dir / rel_path if stat.S_ISREG(st.st_mode) else None


async def _served(agent_slug: str, rel_path: str) -> Path | None:
    """The file a read serves: the platform copy at the canonical name, when
    it is a regular file reached with no link (``_platform_copy``)."""
    return await asyncio.to_thread(_platform_copy, agent_slug, rel_path)


def held_reason(rel_path: str) -> str:
    """Why a held read found nothing: the path is not on the platform, and
    the session may not bring it there from its machine."""
    return (f"{rel_path} is not on the platform, and this session may not copy "
            "files from its machine into that folder (its role cannot write there)")


def _owed_by_lane(machine_id: str, agent_slug: str, rel_path: str) -> str:
    from core.remote import machine_push_lane
    return machine_push_lane.owed(machine_id, agent_slug, rel_path)


async def pull_through(session_id: str, rel_path: str, *,
                       fallback: list[str] | None = None,
                       held: list[str] | None = None) -> Path | None:
    """Ensure a platform-local copy of a satellite file exists; return host path.

    For remote sessions, fetches the file via WS into the actual platform
    workspace at ``AGENTS_DIR/<slug>/<rel_path>``. Docker MCPs (file-tools,
    camoufox) and the dashboard workspace listing both see the file at this
    canonical location.

    Returns ``None`` if:
      - the session isn't remote (caller should fall back to local resolution)
      - the satellite is unreachable / file doesn't exist
      - the platform copy at the name is not a regular file reached with
        no link at any part (the answer is always that name, never resolved)

    ``cm.pull_file_to_path`` streams the body to a staging file of its own and
    atomically renames it into place, so parallel pull_through calls never
    observe a half-written file.

    ``fallback`` collects ``rel_path`` when the platform copy is served
    because the machine could not provide the file (its caller reports
    that it sent older bytes).

    A path the session may not write back (``write_back_allowed``) is never
    read from the machine: the callers only checked that the session may
    READ it, and the pull would land the machine's bytes where the turn-end
    sync refuses them (a viewer's ``knowledge/``, a read-only library
    mirror). The platform's copy, the authoritative one for such a path, is
    returned when it is a regular file reached with no link, else ``None``;
    ``held`` collects the path either way. The gate judges the path as
    written, so the pull is handed the path as written too: its no-follow
    open refuses a link planted in the platform tree (one that would carry
    an allowed path into ``knowledge/``).
    """
    info = _get_remote_session_info(session_id)
    if info is None:
        return None

    # Canonical-form gate: pull_file_to_path eagerly mkdirs the parent chain,
    # so a non-canonical rel_path (e.g. a mistranslated satellite-host
    # absolute like "C:/Users/.../workspace/x") would create a junk dir chain
    # inside the platform agent dir even when the satellite then reports
    # not-found. The relative_to check below can't catch these — they stay
    # in-tree on Linux.
    from core.remote.file_sync import is_canonical_rel_path
    if not is_canonical_rel_path(rel_path):
        logger.warning(
            "pull_through: rejected non-canonical rel_path %r", rel_path,
        )
        return None

    if not await write_back_allowed(session_id, info.agent_name, rel_path):
        if held is not None:
            held.append(rel_path)
        logger.info(
            "pull_through: %s not read from machine %s, session %s may not "
            "write it back (platform copy served when there is one)",
            rel_path, info.machine_id[:8], session_id[:8],
        )
        return await _served(info.agent_name, rel_path)

    st = await _state(session_id)

    # Wait out any pending push_back for this path so we don't read a stale
    # workspace file that's about to be overwritten by a subsequent push.
    pending = st.pending_push.get(rel_path)
    if pending is not None:
        await pending.wait()

    # A push of the platform copy to this machine is on its way (a fan-out,
    # or a read's re-push that holds the path lock for as long as a slow
    # link takes): the platform copy is the file, with no lock to wait on.
    import config
    try:
        agent_dir = config.get_agent_dir(info.agent_name)
    except ValueError:
        logger.warning("Path refused: session=%s names no agent folder", session_id)
        return None
    # The path is never resolved (the rel is canonical: no `..`), and every
    # file this read serves is the platform copy AT that name, a regular
    # file reached with no link at any part (``_served``): the answer is the
    # very name the caller judged, never where a link put in meanwhile (the
    # wait for the path lock, the machine's probe) would lead.
    early_path = agent_dir / rel_path
    early = _standing_platform_ahead((info.machine_id, info.agent_name, rel_path), early_path)
    if early is not None and time.monotonic() < early["in_flight_until"]:
        return await _served(info.agent_name, rel_path)
    owed = _owed_by_lane(info.machine_id, info.agent_name, rel_path)
    if owed == "delete":
        # The platform removed the file and this machine has not heard
        # yet: its copy is never pulled back.
        return None
    if owed:
        # The platform's copy waits its turn in this machine's lane: it
        # is the file, and the machine's older bytes are never pulled
        # over it.
        served = await _served(info.agent_name, rel_path)
        if served is not None:
            return served

    lock = await _acquire_global_path_lock(info.agent_name, rel_path)
    async with lock:
        host_path = agent_dir / rel_path

        # Stream the body from the satellite straight into the workspace at
        # host_path (a canonical rel, never resolved). pull_file_to_path
        # commits atomically (.partial + fsync + rename), so Docker MCP code
        # paths (`/agents/...` mount) and the dashboard workspace listing see
        # a complete file — never a torn write.
        from core.remote.satellite_connection import get_connection_manager
        cm = get_connection_manager()
        from services.path_policy_v2 import PathRef
        ref = PathRef("agent_tree", rel_path)

        # Revalidation fast path: the workspace copy +
        # the stat recorded at the last pull + a matching fresh probe → the
        # satellite file hasn't changed, serve the workspace copy without
        # re-transferring it.
        key = (info.machine_id, info.agent_name, rel_path)
        ahead = _standing_platform_ahead(key, host_path)
        if ahead is not None and time.monotonic() < ahead["in_flight_until"]:
            # A fan-out is pushing the platform copy there now: it is the file.
            return await _served(info.agent_name, rel_path)
        owed = _owed_by_lane(info.machine_id, info.agent_name, rel_path)
        if owed == "delete":
            return None
        if owed:
            served = await _served(info.agent_name, rel_path)
            if served is not None:
                return served
        fresh = await _probe_stat(cm, info.machine_id, ref,
                                  agent_slug=info.agent_name)
        if ahead is not None:
            machine = ahead["machine"]
            if fresh is not None and machine is not None and not _same_machine_copy(machine, fresh):
                # The machine's copy changed after the missed push: the later
                # write wins, the read pulls it as below. A save the machine
                # refused as stale, or a merge could not deliver, is kept first.
                logger.warning(
                    "pull_through: %s changed on machine %s after a failed push "
                    "of the platform copy, the machine's copy is taken",
                    rel_path, info.machine_id[:8],
                )
                if ahead.get("stale") and await refused_save_stands(key, host_path):
                    await capture_losing_platform_copy(
                        info.agent_name, rel_path,
                        f"machine {info.machine_id[:8]} changed the file after the save "
                        "missed it")
                _platform_ahead.pop(key, None)
            else:
                # The machine still holds the older bytes (or cannot tell):
                # the platform copy is the file, and it goes to the machine
                # again, at most once per backoff while it keeps failing. The
                # push runs after this read returns (a slow link may take
                # minutes), marked in flight so reads meanwhile serve the
                # platform copy, and takes the path lock itself.
                if machine is None:
                    logger.warning(
                        "pull_through: no record of %s on machine %s, the platform "
                        "copy is served and pushed there again", rel_path, info.machine_id[:8],
                    )
                if time.monotonic() >= ahead["retry_at"]:
                    from core.remote.satellite_file_transfer import push_ceiling_s
                    note_platform_ahead(info.machine_id, info.agent_name, rel_path,
                                        ahead["size"], ahead["mtime_ns"],
                                        in_flight_s=push_ceiling_s(ahead["size"]),
                                        machine=machine)
                    _repush_tasks.add(asyncio.create_task(_repush(
                        cm, info.machine_id, info.agent_name, rel_path, ref, host_path, key,
                    )))
                return await _served(info.agent_name, rel_path)
        if fresh is not None and _stats_match(_pull_stat_records.get(key), fresh):
            served = await _served(info.agent_name, rel_path)
            if served is not None:
                return served
        if await delete_stands(cm, key, host_path, fresh):
            logger.info("pull_through: %s was deleted here, the copy on machine %s is not "
                        "brought back", rel_path, info.machine_id[:8])
            return None
        if await platform_copy_unsettled(key, host_path):
            logger.info("pull_through: %s on machine %s awaits its merge, the platform "
                        "copy is served", rel_path, info.machine_id[:8])
            return await _served(info.agent_name, rel_path)

        # The gate once more, right before the bytes move: a library
        # attachment may have turned read-only while this read waited for
        # the lock or the probe.
        if not await write_back_allowed(session_id, info.agent_name, rel_path):
            if held is not None:
                held.append(rel_path)
            return await _served(info.agent_name, rel_path)
        # The pull runs while the file moves, however large (its own
        # progress deadline). Its destination is the path as written, never
        # resolved: the pull opens it with no component followed, so a link
        # planted in the platform tree refuses the pull instead of carrying
        # the bytes out of the path the gate judged.
        pulled: dict = {}
        ok = await cm.pull_file_to_path(
            info.machine_id,
            ref,
            agent_dir / rel_path,
            agent_slug=info.agent_name,
            hash_out=pulled,
        )
        if not ok:
            # Serve the platform mirror when the satellite can't provide the
            # file — most commonly a file the PLATFORM ITSELF just wrote
            # (file-tools convert/write) whose flush hasn't landed on the
            # satellite yet (write-then-preview race, live-hit 2026-07-19).
            # Availability over freshness: the preview/display/media read
            # paths prefer last-known bytes + a log line over a hard 400.
            served = await _served(info.agent_name, rel_path)
            if served is not None:
                logger.info(
                    "pull_through: satellite pull failed for %s — serving "
                    "the platform mirror", rel_path,
                )
                if fallback is not None:
                    fallback.append(rel_path)
            return served
        # The bytes the machine sent are the copy both sides hold now
        # (``record_held``).
        if pulled.get("hash"):
            try:
                base_mtime = os.lstat(agent_dir / rel_path).st_mtime
            except OSError:
                base_mtime = 0.0
            await record_held(info.machine_id, info.agent_name, rel_path,
                              pulled["hash"], base_mtime, pulled["size"])
        # Record the PRE-pull probe for the next read's revalidation; a probe
        # that was unavailable leaves no record (the next read pulls).
        if fresh is not None and fresh.get("exists"):
            _record_agent_stat(key, fresh)
        else:
            _pull_stat_records.pop(key, None)
        return await _served(info.agent_name, rel_path)


async def push_back(session_id: str, rel_path: str) -> bool:
    """Flush a platform-side write to the satellite.

    Called after a Docker MCP edits a file in the platform workspace (via
    ``/v1/hooks/file-written``) — pushes the new bytes to the satellite so
    the agent CLI on the satellite sees the update.

    Returns True iff the satellite acked the write. Pending readers on the
    same rel_path block until this completes (write-barrier).
    """
    info = _get_remote_session_info(session_id)
    if info is None:
        return False

    # Same canonical-form gate as pull_through (drive-letter junk etc.): the
    # path stays beneath the agent's folder with no `..` (rel_path comes
    # from POST /v1/hooks/file-written).
    from core.remote.file_sync import is_canonical_rel_path
    if not is_canonical_rel_path(rel_path):
        logger.warning("push_back: rejected non-canonical rel_path %r", rel_path)
        return False

    import config
    try:
        host_path = config.get_agent_dir(info.agent_name) / rel_path
    except ValueError:
        logger.warning("push_back: session %s names no agent folder", session_id[:8])
        return False
    st = await _state(session_id)
    src_fd = -1
    lock = await _acquire_global_path_lock(info.agent_name, rel_path)
    event = st.pending_push.setdefault(rel_path, asyncio.Event())
    event.clear()  # block readers until the session's own satellite holds the bytes
    fanout_lock, fanout_held = None, False   # held = ours: a cancel in acquire() releases nothing
    try:
        async with lock:
            # The bytes pushed are those of the file at the path as written,
            # opened under the lock with no component followed: a link
            # swapped in after the hook judged the path (while this push
            # waited on the lock) refuses the push instead of sending
            # another file. The push reads the descriptor's own path.
            try:
                src_fd, src_st = await asyncio.to_thread(
                    _open_platform_copy, info.agent_name, rel_path)
            except OSError as e:
                logger.warning("push_back: %s not pushed: %s", rel_path, e)
                return False
            from core.remote.satellite_connection import get_connection_manager
            from services.infra import safe_fs
            from services.path_policy_v2 import PathRef
            cm = get_connection_manager()
            # The push expects the machine's copy as last seen. Its record
            # goes first: a 0.5.150 ack re-records the committed copy (the
            # next read fast-paths onto it), a failure leaves the next read
            # to pull. The other machines keep theirs until the fan-out
            # below lands: a read there meanwhile still matches the older
            # copy and is served the platform's.
            record = take_push_record(info.machine_id, info.agent_name, rel_path)
            stale: dict = {}
            # The push carries the hash of the descriptor it streams, which
            # the machine checks before its ack (``record_held``). A copy
            # that cannot be read for its hash is pushed as before, with no
            # hash and so no base.
            from core.remote.file_sync import _hash_fd
            content_hash: str | None
            try:
                content_hash = await asyncio.to_thread(_hash_fd, src_fd)
            except OSError as e:
                logger.warning("push_back: %s pushed with no hash to check: %s", rel_path, e)
                content_hash = None
            # Pass a PATH — push_file streams from disk (memory O(chunk)
            # even for 1GB files; unreadable → False with its own warning).
            ok = await cm.push_file(
                info.machine_id,
                PathRef("agent_tree", rel_path),
                Path(safe_fs.fd_path(src_fd)),
                agent_slug=info.agent_name, expect=push_expectation_of(record),
                stale_out=stale, content_hash=content_hash,
            )
            if ok:
                clear_platform_ahead(info.machine_id, info.agent_name, rel_path)
                if content_hash:
                    await record_held(info.machine_id, info.agent_name, rel_path,
                                      content_hash, src_st.st_mtime, src_st.st_size)
            else:
                try:
                    now_st = os.fstat(src_fd)
                except OSError:
                    now_st = src_st
                if stale:
                    # The machine is changing the file: its copy stays, the
                    # platform's is served there and the next merge compares
                    # both. The ack's stat is the baseline, no probe.
                    note_platform_ahead(info.machine_id, info.agent_name, rel_path,
                                        now_st.st_size, now_st.st_mtime_ns,
                                        machine=stale_machine_stat(stale), stale=True)
                    logger.info(
                        "push_back: %s is being changed on machine %s, the platform "
                        "copy stays ahead there until the next merge compares both",
                        rel_path, info.machine_id[:8],
                    )
                else:
                    await note_push_failed(cm, info.machine_id, info.agent_name, rel_path,
                                           now_st.st_size, now_st.st_mtime_ns, fallback=record)
                    logger.warning(
                        "push_back: %s did not reach machine %s, the platform copy "
                        "stays ahead there until a push lands",
                        rel_path, info.machine_id[:8],
                    )
            os.close(src_fd)
            src_fd = -1

            # Readers of the path wait for the session's OWN satellite to hold
            # the bytes, not for the other machines.
            event.set()
            # Cross-satellite fan-out: the same bytes to every OTHER satellite
            # running this agent, so collaborators see a file-tools edit
            # live (not just at their next session start). Its targets are
            # marked in flight before the path lock is released (a read
            # there with no pull record would otherwise pull the older bytes
            # before the fan-out marks them). The fan-out lock is taken
            # INSIDE the path lock (fan-outs of this path keep apply order
            # with the file_changed applier's) and the push runs after the
            # path lock is released, so a slow target never holds the next
            # writer of the path back; best-effort (never raises) and
            # excludes the source machine.
            from services.remote import workspace_fanout
            marked = await workspace_fanout.mark_fan_out_targets(
                info.agent_name, rel_path, exclude_machine_id=info.machine_id)
            fanout_lock = await acquire_fanout_lock(info.agent_name, rel_path)
            await fanout_lock.acquire()
            fanout_held = True
        try:
            await workspace_fanout.fan_out_write(
                info.agent_name, rel_path, host_path,
                exclude_machine_id=info.machine_id, marked=marked,
            )
        finally:
            fanout_held = False
            fanout_lock.release()
        return ok
    finally:
        event.set()
        if src_fd >= 0:
            os.close(src_fd)
        if fanout_held:
            cast("_PathLock", fanout_lock).release()


def cleanup_session(session_id: str) -> None:
    """Drop per-session lock state and purge the satellite-host pull cache.
    Called on session close.

    Safe to call multiple times. WORKSPACE files are NOT removed — they belong
    to the agent across sessions. The ``.remote-host-cache/{session_id}/`` dir,
    however, holds throwaway copies of satellite-host files (e.g. a user's
    ``~/Desktop/foo.png`` pulled in for a Docker MCP this session) and must be
    removed or it leaks a copy per session. The folder is removed off the
    event loop (``file_commit.behind``); the next start of the id waits for it.
    """
    _sessions.pop(session_id, None)
    _fallback_logged.discard(session_id)
    from core import file_commit
    file_commit.behind(_remove_host_cache, session_id,
                       name="host_cache_removal", key=session_id)


def _remove_host_cache(session_id: str) -> None:
    """The session's host cache and its records, each removed beneath its
    root with no link followed (a link in their place is refused, logged)."""
    import config
    from core.remote import remote_file_cache
    from services.infra import safe_fs
    for root, rel in ((_host_cache_root().parent, _cache_rel(host_cache_session_root(session_id))),
                      (config.SESSIONS_DIR, f"{remote_file_cache._host_meta_root().name}/{session_id}")):
        try:
            safe_fs.rmtree_beneath(root, rel, missing_ok=True)
        except OSError as e:
            logger.warning("host cache of session %s left in place: %s", session_id[:8], e)
