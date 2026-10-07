"""Release copies of apps (APPS.md "Releases and rollback", APPS.md).

A viewer is served the copy made at deploy time, never the working tree
the agent edits. Two kinds of row:

* ``file`` — one html: ``app-releases/<bucket>/<row id>/<n>/index.html``;
  ``release_path`` names the file, ``release_sha256`` its hash.
* ``folder`` — a directory: ``app-releases/<bucket>/<row id>/<n>/`` holding
  ``app.json``, ``client/``, ``server/`` and ``manifest.json`` (``{path:
  {sha256, size}}`` over every copied file); ``release_path`` names the
  directory and ``release_sha256`` is the sha256 of ``manifest.json`` — the
  tree hash the client routes address the release by. Every served file is
  verified against the manifest.

The bucket is ``shared`` or ``users/<username>``, so the quota code sees
both. Next to the numbered releases live the copies an app must never
reach: ``<n>/db-before.sqlite`` (the database as it was before release n
went live), ``rollback-<ts>.sqlite`` (the current database at a rollback),
``preview/`` (the working tree started for the owner or an editor, with its
own ``data/``) and ``app.log``. The app's own database lives apart, at
``app-data/<bucket>/<slug>/`` (``app_data_dir``), the only directory an app
process may write. Synchronous helpers, called off the loop.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import stat
from pathlib import Path

import config
from services.infra import safe_fs
from services.infra.path_confinement import join_under
from services.infra.agent_dirs import agent_dirs
from storage import database as task_store
from storage import db_apps
from core import layout

logger = logging.getLogger("claude-proxy.apps")

RELEASES_DIRNAME = "app-releases"
DATA_DIRNAME = "app-data"
KEEP_RELEASES = 3
MANIFEST_NAME = "manifest.json"
DB_BEFORE_NAME = "db-before.sqlite"
PREVIEW_DIRNAME = "preview"
LOG_NAME = "app.log"
# A scratch copy of the working tree a render job judges (app_render.py):
# ``check-<nonce>`` beside the releases, removed when the job ends.
CHECK_DIR_PREFIX = "check-"

# What a release may hold (APPS.md "Releases"): per file, per tree, per
# count. Enforced before any byte is copied.
MAX_RELEASE_FILE_BYTES = 2 * 1024 * 1024
MAX_RELEASE_BYTES = 32 * 1024 * 1024
MAX_RELEASE_FILES = 500
# Never copied into a release: dependencies the platform does not install,
# a stray data directory, dotfiles.
SKIP_DIR_NAMES = frozenset({"node_modules", "data"})
# A single-file app's working file as the release cut and the working-copy
# serve read it (the cap ``/v1/ui`` serves under).
FILE_APP_MAX_BYTES = 8 * 1024 * 1024
# A release copy's files take one mode, never the working file's bits.
RELEASE_FILE_MODE = 0o644


class ReleaseDamaged(Exception):
    """The release copy on disk no longer matches the row's hash."""


