"""Filesystem access beneath a trusted root on a satellite, no component
followed: the twin of the proxy's ``services/infra/safe_fs`` for the subset
the file-push applier, the pull, the stat and the sessions' own folders use,
on the three host families the daemon runs on.

A path string checked once and opened again later is a race: between the
check and the use a process that can write under the root swaps a component
for a link, and the open follows it wherever the daemon can write. Every
function here refuses ANY link below the root, the last component included.

- POSIX (Linux, macOS): a per-component walk from a held root descriptor,
  each directory opened from the previous handle with ``O_NOFOLLOW`` (a
  swap behind a held handle changes nothing, a swap ahead is refused), the
  leaf with ``O_NOFOLLOW`` and checked with ``fstat``; writes stage a temp
  created ``O_EXCL`` in the parent handle and rename it there.
- Windows: no ``dir_fd`` and no ``O_NOFOLLOW`` exist for Python here, so
  every component of the chain is checked with ``lstat`` and refused when it
  is a name-surrogate reparse point (a symlink, a junction, a mount point;
  a OneDrive placeholder or a compressed file carries the reparse attribute
  too and is admitted), the temp is created ``O_EXCL`` under a random name
  (nothing can be planted at it), the chain is re-checked right before the
  commit, and the object opened is compared with the object checked. What
  remains on Windows is a check-to-use window for a process of the same OS
  user, which already holds every right the daemon has.

Rules: ``root`` is an absolute path to a directory no untrusted process can
replace (the agents root, opened by its realpath) or a root handle from
``open_root``; ``rel`` is relative ``/``-separated text, an empty, ``.`` or
``..`` segment and NUL refused before any call (a ``\\`` or ``:`` inside a
name too on Windows, where they are separators). A refusal is a
``SafeFsError`` (an ``OSError``); a missing path stays ``FileNotFoundError``.
No open blocks on a FIFO. Standard library only; the branch is chosen at
call time from ``config.HOST.posix``. Every call blocks: run it off the
event loop.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from .. import config

__all__ = [
    "SafeFsError", "UnsafePathError", "SymlinkRefused", "NotRegularFile", "FileTooLarge",
    "TooManyEntries", "Root", "Listing", "open_root", "atomic_write_beneath",
    "open_append_beneath", "mkdirs_beneath", "unlink_beneath", "rmdir_beneath",
    "rmtree_beneath", "rename_beneath", "lstat_beneath", "list_beneath",
    "open_regular_for_read", "read_bytes_beneath", "commit_partial", "is_link_like",
    "sweep_temps_beneath",
]


class SafeFsError(OSError):
    """A path refused because using it could leave the root."""


class UnsafePathError(SafeFsError):
    """``rel`` is absolute, carries NUL or a separator inside a name, or has a
    ``..``, ``.`` or empty segment; or a root is not an absolute path."""


class SymlinkRefused(SafeFsError):
    """A component of the path, or the path itself, is a link (a reparse
    point that is a name surrogate on Windows)."""


class NotRegularFile(SafeFsError):
    """The path names a FIFO, socket, device or directory where a regular
    file was wanted, or a file where a directory was."""


class FileTooLarge(SafeFsError):
    """The file is bigger than the caller's cap."""


class TooManyEntries(SafeFsError):
    """A folder's listing would pass the caller's cap on its entries."""


# A temp name is ``.<name>.<12 hex>.partial`` and must fit NAME_MAX (255);
# the suffix is the one the sync manifest skips.
_TEMP_NAME_BYTES = 200
# ``IsReparseTagNameSurrogate``: the bit a symlink, a junction and a mount
# point carry and a placeholder or a compressed file does not.
_NAME_SURROGATE = 0x20000000
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOCTTY = getattr(os, "O_NOCTTY", 0)


def _posix() -> bool:
    return bool(config.HOST.posix)


