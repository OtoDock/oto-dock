"""Filesystem access beneath a trusted root that no symlink swap can redirect.

A path string checked once and opened again later is a race: between the
check and the use, a process that can write under the root swaps a component
for a symlink, and the open follows it wherever the proxy can read or write.
Every function here resolves the path in the kernel, relative to a
descriptor held on the root, and refuses ANY symlink below the root, the
last component included (an in-tree link too: authorization depends on
sub-paths such as ``users/<u>/``), so the object checked and the object
used are the same one.

- Linux 5.6 and later: ``openat2`` with ``RESOLVE_BENEATH |
  RESOLVE_NO_SYMLINKS``, through the raw syscall (Python has no
  ``os.openat2``).
- Otherwise (an older kernel, a seccomp profile that answers EPERM, another
  OS): a per-component walk from the root that holds each directory it
  passed and opens the next one without following, so it cannot be
  redirected either.

Rules for callers:

- ``root`` is an absolute path to a directory no untrusted process can
  replace, opened once by its realpath: ``AGENTS_DIR`` itself, a
  proxy-owned cache. An agent's own folder is NOT such a directory (every
  container that mounts the agents tree runs as the same uid): open it with
  ``open_root(AGENTS_DIR, agent)``, which reaches it strictly. A root may
  also be an open directory descriptor the caller keeps.
- ``rel`` is relative, ``/``-separated text: an absolute path, NUL, and a
  ``..``, ``.`` or empty segment are refused before any syscall; ``""``
  names the root itself where that makes sense.
- A refusal raises ``SafeFsError`` (an ``OSError``), so an existing ``except
  OSError`` arm keeps catching it; a missing path stays
  ``FileNotFoundError``. No open ever blocks on a FIFO or a lease.
- Only ``atomic_writer`` replaces a name; a write through ``open_beneath``
  writes the inode in place (shared with any hard link to it). An atomic
  write needs room for both copies and write permission on the directory.
- Every call blocks: run it in a worker thread, never on the event loop.

Pure stdlib plus ctypes and no proxy import, so an image without the proxy
(file-tools) can carry its own copy.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import os
import posixpath
import secrets
import shutil
import stat
import sys
from collections.abc import Callable, Iterable, Iterator
from typing import BinaryIO

__all__ = [
    "SafeFsError", "UnsafePathError", "SymlinkRefused", "EscapeRefused",
    "NotRegularFile", "HardlinkRefused", "FileTooLarge",
    "openat2_available", "open_root", "open_beneath", "open_dir_beneath",
    "open_regular_for_read", "open_file_for_read", "read_bytes_beneath",
    "copy_fd", "iter_fd", "fd_path", "lstat_beneath", "readlink_beneath",
    "mkdirs_beneath", "atomic_writer", "atomic_write_beneath",
    "copy_file_beneath", "copytree_beneath", "rename_beneath", "move_beneath",
    "unlink_beneath", "rmtree_beneath", "walk_beneath", "WalkStep",
    "rel_under", "split_under", "canonical_rel",
]


class SafeFsError(OSError):
    """A path refused because using it could leave the root."""


class UnsafePathError(SafeFsError):
    """``rel`` is absolute, carries NUL, or has a ``..``, ``.`` or empty
    segment; or a root is not an absolute path or a descriptor."""


class SymlinkRefused(SafeFsError):
    """A component of the path, or the path itself, is a symlink."""


class EscapeRefused(SafeFsError):
    """The path resolves outside the root."""


class NotRegularFile(SafeFsError):
    """The path names a FIFO, socket or device (or a directory where a file
    was wanted)."""


class HardlinkRefused(SafeFsError):
    """The file has more than one link and the caller asked for a single one."""


class FileTooLarge(SafeFsError):
    """The file is bigger than the caller's cap."""


RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
_RESOLVE = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS
# One number on every architecture: syscalls added since 5.1 share a table.
_SYS_OPENAT2 = 437
# openat2 answers EAGAIN only when a rename races a ``..`` (never passed
# here); with O_NONBLOCK a lease being broken says EAGAIN too.
_EAGAIN_RETRIES = 3
_RENAME_NOREPLACE = 1

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_PATH = getattr(os, "O_PATH", 0)
_O_NOCTTY = getattr(os, "O_NOCTTY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_TMPFILE = getattr(os, "O_TMPFILE", 0)
# A directory handle for *at() calls: O_PATH needs only search permission.
_DIR_HANDLE = (_O_PATH or os.O_RDONLY) | os.O_DIRECTORY | _O_CLOEXEC
# A directory handle scandir can list.
_DIR_LIST = os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC
_COPY_CHUNK = 1024 * 1024
# A temp name is ``.<name>.<12 hex>.partial`` and must fit NAME_MAX (255).
_TEMP_NAME_BYTES = 200


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


_libc = None
_openat2_ok: bool | None = None   # None until probed


def _load_libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
    return _libc


def _raw_openat2(dirfd: int, path: str, flags: int, mode: int, resolve: int) -> int:
    how = _OpenHow(flags | _O_CLOEXEC, mode, resolve)
    fd = _load_libc().syscall(
        ctypes.c_long(_SYS_OPENAT2), ctypes.c_int(dirfd),
        ctypes.c_char_p(os.fsencode(path)), ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)
    return fd


def _probe_openat2() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("OTODOCK_SAFE_FS_NO_OPENAT2", "").strip() not in ("", "0"):
        return False
    try:
        root = os.open("/", _DIR_HANDLE)
    except OSError:
        return False
    try:
        os.close(_raw_openat2(root, ".", _DIR_HANDLE, 0, _RESOLVE))
        return True
    except (OSError, AttributeError):
        # ENOSYS before 5.6, EPERM under an older container seccomp profile.
        return False
    finally:
        os.close(root)


def openat2_available() -> bool:
    """Whether this process resolves through ``openat2`` (probed once)."""
    global _openat2_ok
    if _openat2_ok is None:
        _openat2_ok = _probe_openat2()
    return _openat2_ok


def _components(rel: str | os.PathLike[str]) -> list[str]:
    rel = os.fspath(rel)
    if not isinstance(rel, str):
        raise UnsafePathError(errno.EINVAL, "path must be text", repr(rel))
    if rel == "":
        return []
    if "\x00" in rel or rel.startswith("/"):
        raise UnsafePathError(errno.EINVAL, "not a relative path", rel)
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UnsafePathError(errno.EINVAL, "empty or dot segment", rel)
    return parts


def _classified(err: OSError, dirfd: int, name: str, shown: str) -> OSError:
    """The error a failed step becomes: a symlink met on the way and an
    escape are refusals, a socket is not a file, anything else stays what
    the kernel said."""
    code = err.errno
    if code == errno.EXDEV:
        return EscapeRefused(code, "path leaves its root", shown)
    if code == errno.ELOOP:
        return SymlinkRefused(code, "symlink refused", shown)
    if code == errno.ENXIO:
        return NotRegularFile(code, "not a regular file", shown)
    if code in (errno.ENOTDIR, errno.EMLINK) and name:
        # O_NOFOLLOW | O_DIRECTORY on a symlink says ENOTDIR (EMLINK on BSD).
        with contextlib.suppress(OSError):
            if stat.S_ISLNK(os.stat(name, dir_fd=dirfd, follow_symlinks=False).st_mode):
                return SymlinkRefused(errno.ELOOP, "symlink refused", shown)
    return type(err)(code, err.strerror, shown)


def _step_dir(dirfd: int, name: str, shown: str) -> int:
    """A handle on directory ``name`` in ``dirfd``, reached without
    following: a symlink is a refusal, anything else a NotADirectoryError."""
    if _O_PATH:
        fd = os.open(name, _O_PATH | os.O_NOFOLLOW | _O_CLOEXEC, dir_fd=dirfd)
        try:
            mode = os.fstat(fd).st_mode
        except BaseException:
            os.close(fd)
            raise
        if stat.S_ISDIR(mode):
            return fd
        os.close(fd)
        if stat.S_ISLNK(mode):
            raise SymlinkRefused(errno.ELOOP, "symlink refused", shown)
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), shown)
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
                       dir_fd=dirfd)
    except OSError as exc:
        raise _classified(exc, dirfd, name, shown) from None


def _walk_open(dirfd: int, parts: list[str], flags: int, mode: int, shown: str) -> int:
    held: list[int] = []
    fd = dirfd
    try:
        for i, name in enumerate(parts[:-1]):
            fd = _step_dir(fd, name, "/".join(parts[: i + 1]))
            held.append(fd)
        name = parts[-1]
        if flags & _O_PATH:
            if flags & os.O_DIRECTORY:
                return _step_dir(fd, name, shown)
            leaf = os.open(name, _O_PATH | os.O_NOFOLLOW | _O_CLOEXEC, dir_fd=fd)
            if stat.S_ISLNK(os.fstat(leaf).st_mode):
                os.close(leaf)
                raise SymlinkRefused(errno.ELOOP, "symlink refused", shown)
            return leaf
        try:
            return os.open(name, flags | os.O_NOFOLLOW | _O_CLOEXEC, mode, dir_fd=fd)
        except OSError as exc:
            raise _classified(exc, fd, name, shown) from None
    finally:
        for held_fd in held:
            os.close(held_fd)


