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
  ``/v1/hooks/resolve-path``). Only for a path the session may write back
  (``write_back_allowed``); any other is served from the platform copy.
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
from collections import OrderedDict
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("otodock.remote-file-flow")


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


def host_cache_session_root(session_id: str) -> Path:
    """Root of THIS session's lazy-pull host cache. RBAC carve-outs key on
    it: the cache holds only files this session's own path policy admitted
    at pull time, and other sessions' caches stay invisible."""
    return _host_cache_root() / session_id


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


# Platform-ahead markers: the machine holds older bytes than the platform,
# because a push of the platform copy to it failed (``push_back``, a
# fan-out) or is on its way (a fan-out in flight, which runs outside the
# path lock). Without one, ``pull_through`` on that machine could pull the
# older bytes over the platform copy and revert the write (the agent's
# file-tools edit, a person's save). Keyed (machine_id, agent_slug,
# rel_path) and read only for the reading session's own machine: the push
# missed one machine, the others may hold the bytes. Each records the
# platform copy's size and mtime_ns (a later platform write drops it) and
# the machine's stat before the push, as last pulled or probed at the
# failure, None when unknown (a machine whose copy changed since wins: a
# native edit, a merge push, a lost ack). In memory, bounded: a restart
# forgets them and the next merge reconciles.
_platform_ahead: OrderedDict[tuple[str, str, str], dict] = OrderedDict()
_PLATFORM_AHEAD_MAX = 4096
# A failing re-push from a read waits this long before the next read tries.
_REPUSH_BACKOFF_S = 30.0
# The baseline probe at a failure: the machine just missed a push.
_BASELINE_PROBE_S = 3.0


def note_platform_ahead(machine_id: str, agent_slug: str, rel_path: str,
                        size: int, mtime_ns: int, *, in_flight_s: float = 0.0,
                        machine: dict | None = None) -> None:
    """Mark the platform copy (``size``, ``mtime_ns``) ahead on
    ``machine_id``: a push of it failed, or (``in_flight_s``) one is on its
    way for at most that long. ``machine`` is the machine's stat before the
    push when the caller probed it, else the one last pulled. A mark of a
    newer platform copy on the same machine stands (``_mark_current``). A
    mark of the same copy is updated in place: its window, and its baseline
    only when the caller probed one (the mark may already know the machine
    holds bytes its record does not name, ``clear_platform_ahead``)."""
    key = (machine_id, agent_slug, rel_path)
    if not _mark_current(key, (size, mtime_ns)):
        return
    standing = _platform_ahead.get(key)
    if standing is not None and (standing["size"], standing["mtime_ns"]) == (size, mtime_ns):
        if machine is not None:
            standing["machine"] = machine
        standing["in_flight_until"] = time.monotonic() + in_flight_s if in_flight_s else 0.0
        _platform_ahead.move_to_end(key)
        return
    _platform_ahead[key] = {
        "size": size, "mtime_ns": mtime_ns,
        "machine": machine if machine is not None else _pull_stat_records.get(key),
        "in_flight_until": time.monotonic() + in_flight_s if in_flight_s else 0.0,
        "retry_at": 0.0,
    }
    _platform_ahead.move_to_end(key)
    while len(_platform_ahead) > _PLATFORM_AHEAD_MAX:
        _platform_ahead.popitem(last=False)


async def note_push_failed(cm, machine_id: str, agent_slug: str, rel_path: str,
                           size: int, mtime_ns: int) -> None:
    """A push of the platform copy to ``machine_id`` failed: the marker,
    with the machine's stat probed once (the baseline that tells a machine
    still behind from one changed since), the last pulled one when the
    machine does not answer (a record can be older than a change applied
    since). A newer write's mark standing there loses its baseline
    (``_lose_baseline``)."""
    if not _mark_current((machine_id, agent_slug, rel_path), (size, mtime_ns)):
        _lose_baseline((machine_id, agent_slug, rel_path))
        return
    machine = await _probe_baseline(cm, machine_id, agent_slug, rel_path)
    note_platform_ahead(machine_id, agent_slug, rel_path, size, mtime_ns, machine=machine)


def _mark_current(key: tuple[str, str, str], stat: tuple[int, int]) -> bool:
    """Whether a mark write for the platform copy ``stat`` may replace the
    mark standing for ``key``: no mark stands, the standing one is of the
    same copy, or the platform copy is now the one of ``stat`` (the standing
    mark is of an older write). A fan-out of older bytes that is still on
    its way, acks or fails after a newer write marked the machine must not
    overwrite or clear that mark: the next read would pull the machine's
    older bytes over the newer write."""
    marker = _platform_ahead.get(key)
    if marker is None or (marker["size"], marker["mtime_ns"]) == stat:
        return True
    try:
        st = _workspace_path(key[1], key[2]).stat()
    except OSError:
        return False
    return (st.st_size, st.st_mtime_ns) == stat