class ReleaseInvalid(Exception):
    """The working tree cannot become a release (a cap, a symlink); the
    reason is worded for the agent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── opening a tree an agent can rewrite (services/infra/safe_fs.py) ──────


def _join(base: str, rel: str) -> str:
    return f"{base}/{rel}" if base and rel else (base or rel)


def agents_rel(path: Path) -> tuple[str, str]:
    """``(AGENTS_DIR, "<agent>/…")`` for a path joined from an agent's
    folder; ``EscapeRefused`` for anything else. The agent comes from the
    text the caller joined, never from where a link resolves."""
    root = os.fspath(config.AGENTS_DIR)
    return root, safe_fs.rel_under(os.path.abspath(os.fspath(path)), root)


def tree_root(path: Path) -> tuple[str, str]:
    """The root and rel to open ``path`` strictly: beneath ``AGENTS_DIR``
    with the agent as the first component (an agent's folder is never a
    root: a session, and every container that mounts the agents tree, can
    replace it), or, for a directory the platform itself made outside the
    agents tree (an extracted bundle, a template's copy), that directory.
    A path that reaches the agents tree only through a link is refused."""
    try:
        return agents_rel(path)
    except safe_fs.EscapeRefused:
        pass
    real = os.path.realpath(os.fspath(path))
    if Path(real).is_relative_to(os.path.realpath(os.fspath(config.AGENTS_DIR))):
        raise safe_fs.EscapeRefused(errno.EXDEV, "the path reaches the agents tree through a link",
                                    os.fspath(path))
    return real, ""


def read_tree_file(source_dir: Path, rel: str, *, max_size: int) -> bytes:
    """One file of a working tree or a release, read without following any
    link (``safe_fs`` errors are ``OSError``s)."""
    root, base = tree_root(source_dir)
    return safe_fs.read_bytes_beneath(root, _join(base, rel), max_size=max_size)


def write_atomic(target: Path, text: str) -> None:
    """Replace ``target`` with ``text`` in one rename, so a concurrent read
    never sees a truncated file; the temporary name is unpredictable, a link
    at ``target`` is replaced rather than written through, and a linked
    parent is refused. ``target`` is joined from the row's agent folder
    (``config.get_agent_dir(agent) / rel``), never a resolved path."""
    root, rel = agents_rel(target)
    safe_fs.atomic_write_beneath(root, rel, text.encode("utf-8"), mkdirs=True)


def bucket_root(agent_dir: Path, username: str) -> Path:
    """The release root of a bucket: the shared one, or one user's."""
    base = agent_dir / RELEASES_DIRNAME
    return base / layout.USERS / username if username else base / "shared"


def data_root(agent_dir: Path, username: str) -> Path:
    base = agent_dir / DATA_DIRNAME
    return base / layout.USERS / username if username else base / "shared"


def app_release_dir(row: dict) -> Path:
    """The row's release root. Joined in the shape the scanner recognizes as
    confined (``path_confinement``): the id and the bucket come from the
    row, but a slug-derived check id reaches here from the hooks too."""
    agent_dir = config.get_agent_dir(row["agent"])
    return join_under(bucket_root(agent_dir, row.get("username") or ""), row["id"])


def app_data_dir(row: dict) -> Path:
    """The one directory an app process writes (``/app/data`` inside its
    sandbox). Keyed by slug, so a hard unpin keeps it and a later pin of the
    same slug finds it again."""
    agent_dir = config.get_agent_dir(row["agent"])
    return join_under(data_root(agent_dir, row.get("username") or ""), row["slug"])


def preview_dir(row: dict) -> Path:
    return app_release_dir(row) / PREVIEW_DIRNAME


def log_path(row: dict) -> Path:
    return app_release_dir(row) / LOG_NAME


def db_before_path(row: dict, n: int) -> Path:
    return app_release_dir(row) / str(n) / DB_BEFORE_NAME


def rollback_snapshot_path(row: dict, stamp: str) -> Path:
    return app_release_dir(row) / f"rollback-{stamp}.sqlite"


def _is_release(row: dict, child: Path) -> bool:
    if not child.name.isdigit():
        return False
    marker = MANIFEST_NAME if db_apps.app_kind_of(row).serves_tree else "index.html"
    return (child / marker).is_file()


def _all_numbers(row: dict) -> list[int]:
    d = app_release_dir(row)
    if not d.is_dir():
        return []
    return sorted(int(c.name) for c in d.iterdir() if _is_release(row, c))


def release_numbers(row: dict) -> list[int]:
    """The releases that were live at some point — a pending copy (waiting
    for approval, never started) is not one of them."""
    pending = int(row.get("pending_release") or 0)
    return [n for n in _all_numbers(row) if n != pending]


def current_number(row: dict) -> int:
    """The release the row serves (0 = not deployed yet: the working tree)."""
    rel = row.get("release_path") or ""
    parts = rel.rstrip("/").split("/")
    if db_apps.app_kind_of(row).serves_tree:
        return int(parts[-1]) if parts and parts[-1].isdigit() else 0
    if len(parts) >= 2 and parts[-1] == "index.html" and parts[-2].isdigit():
        return int(parts[-2])
    return 0


def previous_number(row: dict) -> int | None:
    cur = current_number(row)
    older = [n for n in release_numbers(row) if n < cur]
    return max(older) if older else None


def _release_rel(row: dict, n: int) -> str:
    agent_dir = config.get_agent_dir(row["agent"])
    return (app_release_dir(row) / str(n) / "index.html").relative_to(agent_dir).as_posix()


def _release_dir_rel(row: dict, n: int) -> str:
    agent_dir = config.get_agent_dir(row["agent"])
    return (app_release_dir(row) / str(n)).relative_to(agent_dir).as_posix()


def prune(row: dict, keep: int = KEEP_RELEASES) -> None:
    """Remove the oldest live releases beyond ``keep``. A file row prunes at
    the cut (cut and live are one step); a folder row prunes when a release
    goes live, so a pending copy (never counted here) leaves the older
    releases alone until it is approved."""
    numbers = release_numbers(row)
    for old in numbers[:-keep] if len(numbers) > keep else []:
        shutil.rmtree(app_release_dir(row) / str(old), ignore_errors=True)


def read_working_file(row: dict) -> bytes:
    """A single-file app's working file, read as a regular file inside the
    agent's tree (``FileNotFoundError`` when it is gone, another ``OSError``
    when it is a link, a special file or over the cap)."""
    root, rel = agents_rel(config.get_agent_dir(row["agent"]) / (row.get("rel_path") or ""))
    return safe_fs.read_bytes_beneath(root, rel, max_size=FILE_APP_MAX_BYTES)


def cut_release(row: dict, source: Path) -> tuple[str, str]:
    """Copy the working file into the next release slot and prune the old
    ones. Returns the agent-dir-relative path and the sha256 of the copy.
    ``ReleaseInvalid`` when the working file is over ``FILE_APP_MAX_BYTES``
    or is not a regular file inside the agent's tree."""
    try:
        root, rel = agents_rel(source)
        data = safe_fs.read_bytes_beneath(root, rel, max_size=FILE_APP_MAX_BYTES)
    except safe_fs.FileTooLarge as e:
        raise ReleaseInvalid(
            f"{source.name} is larger than {FILE_APP_MAX_BYTES // (1024 * 1024)} MB") from e
    except OSError as e:
        raise ReleaseInvalid(f"{source.name} is not a regular file in the workspace") from e
    numbers = _all_numbers(row)
    n = (numbers[-1] + 1) if numbers else 1
    root, rel = agents_rel(app_release_dir(row) / str(n) / "index.html")
    safe_fs.atomic_write_beneath(root, rel, data, mode=RELEASE_FILE_MODE, mkdirs=True, fsync=False)
    prune(row)
    return _release_rel(row, n), _sha256(data)


def walk_tree(source_dir: Path) -> list[tuple[str, Path]]:
    """The files a release takes from a working tree, as ``(relative path,
    file)`` pairs in walk order: no ``node_modules``, no ``data/``, no
    dotfiles, no symlinks (a link to a file refused, not skipped, since it could
    point outside the tree; a link to a directory skipped), and within the
    caps. Walked from descriptors beneath the tree's root
    (``services/infra/safe_fs.py``), so a directory swapped for a link while
    it runs is never followed. A missing folder is empty; anything else that
    cannot be read is ``ReleaseInvalid``."""
    return [(rel, source_dir / rel) for rel, _size in _walk_sized(source_dir)]


def _link_is_dir(dirfd: int, name: str) -> bool:
    """Whether a link names a directory (metadata only: the link is never
    opened)."""
    try:
        return stat.S_ISDIR(os.stat(name, dir_fd=dirfd, follow_symlinks=True).st_mode)
    except OSError:
        return False


def _walk_sized(source_dir: Path) -> list[tuple[str, int]]:
    try:
        root, base = tree_root(source_dir)
    except safe_fs.SafeFsError:
        raise ReleaseInvalid("the app folder is reached through a link: make it a real folder")
    out: list[tuple[str, int]] = []
    total = 0
    started = False
    try:
        for step in safe_fs.walk_beneath(root, base):
            started = True
            rel_root = step.rel[len(base):].lstrip("/") if base else step.rel
            step.dirs[:] = [d for d in step.dirs if not d.startswith(".") and d not in SKIP_DIR_NAMES]
            for name in step.symlinks:
                if name.startswith(".") or _link_is_dir(step.dirfd, name):
                    continue
                raise ReleaseInvalid(f"symlink not allowed in an app: {_join(rel_root, name)}")
            for name in step.files:
                if name.startswith("."):
                    continue
                size = os.stat(name, dir_fd=step.dirfd, follow_symlinks=False).st_size
                if size > MAX_RELEASE_FILE_BYTES:
                    raise ReleaseInvalid(
                        f"{name} is larger than {MAX_RELEASE_FILE_BYTES // (1024 * 1024)} MB")
                total += size
                if total > MAX_RELEASE_BYTES:
                    raise ReleaseInvalid(
                        f"the app is larger than {MAX_RELEASE_BYTES // (1024 * 1024)} MB")
                out.append((f"{rel_root}/{name}" if rel_root else name, size))
                if len(out) > MAX_RELEASE_FILES:
                    raise ReleaseInvalid(f"the app has more than {MAX_RELEASE_FILES} files")
    except FileNotFoundError:
        if not started:
            return []
        raise ReleaseInvalid("the app folder changed while it was read; try again")
    except safe_fs.SymlinkRefused:
        raise ReleaseInvalid("the app folder is reached through a link: make it a real folder")
    except OSError as e:
        raise ReleaseInvalid(f"the app folder could not be read ({e.strerror or 'error'})")
    return out


def tree_bytes(source_dir: Path) -> int:
    """The bytes a release of the working tree takes (the walk's sizes)."""
    return sum(size for _rel, size in _walk_sized(source_dir))


def copy_tree(source_dir: Path, dest_dir: Path, *, replace: bool = False) -> str:
    """Copy the files a release takes (``walk_tree``) from a working tree
    into ``dest_dir`` under the agents tree, each read beneath its root
    without following a link and written with ``RELEASE_FILE_MODE``, then
    ``manifest.json`` and the empty ``data/`` mount point; returns the
    manifest text. ``replace`` clears ``dest_dir`` first (after the walk: a
    refused tree touches nothing). A file that turned into a link, vanished
    or outgrew a cap after the walk is ``ReleaseInvalid``."""
    files = walk_tree(source_dir)
    src_root, src_base = tree_root(source_dir)
    dst_root, dst_base = agents_rel(dest_dir)
    if replace:
        safe_fs.rmtree_beneath(dst_root, dst_base, missing_ok=True)
    entries: dict[str, dict] = {}
    total = 0
    for rel, _path in files:
        digest = hashlib.sha256()
        try:
            size = safe_fs.copy_file_beneath(
                src_root, _join(src_base, rel), dst_root, _join(dst_base, rel),
                max_size=MAX_RELEASE_FILE_BYTES, mode=RELEASE_FILE_MODE, mkdirs=True,
                on_chunk=digest.update)
        except safe_fs.FileTooLarge:
            raise ReleaseInvalid(f"{rel.rsplit('/', 1)[-1]} is larger than "
                                 f"{MAX_RELEASE_FILE_BYTES // (1024 * 1024)} MB")
        except OSError as e:
            if e.errno in (errno.ENOSPC, errno.EDQUOT):
                raise ReleaseInvalid("the disk or the storage quota is full")
            raise ReleaseInvalid(f"{rel} changed while it was copied, deploy again")
        total += size
        if total > MAX_RELEASE_BYTES:
            raise ReleaseInvalid(f"the app is larger than {MAX_RELEASE_BYTES // (1024 * 1024)} MB")
        entries[rel] = {"sha256": digest.hexdigest(), "size": size}
    text = manifest_text(entries)
    safe_fs.atomic_write_beneath(dst_root, _join(dst_base, MANIFEST_NAME), text.encode("utf-8"),
                                 mode=RELEASE_FILE_MODE, mkdirs=True, fsync=False)
    # The mount point of the app's data (bwrap cannot create one under the
    # read-only release bind); empty, never listed in the manifest. A file
    # of that name in the tree is the app's mistake, not a server error.
    try:
        safe_fs.mkdirs_beneath(dst_root, _join(dst_base, "data"))
    except (FileExistsError, NotADirectoryError):
        raise ReleaseInvalid("data is reserved for the app's database, it cannot be a file")
    return text


def manifest_text(entries: dict[str, dict]) -> str:
    """The canonical manifest: sorted, compact, nothing but the files — so
    an identical tree always hashes the same."""
    return json.dumps({"files": entries}, sort_keys=True, separators=(",", ":"))


def cut_folder_release(row: dict, source_dir: Path, *, after: int = 0) -> tuple[str, str, int]:
    """Copy the working tree into the next release slot (written as
    ``<n>.tmp`` and renamed) with its manifest. Returns the agent-dir-
    relative directory, the tree hash and the number. Raises
    ``ReleaseInvalid`` before copying anything. The caller prunes once the
    release is live (``prune``): a copy cut for approval must not evict a
    release that still serves. ``after`` is a number that must never be
    handed out again (a waiting copy just replaced: an approval card still
    showing it names it, and a new tree under that number would ride it)."""
    walk_tree(source_dir)   # a refused tree writes nothing, not even the tmp slot
    numbers = _all_numbers(row)
    n = max(numbers[-1] if numbers else 0, after) + 1
    base = app_release_dir(row)
    root, base_rel = agents_rel(base)
    tmp_rel = _join(base_rel, f"{n}.tmp")
    safe_fs.rmtree_beneath(root, tmp_rel, missing_ok=True)
    try:
        text = copy_tree(source_dir, base / f"{n}.tmp")
        safe_fs.rename_beneath(root, tmp_rel, _join(base_rel, str(n)), replace=True)
    except BaseException:
        safe_fs.rmtree_beneath(root, tmp_rel, missing_ok=True)
        raise
    return _release_dir_rel(row, n), _sha256(text.encode("utf-8")), n


_manifest_cache: dict[str, tuple[tuple[int, int], dict]] = {}


def read_manifest(release_dir: Path) -> dict | None:
    """The parsed manifest of one release directory (cached by mtime and
    size); None when the directory holds none."""
    path = release_dir / MANIFEST_NAME
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _manifest_cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        doc = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, dict):
        return None
    _manifest_cache[key] = (stamp, doc)
    return doc


