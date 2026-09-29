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
  the delete out.

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
from pathlib import Path

import config
from core import layout
from services.infra import safe_fs
from storage.pg import run_db

logger = logging.getLogger("claude-proxy.agents")


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
    from storage.files import file_tombstones_store
    from storage.files import file_author_store
    await asyncio.to_thread(file_tombstones_store.drop, agent_slug, rel_path)
    if writer:
        await asyncio.to_thread(file_author_store.record, agent_slug, rel_path, writer)
    schedule_library_projection(agent_slug, rel_path, deleted=False)


def tombstone_path_sync(agent_slug: str, rel_path: str) -> None:
    """The store half of ``tombstone_path``: the tombstone row and the author
    cleared, for a caller already in a worker thread (the projection is the
    caller's, on the loop)."""
    from storage.files import file_tombstones_store
    from storage.files import file_author_store
    file_tombstones_store.record(agent_slug, rel_path, time.time(), origin="dashboard")
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
            asyncio.create_task(
                library_projector.propagate_mirror_delete(agent_slug, src, sub_rel))
        else:
            asyncio.create_task(
                library_projector.propagate_mirror_write(agent_slug, src, sub_rel))
        return
    knowledge_rel = rel_path[len("knowledge/"):]
    if knowledge_rel:
        asyncio.create_task(
            library_projector.propagate_source_write(
                agent_slug, knowledge_rel, deleted=deleted))


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


def _delete_file_sync(agent_slug: str, agent_dir: Path, rel: str) -> bool:
    """Capture (under the cap), unlink and tombstone ONE regular file at
    ``rel`` beneath the agents root; a link or a special file at the name is
    ``FileNotFoundError`` (never captured, never removed). Returns True when
    the capture was skipped for size."""
    from storage.files import recover_bin_store
    root, name = _root_of(agent_dir)
    top = f"{name}/{rel}"
    st = safe_fs.lstat_beneath(root, top)
    if not stat.S_ISREG(st.st_mode):
        raise FileNotFoundError(top)
    cap = config.RECOVER_BIN_MAX_BYTES
    bin_skipped = st.st_size > cap
    if not bin_skipped:
        try:
            content = safe_fs.read_bytes_beneath(root, top, max_size=cap)
        except safe_fs.FileTooLarge:
            bin_skipped, content = True, b""
        if content:
            recover_bin_store.capture(agent_slug, rel, content, "deleted")
    safe_fs.unlink_beneath(root, top)
    tombstone_path_sync(agent_slug, rel)
    return bin_skipped


async def delete_platform_file(agent_slug: str, agent_dir: Path, target: Path) -> bool:
    """The platform delete sequence for ONE regular file (``target`` inside
    ``agent_dir``): Recover-bin capture (best-effort; a voluntary delete
    → no notification), unlink, tombstone + author clear, fan-out. Files above
    the bin cap are NOT captured (Windows-Recycle-Bin-style) — don't even read
    them. The capture, the removal and the tombstone run in one thread on the
    name checked beneath the root. Returns True when the capture was skipped
    so the caller can say "cannot be undone"."""
    rel = _rel_of(agent_dir, target)
    if rel is None:
        raise FileNotFoundError(str(target))
    bin_skipped = await asyncio.to_thread(_delete_file_sync, agent_slug, agent_dir, rel)
    logger.info("Deleted file: %s/%s", agent_slug, rel)
    schedule_library_projection(agent_slug, rel, deleted=True)
    await push_file_delete(agent_slug, rel)
    return bin_skipped