def _components(rel) -> list[str]:
    rel = os.fspath(rel)
    if not isinstance(rel, str):
        raise UnsafePathError(errno.EINVAL, "path must be text", repr(rel))
    if rel == "":
        return []
    if "\x00" in rel or rel.startswith("/"):
        raise UnsafePathError(errno.EINVAL, "not a relative path", rel)
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UnsafePathError(errno.EINVAL, "empty or dot segment", rel)
    if not _posix() and any("\\" in p or ":" in p for p in parts):
        raise UnsafePathError(errno.EINVAL, "separator inside a name", rel)
    return parts


def _temp_prefix(name: str) -> str:
    return f".{os.fsdecode(os.fsencode(name)[:_TEMP_NAME_BYTES])}."


def _temp_name(name: str) -> str:
    return f"{_temp_prefix(name)}{secrets.token_hex(6)}.partial"


class Root:
    """A held root: a directory descriptor on POSIX, a verified absolute path
    on Windows. Made by ``open_root``; closed with it."""

    __slots__ = ("fd", "path")

    def __init__(self, fd: int = -1, path: str = "") -> None:
        self.fd = fd
        self.path = path


class Listing:
    """One folder's names by kind, each sorted, as its listing reported them
    (a caller that opens one goes through the helpers, which check again):
    ``links`` holds the links (on Windows every name surrogate, a junction
    too), ``other`` a pipe, a socket, a device or a name that could not be
    told."""

    __slots__ = ("dirs", "files", "links", "other")

    def __init__(self) -> None:
        self.dirs: list[str] = []
        self.files: list[str] = []
        self.links: list[str] = []
        self.other: list[str] = []


def _root_path(root) -> str:
    path = os.fspath(root)
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
        raise UnsafePathError(errno.EINVAL, "a root must be an absolute path", repr(root))
    return os.path.realpath(path)


# --- POSIX: the held-handle walk ------------------------------------------

def _dir_flags() -> int:
    return (getattr(os, "O_PATH", 0) or os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC


def _refusal(exc: OSError, dirfd: int, name: str, shown: str) -> OSError:
    if exc.errno == errno.ELOOP:
        return SymlinkRefused(errno.ELOOP, "symlink refused", shown)
    if exc.errno == errno.ENXIO:
        return NotRegularFile(errno.ENXIO, "not a regular file", shown)
    if exc.errno in (errno.ENOTDIR, errno.EMLINK) and name:
        with contextlib.suppress(OSError):
            if stat.S_ISLNK(os.stat(name, dir_fd=dirfd, follow_symlinks=False).st_mode):
                return SymlinkRefused(errno.ELOOP, "symlink refused", shown)
    return type(exc)(exc.errno, exc.strerror, shown)


def _step_dir(dirfd: int, name: str, shown: str) -> int:
    try:
        fd = os.open(name, _dir_flags(), dir_fd=dirfd)
    except OSError as exc:
        raise _refusal(exc, dirfd, name, shown) from None
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), shown)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _parent_posix(rootfd: int, parts: list[str], *, mkdirs: bool = False) -> int:
    """A handle on ``parts`` beneath ``rootfd`` (created when ``mkdirs``),
    every step reached without following; the caller closes it."""
    fd = os.open(".", _dir_flags() & ~os.O_NOFOLLOW, dir_fd=rootfd)
    try:
        for i, name in enumerate(parts):
            shown = "/".join(parts[: i + 1])
            if mkdirs:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o777, dir_fd=fd)
            nxt = _step_dir(fd, name, shown)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _listing_fd(fd: int) -> int:
    """A handle that can list the folder ``fd`` names (the walk's handle may
    only name it: ``O_PATH``); the caller closes it."""
    return os.open(".", os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC, dir_fd=fd)


def _clear_files_posix(fd: int) -> str | None:
    """Unlink every entry of the folder ``fd`` that is not a folder (a link
    among them, never followed); the name of one folder left, if any."""
    lfd = _listing_fd(fd)
    try:
        names = os.listdir(lfd)
    finally:
        os.close(lfd)
    folder = None
    for name in names:
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(st.st_mode):
            folder = folder or name
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=fd)
    return folder


