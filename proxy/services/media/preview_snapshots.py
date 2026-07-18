"""Version-pinned document-preview snapshots.

When file-tools pushes a Collabora preview, the proxy copies the file **as it
was at push time** into a proxy-private cache. When a later push supersedes
that preview, the dashboard swaps the old block to a view-only render of its
snapshot — a trustworthy "previous version" no agent or satellite can rewrite
after the fact (the cache is a sibling of ``agents/``, outside every agent
tree and every sync surface).

Layout: ``PREVIEW_SNAPSHOT_DIR/<chat_id>/<snapshot_id>`` — the id is an opaque
uuid4 hex, the file carries no extension (Collabora gets the display name from
the WOPI token instead). Writes are atomic (copy to a ``.tmp`` sibling, then
``os.replace``) so a concurrent WOPI read can never see a torn file.

Lifecycle is reference-driven: a snapshot lives as long as a non-dismissed
persisted ``document_preview`` event references it (``gc_chat``). The periodic
sweep only reaps whole chat dirs whose chat row is gone (deleted chats,
orphans). A missing snapshot is never an error to the dashboard — the block
degrades to the "preview moved" chip.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import config

logger = logging.getLogger("claude-proxy.media")

# chat_id as used in paths: uuid-ish / "task-run-…" / "meeting-…" shapes.
# Strict allowlist — these values become path segments under the cache dir.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Orphan sweep is cheap but pointless to run every minute; the registry sweep
# loop calls sweep_orphans() on its 60s tick and this throttles it internally.
_SWEEP_INTERVAL_S = 3600
_last_sweep = 0.0


def _valid_id(value: str) -> bool:
    return bool(value) and bool(_ID_RE.match(value))


def snapshot_path(chat_id: str, snapshot_id: str) -> Path | None:
    """Resolve a snapshot's on-disk path. None when the ids are malformed or
    the file does not exist (pruned / never created)."""
    if not (_valid_id(chat_id) and _valid_id(snapshot_id)):
        return None
    p = config.PREVIEW_SNAPSHOT_DIR / chat_id / snapshot_id
    return p if p.is_file() else None


def create_snapshot(chat_id: str, source: Path) -> str | None:
    """Copy ``source`` into the chat's snapshot dir. Returns the new snapshot
    id, or None on any failure (the preview then simply has no pinned version
    — the dashboard degrades that block to a chip on supersede)."""
    if not _valid_id(chat_id):
        return None
    snapshot_id = uuid.uuid4().hex
    dest_dir = config.PREVIEW_SNAPSHOT_DIR / chat_id
    dest = dest_dir / snapshot_id
    tmp = dest_dir / f"{snapshot_id}.tmp"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, tmp)
        os.replace(tmp, dest)
        return snapshot_id
    except OSError as e:
        logger.warning("preview snapshot copy failed for chat %s: %s", chat_id, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def delete_snapshot(chat_id: str, snapshot_id: str) -> None:
    """Best-effort removal of one snapshot (intra-turn supersede, GC)."""
    if not (_valid_id(chat_id) and _valid_id(snapshot_id)):
        return
    try:
        (config.PREVIEW_SNAPSHOT_DIR / chat_id / snapshot_id).unlink(missing_ok=True)
    except OSError:
        pass


def delete_chat_dir(chat_id: str) -> None:
    """Remove a chat's whole snapshot dir (chat deletion)."""
    if not _valid_id(chat_id):
        return
    shutil.rmtree(config.PREVIEW_SNAPSHOT_DIR / chat_id, ignore_errors=True)


def gc_chat(chat_id: str) -> int:
    """Reference-driven prune: delete this chat's snapshots that no
    non-dismissed persisted preview event references. Runs ONLY after a pump
    flush — at that point the just-flushed rows are persisted, so their ids
    are referenced; the sole unreferenced-but-wanted window is a hook-created
    snapshot still sitting in the perm queue (sub-second), which the age gate
    below covers. Dismissal deletes its rows' snapshots precisely instead of
    calling this. Returns the number of files removed."""
    if not _valid_id(chat_id):
        return 0
    chat_dir = config.PREVIEW_SNAPSHOT_DIR / chat_id
    if not chat_dir.is_dir():
        return 0
    from storage import database as task_store
    referenced = task_store.get_referenced_preview_snapshot_ids(chat_id)
    removed = 0
    try:
        entries = list(chat_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        # .tmp leftovers from a crashed copy age out here too. The age gate
        # protects the one legitimate unreferenced state: a snapshot whose
        # perm-queue item the pump has not drained yet (its row persists at
        # the same flush that triggers this GC).
        if entry.name in referenced:
            continue
        try:
            if time.time() - entry.stat().st_mtime < 300:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def sweep_orphans() -> int:
    """Reap snapshot dirs of chats that no longer exist. Called from the
    periodic registry sweep; internally throttled. Returns dirs removed."""
    global _last_sweep
    now = time.time()
    if now - _last_sweep < _SWEEP_INTERVAL_S:
        return 0
    _last_sweep = now
    root = config.PREVIEW_SNAPSHOT_DIR
    if not root.is_dir():
        return 0
    from storage import database as task_store
    removed = 0
    try:
        chat_dirs = list(root.iterdir())
    except OSError:
        return 0
    for chat_dir in chat_dirs:
        if not chat_dir.is_dir():
            continue
        if task_store.get_chat(chat_dir.name) is None:
            shutil.rmtree(chat_dir, ignore_errors=True)
            removed += 1
    return removed
