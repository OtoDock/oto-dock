"""The bookkeeping under the machine file flows: the host cache's paths and locks,
the stats a push expects, the platform-ahead table with its re-push, and the path
and fan-out locks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import time
from collections import OrderedDict
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


def _host_digest(machine_id: str, normalized: str) -> str:
    return hashlib.sha256(f"{machine_id}\x00{normalized}".encode()).hexdigest()[:32]


def _host_basename(normalized: str) -> str:
    return normalized.rsplit("/", 1)[-1] or "_root"


def _host_meta_root() -> Path:
    """Where the host cache's records live: the server's own sessions
    folder, which no session, Docker tool or compute machine writes."""
    import config as _cfg
    return _cfg.SESSIONS_DIR / "host-cache"


def _host_cache_paths(
    session_id: str, machine_id: str, abs_path: str,
) -> tuple[Path, Path]:
    """Return ``(cache_path, meta_path)`` for a satellite-host pull.

    The sha256 of (machine_id, normalized abs_path) namespaces the
    cache so two paths with the same basename don't collide AND two
    case/slash variants of the same path share one cache entry. The
    record (``meta_path``) stores the original (un-normalized) abs_path so
    the push-back targets exactly what the satellite expects. It never
    sits beside the cached file: a session's tools write the cache, and the
    record decides where a write-back lands.
    """
    normalized = _normalize_abs_path_for_key(abs_path)
    digest = _host_digest(machine_id, normalized)
    cache_dir = _host_cache_root() / session_id / digest
    return cache_dir / _host_basename(normalized), _host_meta_root() / session_id / f"{digest}.json"


def _cache_rel(path: Path) -> str:
    """A host cache path as the rel beneath the cache root's parent (the
    path as written, never resolved)."""
    from services.infra import safe_fs
    root = _host_cache_root()
    return safe_fs.rel_under(path, root.parent)


def _make_cache_folder(folder: Path) -> None:
    """The cache folder of one pull, made beneath the cache root's parent
    with no link followed. Blocking."""
    from services.infra import safe_fs
    safe_fs.mkdirs_beneath(_host_cache_root().parent, _cache_rel(folder))


def _cached_regular(cache_path: Path) -> bool:
    """A regular file stands at the cache path, reached with no link."""
    from services.infra import safe_fs
    try:
        return stat.S_ISREG(safe_fs.lstat_beneath(_host_cache_root().parent,
                                                  _cache_rel(cache_path)).st_mode)
    except OSError:
        return False


def host_cache_session_root(session_id: str) -> Path:
    """Root of THIS session's lazy-pull host cache. RBAC carve-outs key on
    it: the cache holds only files this session's own path policy admitted
    at pull time, and other sessions' caches stay invisible."""
    return _host_cache_root() / session_id


# --- Pull-stat revalidation -------------------------------------------------
#
# Over a WAN/tunnel link a full re-transfer of the same unchanged document
# on every read costs seconds. So both pull paths probe the satellite's
# (size, mtime_ns) via the cheap `file_stat` RPC and serve the cached copy
# when it matches the stat RECORDED AT PULL TIME. Both values come from the
# satellite's own clock/filesystem, so no cross-host clock skew is involved.
#
# Fail-open to the transfer, never to staleness: any doubt — probe
# timeout, policy reject, no recorded stat, mismatch, missing cache
# file — falls through to the full pull (the pre-existing behavior). The
# recorded stat is the PRE-pull probe, so a file that changes mid-pull
# mismatches on the next read and re-pulls (wasteful once, never wrong).
# Writes invalidate: push_back drops the record, so a read after a write
# re-pulls (phase 2 — stat-enriched push acks — can tighten that).