def tree_sha(release_dir: Path) -> str:
    path = release_dir / MANIFEST_NAME
    try:
        return _sha256(path.read_bytes())
    except OSError:
        return ""


def find_release_by_sha(row: dict, sha: str) -> Path | None:
    """The release directory (live, older, pending, the preview copy or a
    check copy a render job holds) whose tree hash is ``sha``; the client
    routes address releases this way. None when no copy of this row hashes
    so."""
    if not sha or not db_apps.app_kind_of(row).serves_tree:
        return None
    base = app_release_dir(row)
    if not base.is_dir():
        return None
    candidates = [base / str(n) for n in _all_numbers(row)]
    candidates.append(preview_dir(row))
    candidates.extend(sorted(base.glob(f"{CHECK_DIR_PREFIX}*")))
    for d in candidates:
        if (d / MANIFEST_NAME).is_file() and tree_sha(d) == sha:
            return d
    return None


def read_release_file(release_dir: Path, rel: str) -> bytes | None:
    """One file of a release, verified against the manifest; None when the
    manifest does not list it, ``ReleaseDamaged`` when the bytes changed."""
    manifest = read_manifest(release_dir)
    if manifest is None:
        return None
    entry = manifest["files"].get(rel)
    if not isinstance(entry, dict):
        return None
    path = release_dir / rel
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if _sha256(data) != (entry.get("sha256") or ""):
        raise ReleaseDamaged(rel)
    return data