def _empty_posix(top: int, shown: str) -> None:
    """Empty the folder ``top`` holds with no recursion and no link followed:
    each pass removes the files of the folder it stands in, steps down into
    a folder while there is one, and removes the empty folder it reached
    from its parent. Two handles at a time; going back up opens the names
    again from ``top``, so no ``..`` is ever followed. An entry another
    process removed meanwhile is passed over."""
    path: list[str] = []
    while True:
        try:
            cur = _parent_posix(top, path)
        except FileNotFoundError:
            if not path:
                raise
            path.pop()
            continue
        parent: int | None = None
        try:
            while True:
                sub = _clear_files_posix(cur)
                if sub is None:
                    break
                try:
                    nxt = _step_dir(cur, sub, "/".join([shown, *path, sub]))
                except FileNotFoundError:
                    continue
                if parent is not None:
                    os.close(parent)
                parent, cur = cur, nxt
                path.append(sub)
            if not path:
                return
            if parent is None:
                parent = _parent_posix(top, path[:-1])
            with contextlib.suppress(FileNotFoundError):
                os.rmdir(path.pop(), dir_fd=parent)
        finally:
            os.close(cur)
            if parent is not None:
                os.close(parent)


def _open_leaf_posix(pfd: int, name: str, flags: int, mode: int, shown: str) -> int:
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | _O_NOCTTY | _O_CLOEXEC,
                     mode, dir_fd=pfd)
    except OSError as exc:
        raise _refusal(exc, pfd, name, shown) from None
    try:
        st = os.fstat(fd)
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise NotRegularFile(errno.EINVAL, "not a regular file", shown)
        os.set_blocking(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


# --- Windows: the checked chain --------------------------------------------

def _w_lstat(path: str) -> os.stat_result:
    return os.lstat(path)


def _w_fstat(fd: int) -> os.stat_result:
    return os.fstat(fd)


def _w_open(path: str, flags: int, mode: int) -> int:
    return os.open(path, flags | _O_BINARY, mode)


def _w_mkdir(path: str) -> None:
    os.mkdir(path)


def _w_replace(src: str, dst: str) -> None:
    config.atomic_replace(Path(src), Path(dst))


def _w_rename(src: str, dst: str) -> None:
    os.rename(src, dst)


def _w_unlink(path: str) -> None:
    os.unlink(path)


def _w_rmdir(path: str) -> None:
    os.rmdir(path)


def _w_rmtree(path: str) -> None:
    config.force_rmtree(Path(path))


def _w_entry_lstat(entry: os.DirEntry) -> os.stat_result:
    # The listing's own record: the reparse tag comes with it, no second call.
    return entry.stat(follow_symlinks=False)


def _is_surrogate(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_reparse_tag", 0) & _NAME_SURROGATE)


def is_link_like(path) -> bool:
    """Is ``path`` itself a symlink, a junction or a mount point (a name
    that stands for another place)? False when it cannot be stat'ed."""
    try:
        return _is_surrogate(os.lstat(path))
    except OSError:
        return False


def _w_kind(entry: os.DirEntry) -> str:
    st = _w_entry_lstat(entry)
    if _is_surrogate(st):
        return "links"
    if stat.S_ISDIR(st.st_mode):
        return "dirs"
    if stat.S_ISREG(st.st_mode):
        return "files"
    return "other"


def _w_check_dir(path: str, shown: str) -> None:
    st = _w_lstat(path)
    if _is_surrogate(st):
        raise SymlinkRefused(errno.ELOOP, "symlink refused", shown)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), shown)


def _w_parent(root: str, parts: list[str], *, mkdirs: bool = False) -> str:
    """The parent path under ``root`` with every component checked (created
    when ``mkdirs``, then checked as a real directory)."""
    cur = root
    for i, name in enumerate(parts):
        cur = os.path.join(cur, name)
        shown = "/".join(parts[: i + 1])
        if mkdirs:
            with contextlib.suppress(FileExistsError):
                _w_mkdir(cur)
        _w_check_dir(cur, shown)
    return cur