def _open_parts(dirfd: int, parts: list[str], flags: int, mode: int = 0o666) -> int:
    global _openat2_ok
    if flags & _O_PATH:
        # openat2 refuses any other flag next to O_PATH.
        flags &= _O_PATH | os.O_DIRECTORY | _O_CLOEXEC
    flags &= ~os.O_NOFOLLOW
    mode &= 0o7777
    if not parts:
        # The root itself: a fresh descriptor with the flags asked for.
        return os.open(".", flags | _O_CLOEXEC, dir_fd=dirfd)
    shown = "/".join(parts)
    if openat2_available():
        creating = flags & os.O_CREAT or (_O_TMPFILE and (flags & _O_TMPFILE) == _O_TMPFILE)
        for attempt in range(_EAGAIN_RETRIES):
            try:
                # No O_NOFOLLOW: RESOLVE_NO_SYMLINKS already refuses a trailing
                # link (with O_PATH | O_NOFOLLOW it would hand one out).
                return _raw_openat2(dirfd, shown, flags, mode if creating else 0, _RESOLVE)
            except OSError as exc:
                if exc.errno == errno.EAGAIN and attempt + 1 < _EAGAIN_RETRIES:
                    continue
                if exc.errno == errno.ENOSYS:
                    _openat2_ok = False
                    break
                if exc.errno == errno.ENAMETOOLONG:
                    break  # past PATH_MAX: the walk takes it one name at a time
                raise _classified(exc, dirfd, "", shown) from None
    return _walk_open(dirfd, parts, flags, mode, shown)


def _root_path(root: str | os.PathLike[str]) -> str:
    path = os.fspath(root)
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
        raise UnsafePathError(errno.EINVAL, "a root must be an absolute path", repr(root))
    return os.path.realpath(path)


@contextlib.contextmanager
def open_root(root: str | os.PathLike[str] | int, rel: str = "") -> Iterator[int]:
    """A directory handle, closed on exit, on ``root`` (an absolute path to a
    directory no untrusted process can replace, opened by its realpath) or,
    with ``rel``, on ``root/rel`` reached strictly: ``open_root(AGENTS_DIR,
    agent)`` for an agent's folder. An int ``root`` is a descriptor the
    caller keeps; it is passed through when ``rel`` is empty."""
    if isinstance(root, bool) or (isinstance(root, int) and root < 0):
        raise UnsafePathError(errno.EINVAL, "a root must be an absolute path", repr(root))
    parts = _components(rel)
    if isinstance(root, int):
        if not parts:
            yield root
            return
        fd = _open_parts(root, parts, _DIR_HANDLE)
    else:
        fd = os.open(_root_path(root), _DIR_HANDLE | os.O_NOFOLLOW)
        if parts:
            try:
                sub = _open_parts(fd, parts, _DIR_HANDLE)
            finally:
                os.close(fd)
            fd = sub
    try:
        yield fd
    finally:
        os.close(fd)


def open_beneath(root, rel, flags: int = os.O_RDONLY, mode: int = 0o666, *,
                 allow_special: bool = False) -> int:
    """A descriptor for ``rel`` beneath ``root`` opened with ``flags``. The
    open never blocks (it runs ``O_NONBLOCK``, then drops the flag unless
    asked for), a FIFO, socket or device is refused unless
    ``allow_special``, and with ``O_PATH`` only a directory or file handle
    comes back. The caller closes it."""
    parts = _components(rel)
    path_only = bool(flags & _O_PATH)
    extra = 0 if path_only else _O_NONBLOCK | _O_NOCTTY
    with open_root(root) as rootfd:
        fd = _open_parts(rootfd, parts, flags | extra, mode)
    if path_only:
        return fd
    try:
        st = os.fstat(fd)
        if not allow_special and not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise NotRegularFile(errno.EINVAL, "not a regular file or directory", os.fspath(rel))
        if not flags & _O_NONBLOCK:
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~_O_NONBLOCK)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_dir_beneath(root, rel: str = "") -> int:
    """A listable directory descriptor for ``rel`` (``""``: the root)."""
    return open_beneath(root, rel, _DIR_LIST)


def _checked_regular(fd: int, shown: str, *, single_link: bool,
                     max_size: int | None) -> os.stat_result:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise NotRegularFile(errno.EINVAL, "not a regular file", shown)
    if single_link and st.st_nlink != 1:
        raise HardlinkRefused(errno.EMLINK, "file has other links", shown)
    if max_size is not None and st.st_size > max_size:
        raise FileTooLarge(errno.EFBIG, "file is over the size cap", shown)
    return st


def open_regular_for_read(root, rel, *, single_link: bool = False,
                          max_size: int | None = None) -> tuple[int, os.stat_result]:
    """``(fd, stat)`` for the regular file at ``rel``, refused when it is
    anything else, has other hard links and ``single_link`` is asked (a
    secret-bearing read), or is over ``max_size`` NOW. The file can still
    grow after the open: a reader that must stay bounded reads through
    ``copy_fd(max_size=...)``, ``read_bytes_beneath`` or ``iter_fd`` with a
    length. The caller closes the descriptor and serves from it, never from
    the path."""
    fd = open_beneath(root, rel, os.O_RDONLY)
    try:
        return fd, _checked_regular(fd, os.fspath(rel), single_link=single_link,
                                    max_size=max_size)
    except BaseException:
        os.close(fd)
        raise


def open_file_for_read(root, rel, *, single_link: bool = False,
                       max_size: int | None = None) -> BinaryIO:
    """``open_regular_for_read`` as a binary file object (same caveat on
    growth after the open)."""
    fd, _ = open_regular_for_read(root, rel, single_link=single_link, max_size=max_size)
    return os.fdopen(fd, "rb")


