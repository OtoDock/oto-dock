"""An app's database copies (APPS.md "Releases"): the snapshot taken before
a release goes live, before a rollback, for a preview and for a check, and
the restore of one.

SQLite opens a database by name and follows a link in any part of it, while
the app's own server writes its data folder all the time. So a snapshot
never opens the live files and never trusts a name an app can write: it
holds the data folder (reached from the agent's real folder,
``core/agent_folder.py``, with no link below it followed), hard-links each live
name relative to that handle, with no link followed, into a private folder
at the app's release root (no app process mounts it), and requires every
private name to be a regular file before SQLite opens it. The private names
are the live inodes, so SQLite's locks, its shared memory and a hot journal
are the live writer's (all three are per inode): the copy is as consistent
as a backup of the live database by its own name. The server holds no
descriptor of its own on a live file either, so it never drops SQLite's
locks by closing one.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
from pathlib import Path

from core import agent_folder
from services.apps import releases
from services.infra import safe_fs

logger = logging.getLogger("otodock.apps")

DB_NAME = "app.db"
# A writer that starts on a database with no WAL makes these two; a journal
# is linked when present (a hot one refuses the read-only copy, as before).
_WRITER_SIDES = (f"{DB_NAME}-wal", f"{DB_NAME}-shm")
_LINKED = (DB_NAME, *_WRITER_SIDES, f"{DB_NAME}-journal")
# A private folder a killed proxy left is swept by the next snapshot of the
# app (after this long) and by ``releases.drop_db_copies``.
_STALE_PRIVATE_S = 3600
_TRIES = 3
_COPY_NAME = "copy.sqlite"

NOT_PLAIN = ("the app's database is not a plain file, so the deploy stops here and nothing is "
             "read through it")
MID_WRITE = ("the app's database is in the middle of a write, so the deploy stops here and nothing "
             "is lost. Open the app once, then try again.")
_KEPT_CHANGING = ("the app's database kept changing while it was copied, so the deploy stops here. "
                  "Try again.")

# One snapshot of a database at a time inside this process: a preview cut
# runs without the deploy lock, and SQLite shares one shared-memory mapping
# per database file between the connections of a process.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _refused(text: str) -> Exception:
    from services.apps.app_deploy import DeployError
    return DeployError(text)


def _lock_for(data_rel: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(data_rel, threading.Lock())


def copy_lock(row: dict) -> threading.Lock:
    """The lock a copy of ``row``'s database holds from start to end: what
    removes the copies' private folders takes it too, so a running copy's
    folder is never removed under it."""
    return _lock_for(os.fspath(releases.app_data_dir(row)))


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _writer_sides(data_fd: int) -> dict[str, tuple[int, int]]:
    """The live WAL and shared memory, by identity, looked at with no link
    followed (a link there is not one of them)."""
    out: dict[str, tuple[int, int]] = {}
    for name in _WRITER_SIDES:
        side = safe_fs.kind_beneath(data_fd, name)
        if side.is_file and side.st is not None:
            out[name] = _identity(side.st)
    return out


def _link_live(data_fd: int, priv_fd: int) -> dict[str, tuple[int, int]]:
    """Each live name, as it is in the data folder, linked into the private
    folder. A link, a pipe or a folder at a live name refuses the copy: a
    link is linked as the link itself and is then not a regular file, a
    folder cannot be linked."""
    linked: dict[str, tuple[int, int]] = {}
    for name in _LINKED:
        try:
            os.link(name, name, src_dir_fd=data_fd, dst_dir_fd=priv_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as e:
            if safe_fs.kind_beneath(data_fd, name).is_file:
                logger.warning("App database: %s could not be linked for its copy (%s). The database must "
                               "be owned by the platform's own user, on the same disk as the app's releases.",
                               name, e.strerror or type(e).__name__)
                raise _refused("the app's database could not be copied, so the deploy stops here "
                               "and nothing is lost") from e
            raise _refused(NOT_PLAIN) from e
        st = os.stat(name, dir_fd=priv_fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            raise _refused(NOT_PLAIN)
        linked[name] = _identity(st)
    return linked


def _copy_once(root: Path, data_fd: int, base_rel: str, dst_rel: str,
               max_size: int | None) -> bool | None:
    """True: copied to ``dst_rel``. False: no database, or one over
    ``max_size``. None: a writer started or ended meanwhile, copy again.
    ``root`` is the agent's real folder."""
    priv_rel = f"{base_rel}/{releases.DB_SNAPSHOT_PREFIX}{secrets.token_hex(6)}"
    safe_fs.mkdirs_beneath(root, priv_rel, exist_ok=False)
    try:
        with safe_fs.open_root(root, priv_rel) as priv_fd:
            linked = _link_live(data_fd, priv_fd)
            if DB_NAME not in linked:
                return False
            if max_size is not None and os.stat(DB_NAME, dir_fd=priv_fd,
                                                follow_symlinks=False).st_size > max_size:
                return False
        # SQLite opens these names: the private folder only the server writes.
        priv = Path(root) / priv_rel
        try:
            source = sqlite3.connect(f"file:{priv / DB_NAME}?mode=ro", uri=True)
            try:
                target = sqlite3.connect(priv / _COPY_NAME)
                try:
                    with target:
                        source.backup(target)
                finally:
                    target.close()
                # Looked at while the source still holds its lock, so a
                # writer cannot remove them under the look.
                changed = _writer_sides(data_fd) != {n: linked[n] for n in _WRITER_SIDES if n in linked}
            finally:
                source.close()
        except sqlite3.Error as e:
            # A hot journal (a writer stopped mid-transaction) cannot be rolled
            # back by a read-only copy: the app's own server does it at its
            # next start. A linked journal that is no longer the live one is a
            # writer that committed after the link: copy again.
            if getattr(e, "sqlite_errorname", "") == "SQLITE_READONLY_ROLLBACK":
                live = safe_fs.kind_beneath(data_fd, f"{DB_NAME}-journal")
                if not (live.is_file and live.st is not None
                        and _identity(live.st) == linked.get(f"{DB_NAME}-journal")):
                    return None
                raise _refused(MID_WRITE) from e
            raise _refused(f"the app's database could not be copied ({e}). The deploy stops here "
                           "so nothing is lost") from e
        if changed:
            return None
        # A link at the destination's name is replaced, never written through.
        safe_fs.rename_beneath(root, f"{priv_rel}/{_COPY_NAME}", dst_rel, replace=True, mkdirs=True)
        return True
    finally:
        with contextlib.suppress(OSError):
            safe_fs.rmtree_beneath(root, priv_rel, missing_ok=True)