def _w_leaf_stat(path: str, shown: str) -> os.stat_result | None:
    """The leaf's own status, None when absent; a name surrogate refused."""
    try:
        st = _w_lstat(path)
    except FileNotFoundError:
        return None
    if _is_surrogate(st):
        raise SymlinkRefused(errno.ELOOP, "symlink refused", shown)
    return st


def _w_same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) or not (a.st_ino and b.st_ino)


# --- the API ---------------------------------------------------------------

@contextlib.contextmanager
def open_root(root, rel: str = "") -> Iterator[Root]:
    """A held root on ``root`` (an absolute directory path, opened by its
    realpath) or on ``root/rel`` reached strictly: ``open_root(agents_dir,
    slug)`` for an agent's folder. A ``Root`` passes through when ``rel`` is
    empty."""
    parts = _components(rel)
    if isinstance(root, Root):
        if not parts:
            yield root
            return
        base_fd, base_path = root.fd, root.path
        own = False
    else:
        base_path = _root_path(root)
        base_fd = -1
        own = True
        if _posix():
            base_fd = os.open(base_path, _dir_flags() & ~os.O_NOFOLLOW)
    try:
        if _posix():
            if parts:
                fd = _parent_posix(base_fd, parts)
                try:
                    yield Root(fd=fd)
                finally:
                    os.close(fd)
            else:
                yield Root(fd=base_fd)
        else:
            yield Root(path=_w_parent(base_path, parts))
    finally:
        if own and base_fd >= 0:
            os.close(base_fd)


def _leaf(rel) -> tuple[list[str], str, str]:
    parts = _components(rel)
    if not parts:
        raise UnsafePathError(errno.EINVAL, "names the root itself", "")
    return parts[:-1], parts[-1], "/".join(parts)


def atomic_write_beneath(root, rel, data: bytes, *, mkdirs: bool = False,
                         fsync: bool = True, mode: int = 0o644) -> None:
    """Replace ``rel`` with ``data``: a temp created ``O_EXCL`` with ``mode``
    next to it and renamed onto the name within the parent (a link at the
    name is replaced, never written through; a dangling one never creates
    its target); the temp is removed on any failure."""
    parents, name, shown = _leaf(rel)
    with open_root(root) as r:
        if _posix():
            pfd = _parent_posix(r.fd, parents, mkdirs=mkdirs)
            try:
                tmp = _temp_name(name)
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC,
                             mode, dir_fd=pfd)
                done = False
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)
                        fh.flush()
                        if fsync:
                            os.fsync(fh.fileno())
                    os.rename(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
                    done = True
                finally:
                    if not done:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp, dir_fd=pfd)
            finally:
                os.close(pfd)
            return
        parent = _w_parent(r.path, parents, mkdirs=mkdirs)
        target = os.path.join(parent, name)
        tmp = os.path.join(parent, _temp_name(name))
        fd = _w_open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        done = False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                if fsync:
                    os.fsync(fh.fileno())
            leaf = _w_leaf_stat(target, shown)
            if leaf is not None and stat.S_ISDIR(leaf.st_mode):
                raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), shown)
            _w_parent(r.path, parents)  # the chain, right before the commit
            _w_replace(tmp, target)
            done = True
        finally:
            if not done:
                with contextlib.suppress(OSError):
                    _w_unlink(tmp)


