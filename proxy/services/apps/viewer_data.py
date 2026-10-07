"""A single-file app's document per viewer (APPS.md "Platform catalog",
``viewer.data.read`` / ``viewer.data.write``).

One small JSON file per (app, viewer) in a folder BESIDE the app's data
directory, ``app-data/<bucket>/<slug>.viewers/<hash>.json`` (a slug never
holds a dot, so the name collides with no app's own directory): the
viewer's own page writes it and nobody else reads it, a folder app's
server included (its sandbox mounts the app's data directory alone). The
file is ``{"rev", "doc"}`` with ``doc`` held to the state document's
limits; the name is a hash of the slug and the sub, never the sub itself
(a sub is not a path segment), so a later app pinned under the slug finds
the documents as a folder app finds its database; a purge removes the
folder. Written through ``releases.write_atomic`` (a link at the name is
replaced, a linked parent refused) and read without following a link; one
lock per app around a read, merge and write, and around the count that
caps the files an app keeps. Synchronous: call it on the DB executor.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from pathlib import Path

import config
from services.apps import releases
from services.infra import safe_fs
from services.infra.path_confinement import join_under
from storage import db_apps

VIEWERS_SUFFIX = ".viewers"
# Placed viewers spend the home agent's quota: the count is bounded per app.
VIEWER_FILES_MAX = 1000
_READ_MAX = db_apps.STATE_DOC_MAX_BYTES + 4096

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(app_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(app_id)
        if lock is None:
            lock = _locks[app_id] = threading.Lock()
        return lock


def viewers_dir(row: dict) -> Path:
    agent_dir = config.get_agent_dir(row["agent"])
    return join_under(releases.data_root(agent_dir, row.get("username") or ""),
                      f"{row['slug']}{VIEWERS_SUFFIX}")


def file_for(row: dict, sub: str) -> Path:
    digest = hashlib.sha256(f"{row['slug']}:{sub}".encode("utf-8")).hexdigest()[:32]
    return viewers_dir(row) / f"{digest}.json"


def remove(row: dict) -> None:
    """Every viewer's document of the app (a purge)."""
    shutil.rmtree(viewers_dir(row), ignore_errors=True)


def _load(path: Path) -> dict:
    """``{"rev", "doc"}`` as stored, the empty document when nothing was
    written yet."""
    root, rel = releases.agents_rel(path)
    try:
        raw = safe_fs.read_bytes_beneath(root, rel, max_size=_READ_MAX)
    except FileNotFoundError:
        return {"rev": 0, "doc": {}}
    except OSError:
        raise PermissionError("your saved data is not a regular file")
    try:
        stored = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("your saved data is damaged; save a whole document to start over")
    if not isinstance(stored, dict) or not isinstance(stored.get("doc"), dict):
        raise ValueError("your saved data is damaged; save a whole document to start over")
    return {"rev": int(stored.get("rev") or 0), "doc": stored["doc"]}


def read(row: dict, sub: str) -> dict:
    with _lock(row["id"]):
        return _load(file_for(row, sub))


def write(row: dict, sub: str, *, doc=None, patch=None) -> dict:
    """Replace the document with ``doc`` or merge ``patch`` into it (RFC
    7386, as the state document merges); the result within the state
    document's limits, else ``ValueError`` with the reason. A damaged file
    is replaced by a whole document (``doc``) and refuses a patch."""
    if doc is None and patch is None:
        raise ValueError("doc or patch is required")
    path = file_for(row, sub)
    with _lock(row["id"]):
        try:
            current = _load(path)
        except ValueError:
            if doc is None:
                raise
            current = {"rev": 0, "doc": {}}
        new = doc if doc is not None else db_apps.merge_patch(current["doc"], patch)
        try:
            db_apps.check_state_doc(new)
        except db_apps.AppStateError as e:
            # The page shows the reason to the viewer about their own data.
            raise ValueError(str(e).replace("the state document", "your saved data")
                             .replace("a state key", "a key in your saved data")) from None
        try:
            text = json.dumps({"rev": current["rev"] + 1, "doc": new}, separators=(",", ":"),
                              ensure_ascii=False, sort_keys=True, allow_nan=False)
        except ValueError:
            raise ValueError("the document holds a number JSON cannot carry")
        try:
            if current["rev"] == 0 and not path.exists():
                folder = viewers_dir(row)
                count = sum(1 for p in folder.iterdir() if p.is_file()) if folder.is_dir() else 0
                if count >= VIEWER_FILES_MAX:
                    raise ValueError(f"this app keeps data for {VIEWER_FILES_MAX} viewers already")
            releases.write_atomic(path, text)
        except OSError:
            raise PermissionError("your saved data cannot be written (a link in the way, or no room)")
    return {"doc": new, "rev": current["rev"] + 1}
