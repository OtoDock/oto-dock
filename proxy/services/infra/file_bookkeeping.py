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

Moved out of ``api/agents/files.py`` (Plan B, 2026-09) so the Direct-LLM
builtins run the identical sequence instead of a second copy. Every caller
goes through THIS module (no private re-exports), so one monkeypatch point
covers them all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import config
from services.infra.path_confinement import PathOutsideRoot, resolve_under

logger = logging.getLogger("claude-proxy.agents")


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


async def tombstone_path(agent_slug: str, rel_path: str) -> None:
    """Record a delete tombstone + forget the author for one platform file path,
    so an idle satellite APPLIES the delete (never resurrects it) at next sync."""
    from storage.files import file_tombstones_store
    from storage.files import file_author_store
    await asyncio.to_thread(
        file_tombstones_store.record, agent_slug, rel_path, time.time(), origin="dashboard",
    )
    await asyncio.to_thread(file_author_store.clear, agent_slug, rel_path)
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


async def tombstone_subtree(agent_slug: str, agent_dir: Path, src: Path) -> None:
    """Tombstone every file under ``src`` (a file or dir) BEFORE it is deleted /
    moved / renamed on disk — so an idle satellite removes the old path(s) instead
    of resurrecting them. Per-file (a directory has no file hash to key on)."""
    base = Path(os.path.realpath(agent_dir))
    try:
        src = resolve_under(src, base)
    except PathOutsideRoot:
        return
    if src.is_file():
        files = [src]
    elif src.is_dir():
        files = [f for f in src.rglob("*") if f.is_file() and not f.is_symlink()]
    else:
        return
    for f in files:
        try:
            rel = f.resolve().relative_to(base).as_posix()
        except (OSError, ValueError):
            continue
        await tombstone_path(agent_slug, rel)


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
    no remote session is active (it's platform state, not a push)."""
    await record_platform_write(agent_slug, rel_path, writer)
    from services.remote import workspace_fanout
    if not workspace_fanout.has_fanout_candidates(agent_slug, rel_path, include_idle=True):
        return
    try:
        content = host_path.read_bytes()
    except OSError as e:
        logger.warning("Cannot read %s for satellite push: %s", host_path, e)
        return
    await workspace_fanout.fan_out_write(agent_slug, rel_path, content, include_idle=True)


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
    instead of only at the next manifest sync. Each file is fanned out with
    per-file isolation; the disk read happens only when a file has an allowed
    target. Best-effort."""
    base = Path(os.path.realpath(agent_dir))
    try:
        root = resolve_under(root, base)
    except PathOutsideRoot:
        return
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = [f for f in root.rglob("*") if f.is_file()]
    else:
        return
    from services.remote import workspace_fanout
    for f in files:
        try:
            rel = f.relative_to(base).as_posix()
        except ValueError:
            continue
        await record_platform_write(agent_slug, rel, writer)
        if not workspace_fanout.has_fanout_candidates(agent_slug, rel, include_idle=True):
            continue
        try:
            content = f.read_bytes()
        except OSError as e:
            logger.warning("Cannot read %s for satellite push: %s", f, e)
            continue
        await workspace_fanout.fan_out_write(agent_slug, rel, content, include_idle=True)


async def delete_platform_file(agent_slug: str, agent_dir: Path, target: Path) -> bool:
    """The platform delete sequence for ONE regular file (``target`` resolved,
    inside ``agent_dir``): Recover-bin capture (best-effort; a voluntary delete
    → no notification), unlink, tombstone + author clear, fan-out. Files above
    the bin cap are NOT captured (Windows-Recycle-Bin-style) — don't even read
    them. Returns True when the capture was skipped so the caller can say
    "cannot be undone"."""
    rel = target.relative_to(Path(os.path.realpath(agent_dir))).as_posix()
    bin_skipped = False
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    if size > config.RECOVER_BIN_MAX_BYTES:
        bin_skipped = True
    else:
        try:
            content = target.read_bytes()
        except OSError:
            content = b""
        if content:
            from storage.files import recover_bin_store
            await asyncio.to_thread(
                recover_bin_store.capture, agent_slug, rel, content, "deleted",
            )
    target.unlink()
    logger.info(f"Deleted file: {target}")
    await tombstone_path(agent_slug, rel)  # idle satellites apply the delete
    await push_file_delete(agent_slug, rel)
    return bin_skipped