def sweep_temps_beneath(root, rel, *, older_than_s: float) -> int:
    """Remove the temps ``atomic_write_beneath`` left beside ``rel`` (a write
    a killed process never finished) once older than ``older_than_s``:
    regular files only, by descriptor, never through a link. The number
    removed; POSIX only, a missing parent removes nothing."""
    if not _posix():
        return 0
    parents, name, _shown = _leaf(rel)
    # The temp's two ends as ``_temp_name`` writes them, twelve hex between.
    prefix = _temp_prefix(name)
    suffix = _temp_name(name)[len(prefix) + 12:]
    cutoff = time.time() - older_than_s
    removed = 0
    with open_root(root) as r:
        try:
            pfd = _parent_posix(r.fd, parents)
        except FileNotFoundError:
            return 0
        try:
            # The walk's descriptor may only name the folder (O_PATH): its
            # listing opens the same folder for reading.
            lfd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC, dir_fd=pfd)
            try:
                names = os.listdir(lfd)
            finally:
                os.close(lfd)
            for entry in names:
                if not (entry.startswith(prefix) and entry.endswith(suffix)
                        and len(entry) == len(prefix) + 12 + len(suffix)):
                    continue
                with contextlib.suppress(FileNotFoundError):
                    st = os.stat(entry, dir_fd=pfd, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                        os.unlink(entry, dir_fd=pfd)
                        removed += 1
        finally:
            os.close(pfd)
    return removed


def open_append_beneath(root, rel, *, truncate: bool = False, mkdirs: bool = True,
                        create: bool = True) -> int:
    """A descriptor to append to ``rel`` (created when absent unless
    ``create`` is off, when a missing file is ``FileNotFoundError``;
    truncated first when ``truncate``), a regular file reached without a
    link. The caller closes it."""
    parents, name, shown = _leaf(rel)
    flags = (os.O_WRONLY | (os.O_CREAT if create else 0)
             | (os.O_TRUNC if truncate else os.O_APPEND))
    with open_root(root) as r:
        if _posix():
            pfd = _parent_posix(r.fd, parents, mkdirs=mkdirs)
            try:
                fd = _open_leaf_posix(pfd, name, flags, 0o644, shown)
            finally:
                os.close(pfd)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                os.close(fd)
                raise NotRegularFile(errno.EISDIR, "not a regular file", shown)
            return fd
        parent = _w_parent(r.path, parents, mkdirs=mkdirs)
        target = os.path.join(parent, name)
        before = _w_leaf_stat(target, shown)
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise NotRegularFile(errno.EINVAL, "not a regular file", shown)
        fd = _w_open(target, flags, 0o644)
        try:
            after = _w_fstat(fd)
            if not stat.S_ISREG(after.st_mode) or (before is not None and not _w_same(before, after)):
                raise NotRegularFile(errno.EINVAL, "not the file that was checked", shown)
        except BaseException:
            os.close(fd)
            raise
        return fd


def commit_partial(root, partial_rel, rel) -> None:
    """Rename the staged ``partial_rel`` onto ``rel`` (replacing what is
    there) within the parent; a missing partial is ``FileNotFoundError``."""
    rename_beneath(root, partial_rel, rel, replace=True)


def mkdirs_beneath(root, rel) -> None:
    """``os.makedirs`` beneath the root: every missing directory of ``rel``
    is created and none is ever reached through a link."""
    parts = _components(rel)
    if not parts:
        return
    with open_root(root) as r:
        if _posix():
            os.close(_parent_posix(r.fd, parts, mkdirs=True))
        else:
            _w_parent(r.path, parts, mkdirs=True)


def lstat_beneath(root, rel) -> os.stat_result:
    """The status of ``rel`` itself (a link is reported, never followed; a
    link on the way is refused)."""
    parents, name, shown = _leaf(rel)
    with open_root(root) as r:
        if _posix():
            pfd = _parent_posix(r.fd, parents)
            try:
                return os.stat(name, dir_fd=pfd, follow_symlinks=False)
            finally:
                os.close(pfd)
        return _w_lstat(os.path.join(_w_parent(r.path, parents), name))


def _sort_entries(entries, max_entries: int, shown: str,
                  kind: Callable[[os.DirEntry], str]) -> Listing:
    out = Listing()
    seen = 0
    for entry in entries:
        seen += 1
        if seen > max_entries:
            raise TooManyEntries(errno.EFBIG, "more entries than the listing's cap", shown)
        try:
            bucket = kind(entry)
        except OSError:
            bucket = "other"
        getattr(out, bucket).append(entry.name)
    for names in (out.dirs, out.files, out.links, out.other):
        names.sort()
    return out


def _posix_kind(entry: os.DirEntry) -> str:
    if entry.is_symlink():
        return "links"
    if entry.is_dir(follow_symlinks=False):
        return "dirs"
    if entry.is_file(follow_symlinks=False):
        return "files"
    return "other"


def list_beneath(root, rel: str = "", *, max_entries: int) -> Listing:
    """The names in the folder ``rel`` (the root itself for ``""``) by kind,
    the folder reached as every call here reaches it; a folder of more than
    ``max_entries`` entries raises ``TooManyEntries`` as it is read, before
    its names are held or sorted. A link at ``rel`` is refused."""
    parts = _components(rel)
    shown = "/".join(parts)
    with open_root(root) as r:
        if _posix():
            fd = _parent_posix(r.fd, parts)
            try:
                lfd = _listing_fd(fd)
            finally:
                os.close(fd)
            try:
                with os.scandir(lfd) as it:
                    return _sort_entries(it, max_entries, shown, _posix_kind)
            finally:
                os.close(lfd)
        folder = _w_parent(r.path, parts)
        with os.scandir(folder) as it:
            return _sort_entries(it, max_entries, shown, _w_kind)


def unlink_beneath(root, rel, *, missing_ok: bool = False) -> None:
    """Remove the file or link at ``rel`` (a link goes, its target stays)."""
    parents, name, shown = _leaf(rel)
    with open_root(root) as r:
        try:
            if _posix():
                pfd = _parent_posix(r.fd, parents)
                try:
                    os.unlink(name, dir_fd=pfd)
                finally:
                    os.close(pfd)
            else:
                target = os.path.join(_w_parent(r.path, parents), name)
                st = _w_lstat(target)
                if stat.S_ISDIR(st.st_mode):
                    if _is_surrogate(st):
                        _w_rmdir(target)  # a junction or directory link: the point itself
                    else:
                        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), shown)
                else:
                    _w_unlink(target)
        except FileNotFoundError:
            if not missing_ok:
                raise