def read_bytes_beneath(root, rel, *, max_size: int, single_link: bool = False) -> bytes:
    """The whole file at ``rel``, refused past ``max_size`` bytes (checked
    again while reading, so a file growing after the open cannot pass)."""
    fd, _ = open_regular_for_read(root, rel, single_link=single_link, max_size=max_size)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(max_size + 1)
    if len(data) > max_size:
        raise FileTooLarge(errno.EFBIG, "file is over the size cap", os.fspath(rel))
    return data


def copy_fd(src: BinaryIO, dst: BinaryIO, *, max_size: int | None = None,
            on_chunk: Callable[[bytes], None] | None = None) -> int:
    """Copy ``src`` into ``dst`` (a zip member, a temp file), refusing past
    ``max_size`` bytes as they are read; ``on_chunk`` sees each chunk (a
    hash). Returns the bytes copied."""
    total = 0
    while chunk := src.read(_COPY_CHUNK):
        total += len(chunk)
        if max_size is not None and total > max_size:
            raise FileTooLarge(errno.EFBIG, "file is over the size cap", getattr(src, "name", ""))
        if on_chunk is not None:
            on_chunk(chunk)
        dst.write(chunk)
    return total


def iter_fd(fd: int, start: int = 0, length: int | None = None, *,
            chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    """The bytes of ``fd`` from ``start``, at most ``length`` of them, read
    with ``pread`` (no shared offset moves). The caller owns and closes
    ``fd``: a generator that is never started never runs its cleanup."""
    pos, left = start, length
    while left is None or left > 0:
        want = chunk_size if left is None else min(chunk_size, left)
        data = os.pread(fd, want, pos)
        if not data:
            return
        pos += len(data)
        if left is not None:
            left -= len(data)
        yield data


def fd_path(fd: int) -> str:
    """A path that re-opens the very file ``fd`` refers to (a magic link, not
    a name, so no swap can redirect it): for a library that wants a path,
    e.g. ``FileResponse(fd_path(fd), stat_result=st)`` keeping Range/206.
    Valid while ``fd`` stays open; a subprocess needs it passed
    (``pass_fds``)."""
    return f"/proc/self/fd/{fd}" if os.path.isdir("/proc/self/fd") else f"/dev/fd/{fd}"


def _leaf(rootfd: int, rel, *, mkdirs: bool = False, dir_mode: int = 0o777) -> tuple[int, str]:
    """``(parent handle, last name)`` of ``rel``, which must name something
    below the root."""
    parts = _components(rel)
    if not parts:
        raise UnsafePathError(errno.EINVAL, "names the root itself", "")
    if mkdirs:
        return _ensure_dirs(rootfd, parts[:-1], dir_mode), parts[-1]
    return _open_parts(rootfd, parts[:-1], _DIR_HANDLE), parts[-1]


def lstat_beneath(root, rel) -> os.stat_result:
    """The status of ``rel`` itself (a symlink is reported, never followed).
    Prefer an exclusive create or rename over probing for a free name."""
    parts = _components(rel)
    with open_root(root) as rootfd:
        if not parts:
            return os.fstat(rootfd)
        parent, name = _leaf(rootfd, rel)
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)


def readlink_beneath(root, rel) -> str:
    """The target text of the symlink at ``rel`` (never followed)."""
    with open_root(root) as rootfd:
        parent, name = _leaf(rootfd, rel)
        try:
            return os.readlink(name, dir_fd=parent)
        finally:
            os.close(parent)


def _ensure_dirs(rootfd: int, parts: list[str], mode: int) -> int:
    """A handle on ``parts`` beneath ``rootfd``, creating what is missing;
    each step re-opens what it created without following, so a symlink
    swapped in behind a ``mkdir`` is refused, not entered."""
    try:
        return _open_parts(rootfd, parts, _DIR_HANDLE)
    except (FileNotFoundError, NotADirectoryError):
        pass  # the walk below creates what is missing and names what is in the way
    fd = os.open(".", _DIR_HANDLE, dir_fd=rootfd)
    try:
        for i, name in enumerate(parts):
            shown = "/".join(parts[: i + 1])
            try:
                os.mkdir(name, mode, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _classified(exc, fd, name, shown) from None
            try:
                nxt = _step_dir(fd, name, shown)
            except NotADirectoryError:
                if i == len(parts) - 1:
                    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), shown) from None
                raise
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def mkdirs_beneath(root, rel, *, mode: int = 0o777, exist_ok: bool = True) -> None:
    """``os.makedirs`` beneath ``root``: every missing directory of ``rel``
    is created and none is ever reached through a symlink."""
    parts = _components(rel)
    if not parts:
        return
    with open_root(root) as rootfd:
        if not exist_ok:
            parent = _ensure_dirs(rootfd, parts[:-1], mode)
            try:
                os.mkdir(parts[-1], mode, dir_fd=parent)
            finally:
                os.close(parent)
            return
        os.close(_ensure_dirs(rootfd, parts, mode))