def snapshot_db(row: dict, dst: Path, *, max_size: int | None = None) -> bool:
    """A consistent copy of the app's database at ``dst`` (a path joined
    from the row's release folder) through the SQLite backup API; False
    when there is none, or ``max_size`` is passed. Synchronous.

    Anything but a plain file at a live name, or a link on the way to the
    data folder, stops the deploy, the rollback or the preview with the same
    refusal, and nothing is read through it."""
    agent = row["agent"]
    root, data_rel = agent_folder.beneath_agent(releases.app_data_dir(row), agent)
    _root, base_rel = agent_folder.beneath_agent(releases.app_release_dir(row), agent)
    _root, dst_rel = agent_folder.beneath_agent(dst, agent)
    with copy_lock(row):
        releases.sweep_stale(releases.app_release_dir(row), releases.DB_SNAPSHOT_PREFIX, _STALE_PRIVATE_S)
        for attempt in range(_TRIES):
            with contextlib.ExitStack() as stack:
                try:
                    data_fd = stack.enter_context(safe_fs.open_root(root, data_rel))
                except FileNotFoundError:
                    return False
                except safe_fs.SafeFsError as e:
                    raise _refused(NOT_PLAIN) from e
                done = _copy_once(root, data_fd, base_rel, dst_rel, max_size)
            if done is not None:
                return done
            time.sleep(0.2 * (attempt + 1))
    raise _refused(_KEPT_CHANGING)


def restore_db(snapshot: Path, target: Path) -> None:
    """Put a snapshot in the database's place and drop the stale WAL, shm
    and journal (a replaced main file under an old one is corrupt). The copy
    lands beneath the agent's real folder with no link below it followed and
    replaces the name atomically: the data directory is the app's own to
    write, so a link planted at the database's name is replaced, never
    written through."""
    root, target_rel = agent_folder.beneath_agent(target)
    src_root, src_rel = agent_folder.beneath_agent(snapshot)
    for suffix in ("-wal", "-shm", "-journal"):
        safe_fs.unlink_beneath(root, target_rel + suffix, missing_ok=True)
    safe_fs.copy_file_beneath(src_root, src_rel, root, target_rel, mkdirs=True)
