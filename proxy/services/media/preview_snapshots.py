"""The versions of a document in a chat: version-pinned preview copies.

When file-tools pushes a document into a chat, the proxy copies the file
**as it was at push time** into a proxy-private cache: that copy is the
push's version, which the chat's document pane opens read-only from its
Versions menu (a copy no agent or satellite can rewrite after the fact: the
cache is a sibling of ``agents/``, outside every agent tree and every sync
surface). A save from the pane refreshes the file's newest version while it
still mirrors the file (``refresh_newest``), so a version holds the push's
bytes plus the person's edits until the agent's next change.

Layout: ``PREVIEW_SNAPSHOT_DIR/<chat_id>/<snapshot_id>`` — the id is an opaque
uuid4 hex, the file carries no extension (Collabora gets the display name from
the WOPI token instead). The copy goes through ``safe_fs``: the source is
opened beneath the agents root with no symlink followed (a file the agent
swapped for a link before the push is not pinned) and the write is atomic
(a temp sibling renamed into place) so a concurrent WOPI read can never see
a torn file. The copy keeps the source's times.

Lifecycle: a snapshot lives while a non-dismissed persisted
``document_preview`` row references it (``gc_chat``) and is among the file's
newest ``MAX_VERSIONS_PER_FILE`` in the chat (``stamp_and_cap``, at each
push). The periodic sweep only reaps whole chat dirs whose chat row is gone
(deleted chats, orphans). A missing snapshot is never an error to the
dashboard: the version reads "no longer available".
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import time
import uuid
from pathlib import Path

import config
import contextlib
from services.infra import safe_fs

logger = logging.getLogger("claude-proxy.media")

# chat_id as used in paths: uuid-ish / "task-run-…" / "meeting-…" shapes.
# Strict allowlist — these values become path segments under the cache dir.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Orphan sweep is cheap but pointless to run every minute; the registry sweep
# loop calls sweep_orphans() on its 60s tick and this throttles it internally.
_SWEEP_INTERVAL_S = 3600
_last_sweep = 0.0


def _clock() -> float:
    return time.time()


def _valid_id(value: str) -> bool:
    return bool(value) and bool(_ID_RE.match(value))


def snapshot_path(chat_id: str, snapshot_id: str) -> Path | None:
    """Resolve a snapshot's on-disk path. None when the ids are malformed or
    the file does not exist (pruned / never created)."""
    if not (_valid_id(chat_id) and _valid_id(snapshot_id)):
        return None
    p = config.PREVIEW_SNAPSHOT_DIR / chat_id / snapshot_id
    return p if p.is_file() else None


# Pinning a version copies the whole document; past this size the copy's disk
# I/O costs more than losing the pin (a 536MB OCR artifact once helped drive
# an 8-minute io-wait storm). Mirrors the recover-bin capture cap.
_MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024


def create_snapshot(chat_id: str, source: Path) -> str | None:
    """Copy ``source`` into the chat's snapshot dir. Returns the new snapshot
    id, or None on any failure (the push then has no version copy: its card
    and the Versions menu read "no longer available")."""
    if not _valid_id(chat_id):
        return None
    snapshot_id = uuid.uuid4().hex
    try:
        rel = safe_fs.rel_under(source, config.AGENTS_DIR)
        os.makedirs(config.PREVIEW_SNAPSHOT_DIR, exist_ok=True)
        # The size is checked at the open, before the chat's directory exists.
        safe_fs.copy_file_beneath(
            config.AGENTS_DIR, rel, config.PREVIEW_SNAPSHOT_DIR,
            f"{chat_id}/{snapshot_id}", max_size=_MAX_SNAPSHOT_BYTES, mkdirs=True,
        )
        return snapshot_id
    except safe_fs.FileTooLarge:
        logger.info(
            "preview snapshot skipped for chat %s: %s exceeds %d MB",
            chat_id, source.name, _MAX_SNAPSHOT_BYTES // (1024 * 1024),
        )
        return None
    except OSError as e:
        logger.warning("preview snapshot copy failed for chat %s: %s", chat_id, e)
        return None


def delete_snapshot(chat_id: str, snapshot_id: str) -> None:
    """Best-effort removal of one snapshot (intra-turn supersede, GC)."""
    if not (_valid_id(chat_id) and _valid_id(snapshot_id)):
        return
    with contextlib.suppress(OSError):
        (config.PREVIEW_SNAPSHOT_DIR / chat_id / snapshot_id).unlink(missing_ok=True)


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
        with contextlib.suppress(OSError):
            # ctime is the copy's own time; mtime is the source's, copied.
            if _clock() - entry.stat().st_ctime < 300:
                continue
            entry.unlink()
            removed += 1
    return removed


# Versions kept per file per chat: a push beyond it deletes the oldest
# versions' copies (their rows stay; their cards read "no longer available").
MAX_VERSIONS_PER_FILE = 30


def stamp_and_cap(chat_id: str, file_id: str) -> int:
    """At a push of ``file_id`` into ``chat_id``: the push's version number
    (1 + the file's persisted pushes in the chat) and the cap, which keeps
    the copies of the newest ``MAX_VERSIONS_PER_FILE - 1`` persisted pushes
    (the new push is the last one) and deletes the older ones. Store reads
    and unlinks: a ``run_db`` job. Two pushes racing can leave one copy over
    the cap until the file's next push."""
    from storage import database as task_store
    rows = task_store.get_preview_rows(chat_id, file_id)
    for row in rows[: max(0, len(rows) - (MAX_VERSIONS_PER_FILE - 1))]:
        delete_snapshot(chat_id, row["data"].get("snapshot_id") or "")
    return len(rows) + 1


def refresh_newest(chat_id: str, file_id: str, pending_snapshot_id: str | None,
                   body: bytes, saved_mtime_ns: int, before: tuple[int, int, str | None]) -> bool:
    """After a save from the document pane: overwrite the file's newest
    version in the chat with the saved bytes while that version still
    mirrors the file as it stood just before the save, so a version holds
    the push's bytes plus the pane's saves until anything else changes the
    file. The newest version is the pump's pending push of the file when
    there is one, else the file's newest persisted push (that one only).
    ``before`` is the file's ``(size, mtime_ns, sha256 or None)`` before the
    save. Runs on the chat's writer lane, after the turn's rows; returns
    whether a copy was written."""
    if not _valid_id(chat_id) or len(body) > _MAX_SNAPSHOT_BYTES:
        return False
    snapshot_id = pending_snapshot_id
    if not snapshot_id:
        from storage import database as task_store
        rows = task_store.get_preview_rows(chat_id, file_id)
        snapshot_id = rows[-1]["data"].get("snapshot_id") if rows else ""
    if not _valid_id(snapshot_id or ""):
        return False
    path = config.PREVIEW_SNAPSHOT_DIR / chat_id / snapshot_id
    try:
        st = os.lstat(path)
    except OSError:
        return False
    size, mtime_ns, digest = before
    if not stat.S_ISREG(st.st_mode) or st.st_size != size:
        return False
    if st.st_mtime_ns != mtime_ns:
        if not digest or _sha256_file(path) != digest:
            return False
    try:
        with safe_fs.atomic_writer(config.PREVIEW_SNAPSHOT_DIR, f"{chat_id}/{snapshot_id}",
                                   fsync=False) as fh:
            fh.write(body)
            fh.flush()
            # The save's own time: the next save's check compares it.
            os.utime(fh.fileno(), ns=(saved_mtime_ns, saved_mtime_ns))
    except OSError as e:
        logger.debug("preview version refresh failed for chat %s: %s", chat_id, e)
        return False
    return True


def _sha256_file(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


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