# Agent-tree records: (machine_id, agent_slug, rel_path) → stat dict.
# In-memory, bounded. A restart loses them with the lanes and the marks: until
# the pair's next merge a read asks ``platform_copy_unsettled`` before it pulls.
_pull_stat_records: dict[tuple[str, str, str], dict] = {}
# Each machine signed in during this process, with the agents a merge has
# settled on it since.
_merged_agents: dict[str, set[str]] = {}
# The paths a finished merge left to the next one, by (machine, agent): the
# machine could not read them, or a save landed after the plan. A mark would
# have a read push over a copy nobody has compared, so these wait for the merge,
# whose rule keeps the loser.
_merge_left: dict[tuple[str, str], set[str]] = {}
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
    """The `file_stat` probe: None means don't trust the cache.

    Strictly best-effort: ANY failure (timeout, disconnect, or a bug in the
    probe path itself) returns None and the caller does the full pull — the
    probe must never be able to break a read."""
    try:
        res = await cm.stat_file(machine_id, ref, agent_slug=agent_slug)
        # Type-gate the reply: callers `.get()` it, JSON-serialize it into
        # the pull's record, and store it in `_pull_stat_records` — anything
        # that isn't a plain dict (malformed ack, test double) must degrade
        # to "no probe" instead of breaking the read downstream.
        return res if isinstance(res, dict) else None
    except (ValueError, TypeError) as e:
        logger.debug("file_stat probe failed (%s) — falling back to pull", e)
        return None


def note_merge_left(machine_id: str, agent_slug: str, paths: list[str]) -> None:
    """What this merge's plan could not judge, in place of the last merge's: a
    read leaves those platform copies alone until a merge can
    (``platform_copy_unsettled``)."""
    if paths:
        _merge_left[(machine_id, agent_slug)] = set(paths)
    else:
        _merge_left.pop((machine_id, agent_slug), None)


def note_merge_left_one(machine_id: str, agent_slug: str, rel_path: str) -> None:
    """One more for the next merge: a pull that found a save it never planned for."""
    _merge_left.setdefault((machine_id, agent_slug), set()).add(rel_path)


def note_signed_in(machine_id: str) -> None:
    _merged_agents.setdefault(machine_id, set())


def note_merged(machine_id: str, agent_slug: str) -> None:
    _merged_agents.setdefault(machine_id, set()).add(agent_slug)


def forget_merges(machine_id: str) -> None:
    """The machine's lane was forgotten with what it was owed: as after a
    restart, only its next merge settles what the names stood for."""
    _merged_agents[machine_id] = set()


def _platform_digest(agent_slug: str, rel_path: str, host_path: Path) -> str:
    """The platform copy's hash, cached by its stat under ``host_path``'s text
    (a read repeats this under the path lock); ``""`` when no regular file is
    there reached with no link, never waiting on a pipe (``_open_platform_copy``'s
    rule)."""
    import config
    from core.remote.file_sync import _hash_listed
    try:
        st = os.lstat(host_path)
        if not stat.S_ISREG(st.st_mode):
            return ""
        return _hash_listed(config.AGENTS_DIR, f"{agent_slug}/{rel_path}", host_path, st,
                            max_size=None)[0]
    except OSError:
        return ""


async def platform_copy_unsettled(key: tuple[str, str, str], host_path: Path) -> bool:
    """Whether a read must not pull over the platform copy, though no record says
    why: the process restarted, and with it went the names and marks that would
    have told this read a save has not reached the machine, or the last merge
    left this path to the next one (``note_merge_left``). So until the pair's
    next finished merge, a platform copy that differs from the machine's merge
    base (or has none) may hold a change the machine lacks, and the read serves
    it. Nothing is written, on either side: if the machine changed the file too,
    the merge's conflict rule still sees both. A copy that matches the base
    changed nowhere on the platform, so a pull loses nothing."""
    from storage.files import sync_state_store
    from storage.pg import run_db
    machine_id, agent_slug, rel_path = key
    if rel_path not in _merge_left.get((machine_id, agent_slug), ()):
        merged = _merged_agents.get(machine_id)
        if merged is None or agent_slug in merged or key in _pull_stat_records:
            return False
    digest = await asyncio.to_thread(_platform_digest, agent_slug, rel_path, host_path)
    if not digest:
        return False
    base = await run_db(sync_state_store.get_one, machine_id, agent_slug, rel_path)
    return base is None or base[0] != digest


