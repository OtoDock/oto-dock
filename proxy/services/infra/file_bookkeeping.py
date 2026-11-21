"""Platform-side bookkeeping for a file write / delete — the invariants every
writer path shares (the dashboard file API, the Direct-LLM builtin file
tools, recover-bin restore, knowledge-library rename projection):

- WRITE (``push_file_write`` / ``push_tree_write``): retire any delete
  tombstone (the path is live again), record the author for cross-user
  conflict attribution, fire the knowledge-library projection, then push the
  bytes to active remote sessions through the isolation-aware fan-out.
- DELETE (``delete_platform_file``): capture the bytes in the Recover bin
  (under the size cap), unlink, write the tombstone + clear the author (so an
  idle satellite APPLIES the delete instead of resurrecting the file), fan
  the delete out. A Direct LLM session's Delete is judged in the session's
  sandbox first and hands over what the sandbox saw of the file (``expect``):
  the file is removed only when the look here shows the same file.
- AGING OUT (``retire_aged_file_sync``, the retention sweep's working-files
  pass): the same tombstone, the capture refused rather than skipped, no
  fan-out.
- A SESSION'S OWN DELETE (``_scan_agent_sync``, at each turn's end of a session
  working on the platform's own files): the tombstone a sandbox's ``rm`` cannot
  write, for the paths a paired machine holds, so the file does not come back
  from that machine.

Every filesystem step opens beneath ``AGENTS_DIR`` through ``safe_fs`` with
the agent's name as the first component (SAFE-FS.md): a walk never follows a
link, a capture reads the file it lists, a delete removes the name it checked.
The ``*_sync`` twins carry the store half for a caller already in a worker
thread (the files routes run their whole filesystem step in one thread).

Moved out of ``api/agents/files.py`` (Plan B, 2026-09) so the Direct-LLM
builtins run the identical sequence instead of a second copy. Every caller
goes through THIS module (no private re-exports), so one monkeypatch point
covers them all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import config
from core import layout
from core.background import spawn
from core.session import session_events
from services.infra import safe_fs
from storage.pg import run_db

if TYPE_CHECKING:
    from auth.path_policy import SecurityContext

logger = logging.getLogger("otodock.agents")


def _rel_of(agent_dir: Path, path: Path) -> str | None:
    """The agent-relative form of ``path`` (a Path under ``agent_dir``, as
    joined or as resolved), judged on the text; None for the agent dir
    itself or a path outside it."""
    try:
        rel = safe_fs.rel_under(path, os.path.realpath(agent_dir))
    except OSError:
        try:
            rel = safe_fs.rel_under(path, agent_dir)
        except OSError:
            return None
    return rel or None


def _root_of(agent_dir: Path) -> tuple[Path, str]:
    """``(agents root, agent name)`` for a caller's ``agent_dir``
    (``AGENTS_DIR/<slug>`` as joined, never resolved): the root the helpers
    open and the first component of every rel, so a linked agent folder is
    refused at the open."""
    return Path(agent_dir).parent, Path(agent_dir).name


def walk_files_beneath(agent_dir: Path, rel: str) -> list[str]:
    """Agent-relative paths of the regular files at or under ``rel`` (a file
    or a directory of the agent's tree), read from a walk beneath the agents
    root that never follows a link and never lists one. Blocking: a
    worker-thread call."""
    root, name = _root_of(agent_dir)
    top = f"{name}/{rel}"
    st = safe_fs.lstat_beneath(root, top)
    if stat.S_ISREG(st.st_mode):
        return [rel]
    if not stat.S_ISDIR(st.st_mode):
        return []
    out: list[str] = []
    for step in safe_fs.walk_beneath(root, top):
        base = step.rel[len(name) + 1:]
        out.extend(f"{base}/{n}" if base else n for n in step.files)
    return out


async def _has_candidates(agent_slug: str, rel_path: str) -> bool:
    """``has_fanout_candidates`` with the Shared-only read done on the DB
    lane (only a ``users/`` path asks it)."""
    from services.remote import workspace_fanout
    shared_only = None
    if layout.is_personal(rel_path):
        from core.session.visibility import is_shared_only
        shared_only = await run_db(is_shared_only, agent_slug)
    return workspace_fanout.has_fanout_candidates(
        agent_slug, rel_path, include_idle=True, shared_only=shared_only,
    )


async def record_platform_write(agent_slug: str, rel_path: str, writer: str | None) -> None:
    """Versioned-sync bookkeeping for a platform-side write: retire any tombstone
    (the path is live again) and record the author (username slug) for cross-user
    conflict attribution. Best-effort; runs regardless of remote targets."""
    from storage.files import file_author_store, file_tombstones_store
    await asyncio.to_thread(file_tombstones_store.drop, agent_slug, rel_path)
    if writer:
        await asyncio.to_thread(file_author_store.record, agent_slug, rel_path, writer)
    schedule_library_projection(agent_slug, rel_path, deleted=False)


def tombstone_path_sync(agent_slug: str, rel_path: str, *, origin: str = "dashboard") -> None:
    """The store half of ``tombstone_path``: the tombstone row and the author
    cleared, for a caller already in a worker thread (the projection is the
    caller's, on the loop). ``origin`` says which delete wrote it: the sweep
    and the end-of-turn scan name themselves."""
    from storage.files import file_author_store, file_tombstones_store
    file_tombstones_store.record(agent_slug, rel_path, time.time(), origin=origin)
    file_author_store.clear(agent_slug, rel_path)


async def tombstone_path(agent_slug: str, rel_path: str) -> None:
    """Record a delete tombstone + forget the author for one platform file path,
    so an idle satellite APPLIES the delete (never resurrects it) at next sync."""
    await asyncio.to_thread(tombstone_path_sync, agent_slug, rel_path)
    schedule_library_projection(agent_slug, rel_path, deleted=True)


def schedule_library_projection(agent_slug: str, rel_path: str, *, deleted: bool) -> None:
    """Fire-and-forget knowledge-library projection for a platform write /
    delete (rename and move decompose into exactly these two): a promoted
    source's knowledge change reaches every consumer mirror; an RW mirror
    change flows back to its source. Platform deletes of RW mirror files
    are the one EXPLICIT mirror→source delete channel — satellite/absence
    deletes never propagate (they heal). Cheap no-op off knowledge/."""
    if not rel_path.startswith("knowledge/"):
        return
    from services.knowledge import library_projector
    parsed = library_projector.parse_library_rel(rel_path)
    if parsed is not None:
        src, sub_rel = parsed
        if not sub_rel:
            return
        if deleted:
            spawn(library_projector.propagate_mirror_delete(agent_slug, src, sub_rel),
                  name="propagate_mirror_delete", owner="file_bookkeeping")
        else:
            spawn(library_projector.propagate_mirror_write(agent_slug, src, sub_rel),
                  name="propagate_mirror_write", owner="file_bookkeeping")
        return
    knowledge_rel = rel_path[len("knowledge/"):]
    if knowledge_rel:
        spawn(library_projector.propagate_source_write(
                agent_slug, knowledge_rel, deleted=deleted),
              name="propagate_source_write", owner="file_bookkeeping")


def tombstone_subtree_sync(agent_slug: str, agent_dir: Path, rel: str) -> list[str]:
    """Tombstone every regular file at or under ``rel`` (agent-relative) from
    a worker thread; returns their paths so the caller schedules the
    projections on the loop. A source that cannot be walked yields nothing."""
    try:
        files = walk_files_beneath(agent_dir, rel)
    except OSError:
        return []
    for f in files:
        tombstone_path_sync(agent_slug, f)
    return files


async def tombstone_subtree(agent_slug: str, agent_dir: Path, src: Path) -> None:
    """Tombstone every file under ``src`` (a file or dir) BEFORE it is deleted /
    moved / renamed on disk — so an idle satellite removes the old path(s) instead
    of resurrecting them. Per-file (a directory has no file hash to key on)."""
    rel = _rel_of(agent_dir, src)
    if rel is None:
        return
    try:
        files = await asyncio.to_thread(walk_files_beneath, agent_dir, rel)
    except OSError:
        return
    for f in files:
        await tombstone_path(agent_slug, f)


async def push_file_write(
    agent_slug: str, rel_path: str, host_path: Path, *, writer: str | None = None,
) -> None:
    """Publish a written/created FILE: record platform authorship + retire any
    tombstone, then push to active remote sessions so a platform edit reaches the
    satellite immediately — not only at the next end-of-turn manifest sync.

    Routes the push through ``services/remote/workspace_fanout`` so the SAME per-user /
    per-role isolation that gates session-start sync applies here too: a write
    under ``users/{alice}/`` or ``config/`` only reaches machines whose active
    session is allowed to see it. The author/tombstone bookkeeping runs even when
    no remote session is active (it's platform state, not a push). The Path is
    handed on: the fan-out opens the platform copy beneath the agents root and
    streams from that descriptor (nothing is read whole here)."""
    await record_platform_write(agent_slug, rel_path, writer)
    if not await _has_candidates(agent_slug, rel_path):
        return
    from services.remote import workspace_fanout
    await workspace_fanout.fan_out_write(agent_slug, rel_path, host_path, include_idle=True)


async def push_file_delete(agent_slug: str, rel_path: str) -> None:
    """Push a delete (file or dir) to active remote sessions of this agent, via
    the isolation-aware fan-out (reaches only allowed machines). The delete
    tombstone is written separately at the delete source (per file)."""
    from services.remote import workspace_fanout
    await workspace_fanout.fan_out_delete(agent_slug, rel_path, include_idle=True)


async def push_tree_write(
    agent_slug: str, root: Path, agent_dir: Path, *, writer: str | None = None,
) -> None:
    """Publish a written FILE — or every file under a moved/copied DIRECTORY: record
    platform authorship + retire any tombstone per file, then fan out to active
    remote sessions so a platform move/copy reaches the satellite immediately
    instead of only at the next manifest sync. The walk never follows a link
    (a link is not published); each file is fanned out with per-file
    isolation from the platform copy's descriptor. Best-effort."""
    rel = _rel_of(agent_dir, root)
    if rel is None:
        return
    try:
        files = await asyncio.to_thread(walk_files_beneath, agent_dir, rel)
    except OSError as e:
        logger.warning("Cannot walk %s/%s for satellite push: %s", agent_slug, rel, e)
        return
    from services.remote import workspace_fanout
    for f in files:
        await record_platform_write(agent_slug, f, writer)
        if not await _has_candidates(agent_slug, f):
            continue
        await workspace_fanout.fan_out_write(
            agent_slug, f, Path(agent_dir) / f, include_idle=True,
        )


#: What tells one file from every other, and from itself once changed: its
#: size, its inode number and its change time in nanoseconds. The device
#: number is left out: two mount namespaces can show one file under two.
FileFacts = tuple[int, int, int]


def _facts_of(st: os.stat_result) -> FileFacts:
    return (st.st_size, st.st_ino, st.st_ctime_ns)


def _delete_file_sync(agent_slug: str, agent_dir: Path, rel: str,
                      expect: FileFacts | None = None) -> bool:
    """Capture (under the cap), unlink and tombstone ONE regular file at
    ``rel`` beneath the agents root; a link or a special file at the name is
    ``FileNotFoundError`` (never captured, never removed). The name's folder
    is opened once and held: the look, the capture and the removal are made
    in that one folder, whatever is moved around it meanwhile.

    ``expect`` is what a session's sandbox saw of the file it judged. With
    it, the file here must be that file: on the descriptor the capture is
    read from, before a byte of it is kept, and by the name once more right
    before the removal. Anything else is ``FileNotFoundError`` and nothing is
    removed. Nothing looks again between that last look and the removal: a
    close and an open of the held folder lie between them. A file over the cap is not captured, so only the last look
    runs for it. A file that changes between its capture and the last look
    is not removed, and its captured copy stays in the Recover bin.

    Returns True when the capture was skipped for size."""
    from storage.files import recover_bin_store
    root, name = _root_of(agent_dir)
    top = f"{name}/{rel}"
    folder_rel, _, leaf = top.rpartition("/")

    def judged(st: os.stat_result) -> None:
        if expect is not None and _facts_of(st) != expect:
            raise FileNotFoundError(top)

    with safe_fs.open_root(root, folder_rel) as folder:
        st = safe_fs.lstat_beneath(folder, leaf)
        if not stat.S_ISREG(st.st_mode):
            raise FileNotFoundError(top)
        cap = config.RECOVER_BIN_MAX_BYTES
        bin_skipped = st.st_size > cap
        if not bin_skipped:
            content = b""
            try:
                fd, opened = safe_fs.open_regular_for_read(folder, leaf, max_size=cap)
            except safe_fs.FileTooLarge:
                bin_skipped = True
            else:
                with os.fdopen(fd, "rb") as fh:
                    judged(opened)
                    content = fh.read(cap + 1)
                if len(content) > cap:
                    bin_skipped, content = True, b""
            if content:
                recover_bin_store.capture(agent_slug, rel, content, "deleted")
        if expect is not None:
            judged(safe_fs.lstat_beneath(folder, leaf))
        safe_fs.unlink_beneath(folder, leaf)
    tombstone_path_sync(agent_slug, rel)
    return bin_skipped


# What ``retire_aged_file_sync`` did with one file.
AGED_GONE = "gone"              # unlinked and tombstoned
AGED_FRESH = "fresh"            # touched since the sweep listed it, left alone
AGED_TOO_LARGE = "too_large"    # over the bin's per-file cap, can never be parked
AGED_NO_ROOM = "no_room"        # the sweep's share of the bin is used up today
AGED_KEPT = "kept"              # the capture did not land, the file stays


def retire_aged_file_sync(agent_slug: str, agent_dir: Path, rel: str, *,
                          cutoff: float) -> tuple[str, int]:
    """One upload the retention sweep aged out, beneath the agents root;
    returns ``(outcome, size)``, ``outcome`` one of ``AGED_*``.

    Age is the newer of mtime and ctime, read again here: an unpacked or
    copied file keeps an old mtime but gets a fresh ctime, and a file touched
    since the sweep listed it is not this delete's to make. The file is
    unlinked only when the capture landed, the file is empty or these bytes
    are already in the bin: a sweep is likeliest to run on a full disk, and
    nobody asked for this delete. The tombstone
    carries the sweep's own time, not the file's: a machine's synced copy has
    its mtime from the sync, so an older stamp would read it as a newer
    re-create and bring the file back. A link or a special file at the name
    raises, as ``_delete_file_sync`` does."""
    from storage.files import file_tombstones_store, recover_bin_store
    root, name = _root_of(agent_dir)
    top = f"{name}/{rel}"
    st = safe_fs.lstat_beneath(root, top)
    if not stat.S_ISREG(st.st_mode):
        raise FileNotFoundError(top)
    if max(st.st_mtime, st.st_ctime) >= cutoff:
        return AGED_FRESH, 0
    if st.st_size:
        cap = config.RECOVER_BIN_MAX_BYTES
        if st.st_size > cap:
            return AGED_TOO_LARGE, st.st_size
        room = recover_bin_store.room_left(agent_slug)
        if room is not None and st.st_size > room:
            return AGED_NO_ROOM, st.st_size
        content = safe_fs.read_bytes_beneath(root, top, max_size=cap)
        entry = recover_bin_store.capture(agent_slug, rel, content, "deleted", evict=False)
        if entry is None and not recover_bin_store.already_binned(agent_slug, rel, content):
            return AGED_KEPT, st.st_size
    safe_fs.unlink_beneath(root, top)
    tombstone_path_sync(agent_slug, rel, origin=file_tombstones_store.RETENTION_ORIGIN)
    return AGED_GONE, st.st_size


async def delete_platform_file(agent_slug: str, agent_dir: Path, target: Path, *,
                               expect: FileFacts | None = None) -> bool:
    """The platform delete sequence for ONE regular file (``target`` inside
    ``agent_dir``): Recover-bin capture (best-effort; a voluntary delete
    → no notification), unlink, tombstone + author clear, fan-out. Files above
    the bin cap are NOT captured (Windows-Recycle-Bin-style) — don't even read
    them. The capture, the removal and the tombstone run in one thread on the
    name checked beneath the root. ``expect`` is what a session's sandbox saw
    of the file (the Direct LLM Delete): a file that is not that one raises
    ``FileNotFoundError`` and stays (``_delete_file_sync``). Returns True when
    the capture was skipped so the caller can say "cannot be undone"."""
    rel = _rel_of(agent_dir, target)
    if rel is None:
        raise FileNotFoundError(str(target))
    bin_skipped = await asyncio.to_thread(_delete_file_sync, agent_slug, agent_dir, rel, expect)
    logger.info("Deleted file: %s/%s", agent_slug, rel)
    schedule_library_projection(agent_slug, rel, deleted=True)
    await push_file_delete(agent_slug, rel)
    return bin_skipped


# -- A delete inside a session's sandbox ---------------------------------------
#
# A session on the platform's own files (the sandbox, a compute machine) runs
# ``rm``, a script's cleanup, a patch that drops a file: none of it passes the
# files route, so no tombstone is written, and a paired machine's copy of the
# file comes back at its next merge. What can come back is what a machine
# holds, so ``sync_state`` bounds this: an agent no machine holds files of
# costs one query.

# The projector owns mirrors, and absence heals there. The merge refuses to
# read a delete out of absence in ``config/`` either.
_SCAN_LEFT_OUT = ("knowledge/shared/", f"{layout.CONFIG}/")
# A scope that loses this many tracked paths at once, at least half of them,
# looks like a lost folder rather than a turn's work.
_SCAN_MASS_PATHS = 50
# A scope with this many tracked paths and none left is a lost folder too.
_SCAN_EMPTY_PATHS = 5
# The rest between two scans of one agent: each stats every path a machine holds.
_SCAN_REST_S = 30
# When this process started looking. A row converged after it says the file was
# there then, so an agent first paired after the boot has its first delete heard.
_watching_since = datetime.now(timezone.utc).isoformat()


@dataclass
class _ScanState:
    """One agent's scan. ``standing``: paths already absent at the last look
    or already decided, each with the time of its newest base row then (a
    newer row says a machine converged on the file since, so it came back),
    None until the process first looked. ``pending``: the sessions whose turn
    ended, by id, so a scan that is running loses none."""
    standing: dict[str, str] | None = None
    running: bool = False
    pending: dict[str, SecurityContext] = field(default_factory=dict)


_scans: dict[str, _ScanState] = {}


def _scan_state(agent_slug: str) -> _ScanState:
    state = _scans.get(agent_slug)
    if state is None:
        state = _scans[agent_slug] = _ScanState()
    return state


def _tracked_paths(agent_slug: str) -> dict[str, str]:
    """The paths a paired machine holds, each with its newest row's time."""
    from core.remote import file_sync
    from storage.files import sync_state_store
    return {p: at for p, at in sync_state_store.paths_for_agent(agent_slug).items()
            if not p.startswith(_SCAN_LEFT_OUT) and not file_sync.is_unsynced_engine_home_path(p)}


def _look(agent_dir: Path, paths: set[str]) -> tuple[set[str], set[str]]:
    """``(absent, present)`` of ``paths``. Absent is the kernel saying no such
    name: a link at the name or on the way, or an error, is neither (the file
    may still be there), so such a path is left out of this scan."""
    root, name = _root_of(agent_dir)
    absent: set[str] = set()
    present: set[str] = set()
    for rel in paths:
        try:
            safe_fs.lstat_beneath(root, f"{name}/{rel}")
        except (FileNotFoundError, NotADirectoryError):
            absent.add(rel)
        except (OSError, ValueError):
            continue
        else:
            present.add(rel)
    return absent, present


def prime_session_delete_watch() -> None:
    """Note what is already absent, once at boot and in a worker thread: only
    what goes missing after this is a session's delete. So an upgraded
    install's old ``rm``s are not acted on, and a restart forgets at most the
    deletes since the last scan, which is what happened before this existed.
    An agent first paired after this has no entry and is dated by its rows
    (``_watching_since``)."""
    from storage.files import sync_state_store
    for agent_slug in sync_state_store.agents_with_paths():
        state = _scan_state(agent_slug)
        if state.standing is None:
            tracked = _tracked_paths(agent_slug)
            state.standing = {p: tracked[p] for p in _look(config.AGENTS_DIR / agent_slug, set(tracked))[0]}


def _may_write(agent_dir: Path, rel: str, contexts: list[SecurityContext]) -> bool:
    from auth import path_policy
    return any(path_policy.check_host_path_access(agent_dir / rel, ctx, writing=True).allowed
               for ctx in contexts)


def _folded(name: str) -> str:
    return unicodedata.normalize("NFD", name.casefold())


def _only_case_differs(agent_dir: Path, rel: str,
                       listed: dict[str, tuple[set[str], set[str]]]) -> bool:
    """A sibling with the same name in another case or Unicode form, at any
    depth of ``rel``: a machine that folds names holds one file under both, so
    a tombstone on the old name removes it. ``listed`` keeps one scan's folders,
    so a folder of many deletes is read once. A folder that cannot be read
    answers yes: doubt writes no tombstone."""
    root, name = _root_of(agent_dir)
    here = name
    for part in rel.split("/"):
        names = listed.get(here)
        if names is None:
            try:
                walk = safe_fs.walk_beneath(root, here)
                try:
                    step = next(walk)
                finally:
                    walk.close()
            except (FileNotFoundError, NotADirectoryError):
                return False
            except (OSError, ValueError):
                return True
            found = step.dirs + step.files + step.symlinks + step.other
            names = listed[here] = (set(found), {_folded(n) for n in found})
        if part in names[0]:
            here = f"{here}/{part}"
            continue
        return _folded(part) in names[1]
    return False


def _scope_of(rel: str) -> str:
    parts = rel.split("/")
    return "/".join(parts[:2]) if parts[0] == layout.USERS and len(parts) > 1 else parts[0]


def _scan_agent_sync(agent_slug: str, contexts: list[SecurityContext]) -> list[str]:
    """Tombstone what the ended sessions removed; the paths written. A path
    is decided once per absence: it joins the standing set, and leaves it
    only when seen present again or when a machine converged on it since, so
    an expired tombstone is never re-armed. A live tombstone older than the
    newest base row is from before the file came back (a machine's live write,
    an upload or a file tool's write leaves it standing): the delete is
    recorded anew."""
    from storage.files import file_tombstones_store
    state = _scan_state(agent_slug)
    agent_dir = config.AGENTS_DIR / agent_slug
    tracked = _tracked_paths(agent_slug)
    absent, present = _look(agent_dir, set(tracked))
    if state.standing is None:
        # The agent's first look in this process: what is absent now and was
        # converged before the process started is undatable, and stands.
        state.standing = {p: tracked[p] for p in absent if tracked[p] < _watching_since}
    # A path seen present again, or one a machine converged on since it was
    # decided, came back: its absence now is a new delete.
    for p in list(state.standing):
        if p in present or tracked.get(p, state.standing[p]) > state.standing[p]:
            del state.standing[p]
    candidates: dict[str, list[str]] = {}
    listed: dict[str, tuple[set[str], set[str]]] = {}
    for rel in absent - state.standing.keys():
        if _only_case_differs(agent_dir, rel, listed):
            state.standing[rel] = tracked[rel]
        elif _may_write(agent_dir, rel, contexts):
            candidates.setdefault(_scope_of(rel), []).append(rel)
    written: list[str] = []
    for scope, paths in candidates.items():
        try:
            safe_fs.lstat_beneath(config.AGENTS_DIR, f"{agent_slug}/{scope}")
        except OSError:
            # The whole scope is gone, a lost mount more often than a delete.
            state.standing.update({p: tracked[p] for p in paths})
            continue
        in_scope = [t for t in tracked if _scope_of(t) == scope]
        left = sum(1 for t in in_scope if t in present)
        if (left == 0 and len(in_scope) >= _SCAN_EMPTY_PATHS) \
                or (len(paths) >= _SCAN_MASS_PATHS and len(paths) * 2 >= len(in_scope)):
            state.standing.update({p: tracked[p] for p in paths})
            logger.warning("session delete scan: %s/%s lost %d of its %d tracked files at once, "
                           "which looks like a lost folder: no delete recorded",
                           agent_slug, scope, len(paths), len(in_scope))
            continue
        for rel in paths:
            state.standing[rel] = tracked[rel]
            row = file_tombstones_store.get(agent_slug, rel)
            # Both stamps are the proxy's own clock. A record as new as the
            # newest base stands; an older one is from before the file came back.
            if row is not None and (row["origin"] == file_tombstones_store.HOLD_ORIGIN
                                    or row["deleted_at"] >= tracked[rel]):
                continue
            tombstone_path_sync(agent_slug, rel, origin=file_tombstones_store.SESSION_DELETE_ORIGIN)
            written.append(rel)
    return written


async def _drain_scans(agent_slug: str) -> None:
    state = _scan_state(agent_slug)
    if state.running:
        return
    state.running = True
    try:
        while state.pending:
            contexts = list(state.pending.values())
            state.pending.clear()
            try:
                written = await asyncio.to_thread(_scan_agent_sync, agent_slug, contexts)
            except Exception:
                logger.exception("session delete scan failed for %s", agent_slug)
                continue
            for rel in written:
                logger.info("Session deleted: %s/%r (tombstone written)", agent_slug, rel)
            if state.pending:
                await asyncio.sleep(_SCAN_REST_S)
    finally:
        state.running = False


async def _on_turn_end(ctx: session_events.TurnEndContext) -> None:
    """A turn ended: its session may have removed files of the platform's
    own. A paired machine's session is skipped (the satellite reports its
    own), and a judge and an external session never write the shared tree."""
    from core.session import session_state
    from core.session.external_identity import is_external_ctx
    sec = session_state.get_session_security(ctx.session_id)
    if sec is None or not sec.agent or sec.placement is None or sec.placement.files_synced:
        return
    if (session_state._sessions.get(ctx.session_id) or {}).get("check") or is_external_ctx(sec):
        return
    _scan_state(sec.agent).pending[ctx.session_id] = sec
    spawn(_drain_scans(sec.agent), name="session_delete_scan", owner="file_bookkeeping")


def install_session_delete_watch() -> None:
    """Register the turn-end handler (startup, before the checks': a check's
    continue ends the handler loop)."""
    global _watching_since
    _watching_since = datetime.now(timezone.utc).isoformat()
    session_events.register_turn_end_handler(_on_turn_end)
