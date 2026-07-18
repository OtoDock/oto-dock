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
with a metadata sidecar so write-back targets the original abs_path. The
per-(machine_id, abs_path) write lock prevents concurrent Docker-MCP
edits from clobbering each other.

Public API
----------
- ``pull_through(session_id, rel_path)`` — fetch the satellite copy and write
  to platform's ``AGENTS_DIR/<slug>/<rel_path>``. Returns the host path. Used
  by hook callbacks (``/v1/hooks/file``, ``/v1/hooks/document-preview``,
  ``/v1/hooks/resolve-path``).
- ``push_back(session_id, rel_path)`` — flush a platform-side write back to
  the satellite. Used by ``/v1/hooks/file-written`` after a Docker MCP edits
  a file. Pending readers on the same path block until the push completes
  (write-barrier).
- ``pull_through_host_path(session_id, abs_path)`` — lazy-pull
  a satellite-host absolute path into a session-scoped temp dir; metadata
  sidecar records the (machine_id, abs_path) for push-back.
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
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("claude-proxy.remote-file-flow")


# ---------------------------------------------------------------------------
# Satellite-host path cache + locks
# ---------------------------------------------------------------------------

# Per-(machine_id, abs_path) lock. Held during the satellite-host
# pull→edit→push window so concurrent Docker-MCP edits to the same
# satellite file don't clobber each other.
_machine_host_locks: dict[tuple[str, str], asyncio.Lock] = {}
_machine_host_locks_lock = asyncio.Lock()


async def _acquire_machine_host_lock(
    machine_id: str, abs_path: str,
) -> asyncio.Lock:
    """Return the lock for a (machine_id, abs_path) pair.

    Uses the same normalization as the cache key so equivalent forms
    (case-twin Windows paths, backslash variants, trailing slash) share
    a single lock — without this, concurrent pulls via different case
    forms could clobber each other.
    """
    key = (machine_id, _normalize_abs_path_for_key(abs_path))
    async with _machine_host_locks_lock:
        lock = _machine_host_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _machine_host_locks[key] = lock
        return lock


def _host_cache_root() -> Path:
    """The proxy temp dir for satellite-host pulls. Lives under
    ``AGENTS_DIR/.remote-host-cache/`` so the Docker MCP's ``/agents``
    mount resolves the path without extra mounts.
    """
    import config as _cfg
    return _cfg.AGENTS_DIR / ".remote-host-cache"


def _normalize_abs_path_for_key(abs_path: str) -> str:
    """Normalize a satellite-host path for cache-key purposes.

    Folds backslash → forward-slash, lowercases Windows drive letters,
    strips trailing slash. This makes ``C:\\Users\\...``, ``C:/Users/...``,
    and ``c:/users/...`` all map to the same cache entry — preventing
    concurrent pulls of the "same" file from racing on different cache
    entries.
    """
    s = abs_path.replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    if len(s) > 1 and s.endswith("/"):
        s = s.rstrip("/")
    return s


def _host_cache_paths(
    session_id: str, machine_id: str, abs_path: str,
) -> tuple[Path, Path]:
    """Return ``(cache_path, sidecar_path)`` for a satellite-host pull.

    The sha256 of (machine_id, normalized abs_path) namespaces the
    cache so two paths with the same basename don't collide AND two
    case/slash variants of the same path share one cache entry. The
    sidecar JSON stores the original (un-normalized) abs_path so the
    push-back targets exactly what the satellite expects.
    """
    normalized = _normalize_abs_path_for_key(abs_path)
    digest = hashlib.sha256(
        f"{machine_id}\x00{normalized}".encode("utf-8")
    ).hexdigest()[:32]
    basename = normalized.rsplit("/", 1)[-1] or "_root"
    cache_dir = _host_cache_root() / session_id / digest
    return cache_dir / basename, cache_dir / "_meta.json"