def rmdir_beneath(root, rel) -> None:
    """Remove the EMPTY directory at ``rel`` (a link in its place refused)."""
    parents, name, shown = _leaf(rel)
    with open_root(root) as r:
        if _posix():
            pfd = _parent_posix(r.fd, parents)
            try:
                st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    raise SymlinkRefused(errno.ELOOP, "symlink refused", shown)
                os.rmdir(name, dir_fd=pfd)
            finally:
                os.close(pfd)
            return
        target = os.path.join(_w_parent(r.path, parents), name)
        st = _w_lstat(target)
        if _is_surrogate(st):
            raise SymlinkRefused(errno.ELOOP, "symlink refused", shown)
        _w_rmdir(target)


def rmtree_beneath(root, rel, *, missing_ok: bool = False) -> None:
    """Remove the folder ``rel`` and everything in it. POSIX: entry by entry
    from held handles, no recursion, no link followed (a link inside goes,
    its target stays; the 3.10 floor's ``rmtree`` takes no ``dir_fd``).
    Windows: the chain checked and the name's own status read, then
    ``config.force_rmtree`` (its retries outlast an antivirus's hold). A
    link at ``rel`` itself is refused (``unlink_beneath`` removes one), a
    file there is ``NotADirectoryError``; a missing folder raises unless
    ``missing_ok``."""
    parents, name, shown = _leaf(rel)
    with open_root(root) as r:
        if _posix():
            try:
                pfd = _parent_posix(r.fd, parents)
            except FileNotFoundError:
                if missing_ok:
                    return
                raise
            try:
                try:
                    # A link here is refused, a file is NotADirectoryError.
                    top = _step_dir(pfd, name, shown)
                except FileNotFoundError:
                    if missing_ok:
                        return
                    raise
                try:
                    _empty_posix(top, shown)
                finally:
                    os.close(top)
                os.rmdir(name, dir_fd=pfd)
            finally:
                os.close(pfd)
            return
        try:
            target = os.path.join(_w_parent(r.path, parents), name)
            st = _w_leaf_stat(target, shown)
        except FileNotFoundError:
            st = None
        if st is None:
            if missing_ok:
                return
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), shown)
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), shown)
        _w_rmtree(target)