async def _probe_baseline(cm, machine_id: str, agent_slug: str, rel_path: str) -> dict | None:
    try:
        if not cm.satellite_supports_file_stat(machine_id):
            return None
        from services.path_policy_v2 import PathRef
        res = await cm.stat_file(machine_id, PathRef("agent_tree", rel_path),
                                 agent_slug=agent_slug, timeout=_BASELINE_PROBE_S)
        return res if isinstance(res, dict) else None
    except Exception as e:
        logger.debug("baseline probe failed (%s)", e)
        return None


def clear_platform_ahead(machine_id: str, agent_slug: str, rel_path: str, *,
                         stat: tuple[int, int] | None = None) -> None:
    """A push of the file to ``machine_id`` landed: of the platform copy
    ``stat`` when the caller knows which (a fan-out), which leaves a newer
    write's mark standing, else of the copy as it is (a merge push, a read's
    re-push, under the path lock)."""
    key = (machine_id, agent_slug, rel_path)
    if stat is not None and not _mark_current(key, stat):
        _lose_baseline(key)
        return
    _platform_ahead.pop(key, None)


def _lose_baseline(key: tuple[str, str, str]) -> None:
    """A push of older bytes than the standing mark's acked or failed on the
    machine (an ack can be lost): the machine may hold those bytes now, which
    no baseline names, so a read there serves the platform copy and pushes
    it again instead of taking the machine's copy as a later edit."""
    marker = _platform_ahead.get(key)
    if marker is not None:
        marker["machine"] = None


def end_in_flight(machine_id: str, agent_slug: str, rel_path: str,
                  stat: tuple[int, int]) -> None:
    """A mark of the platform copy ``stat`` set in flight before a fan-out
    that will not push there (the machine left its targets, the fan-out
    ended early): its window ends and the failure's rules apply. A mark of
    another copy is a later write's, left as it is."""
    marker = _platform_ahead.get((machine_id, agent_slug, rel_path))
    if marker is not None and (marker["size"], marker["mtime_ns"]) == stat:
        marker["in_flight_until"] = 0.0


# The re-pushes a read started (held so the loop keeps them).
_repush_tasks: set[asyncio.Task] = set()


async def _repush(cm, machine_id: str, agent_slug: str, rel_path: str, ref,
                  host_path: Path, key: tuple[str, str, str]) -> None:
    """Push the platform copy a read found ahead on ``machine_id``, under the
    path lock and a transfer slot, if the marker still stands then. An ack
    drops the marker and the machine's stat record, a failure keeps the
    marker and backs off."""
    src_fd = -1
    try:
        lock = await _acquire_global_path_lock(agent_slug, rel_path)
        async with lock:
            # The platform copy is opened with no component followed and
            # judged and pushed from that descriptor (push_back's rule).
            try:
                src_fd, src_st = await asyncio.to_thread(_open_platform_copy, agent_slug, rel_path)
            except OSError:
                _platform_ahead.pop(key, None)
                return
            ahead = _standing_platform_ahead(key, host_path, st=src_st)
            if ahead is None:
                return
            from core.remote import transfer_gate
            from core.remote.satellite_file_transfer import push_ceiling_s
            from services.infra import safe_fs
            async with transfer_gate.slot(machine_id, agent_slug, rel_path, ahead["size"]):
                # The window counts from the slot, as a fan-out's does: a
                # push that queued behind others still covers the reads.
                note_platform_ahead(machine_id, agent_slug, rel_path, ahead["size"],
                                    ahead["mtime_ns"], in_flight_s=push_ceiling_s(ahead["size"]))
                ok = await cm.push_file(machine_id, ref, Path(safe_fs.fd_path(src_fd)),
                                        agent_slug=agent_slug)
            if ok:
                _platform_ahead.pop(key, None)
                _pull_stat_records.pop(key, None)
            else:
                _back_off(key, ahead)
    except Exception:
        logger.warning("re-push of %s to machine %s failed", rel_path, machine_id[:8],
                       exc_info=True)
        _back_off(key, _platform_ahead.get(key))
    finally:
        if src_fd >= 0:
            os.close(src_fd)
        _repush_tasks.discard(asyncio.current_task())