# --- Pull-stat revalidation (satellite ≥ 0.5.95) ---------------------------
#
# Both pull paths historically re-transferred the FULL file on every call —
# correct, but over a WAN/tunnel link every repeated read of the same
# unchanged document costs seconds. With a new-enough satellite we probe
# (size, mtime_ns) via the cheap `file_stat` RPC and serve the cached copy
# when it matches the stat RECORDED AT PULL TIME. Both values come from the
# satellite's own clock/filesystem, so no cross-host clock skew is involved.
#
# Fail-open to the transfer, never to staleness: any doubt — old satellite,
# probe timeout, policy reject, no recorded stat, mismatch, missing cache
# file — falls through to the full pull (the pre-existing behavior). The
# recorded stat is the PRE-pull probe, so a file that changes mid-pull
# mismatches on the next read and re-pulls (wasteful once, never wrong).
# Writes invalidate: push_back drops the record, so a read after a write
# re-pulls (phase 2 — stat-enriched push acks — can tighten that).

# Agent-tree records: (machine_id, agent_slug, rel_path) → stat dict.
# In-memory (lost on restart → first read re-pulls, correct); bounded.
_pull_stat_records: dict[tuple[str, str, str], dict] = {}
_PULL_STAT_RECORDS_MAX = 4096


def _stats_match(recorded: dict | None, fresh: dict | None) -> bool:
    """Exact-match gate for the cache fast path."""
    return (
        isinstance(recorded, dict)
        and isinstance(fresh, dict)
        and bool(fresh.get("exists"))
        and recorded.get("size") == fresh.get("size")
        and recorded.get("mtime_ns") == fresh.get("mtime_ns")
        and recorded.get("size") is not None
        and recorded.get("mtime_ns") is not None
    )


async def _probe_stat(cm, machine_id: str, ref, agent_slug: str = "") -> dict | None:
    """Version-gated `file_stat` probe; None = don't trust the cache.

    Strictly best-effort: ANY failure (old satellite, timeout, disconnect,
    or a bug in the probe path itself) returns None and the caller does the
    full pull — the probe must never be able to break a read."""
    try:
        if not cm.satellite_supports_file_stat(machine_id):
            return None
        res = await cm.stat_file(machine_id, ref, agent_slug=agent_slug)
        # Type-gate the reply: callers `.get()` it, JSON-serialize it into
        # the cache sidecar, and store it in `_pull_stat_records` — anything
        # that isn't a plain dict (malformed ack, test double) must degrade
        # to "no probe" instead of breaking the read downstream.
        return res if isinstance(res, dict) else None
    except Exception as e:
        logger.debug("file_stat probe failed (%s) — falling back to pull", e)
        return None


def _record_agent_stat(key: tuple[str, str, str], stat: dict) -> None:
    if len(_pull_stat_records) >= _PULL_STAT_RECORDS_MAX:
        _pull_stat_records.pop(next(iter(_pull_stat_records)))
    _pull_stat_records[key] = stat


def _drop_agent_stat_all(agent_slug: str, rel_path: str) -> None:
    """Invalidate records for this file on EVERY machine (push fan-out
    rewrites the file on all satellites running the agent)."""
    for k in [k for k in _pull_stat_records
              if k[1] == agent_slug and k[2] == rel_path]:
        _pull_stat_records.pop(k, None)