async def delete_stands(cm, key: tuple[str, str, str], host_path: Path, fresh: dict | None) -> bool:
    """Whether a read must not bring back a file deleted here: no platform copy,
    a tombstone stands, and the machine's copy is not provably made after it,
    the merge's own rule (``file_sync``). The machine can keep a copy past the
    delete, and a pull would make two matching copies, which the next merge reads
    as a reason to retire the tombstone. A hold is not a delete."""
    from core.remote.file_sync import (
        CLOCK_JITTER_MARGIN_SECONDS,
        _adjusted_satellite_mtime,
    )
    from storage.files import file_tombstones_store
    from storage.pg import run_db
    machine_id, agent_slug, rel_path = key
    if await asyncio.to_thread(os.path.lexists, host_path):
        return False
    row = await run_db(file_tombstones_store.get, agent_slug, rel_path)
    if row is None or row["origin"] == file_tombstones_store.HOLD_ORIGIN:
        return False
    if not fresh or not fresh.get("exists") or not isinstance(fresh.get("mtime_ns"), int):
        return True
    made_at = _adjusted_satellite_mtime(fresh["mtime_ns"] / 1e9, cm.get_clock_offset(machine_id))
    return made_at is None or made_at <= row["deleted_at_mtime"] + CLOCK_JITTER_MARGIN_SECONDS


def _record_agent_stat(key: tuple[str, str, str], stat: dict) -> None:
    if len(_pull_stat_records) >= _PULL_STAT_RECORDS_MAX:
        _pull_stat_records.pop(next(iter(_pull_stat_records)))
    _pull_stat_records[key] = stat


def record_pushed_stat(machine_id: str, agent_slug: str, rel_path: str,
                       size: int, mtime_ns: int) -> None:
    """A push landed and the machine (0.5.150+) answered the committed
    file's stat: recorded as the machine's copy, so a read fast-paths onto
    it and the next push of the path expects exactly it
    (``take_push_record``), never refused as a live write of the platform's
    own previous push."""
    _record_agent_stat((machine_id, agent_slug, rel_path),
                       {"exists": True, "size": size, "mtime_ns": mtime_ns})


def take_push_record(machine_id: str, agent_slug: str, rel_path: str) -> dict | None:
    """The machine's copy of ``rel_path`` as last pulled, probed or pushed,
    taken off the records before a push of the path: the push expects it
    (``push_expectation_of``), the ack re-records the committed copy, a
    failed push leaves the next read to pull (the caller keeps it as the
    mark's fallback baseline, ``note_push_failed``)."""
    return _pull_stat_records.pop((machine_id, agent_slug, rel_path), None)


def push_expectation_of(stat: dict | None) -> dict | str:
    """The ``expect`` a stat record makes: its size and mtime_ns, or
    ``"live"`` for a record without both."""
    if (isinstance(stat, dict) and isinstance(stat.get("size"), int)
            and isinstance(stat.get("mtime_ns"), int)):
        return {"size": stat["size"], "mtime_ns": stat["mtime_ns"]}
    return "live"


def stale_machine_stat(stale: dict) -> dict | None:
    """The machine's copy as a stale ack reported it (``push_file``'s
    ``stale_out``), in the shape of a pull-stat record: the baseline of a
    platform-ahead mark. None when the ack carried no stat."""
    size, mtime_ns = stale.get("size"), stale.get("mtime_ns")
    if isinstance(size, int) and isinstance(mtime_ns, int) and not isinstance(size, bool):
        return {"exists": True, "size": size, "mtime_ns": mtime_ns}
    return None


# The satellite's ``_EXPECT_MTIME_SLACK_NS``: a manifest's mtime is a float of seconds.
_MANIFEST_MTIME_SLACK_NS = 1_000

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
                        machine: dict | None = None, stale: bool = False) -> None:
    """Mark the platform copy (``size``, ``mtime_ns``) ahead on
    ``machine_id``: a push of it failed, or (``in_flight_s``) one is on its
    way for at most that long. ``machine`` is the machine's stat before the
    push when the caller probed it, else the one last pulled. ``stale``: the
    machine refused the push because it is changing the file; should its
    copy win later, the platform copy (a person's save) is kept in the
    recover bin first. A mark of a newer platform copy on the same machine
    stands (``_mark_current``). A mark of the same copy is updated in place:
    its window, and its baseline only when the caller probed one (the mark
    may already know the machine holds bytes its record does not name,
    ``clear_platform_ahead``)."""
    key = (machine_id, agent_slug, rel_path)
    if not _mark_current(key, (size, mtime_ns)):
        return
    standing = _platform_ahead.get(key)
    if standing is not None and (standing["size"], standing["mtime_ns"]) == (size, mtime_ns):
        if machine is not None:
            standing["machine"] = machine
        if stale:
            standing["stale"] = True
        standing["in_flight_until"] = time.monotonic() + in_flight_s if in_flight_s else 0.0
        _platform_ahead.move_to_end(key)
        return
    _platform_ahead[key] = {
        "size": size, "mtime_ns": mtime_ns,
        "machine": machine if machine is not None else _pull_stat_records.get(key),
        "in_flight_until": time.monotonic() + in_flight_s if in_flight_s else 0.0,
        "retry_at": 0.0,
        "stale": stale,
    }
    _platform_ahead.move_to_end(key)
    while len(_platform_ahead) > _PLATFORM_AHEAD_MAX:
        _platform_ahead.popitem(last=False)