def _libc_renameat2(src_fd: int, src: str, dst_fd: int, dst: str, flags: int) -> bool:
    """True when renameat2 ran; False when this libc or filesystem lacks it."""
    try:
        fn = _load_libc().renameat2
    except (AttributeError, OSError):
        return False
    fn.restype = ctypes.c_int
    rc = fn(ctypes.c_int(src_fd), ctypes.c_char_p(os.fsencode(src)),
            ctypes.c_int(dst_fd), ctypes.c_char_p(os.fsencode(dst)), ctypes.c_uint(flags))
    if rc == 0:
        return True
    err = ctypes.get_errno()
    if err in (errno.ENOSYS, errno.EINVAL):
        return False
    raise OSError(err, os.strerror(err), dst)


def _rename_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    if _libc_renameat2(src_fd, src, dst_fd, dst, _RENAME_NOREPLACE):
        return
    # No renameat2: a hard link fails on an existing name (files only);
    # a directory falls back to a check first, which can only race inside
    # the same root, never out of it.
    st = os.stat(src, dir_fd=src_fd, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        os.link(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd, follow_symlinks=False)
        os.unlink(src, dir_fd=src_fd)
        return
    with contextlib.suppress(FileNotFoundError):
        os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
    os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)


def _temp_name(name: str) -> str:
    # ``.partial``: the sync manifest skips it and retention reaps an orphan.
    base = os.fsdecode(os.fsencode(name)[:_TEMP_NAME_BYTES])
    return f".{base}.{secrets.token_hex(6)}.partial"


def _fsync_dir(dirfd: int) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC, dir_fd=dirfd)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def atomic_writer(root, rel, *, mode: int | None = None, mkdirs: bool = False,
                  exclusive: bool = False, fsync: bool = True) -> Iterator[BinaryIO]:
    """A binary file to write ``rel``'s new content into. It is a temp
    file created ``O_EXCL | O_NOFOLLOW`` next to ``rel``, with its final
    bits from the start, and renamed onto ``rel`` within the same directory
    handle when the block exits cleanly (removed otherwise): readers see
    the old or the new bytes, never a part, and a symlink at ``rel`` is
    replaced, never written through.

    ``mode`` sets the permission bits; ``None`` keeps an existing file's
    bits (a new file gets the umask's). ``exclusive`` refuses an existing
    ``rel`` (``FileExistsError``). ``mkdirs`` creates missing parents.
    ``fsync`` flushes the file and then the directory."""
    with open_root(root) as rootfd:
        parent, name = _leaf(rootfd, rel, mkdirs=mkdirs)
        try:
            final = None if mode is None else mode & 0o7777
            if final is None:
                with contextlib.suppress(FileNotFoundError):
                    st = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        final = stat.S_IMODE(st.st_mode)
            tmp = _temp_name(name)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC,
                         0o600 if final is not None else 0o666, dir_fd=parent)
            committed = False
            try:
                with os.fdopen(fd, "wb") as fh:
                    if final is not None:
                        os.fchmod(fh.fileno(), final)
                    yield fh
                    fh.flush()
                    if fsync:
                        os.fsync(fh.fileno())
                if exclusive:
                    _rename_noreplace(parent, tmp, parent, name)
                else:
                    os.rename(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
                committed = True
                if fsync:
                    _fsync_dir(parent)
            finally:
                if not committed:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp, dir_fd=parent)
        finally:
            os.close(parent)


def atomic_write_beneath(root, rel, data: bytes | bytearray | memoryview | Iterable[bytes], *,
                         mode: int | None = None, mkdirs: bool = False,
                         exclusive: bool = False, fsync: bool = True) -> None:
    """Replace ``rel`` with ``data`` (bytes, or an iterable of chunks) the
    way ``atomic_writer`` does."""
    with atomic_writer(root, rel, mode=mode, mkdirs=mkdirs,
                       exclusive=exclusive, fsync=fsync) as fh:
        if isinstance(data, (bytes, bytearray, memoryview)):
            fh.write(data)
        else:
            for chunk in data:
                fh.write(chunk)