def point_to(row: dict, n: int) -> tuple[str, str]:
    """The path and hash of an existing release (for rollback)."""
    if db_apps.app_kind_of(row).serves_tree:
        d = app_release_dir(row) / str(n)
        sha = tree_sha(d)
        if not sha:
            raise ReleaseDamaged(str(n))
        return _release_dir_rel(row, n), sha
    path = app_release_dir(row) / str(n) / "index.html"
    return _release_rel(row, n), _sha256(path.read_bytes())


def live_release_dir(row: dict) -> Path | None:
    """The directory of a folder row's live release, verified against the
    row's tree hash; None when the row has no release or the copy is gone,
    ``ReleaseDamaged`` when the manifest changed."""
    rel = row.get("release_path") or ""
    if not rel or not db_apps.app_kind_of(row).serves_tree:
        return None
    d = config.get_agent_dir(row["agent"]) / rel
    if not (d / MANIFEST_NAME).is_file():
        return None
    if tree_sha(d) != (row.get("release_sha256") or ""):
        raise ReleaseDamaged(rel)
    return d


def read_release(row: dict) -> bytes | None:
    """The bytes the row's release names — the html of a file row, the
    ``client/index.html`` of a folder row — verified against the hash; None
    when the copy is gone (a file row's caller falls back to the working
    file)."""
    if db_apps.app_kind_of(row).serves_tree:
        d = live_release_dir(row)
        return None if d is None else read_release_file(d, "client/index.html")
    rel = row.get("release_path") or ""
    if not rel:
        return None
    path = config.get_agent_dir(row["agent"]) / rel
    if not path.is_file():
        return None
    data = path.read_bytes()
    if _sha256(data) != (row.get("release_sha256") or ""):
        raise ReleaseDamaged(rel)
    return data