def is_host_cache_path(host_path: str) -> bool:
    """Does ``host_path`` live in the satellite-host cache subtree?"""
    root = str(_host_cache_root())
    return host_path == root or host_path.startswith(root + "/")


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
    cache_path, sidecar = _host_cache_paths(session_id, info.machine_id, abs_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    lock = await _acquire_machine_host_lock(info.machine_id, abs_path)
    async with lock:
        from core.remote.satellite_connection import get_connection_manager
        from services.path_policy_v2 import PathRef
        cm = get_connection_manager()
        ref = PathRef("satellite_host", abs_path)

        # Revalidation fast path: cached copy + recorded pull-time stat +
        # matching fresh probe → skip the transfer entirely.
        fresh = await _probe_stat(cm, info.machine_id, ref)
        if fresh is not None and cache_path.is_file():
            try:
                recorded = json.loads(sidecar.read_text()).get("stat")
            except (OSError, json.JSONDecodeError):
                recorded = None
            if _stats_match(recorded, fresh):
                return cache_path

        ok = await cm.pull_file_to_path(info.machine_id, ref, cache_path)
        if not ok:
            return None
        # The file itself was already committed atomically (.partial + fsync
        # + rename) by pull_file_to_path. Write the sidecar metadata so a
        # later push-back targets the original abs_path — plus the PRE-pull
        # stat for the next read's revalidation (absent when the probe was
        # unavailable → next read pulls, today's behavior). Sidecar still uses
        # explicit str+`.partial` (not with_suffix) for dot-file edge cases.
        meta = {"machine_id": info.machine_id, "abs_path": abs_path}
        if fresh is not None and fresh.get("exists"):
            meta["stat"] = fresh
        sidecar_partial = Path(str(sidecar) + ".partial")
        sidecar_partial.write_text(json.dumps(meta))
        sidecar_partial.replace(sidecar)
        return cache_path


async def push_back_host_path(session_id: str, cache_path: str) -> bool:
    """Push a satellite-host cache file's bytes back to the satellite.

    Looks up the (machine_id, abs_path) via the sidecar written at
    pull time. Acquires the same per-(machine_id, abs_path) lock to
    serialize concurrent edits. Returns True on satellite ack.
    """
    cp = Path(cache_path)
    sidecar = cp.parent / "_meta.json"
    try:
        meta = json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "push_back_host_path: cannot read sidecar for %s: %s",
            cache_path, e,
        )
        return False
    machine_id = meta.get("machine_id", "")
    abs_path = meta.get("abs_path", "")
    if not machine_id or not abs_path:
        return False
    try:
        content = cp.read_bytes()
    except OSError as e:
        logger.warning("push_back_host_path: cannot read %s: %s", cache_path, e)
        return False

    lock = await _acquire_machine_host_lock(machine_id, abs_path)
    async with lock:
        from core.remote.satellite_connection import get_connection_manager
        from services.path_policy_v2 import PathRef
        cm = get_connection_manager()
        ok = await cm.push_file(
            machine_id, PathRef("satellite_host", abs_path), content,
        )
        if ok and "stat" in meta:
            # The write changed the satellite file's mtime — the recorded
            # pull-time stat is stale. Invalidate it so the next read
            # re-pulls instead of fast-pathing onto a pre-write comparison.
            meta.pop("stat", None)
            try:
                sp = Path(str(sidecar) + ".partial")
                sp.write_text(json.dumps(meta))
                sp.replace(sidecar)
            except OSError as e:
                logger.warning(
                    "push_back_host_path: sidecar stat invalidation failed "
                    "for %s: %s", cache_path, e,
                )
        return ok


# ---------------------------------------------------------------------------
# Global per-(agent_slug, rel_path) workspace write lock
# ---------------------------------------------------------------------------

# Serializes ALL platform-side writes to a given agent-tree file ACROSS sessions
# and machines: pull_through, push_back, the per-turn file_changed applier
# (core/remote/satellite_connection.py), and the active-session fan-out
# (services/remote/workspace_fanout.py) all take this lock keyed by
# (agent_slug, rel_path). Two different sessions (e.g. satellite-A and
# satellite-B running the same collaborative agent) editing the same workspace
# file therefore serialize against each other — last writer wins on a consistent
# byte sequence, never a torn interleave.
#
# Lifecycle: the registry grows by the number of DISTINCT files ever written for
# the life of the process (one tiny asyncio.Lock each) — bounded and acceptable
# for v1; there is no per-session cleanup hook (these locks are NOT session-scoped,
# unlike the pending_push events in _SessionState).
_global_path_locks: dict[tuple[str, str], asyncio.Lock] = {}
_global_path_locks_lock = asyncio.Lock()