def copy_file_beneath(src_root, src_rel, dst_root, dst_rel, *,
                      max_size: int | None = None, single_link: bool = False,
                      mode: int | None = None, mkdirs: bool = False,
                      exclusive: bool = False, fsync: bool = False,
                      on_chunk: Callable[[bytes], None] | None = None) -> int:
    """``shutil.copy2`` between two roots: the source must be a regular file
    reached without a symlink and stays under ``max_size`` while it is
    copied; the destination is written by ``atomic_writer`` with the
    source's times and its permission bits (never setuid, setgid or sticky)
    unless ``mode`` is given. Returns the bytes copied."""
    fd, st = open_regular_for_read(src_root, src_rel, single_link=single_link,
                                   max_size=max_size)
    bits = stat.S_IMODE(st.st_mode) & 0o777 if mode is None else mode
    with os.fdopen(fd, "rb") as src:
        with atomic_writer(dst_root, dst_rel, mode=bits, mkdirs=mkdirs,
                           exclusive=exclusive, fsync=fsync) as dst:
            copied = copy_fd(src, dst, max_size=max_size, on_chunk=on_chunk)
            dst.flush()
            os.utime(dst.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
    return copied


class WalkStep:
    """One directory of ``walk_beneath``: its path relative to the walk's
    root, a listable handle (``dirfd``) valid only until the walk moves on,
    and its entries by kind as the directory listing reports them (the open
    helpers re-check each one; ``dirs`` may be pruned in place to skip a
    subtree)."""

    __slots__ = ("rel", "dirs", "files", "symlinks", "other", "_fd")

    def __init__(self, rel: str, fd: int) -> None:
        self.rel = rel
        self._fd = fd
        self.dirs: list[str] = []
        self.files: list[str] = []
        self.symlinks: list[str] = []
        self.other: list[str] = []

    @property
    def dirfd(self) -> int:
        if self._fd < 0:
            raise ValueError(f"the handle of walk step {self.rel!r} is used after its step")
        return self._fd

    def path(self, name: str) -> str:
        return f"{self.rel}/{name}" if self.rel else name


def _list_dir(fd: int, rel: str) -> WalkStep:
    step = WalkStep(rel, fd)
    with os.scandir(fd) as it:
        for entry in it:
            try:
                if entry.is_symlink():
                    step.symlinks.append(entry.name)
                elif entry.is_dir(follow_symlinks=False):
                    step.dirs.append(entry.name)
                elif entry.is_file(follow_symlinks=False):
                    step.files.append(entry.name)
                else:
                    step.other.append(entry.name)
            except OSError:
                step.other.append(entry.name)
    for bucket in (step.dirs, step.files, step.symlinks, step.other):
        bucket.sort()
    return step


def walk_beneath(root, rel: str = "", *,
                 onerror: Callable[[OSError], None] | None = None) -> Iterator[WalkStep]:
    """``os.walk`` beneath ``root``, top-down, holding one directory handle
    per level (a wide tree cannot run the process out of descriptors). A
    directory is entered from its parent's handle without following: one
    that vanished or was swapped for a link since the listing is skipped;
    any other failure to enter one (a descriptor limit, a directory made
    unreadable) raises, or goes to ``onerror`` and is skipped, so a caller
    never mistakes a partial walk for a whole one."""
    with open_root(root) as rootfd:
        pending: int | None = _open_parts(rootfd, _components(rel), _DIR_LIST)
    here = rel
    stack: list[tuple[int, str, Iterator[str]]] = []
    try:
        while True:
            if pending is not None:
                fd, pending = pending, None
                stack.append((fd, here, iter(())))
                step = _list_dir(fd, here)
                try:
                    yield step
                finally:
                    step._fd = -1
                stack[-1] = (fd, here, iter(list(step.dirs)))
            if not stack:
                return
            parent, parent_rel, names = stack[-1]
            name = next(names, None)
            if name is None:
                stack.pop()
                os.close(parent)
                continue
            child = f"{parent_rel}/{name}" if parent_rel else name
            try:
                pending = os.open(name, _DIR_LIST | os.O_NOFOLLOW, dir_fd=parent)
                here = child
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    continue
                err = _classified(exc, parent, name, child)
                if isinstance(err, SymlinkRefused):
                    continue
                if onerror is None:
                    raise err from None
                onerror(err)
    finally:
        if pending is not None:
            os.close(pending)
        for fd, _rel, _names in stack:
            os.close(fd)


def _link_stays_inside(dir_rel: str, target: str) -> bool:
    """Whether a relative link placed in ``dir_rel`` lands inside its root,
    judged on the text alone (what ``readlink`` returned is exactly what is
    written, so the judgement cannot race)."""
    if not target or target.startswith("/") or "\x00" in target:
        return False
    landed = posixpath.normpath(posixpath.join(dir_rel or ".", target))
    return landed != ".." and not landed.startswith("../")


def copytree_beneath(src_root, src_rel, dst_root, dst_rel, *,
                     symlinks: str = "skip", dirs_exist_ok: bool = False,
                     ignore: Callable[[str, list[str]], Iterable[str]] | None = None) -> list[str]:
    """``shutil.copytree`` between two roots. Regular files keep their bits
    (never setuid, setgid or sticky) and times; directories are created
    fresh. A symlink is skipped (``symlinks="skip"``), ends the copy with
    ``SymlinkRefused`` (``"refuse"``), or is recreated as the same link
    without reading its target (``"copy"``, ``copytree(symlinks=True)``)
    when the link stays inside the destination root at its new place;
    one that would leave it is skipped. FIFOs, sockets and devices are
    always skipped. ``ignore(rel_dir, names)`` names entries to leave out.
    A failure removes the directory the copy created. Returns the source
    paths it skipped."""
    if symlinks not in ("skip", "refuse", "copy"):
        raise ValueError("symlinks must be 'skip', 'refuse' or 'copy'")
    skipped: list[str] = []
    src_parts, base = _components(src_rel), _components(dst_rel)
    with open_root(src_root) as srcfd, open_root(dst_root) as dstfd:
        same = os.fstat(srcfd)[1:3] == os.fstat(dstfd)[1:3]
        if same and base[:len(src_parts)] == src_parts:
            raise UnsafePathError(errno.EINVAL, "cannot copy a directory into itself", os.fspath(dst_rel))
        made: set[tuple[int, int]] = set()
        created_base = False
        if base:
            parent = _ensure_dirs(dstfd, base[:-1], 0o777)
            try:
                os.mkdir(base[-1], 0o777, dir_fd=parent)
                created_base = True
            except FileExistsError:
                if not dirs_exist_ok:
                    raise
            finally:
                os.close(parent)
        try:
            for step in walk_beneath(srcfd, src_rel):
                sub = step.rel.split("/")[len(src_parts):] if step.rel else []
                outfd = _ensure_dirs(dstfd, base + sub, 0o777)
                try:
                    out = os.fstat(outfd)
                    made.add((out.st_dev, out.st_ino))
                    left_out = set(ignore(step.rel, step.dirs + step.files + step.symlinks + step.other)) \
                        if ignore else set()
                    kept_dirs = []
                    for name in step.dirs:
                        if name in left_out:
                            continue
                        with contextlib.suppress(OSError):
                            st = os.stat(name, dir_fd=step.dirfd, follow_symlinks=False)
                            if (st.st_dev, st.st_ino) in made:
                                continue  # the copy itself, reached through another root
                        kept_dirs.append(name)
                    step.dirs[:] = kept_dirs
                    dst_dir_rel = "/".join(base + sub)
                    for name in step.symlinks:
                        if name in left_out:
                            continue
                        if symlinks == "refuse":
                            raise SymlinkRefused(errno.ELOOP, "symlink refused", step.path(name))
                        if symlinks == "copy":
                            target = os.readlink(name, dir_fd=step.dirfd)
                            if _link_stays_inside(dst_dir_rel, target):
                                try:
                                    os.symlink(target, name, dir_fd=outfd)
                                except FileExistsError:
                                    old = os.stat(name, dir_fd=outfd, follow_symlinks=False)
                                    if not (dirs_exist_ok and stat.S_ISLNK(old.st_mode)):
                                        raise
                                    os.unlink(name, dir_fd=outfd)
                                    os.symlink(target, name, dir_fd=outfd)
                                continue
                        skipped.append(step.path(name))
                    skipped.extend(step.path(n) for n in step.other if n not in left_out)
                    for name in step.files:
                        if name in left_out:
                            continue
                        try:
                            copy_file_beneath(step.dirfd, name, outfd, name)
                        except SymlinkRefused:
                            if symlinks == "refuse":
                                raise
                            skipped.append(step.path(name))
                        except NotRegularFile:
                            skipped.append(step.path(name))
                finally:
                    os.close(outfd)
        except BaseException:
            if created_base:
                with contextlib.suppress(OSError):
                    rmtree_beneath(dstfd, dst_rel)
            raise
    return skipped


def rename_beneath(root, src_rel, dst_rel, *, dst_root=None, replace: bool = False,
                   mkdirs: bool = False) -> None:
    """Move ``src_rel`` to ``dst_rel`` (under ``dst_root``, the same root by
    default) with one ``renameat`` between two parent handles reached
    without a symlink. A symlink at ``src_rel`` moves as the link itself.
    An existing destination is refused (``FileExistsError``) unless
    ``replace``. Across filesystems this raises ``OSError(EXDEV)`` and
    moves nothing (``move_beneath`` copies instead)."""
    with open_root(root) as rootfd, open_root(rootfd if dst_root is None else dst_root) as dstfd:
        src_parent, src_name = _leaf(rootfd, src_rel)
        try:
            dst_parent, dst_name = _leaf(dstfd, dst_rel, mkdirs=mkdirs)
            try:
                if replace:
                    os.rename(src_name, dst_name, src_dir_fd=src_parent, dst_dir_fd=dst_parent)
                else:
                    _rename_noreplace(src_parent, src_name, dst_parent, dst_name)
            finally:
                os.close(dst_parent)
        finally:
            os.close(src_parent)


def move_beneath(root, src_rel, dst_rel, *, dst_root=None, replace: bool = False,
                 symlinks: str = "copy") -> None:
    """``shutil.move`` between roots: one ``rename_beneath``, or where the
    two sit on different filesystems (EXDEV: a Docker volume boundary, an
    XFS project quota) a copy followed by removing the source. The source
    is removed only when the copy is whole: anything the copy skipped (a
    FIFO, a link that would leave the destination root) undoes the copy and
    raises ``OSError(EXDEV)`` with the source untouched. ``replace``
    applies to a file; a directory's destination must not exist."""
    try:
        rename_beneath(root, src_rel, dst_rel, dst_root=dst_root, replace=replace)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV or isinstance(exc, SafeFsError):
            raise
    dst = root if dst_root is None else dst_root
    st = lstat_beneath(root, src_rel)
    if stat.S_ISDIR(st.st_mode):
        skipped = copytree_beneath(root, src_rel, dst, dst_rel, symlinks=symlinks)
        if skipped:
            rmtree_beneath(dst, dst_rel)
            raise OSError(errno.EXDEV, f"cannot move across filesystems: {len(skipped)} "
                          "entries could not be copied", ", ".join(skipped[:5]))
        rmtree_beneath(root, src_rel)
    elif stat.S_ISLNK(st.st_mode):
        target = readlink_beneath(root, src_rel)
        dst_dir = posixpath.dirname(os.fspath(dst_rel))
        if symlinks != "copy" or not _link_stays_inside(dst_dir, target):
            raise SymlinkRefused(errno.ELOOP, "symlink refused", os.fspath(src_rel))
        with open_root(dst) as dstfd:
            parent, name = _leaf(dstfd, dst_rel)
            try:
                if replace:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(name, dir_fd=parent)
                os.symlink(target, name, dir_fd=parent)
            finally:
                os.close(parent)
        unlink_beneath(root, src_rel)
    else:
        copy_file_beneath(root, src_rel, dst, dst_rel, exclusive=not replace, fsync=True)
        unlink_beneath(root, src_rel)


def unlink_beneath(root, rel, *, missing_ok: bool = False) -> None:
    """Remove the file or symlink at ``rel`` (a symlink goes, its target stays)."""
    with open_root(root) as rootfd:
        parent, name = _leaf(rootfd, rel)
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            if not missing_ok:
                raise
        finally:
            os.close(parent)


def rmtree_beneath(root, rel, *, missing_ok: bool = False) -> None:
    """Remove the directory tree at ``rel`` with ``shutil.rmtree``'s
    descriptor-based walk, which never follows a symlink inside it. A symlink
    at ``rel`` itself is refused (``unlink_beneath`` removes one)."""
    with open_root(root) as rootfd:
        try:
            parent, name = _leaf(rootfd, rel)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        try:
            try:
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if missing_ok:
                    return
                raise
            if stat.S_ISLNK(st.st_mode):
                raise SymlinkRefused(errno.ELOOP, "symlink refused", os.fspath(rel))
            if not stat.S_ISDIR(st.st_mode):
                raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), os.fspath(rel))
            shutil.rmtree(name, dir_fd=parent)
        finally:
            os.close(parent)