def rename_beneath(root, src_rel, dst_rel, *, replace: bool = False, mkdirs: bool = False) -> None:
    """Move ``src_rel`` to ``dst_rel`` within the root, both parents reached
    without a link; an existing destination is refused unless ``replace``."""
    s_parents, s_name, s_shown = _leaf(src_rel)
    d_parents, d_name, d_shown = _leaf(dst_rel)
    with open_root(root) as r:
        if _posix():
            spfd = _parent_posix(r.fd, s_parents)
            try:
                dpfd = _parent_posix(r.fd, d_parents, mkdirs=mkdirs)
                try:
                    if not replace:
                        with contextlib.suppress(FileNotFoundError):
                            os.stat(d_name, dir_fd=dpfd, follow_symlinks=False)
                            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), d_shown)
                    os.rename(s_name, d_name, src_dir_fd=spfd, dst_dir_fd=dpfd)
                finally:
                    os.close(dpfd)
            finally:
                os.close(spfd)
            return
        src = os.path.join(_w_parent(r.path, s_parents), s_name)
        dst = os.path.join(_w_parent(r.path, d_parents, mkdirs=mkdirs), d_name)
        if _w_leaf_stat(src, s_shown) is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), s_shown)
        existing = _w_leaf_stat(dst, d_shown)
        if existing is not None and not replace:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), d_shown)
        if replace:
            _w_replace(src, dst)
        else:
            _w_rename(src, dst)


def open_regular_for_read(root, rel, *, max_size: int | None = None) -> tuple[int, os.stat_result]:
    """``(fd, stat)`` of the regular file at ``rel``, refused when it is
    anything else or over ``max_size`` now; a FIFO never blocks the open.
    The caller closes the descriptor and reads from it, never from the name."""
    parents, name, shown = _leaf(rel)
    with open_root(root) as r:
        if _posix():
            pfd = _parent_posix(r.fd, parents)
            try:
                fd = _open_leaf_posix(pfd, name, os.O_RDONLY, 0o666, shown)
            finally:
                os.close(pfd)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise NotRegularFile(errno.EINVAL, "not a regular file", shown)
            except BaseException:
                os.close(fd)
                raise
        else:
            target = os.path.join(_w_parent(r.path, parents), name)
            before = _w_leaf_stat(target, shown)
            if before is None:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), shown)
            if not stat.S_ISREG(before.st_mode):
                raise NotRegularFile(errno.EINVAL, "not a regular file", shown)
            fd = _w_open(target, os.O_RDONLY, 0o666)
            try:
                st = _w_fstat(fd)
                if not stat.S_ISREG(st.st_mode) or not _w_same(before, st):
                    raise NotRegularFile(errno.EINVAL, "not the file that was checked", shown)
            except BaseException:
                os.close(fd)
                raise
    if max_size is not None and st.st_size > max_size:
        os.close(fd)
        raise FileTooLarge(errno.EFBIG, "file is over the size cap", shown)
    return fd, st


def read_bytes_beneath(root, rel, *, max_size: int) -> bytes:
    """The whole file at ``rel``, refused past ``max_size`` (checked again
    while reading)."""
    fd, _st = open_regular_for_read(root, rel, max_size=max_size)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(max_size + 1)
    if len(data) > max_size:
        raise FileTooLarge(errno.EFBIG, "file is over the size cap", os.fspath(rel))
    return data