def stale_hold(machine_id: str, agent_slug: str, rel_path: str, *,
               platform_hash: str, base_hash: str | None) -> bool:
    """Whether the platform copy of ``rel_path`` is a save ``machine_id``
    refused as stale (it was changing the file) and never held. The merge
    that then takes the machine's copy keeps the platform's in the recover
    bin first and tells its author. A read that takes it keeps the copy and
    tells nobody (``capture_losing_platform_copy``). The mark is in memory
    and outlives the push it was made for: a merge that finds both copies
    equal, or the machine's own write reaching the platform, brings the two
    sides together and leaves it. So the save stands only while the
    platform copy (``platform_hash``) is not the machine's merge base
    (``base_hash``, the copy they last held in common): a copy that is the
    base reached the machine before its change, the two saves followed each
    other, and nothing is lost. With no base, or a platform hash that could
    not be read (``""``), the save stands."""
    marker = _platform_ahead.get((machine_id, agent_slug, rel_path))
    return bool(marker and marker.get("stale")) and (not platform_hash or platform_hash != base_hash)


async def refused_save_stands(key: tuple[str, str, str], host_path: Path) -> bool:
    """``stale_hold`` for a read, which has no manifest: the platform copy's
    hash and the machine's merge base are read here."""
    from storage.files import sync_state_store
    from storage.pg import run_db
    machine_id, agent_slug, rel_path = key
    digest = await asyncio.to_thread(_platform_digest, agent_slug, rel_path, host_path)
    base = await run_db(sync_state_store.get_one, machine_id, agent_slug, rel_path)
    return stale_hold(machine_id, agent_slug, rel_path, platform_hash=digest,
                      base_hash=base[0] if base else None)


async def capture_losing_platform_copy(agent_slug: str, rel_path: str, why: str) -> bool:
    """A save the machine refused as stale, or a merge could not deliver, loses
    to the copy the machine changed: the platform copy goes to the recover bin (reason conflict)
    before the machine's bytes replace it, whatever the merge's own capture
    rule says for the path (a person's own file, a same-user divergence).
    Best effort: True when an entry was made."""
    import config
    from core.remote.file_sync import MAX_FILE_SIZE
    from services.infra import safe_fs
    from storage.files import recover_bin_store

    def _capture() -> dict | None:
        try:
            data = safe_fs.read_bytes_beneath(
                config.AGENTS_DIR, f"{agent_slug}/{rel_path}", max_size=MAX_FILE_SIZE)
        except OSError:
            return None
        if not data:
            return None
        return recover_bin_store.capture(agent_slug, rel_path, data, "conflict")

    try:
        entry = await asyncio.to_thread(_capture)
    except Exception:  # noqa: BLE001  # a safety net on the way to a pull must never stop the pull
        logger.warning("recover-bin capture of %s failed", rel_path, exc_info=True)
        return False
    if entry is not None:
        logger.info("the platform copy of %s is kept in the recover bin: %s", rel_path, why)
    return entry is not None


# The push ack's third answer beside ``ok`` and ``error`` (0.5.150): the
# machine left the file alone because it changed under the platform.
PUSH_ACK_STALE = "stale"


def record_committed_stat(machine_id: str, agent_slug: str, rel_path: str, ack: dict) -> None:
    """The committed file's stat a 0.5.150 satellite puts on a write's
    ``ok`` ack becomes the machine's copy on record; an older satellite's
    bare ack records nothing."""
    size, mtime_ns = ack.get("size"), ack.get("mtime_ns")
    if not (isinstance(size, int) and isinstance(mtime_ns, int)) or isinstance(size, bool):
        return
    record_pushed_stat(machine_id, agent_slug, rel_path, size, mtime_ns)