def rel_under(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    """The ``rel`` of an absolute ``path`` stored or received earlier,
    judged on the text against ``root`` as given and as its realpath (the
    filesystem is not consulted: the open that follows is the check). A
    ``..``, ``.`` or empty segment is refused, never collapsed. ``""`` for
    the root itself; ``EscapeRefused`` for anything outside."""
    candidate = os.fspath(path)
    if not isinstance(candidate, str) or not candidate.startswith("/") or "\x00" in candidate:
        raise UnsafePathError(errno.EINVAL, "not an absolute path", repr(path))
    given = os.path.normpath(os.fspath(root))
    for base in dict.fromkeys((given, os.path.realpath(given))):
        if candidate in (base, base.rstrip("/") + "/"):
            return ""
        prefix = base.rstrip("/") + "/"
        if candidate.startswith(prefix):
            rest = candidate[len(prefix):]
            rest = rest[:-1] if rest.endswith("/") else rest
            _components(rest)
            return rest
    raise EscapeRefused(errno.EXDEV, "path leaves its root", candidate)


def split_under(path: str | os.PathLike[str],
                roots: Iterable[str | os.PathLike[str]]) -> tuple[str, str]:
    """``(root, rel)`` for the root among ``roots`` that most closely holds
    ``path`` (see ``rel_under``; a cache nested in the agents tree wins over
    the tree); ``EscapeRefused`` when none does."""
    best: tuple[str, str] | None = None
    for root in roots:
        try:
            rel = rel_under(path, root)
        except EscapeRefused:
            continue
        if best is None or len(rel) < len(best[1]):
            best = (os.fspath(root), rel)
    if best is None:
        raise EscapeRefused(errno.EXDEV, "path is under none of its roots", os.fspath(path))
    return best


def canonical_rel(root: str | os.PathLike[str], rel) -> str:
    """Where ``rel`` lands beneath ``root`` once symlinks inside the tree are
    followed, as a rel; ``EscapeRefused`` when it lands outside. For callers
    that judge a link by its target: AUTHORIZE the answer, then open the
    answer (never ``rel``) with the helpers above, which refuse any symlink,
    so a swap after this call cannot move the open anywhere unauthorized."""
    parts = _components(rel)
    base = _root_path(root)
    resolved = os.path.realpath(os.path.join(base, *parts)) if parts else base
    return rel_under(resolved, base)