def _back_off(key: tuple[str, str, str], pushed: dict | None) -> None:
    """A re-push of the copy ``pushed`` names failed: the mark standing for
    that copy waits its backoff, out of flight (a mark of another copy is a
    later write's, left as it is)."""
    marker = _platform_ahead.get(key)
    if marker is None or pushed is None or (
            (marker["size"], marker["mtime_ns"]) != (pushed["size"], pushed["mtime_ns"])):
        return
    marker["in_flight_until"] = 0.0
    marker["retry_at"] = time.monotonic() + _REPUSH_BACKOFF_S


def _standing_platform_ahead(key: tuple[str, str, str], host_path: Path, *,
                             st: os.stat_result | None = None) -> dict | None:
    """The marker for ``key`` while the platform copy still has the size and
    mtime it had at the push, else None (a later platform write, or the copy
    gone, drops it). ``st``: the copy's stat when the caller holds it open."""
    marker = _platform_ahead.get(key)
    if marker is None:
        return None
    if st is None:
        try:
            st = host_path.stat()
        except OSError:
            st = None
    if st is None or (st.st_size, st.st_mtime_ns) != (marker["size"], marker["mtime_ns"]):
        _platform_ahead.pop(key, None)
        return None
    return marker


def _same_machine_copy(before: dict, now: dict) -> bool:
    """The machine's copy is the one it held before the push: both absent,
    or both there with the same size and mtime (satellite-clock facts)."""
    if not now.get("exists") or not before.get("exists"):
        return not now.get("exists") and not before.get("exists")
    return (before.get("size"), before.get("mtime_ns")) == (now.get("size"), now.get("mtime_ns"))


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

    Only this session's own cache counts, and only toward its own machine:
    ``is_host_cache_path`` is a prefix test, so a path that climbs out of
    the cache (``.remote-host-cache/../<agent>/…``) would otherwise read a
    sidecar the caller synced into its own workspace, naming any machine
    and any absolute path.
    """
    info = _get_remote_session_info(session_id)
    try:
        cp = Path(cache_path).resolve()
        session_root = host_cache_session_root(session_id).resolve()
    except OSError:
        return False
    if info is None or not cp.is_relative_to(session_root) or cp == session_root:
        logger.warning(
            "push_back_host_path: %s is not in session %s's own cache, refused",
            cache_path, session_id[:8],
        )
        return False
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
    if machine_id != info.machine_id:
        logger.warning(
            "push_back_host_path: the sidecar of %s names another machine than "
            "session %s's, refused", cache_path, session_id[:8],
        )
        return False

    lock = await _acquire_machine_host_lock(machine_id, abs_path)
    async with lock:
        from core.remote.satellite_connection import get_connection_manager
        from services.path_policy_v2 import PathRef
        cm = get_connection_manager()
        # Pass the PATH — push_file streams from disk (a vanished/unreadable
        # cache file returns False with its own warning log).
        ok = await cm.push_file(
            machine_id, PathRef("satellite_host", abs_path), cp,
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
class _PathLock(asyncio.Lock):
    """An asyncio.Lock that can say whether anyone is using it: held, or with
    a waiter (a woken waiter stays in ``_waiters`` until its task resumes, so
    a lock in hand-off is never idle)."""

    def idle(self) -> bool:
        return not self.locked() and not self._waiters


# One lock per distinct (agent_slug, rel_path) written through the platform,
# insertion-ordered and bounded: past the bound the oldest IDLE entries are
# evicted. Eviction is safe because of the shape every caller keeps (pinned
# by tests/remote/test_remote_file_flow.py): the lock handed out by
# ``_acquire_global_path_lock`` is entered on the caller's next statement
# with no await in between, and the acquire itself never suspends, so a lock
# that is idle here is referenced by nobody about to enter it. A second map
# with the same rule serializes fan-outs per path (``acquire_fanout_lock``);
# the lock order is the path lock, then the fan-out lock, then the transfer
# gate, and no caller takes them the other way round.
_PATH_LOCKS_MAX = 4096
_global_path_locks: OrderedDict[tuple[str, str], _PathLock] = OrderedDict()
_fanout_locks: OrderedDict[tuple[str, str], _PathLock] = OrderedDict()


def _get_or_create_lock(table: OrderedDict, key: tuple[str, str]) -> _PathLock:
    lock = table.get(key)
    if lock is None:
        lock = _PathLock()
    else:
        del table[key]
    table[key] = lock
    if len(table) > _PATH_LOCKS_MAX:
        # A bounded scan from the oldest end: idle entries go until the table
        # fits; an entry in use stays whatever its age.
        for old_key in list(table.keys())[:64]:
            if len(table) <= _PATH_LOCKS_MAX:
                break
            if old_key != key and table[old_key].idle():
                del table[old_key]
    return lock


async def _acquire_global_path_lock(agent_slug: str, rel_path: str) -> _PathLock:
    """Get-or-create the global per-(agent_slug, rel_path) workspace write lock.

    The SINGLE serialization point for platform-side writes to an agent-tree
    file. Cross-module + cross-session: pull_through / push_back (this module),
    the ``file_changed`` applier (core/remote/satellite_file_transfer.py), and
    the fan-out (services/remote/workspace_fanout.py) all serialize on the same
    key so concurrent writers to one shared workspace file can't clobber each
    other mid-write. Never suspends (the map belongs to the loop thread); the
    caller enters the lock on its next statement (see the note on the map).
    """
    return _get_or_create_lock(_global_path_locks, (agent_slug, rel_path))


async def acquire_fanout_lock(agent_slug: str, rel_path: str) -> _PathLock:
    """The per-(agent_slug, rel_path) lock that serializes fan-outs of one
    file to the other machines. Taken INSIDE the path lock and kept after it
    is released, so same-path fan-outs run in apply order while a slow
    target no longer holds the next writer of the path back. Same shape rule
    as the path lock."""
    return _get_or_create_lock(_fanout_locks, (agent_slug, rel_path))


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
    machine_id = ctx.placement.machine_id if ctx is not None else ""
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
    satellite (0.5.95+): ``{"exists", "size", "mtime_ns"}`` — ``exists`` is
    False for a directory as well as for an absent path. ``None`` when the
    session is not remote, the path is non-canonical, the satellite predates
    the probe, or the probe failed; callers then fall back to
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
    for another agent answers ``None``. ``report_skipped`` adds what a
    satellite that lists them (0.5.145) could not read, as
    ``{"path", "size": 0, "skipped": <why>}`` entries, a folder's with
    ``"dir": True`` (``""`` = the whole tree): a caller that removes what
    the machine lacks keeps those, and the unreadable folder's contents."""
    info = _get_remote_session_info(session_id)
    if info is None or (agent and info.agent_name != agent):
        return None
    from core.remote.remote_workspace_sync import manifest_request
    from core.remote.satellite_connection import get_connection_manager
    cm = get_connection_manager()
    ask_skipped = report_skipped and cm.satellite_supports_skipped_manifest(info.machine_id)
    try:
        ack = await cm.send_command(
            info.machine_id,
            manifest_request(cm, info.machine_id, info.agent_name, report_skipped=ask_skipped),
            timeout=30.0,
        )
    except Exception as e:
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
        if skipped and not ask_skipped:
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