async def record_held(machine_id: str, agent_slug: str, rel_path: str,
                      content_hash: str, mtime: float, size: int) -> None:
    """``machine_id`` and the platform both hold ``content_hash`` of
    ``rel_path`` (``size`` bytes), proven by the transfer itself: the bytes
    an acked push carried (the machine checks them against the hash before
    its ack) or the bytes a pull received from the machine. That copy
    becomes the pair's merge base, as the push lane's acked push records it,
    so a change the machine makes after it reads as one that followed it,
    not as a clash. A push that was refused, failed or lost its ack proves
    nothing and records nothing.

    A file larger than the recover bin can keep (``RECOVER_BIN_MAX_BYTES``)
    gets no base here: the base would also let the machine's later delete
    of it reach the platform, which keeps no copy of such a file, and what
    that delete should do is not decided. Such a file merges as it did
    before these three callers recorded a base.

    Best effort: a store that does not answer leaves the base to the next
    merge."""
    import psycopg

    import config
    from storage.files import sync_state_store
    from storage.pg import run_db
    if size > config.RECOVER_BIN_MAX_BYTES:
        logger.debug("no merge base for %s on machine %s: %d bytes, past the recover bin's cap",
                     rel_path, machine_id[:8], size)
        return
    try:
        await run_db(sync_state_store.record_one, machine_id, agent_slug, rel_path,
                     content_hash, mtime)
    except psycopg.Error as e:
        logger.warning("the merge base of %s on machine %s was not recorded: %s",
                       rel_path, machine_id[:8], e)


async def note_push_failed(cm, machine_id: str, agent_slug: str, rel_path: str,
                           size: int, mtime_ns: int, *, fallback: dict | None = None,
                           stale: bool = False) -> None:
    """A push of the platform copy to ``machine_id`` failed: the marker,
    with the machine's stat probed once (the baseline that tells a machine
    still behind from one changed since), the last pulled one when the
    machine does not answer (a record can be older than a change applied
    since; ``fallback`` is the record the push took, ``take_push_record``).
    A newer write's mark standing there loses its baseline
    (``_lose_baseline``)."""
    if not _mark_current((machine_id, agent_slug, rel_path), (size, mtime_ns)):
        _lose_baseline((machine_id, agent_slug, rel_path))
        return
    machine = await _probe_baseline(cm, machine_id, agent_slug, rel_path)
    if machine is None:
        machine = fallback
    note_platform_ahead(machine_id, agent_slug, rel_path, size, mtime_ns, machine=machine, stale=stale)


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
    except (OSError, ValueError):
        return False
    return (st.st_size, st.st_mtime_ns) == stat


