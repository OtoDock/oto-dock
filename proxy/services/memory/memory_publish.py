"""What follows a memory write: the attributed git commit and the
platform-write bookkeeping with the live propagation (tombstone retire or
record, ``file_author`` attribution, satellite fan-out, the dashboard
``file_updated`` broadcast), mirroring the dashboard file API so tool
writes reach open satellites and Files-UI views mid-session. The memory
API calls both after an op; a promotion calls them for each of its two
roots.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from services.memory import memory_file
from services.memory.memory_file import OpResult

logger = logging.getLogger("otodock.memory-api")


def tree_rel(agent_dir: Path, root: Path, rel: str) -> str | None:
    """Agent-tree-relative posix path for a scope-relative memory path."""
    try:
        return (root / rel).resolve().relative_to(agent_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _is_external_tree(rel: str) -> bool:
    """Caller trees never sync, fan out or broadcast to the Files UI."""
    return rel.startswith("externals/")


async def publish_changes(
    agent_slug: str,
    agent_dir: Path,
    root: Path,
    result: OpResult,
    *,
    writer_slug: str | None,
    exclude_user_sub: str = "",
) -> None:
    """Bookkeeping + live propagation for a successful mutation. Best-effort —
    a satellite being offline or a broadcast failing never fails the op."""
    try:
        from services.notifications.notification_manager import broadcast_file_updated
        from services.remote import workspace_fanout
        from storage.files import file_author_store, file_tombstones_store

        for rel in result.changed:
            path = tree_rel(agent_dir, root, rel)
            if not path or _is_external_tree(path):
                continue
            await asyncio.to_thread(file_tombstones_store.drop, agent_slug, path)
            if writer_slug:
                await asyncio.to_thread(
                    file_author_store.record, agent_slug, path, writer_slug,
                )
            if workspace_fanout.has_fanout_candidates(
                agent_slug, path, include_idle=True,
            ):
                try:
                    content = (root / rel).read_bytes()
                except OSError:
                    content = None
                if content is not None:
                    await workspace_fanout.fan_out_write(
                        agent_slug, path, content, include_idle=True,
                    )
            await broadcast_file_updated(
                agent_slug, path, exclude_user_sub=exclude_user_sub,
            )

        now = time.time()
        for rel in result.deleted:
            path = tree_rel(agent_dir, root, rel)
            if not path or _is_external_tree(path):
                continue
            await asyncio.to_thread(
                file_tombstones_store.record, agent_slug, path, now,
                origin="memory",
            )
            await asyncio.to_thread(file_author_store.clear, agent_slug, path)
            await workspace_fanout.fan_out_delete(
                agent_slug, path, include_idle=True,
            )
            await broadcast_file_updated(
                agent_slug, path, exclude_user_sub=exclude_user_sub,
            )
    except Exception:  # noqa: BLE001  # propagation spans the db and satellites, the memory op already landed
        logger.warning(
            "memory write propagation failed for %s", agent_slug, exc_info=True,
        )


def commit(
    agent_dir: Path, scope: str, username: str | None,
    result: OpResult, command: str, writer: str,
) -> str | None:
    """One attributed git commit covering every path the op touched."""
    try:
        from services.infra import git_writer
        repo = memory_file.git_repo_root(agent_dir, scope, username)
        repo.mkdir(parents=True, exist_ok=True)
        git_writer.init_if_missing(repo)
        root = memory_file.scope_root(agent_dir, scope, username)
        # A deleted topic the repo never tracked (a hand-written file the
        # Files UI saved) cannot be staged: ``git add`` refuses the whole
        # pathspec and the index's own change would go uncommitted with it.
        gone = [
            rel for rel in result.deleted
            if git_writer.is_tracked(repo, (root / rel).relative_to(repo).as_posix())
        ]
        paths: list[Path | str] = [root / rel for rel in (result.changed + gone)]
        if not paths:
            return None
        return git_writer.commit_paths(
            repo, paths, f"memory: {command} ({scope} scope, by {writer})",
        )
    except (ImportError, OSError, ValueError):
        logger.warning("memory git commit failed", exc_info=True)
        return None