def absence_is_reliable(session_id: str) -> bool:
    """Whether a file missing from the session's machine listing is one the
    machine lacks: false for a Windows satellite whose manifest may leave
    out files it has (the hash bug of 0.5.130 to 0.5.143), where a caller
    must not remove the platform's copy of what the listing lacks."""
    info = _get_remote_session_info(session_id)
    if info is None:
        return False
    from core.remote.satellite_connection import get_connection_manager
    return not get_connection_manager().satellite_absence_unreliable(info.machine_id)


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
    except Exception:
        logger.warning("pull_into: pull of %s failed", rel_path, exc_info=True)
        return False


def _workspace_path(agent_slug: str, rel_path: str) -> Path:
    """Compute the platform's host path for (agent, rel_path).

    Mirrors the agent dir layout: ``AGENTS_DIR / <agent_slug> / <rel_path>``.
    """
    import config
    return (config.AGENTS_DIR / agent_slug / rel_path).resolve()


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
        st = safe_fs.lstat_beneath(config.AGENTS_DIR, f"{agent_slug}/{rel_path}")
    except OSError:
        return None
    return config.AGENTS_DIR / agent_slug / rel_path if stat.S_ISREG(st.st_mode) else None


def held_reason(rel_path: str) -> str:
    """Why a held read found nothing: the path is not on the platform, and
    the session may not bring it there from its machine."""
    return (f"{rel_path} is not on the platform, and this session may not copy "
            "files from its machine into that folder (its role cannot write there)")


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
      - the resolved host path escapes the agent dir (path traversal)

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
        return await asyncio.to_thread(_platform_copy, info.agent_name, rel_path)

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
    early_path = _workspace_path(info.agent_name, rel_path)
    try:
        early_path.relative_to((config.AGENTS_DIR / info.agent_name).resolve())
    except ValueError:
        pass
    else:
        early = _standing_platform_ahead((info.machine_id, info.agent_name, rel_path), early_path)
        if early is not None and time.monotonic() < early["in_flight_until"]:
            return early_path

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
        ahead = _standing_platform_ahead(key, host_path)
        if ahead is not None and time.monotonic() < ahead["in_flight_until"]:
            # A fan-out is pushing the platform copy there now: it is the file.
            return host_path
        fresh = await _probe_stat(cm, info.machine_id, ref,
                                  agent_slug=info.agent_name)
        if ahead is not None:
            machine = ahead["machine"]
            if fresh is not None and machine is not None and not _same_machine_copy(machine, fresh):
                # The machine's copy changed after the missed push: the later
                # write wins, the read pulls it as below.
                logger.warning(
                    "pull_through: %s changed on machine %s after a failed push "
                    "of the platform copy, the machine's copy is taken",
                    rel_path, info.machine_id[:8],
                )
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
                return host_path
        if (fresh is not None and host_path.is_file()
                and _stats_match(_pull_stat_records.get(key), fresh)):
            return host_path

        # The gate once more, right before the bytes move: a library
        # attachment may have turned read-only while this read waited for
        # the lock or the probe.
        if not await write_back_allowed(session_id, info.agent_name, rel_path):
            if held is not None:
                held.append(rel_path)
            return await asyncio.to_thread(_platform_copy, info.agent_name, rel_path)
        # The pull runs while the file moves, however large (its own
        # progress deadline). Its destination is the path as written, never
        # resolved: the pull opens it with no component followed, so a link
        # planted in the platform tree refuses the pull instead of carrying
        # the bytes out of the path the gate judged.
        ok = await cm.pull_file_to_path(
            info.machine_id,
            ref,
            config.AGENTS_DIR / info.agent_name / rel_path,
            agent_slug=info.agent_name,
        )
        if not ok:
            # Serve the platform mirror when the satellite can't provide the
            # file — most commonly a file the PLATFORM ITSELF just wrote
            # (file-tools convert/write) whose flush hasn't landed on the
            # satellite yet (write-then-preview race, live-hit 2026-07-19).
            # Availability over freshness: the preview/display/media read
            # paths prefer last-known bytes + a log line over a hard 400.
            if host_path.is_file():
                logger.info(
                    "pull_through: satellite pull failed for %s — serving "
                    "the platform mirror", rel_path,
                )
                if fallback is not None:
                    fallback.append(rel_path)
                return host_path
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

    # Same canonical-form gate as pull_through (drive-letter junk etc.): the
    # path stays beneath the agent's folder with no `..` (rel_path comes
    # from POST /v1/hooks/file-written).
    from core.remote.file_sync import is_canonical_rel_path
    if not is_canonical_rel_path(rel_path):
        logger.warning("push_back: rejected non-canonical rel_path %r", rel_path)
        return False

    import config
    host_path = config.AGENTS_DIR / info.agent_name / rel_path
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
            # Pass a PATH — push_file streams from disk (memory O(chunk)
            # even for 1GB files; unreadable → False with its own warning).
            ok = await cm.push_file(
                info.machine_id,
                PathRef("agent_tree", rel_path),
                Path(safe_fs.fd_path(src_fd)),
                agent_slug=info.agent_name,
            )
            if ok:
                # The write changed the file on this satellite: its pull-time
                # stat is stale, so the next read there re-pulls instead of
                # fast-pathing onto a pre-write comparison. The other
                # machines keep theirs until the fan-out below lands: a read
                # there meanwhile still matches the older copy and is served
                # the platform's (a changed stat then re-pulls the same bytes).
                _pull_stat_records.pop((info.machine_id, info.agent_name, rel_path), None)
                clear_platform_ahead(info.machine_id, info.agent_name, rel_path)
            else:
                try:
                    st = os.fstat(src_fd)
                except OSError:
                    st = src_st
                await note_push_failed(cm, info.machine_id, info.agent_name, rel_path,
                                       st.st_size, st.st_mtime_ns)
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
            fanout_lock.release()


def _open_platform_copy(agent_slug: str, rel_path: str) -> tuple[int, os.stat_result]:
    """``(fd, stat)`` of the platform copy of a canonical agent-tree path,
    a regular file reached with no component followed (``OSError``
    otherwise). The caller closes the descriptor. Blocking."""
    import config
    from services.infra import safe_fs
    return safe_fs.open_regular_for_read(config.AGENTS_DIR, f"{agent_slug}/{rel_path}")


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