async def _acquire_global_path_lock(agent_slug: str, rel_path: str) -> asyncio.Lock:
    """Get-or-create the global per-(agent_slug, rel_path) workspace write lock.

    The SINGLE serialization point for platform-side writes to an agent-tree
    file. Cross-module + cross-session: pull_through / push_back (this module),
    the ``file_changed`` applier (core/remote/satellite_connection.py), and the fan-out
    (services/remote/workspace_fanout.py) all serialize on the same key so concurrent
    writers to one shared workspace file can't clobber each other mid-write.
    """
    key = (agent_slug, rel_path)
    async with _global_path_locks_lock:
        lock = _global_path_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _global_path_locks[key] = lock
        return lock


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
    except Exception:
        info = None
    if info is not None:
        return info
    from core.session.session_state import get_session_security
    ctx = get_session_security(session_id)
    machine_id = getattr(ctx, "target_machine_id", "") if ctx is not None else ""
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


def _workspace_path(agent_slug: str, rel_path: str) -> Path:
    """Compute the platform's host path for (agent, rel_path).

    Mirrors the agent dir layout: ``AGENTS_DIR / <agent_slug> / <rel_path>``.
    """
    import config
    return (config.AGENTS_DIR / agent_slug / rel_path).resolve()


async def pull_through(session_id: str, rel_path: str) -> Path | None:
    """Ensure a platform-local copy of a satellite file exists; return host path.

    For remote sessions, fetches the file via WS into the actual platform
    workspace at ``AGENTS_DIR/<slug>/<rel_path>``. Docker MCPs (file-tools,
    camoufox) and the dashboard workspace listing both see the file at this
    canonical location.

    Returns ``None`` if:
      - the session isn't remote (caller should fall back to local resolution)
      - the satellite is unreachable / file doesn't exist
      - the resolved host path escapes the agent dir (path traversal)

    ``cm.pull_file_to_path`` streams the body to a ``.partial`` and atomically
    renames it into place, so parallel pull_through calls never observe a
    half-written file.
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

    st = await _state(session_id)

    # Wait out any pending push_back for this path so we don't read a stale
    # workspace file that's about to be overwritten by a subsequent push.
    pending = st.pending_push.get(rel_path)
    if pending is not None:
        await pending.wait()

    lock = await _acquire_global_path_lock(info.agent_name, rel_path)
    async with lock:
        host_path = _workspace_path(info.agent_name, rel_path)

        # Path traversal check — `_workspace_path` resolves `..` segments,
        # so a malicious rel_path can't escape AGENTS_DIR/<slug>/.
        import config
        try:
            agent_dir = (config.AGENTS_DIR / info.agent_name).resolve()
            host_path.relative_to(agent_dir)
        except ValueError:
            logger.warning("Path traversal blocked: session=%s path=%s", session_id, rel_path)
            return None

        # Stream the body from the satellite straight into the workspace at
        # host_path (already traversal-checked above). pull_file_to_path
        # commits atomically (.partial + fsync + rename), so Docker MCP code
        # paths (`/agents/...` mount) and the dashboard workspace listing see
        # a complete file — never a torn write.
        from core.remote.satellite_connection import get_connection_manager
        cm = get_connection_manager()
        from services.path_policy_v2 import PathRef
        ref = PathRef("agent_tree", rel_path)

        # Revalidation fast path (satellite ≥ 0.5.95): the workspace copy +
        # the stat recorded at the last pull + a matching fresh probe → the
        # satellite file hasn't changed, serve the workspace copy without
        # re-transferring it.
        key = (info.machine_id, info.agent_name, rel_path)
        fresh = await _probe_stat(cm, info.machine_id, ref,
                                  agent_slug=info.agent_name)
        if (fresh is not None and host_path.is_file()
                and _stats_match(_pull_stat_records.get(key), fresh)):
            return host_path

        ok = await cm.pull_file_to_path(
            info.machine_id,
            ref,
            host_path,
            agent_slug=info.agent_name,
        )
        if not ok:
            return None
        # Record the PRE-pull probe for the next read's revalidation; a probe
        # that was unavailable leaves no record (next read pulls — the
        # pre-0.5.95 behavior).
        if fresh is not None and fresh.get("exists"):
            _record_agent_stat(key, fresh)
        else:
            _pull_stat_records.pop(key, None)
        return host_path


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

    # Same canonical-form gate as pull_through (drive-letter junk etc.).
    from core.remote.file_sync import is_canonical_rel_path
    if not is_canonical_rel_path(rel_path):
        logger.warning("push_back: rejected non-canonical rel_path %r", rel_path)
        return False

    host_path = _workspace_path(info.agent_name, rel_path)
    # Confine to the agent's tree: rel_path comes from POST /v1/hooks/file-written,
    # and a value like '../../config.env' would otherwise be read here and pushed
    # to the satellite (cross-tree exfil). Mirrors pull_through's containment.
    import config
    agent_root = (config.AGENTS_DIR / info.agent_name).resolve()
    if not host_path.is_relative_to(agent_root):
        logger.warning("push_back: rejected out-of-tree rel_path %r", rel_path)
        return False
    if not host_path.is_file():
        return False

    st = await _state(session_id)
    lock = await _acquire_global_path_lock(info.agent_name, rel_path)
    event = st.pending_push.setdefault(rel_path, asyncio.Event())
    event.clear()  # block readers until set() in the finally
    try:
        async with lock:
            try:
                content = host_path.read_bytes()
            except OSError as e:
                logger.warning(
                    "push_back: cannot read %s: %s", host_path, e,
                )
                return False

            from core.remote.satellite_connection import get_connection_manager
            from services.path_policy_v2 import PathRef
            cm = get_connection_manager()
            ok = await cm.push_file(
                info.machine_id,
                PathRef("agent_tree", rel_path),
                content,
                agent_slug=info.agent_name,
            )
            if ok:
                # The write changed the file on the satellite (and the
                # fan-out below rewrites it on every other machine) — the
                # pull-time stats are stale on all of them. Invalidate so
                # the next read re-pulls instead of fast-pathing onto a
                # pre-write comparison.
                _drop_agent_stat_all(info.agent_name, rel_path)

            # Cross-satellite fan-out. The push above reaches
            # the session's OWN satellite; propagate the same bytes to every
            # OTHER satellite running this agent so collaborators see a
            # file-tools edit live (not just at their next session start). The
            # global lock is already held → no interleave; fan-out is
            # best-effort (never raises) and excludes the source machine.
            # NO conflict capture: the Docker MCP already overwrote the platform
            # cache before this hook fired, so the loser's pre-overwrite bytes
            # are unrecoverable here. Last-writer-wins still converges; conflict
            # recovery stays on the file_changed path (which pre-captures).
            from services.remote import workspace_fanout
            await workspace_fanout.fan_out_write(
                info.agent_name, rel_path, content,
                exclude_machine_id=info.machine_id,
            )
            return ok
    finally:
        event.set()


def cleanup_session(session_id: str) -> None:
    """Drop per-session lock state and purge the satellite-host pull cache.
    Called on session close.

    Safe to call multiple times. WORKSPACE files are NOT removed — they belong
    to the agent across sessions. The ``.remote-host-cache/{session_id}/`` dir,
    however, holds throwaway copies of satellite-host files (e.g. a user's
    ``~/Desktop/foo.png`` pulled in for a Docker MCP this session) and must be
    removed or it leaks a copy per session (close_session's docstring claimed
    this cleanup happened, but it was never wired).
    """
    _sessions.pop(session_id, None)
    _fallback_logged.discard(session_id)
    try:
        import shutil
        shutil.rmtree(_host_cache_root() / session_id, ignore_errors=True)
    except Exception as e:
        logger.warning(
            "remote-host-cache cleanup failed for %s: %s", session_id[:8], e,
        )