async def _probe_baseline(cm, machine_id: str, agent_slug: str, rel_path: str) -> dict | None:
    try:
        from services.path_policy_v2 import PathRef
        res = await cm.stat_file(machine_id, PathRef("agent_tree", rel_path),
                                 agent_slug=agent_slug, timeout=_BASELINE_PROBE_S)
        return res if isinstance(res, dict) else None
    except (ImportError, ValueError, TypeError) as e:
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
                # The push expects the copy the mark knows: the machine's
                # record, else the baseline probed at the failure. A copy
                # changed since is refused as stale and wins at the next read.
                record = take_push_record(machine_id, agent_slug, rel_path)
                expect = push_expectation_of(record if record is not None else ahead.get("machine"))
                # The push carries the hash of the descriptor it streams,
                # which the machine checks before its ack (``record_held``).
                from core.remote.file_sync import _hash_fd
                content_hash = await asyncio.to_thread(_hash_fd, src_fd)
                ok = await cm.push_file(machine_id, ref, Path(safe_fs.fd_path(src_fd)),
                                        agent_slug=agent_slug, expect=expect,
                                        content_hash=content_hash)
            if ok:
                _platform_ahead.pop(key, None)
                await record_held(machine_id, agent_slug, rel_path, content_hash,
                                  src_st.st_mtime, src_st.st_size)
            else:
                _back_off(key, ahead)
    except Exception:  # noqa: BLE001  # a re-push task nobody awaits: a raise would vanish unlogged
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
    or both there with the same size and mtime (satellite-clock facts). A
    baseline read off a manifest has its mtime rounded, by the satellite's own
    slack: read as a change, the older bytes would be pulled over the save."""
    if not now.get("exists") or not before.get("exists"):
        return not now.get("exists") and not before.get("exists")
    then, seen = before.get("mtime_ns"), now.get("mtime_ns")
    if not isinstance(then, int) or not isinstance(seen, int):
        return (before.get("size"), then) == (now.get("size"), seen)
    return before.get("size") == now.get("size") and abs(then - seen) <= _MANIFEST_MTIME_SLACK_NS


def is_host_cache_path(host_path: str) -> bool:
    """Does ``host_path`` live in the satellite-host cache subtree?"""
    root = str(_host_cache_root())
    return host_path == root or host_path.startswith(root + "/")


#: A host cache record is a few hundred bytes.
_META_MAX_BYTES = 64 * 1024


def _write_host_meta(meta_path: Path, meta: dict) -> None:
    """A pull's record, written in one rename beneath the sessions folder.
    Blocking."""
    import config as _cfg
    from services.infra import safe_fs
    safe_fs.atomic_write_beneath(_cfg.SESSIONS_DIR, safe_fs.rel_under(meta_path, _cfg.SESSIONS_DIR),
                                 json.dumps(meta).encode(), mkdirs=True, fsync=False)


def _read_host_meta(meta_path: Path) -> dict | None:
    """A pull's record, None when there is none to read. Blocking."""
    import config as _cfg
    from services.infra import safe_fs
    try:
        meta = json.loads(safe_fs.read_bytes_beneath(
            _cfg.SESSIONS_DIR, safe_fs.rel_under(meta_path, _cfg.SESSIONS_DIR),
            max_size=_META_MAX_BYTES))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _read_old_host_meta(cache_path: Path) -> dict | None:
    """The record a release before this one kept beside the cached file
    (``_meta.json``), read once with no link followed. The caller accepts it
    only when it names this folder's own machine path. Blocking."""
    from services.infra import safe_fs
    try:
        meta = json.loads(safe_fs.read_bytes_beneath(
            _host_cache_root().parent, _cache_rel(cache_path.parent / "_meta.json"),
            max_size=_META_MAX_BYTES))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def host_cache_entry(cache_path: str, session_id: str) -> tuple[str, str] | None:
    """``(digest, name)`` when ``cache_path``, as written, is a file of this
    session's own host cache (``<root>/<session>/<digest>/<name>``); None
    for anything else, a path that climbs out included."""
    try:
        rel = _cache_rel(Path(cache_path))
    except (OSError, ValueError):
        return None
    parts = rel.split("/")
    if len(parts) != 4 or parts[0] != _host_cache_root().name or parts[1] != session_id:
        return None
    return parts[2], parts[3]


def host_meta_matches(meta: dict, machine_id: str, digest: str, name: str) -> str:
    """The record's machine path when it belongs to this cache file: its
    machine is ``machine_id`` and its path is the one the folder's digest
    and the file's name were made from; ``""`` otherwise."""
    abs_path = meta.get("abs_path")
    if meta.get("machine_id") != machine_id or not isinstance(abs_path, str) or not abs_path:
        return ""
    normalized = _normalize_abs_path_for_key(abs_path)
    if _host_digest(machine_id, normalized) != digest or _host_basename(normalized) != name:
        return ""
    return abs_path


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


def _workspace_path(agent_slug: str, rel_path: str) -> Path:
    """Compute the platform's host path for (agent, rel_path): the agent's
    folder (``config.get_agent_dir``, which refuses a name that is no agent
    folder with ``PathOutsideRoot``) / ``rel_path``, resolved."""
    import config
    return (config.get_agent_dir(agent_slug) / rel_path).resolve()


def _open_platform_copy(agent_slug: str, rel_path: str) -> tuple[int, os.stat_result]:
    """``(fd, stat)`` of the platform copy of a canonical agent-tree path,
    a regular file reached with no component followed (``OSError``
    otherwise). The caller closes the descriptor. Blocking."""
    import config
    from services.infra import safe_fs
    return safe_fs.open_regular_for_read(config.AGENTS_DIR, f"{agent_slug}/{rel_path}")