def remove_release_dir(row: dict) -> None:
    shutil.rmtree(app_release_dir(row), ignore_errors=True)


def remove_data_dir(row: dict) -> None:
    shutil.rmtree(app_data_dir(row), ignore_errors=True)


def reconcile() -> dict[str, int]:
    """Boot pass: a row whose release copy is missing falls back to the
    working tree (the column is cleared and logged); a release directory
    with no row is removed. ``app-data`` is never touched here (a hard
    unpin keeps it on purpose). Returns the counts."""
    cleared = reaped = 0
    rows = task_store.list_released_apps()
    for row in rows:
        target = config.get_agent_dir(row["agent"]) / row["release_path"]
        present = (target / MANIFEST_NAME).is_file() if db_apps.app_kind_of(row).serves_tree else target.is_file()
        if not present:
            task_store.clear_app_release(row["id"])
            cleared += 1
            logger.warning(
                "App release missing on disk, serving the working tree: app=%s path=%s",
                row.get("slug"), row["release_path"],
            )
    live_ids = {r["id"] for r in task_store.list_all_app_ids()}
    agents_dir = Path(config.AGENTS_DIR)
    if agents_dir.is_dir():
        for agent_dir in agent_dirs(agents_dir):
            base = agent_dir / RELEASES_DIRNAME
            if not base.is_dir():
                continue
            buckets = [base / "shared"]
            users = base / layout.USERS
            if users.is_dir():
                buckets.extend(p for p in users.iterdir() if p.is_dir())
            for bucket in buckets:
                if not bucket.is_dir():
                    continue
                for app_dir in bucket.iterdir():
                    if app_dir.is_dir() and app_dir.name not in live_ids:
                        shutil.rmtree(app_dir, ignore_errors=True)
                        reaped += 1
    if cleared or reaped:
        logger.info("App releases reconciled: %d row(s) fell back, %d orphan dir(s) removed",
                    cleared, reaped)
    return {"cleared": cleared, "reaped": reaped}
