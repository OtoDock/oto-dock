"""A session's own engine folder, its home, inside its scope's engine folder.

The scope's engine folder (``.claude`` under ``workspace/`` or
``users/<u>/``) is the STORE: every session of the scope reads its hooks,
its tools' config and the platform's readers find its transcripts there. A
session that runs from a home of its own, ``<store>/own/<session id>/``,
holds its own sign-in file, its own account record and its own settings
file there, and reaches the store's shared folders (its transcripts, its
skills) through links. So two sessions of one scope run on two accounts at
once, and the pool picks an account per session.

This module owns the layout and nothing else: where a home is, how it is
made and how it is removed. Which sessions get a home is the platform's
(``session_config_dir.session_home_for``); what an engine keeps there is the
engine's declaration (``engine_records.SessionHomeSpec``).

A removal never follows a link: a home's links are removed by their name
before the folder, and a link or a junction found anywhere below it is
removed, never entered. On Linux and macOS every step goes through a folder
descriptor opened with no link followed; on Windows a reparse point at a
name the module walks is refused or removed by its name.

Standard library only: the satellite carries a byte-identical copy
(``scripts/sync-satellite-code.sh``).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path

#: The folder under a store that holds its sessions' homes.
OWN = "own"

_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_WINDOWS = os.name == "nt"
# The link primitives follow the OS itself; ``_WINDOWS`` picks the walk (a
# test runs the path walk with links standing in for junctions).
_NT = os.name == "nt"
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
_TEMP = ".oto-home-"
# How deep a merge follows a folder both sides hold. No engine writes deeper;
# a session can (its home is writable), and the start then fails closed.
_MERGE_DEPTH = 64
#: A store's record seeds a home only up to this size (the CLI's own seed
#: cap); a larger one seeds nothing and is never cut.
SEED_MAX_BYTES = 64 * 1024 * 1024


def self_hash() -> str:
    """SHA256 of this module's source: the satellite's drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


class HomeRefused(OSError):
    """A store, its ``own`` folder or a home is a link or not a folder: the
    session's start fails rather than write through a redirected path."""


def valid_id(session_id: str) -> bool:
    """Whether ``session_id`` may name a home (a UUID on every start path)."""
    return bool(session_id) and _ID.fullmatch(session_id) is not None


def home_of(store: str | os.PathLike[str], session_id: str) -> Path:
    """The home of ``session_id`` in ``store``."""
    if not valid_id(session_id):
        raise ValueError(f"not a session id a home may carry: {session_id!r}")
    return Path(store) / OWN / session_id


def relative(session_id: str) -> str:
    """A home's path under its store, with forward slashes (what a machine's
    sign-in push names after the store, ``credentials_update``)."""
    return home_of("", session_id).as_posix()


def is_home(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` names a home (``<store>/own/<id>``)."""
    p = Path(path)
    return p.parent.name == OWN and valid_id(p.name)


def store_of(home: str | os.PathLike[str]) -> Path:
    """The store a home belongs to."""
    if not is_home(home):
        raise ValueError(f"not a session home: {home}")
    return Path(home).parent.parent


def prepare(store: str | os.PathLike[str], session_id: str, links: Iterable[str],
            seed: Mapping[str, Collection[str]] | None = None) -> Path:
    """Make ``session_id``'s home in ``store`` and return it. The home and
    ``own`` are the owner's alone (mode 0700), made here or found.

    Each name of ``links`` becomes a link in the home to the store's folder
    of that name, made in the store when absent: a relative link made beside
    its name and renamed over it (Linux, macOS), a junction made at its name
    (Windows). A real folder found at a link's name is merged into the
    store's folder first, entry by entry (a folder into the store's folder of
    its name, a file whose name is taken kept beside it with a suffix), so
    nothing an engine wrote there is lost or hidden. Each file of
    ``seed`` that the home lacks is copied from the store's copy with the
    named top-level keys left out (a JSON object; an unreadable copy seeds
    nothing). Idempotent: a home that is already right is left as it is."""
    ops = _PathOps() if _WINDOWS else _FdOps()
    return ops.prepare(Path(store), session_id, tuple(links), dict(seed or {}))


def drop(store: str | os.PathLike[str], session_id: str, links: Iterable[str]) -> bool:
    """Remove ``session_id``'s home; True when one was there. Its links go
    by their names first, then its own files (the sign-in) before any folder
    is entered, then everything else below it, a link or a junction removed
    and never entered. No recursion: a session may leave a tree of any
    depth."""
    if not valid_id(session_id):
        return False
    ops = _PathOps() if _WINDOWS else _FdOps()
    return ops.drop(Path(store), session_id, tuple(links))


def sweep(store: str | os.PathLike[str], live: Collection[str], links: Iterable[str]) -> list[str]:
    """Remove every home in ``store`` whose session is not in ``live``; the
    ids removed. A name under ``own`` that is not a session id is left, and a
    home that cannot be removed now is left for the next sweep."""
    ops = _PathOps() if _WINDOWS else _FdOps()
    return ops.sweep(Path(store), set(live), tuple(links))


def _without(data: bytes, keys: Collection[str]) -> bytes | None:
    """``data`` as a JSON object without ``keys``; None when it is not one.
    A seed that names no keys takes the bytes as they are (a bare id)."""
    if not keys:
        return data
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    for key in keys:
        obj.pop(key, None)
    return json.dumps(obj, indent=2).encode()


class _FdOps:
    """Linux and macOS: every step relative to a folder descriptor held on
    its parent, opened with no link followed."""

    def __init__(self) -> None:
        # Read here, never in the class body: a class body runs at import on
        # every system, and Windows has neither flag.
        self._dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    def _open_dir(self, name: str | os.PathLike[str], parent: int | None = None) -> int:
        try:
            return os.open(name, self._dir_flags, dir_fd=parent)
        except NotADirectoryError as e:
            raise HomeRefused(f"{name} is not a folder") from e
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise HomeRefused(f"{name} is a link") from e
            raise

    def _mkdir_open(self, name: str, parent: int, *, own_only: bool = False) -> int:
        """``own_only``: a folder found gets the mode one made here has (a
        writer before this one made it with the process's default)."""
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent)
        fd = self._open_dir(name, parent)
        if own_only and stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            try:
                # Another owner's folder cannot be changed: it stays as made.
                with contextlib.suppress(PermissionError):
                    os.fchmod(fd, 0o700)
            except BaseException:
                os.close(fd)
                raise
        return fd

    def _lstat(self, name: str, parent: int) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def prepare(self, store: Path, session_id: str, links: tuple[str, ...],
                seed: dict[str, Collection[str]]) -> Path:
        home = home_of(store, session_id)
        store_fd = self._open_dir(store)
        try:
            own_fd = self._mkdir_open(OWN, store_fd, own_only=True)
            try:
                home_fd = self._mkdir_open(session_id, own_fd, own_only=True)
            finally:
                os.close(own_fd)
            try:
                for name in links:
                    self._link(name, store_fd, home_fd)
                for name, keys in seed.items():
                    self._seed(name, keys, store_fd, home_fd)
            finally:
                os.close(home_fd)
        finally:
            os.close(store_fd)
        return home

    def _link(self, name: str, store_fd: int, home_fd: int) -> None:
        shared_fd = self._mkdir_open(name, store_fd)
        try:
            target = f"../../{name}"
            st = self._lstat(name, home_fd)
            if st is not None and stat.S_ISLNK(st.st_mode):
                if os.readlink(name, dir_fd=home_fd) == target:
                    return
            elif st is not None and stat.S_ISDIR(st.st_mode):
                self._move_into(name, home_fd, shared_fd)
            elif st is not None:
                os.unlink(name, dir_fd=home_fd)
            tmp = f"{_TEMP}{secrets.token_hex(6)}"
            os.symlink(target, tmp, dir_fd=home_fd)
            os.rename(tmp, name, src_dir_fd=home_fd, dst_dir_fd=home_fd)
        finally:
            os.close(shared_fd)

    def _move_into(self, name: str, home_fd: int, shared_fd: int) -> None:
        """Merge the home's real folder ``name`` into the store's, then
        remove the emptied folder."""
        sub_fd = self._open_dir(name, home_fd)
        try:
            self._merge(sub_fd, shared_fd, 0)
        finally:
            os.close(sub_fd)
        os.rmdir(name, dir_fd=home_fd)

    def _merge(self, src_fd: int, dst_fd: int, depth: int) -> None:
        """Every entry of ``src_fd`` into ``dst_fd``: a folder both hold
        merged into the store's (a project's conversations stay where a
        resume reads them), any other name that is taken kept beside it."""
        if depth >= _MERGE_DEPTH:
            raise HomeRefused("a folder in a session's home is nested deeper than a merge follows")
        for entry in os.listdir(src_fd):
            mine, theirs = self._lstat(entry, src_fd), self._lstat(entry, dst_fd)
            if (mine is not None and theirs is not None
                    and stat.S_ISDIR(mine.st_mode) and stat.S_ISDIR(theirs.st_mode)):
                sub_src, sub_dst = self._open_dir(entry, src_fd), self._open_dir(entry, dst_fd)
                try:
                    self._merge(sub_src, sub_dst, depth + 1)
                finally:
                    os.close(sub_src)
                    os.close(sub_dst)
                os.rmdir(entry, dir_fd=src_fd)
                continue
            dest = entry
            while self._lstat(dest, dst_fd) is not None:
                dest = f"{entry}.{secrets.token_hex(3)}"
            os.rename(entry, dest, src_dir_fd=src_fd, dst_dir_fd=dst_fd)

    def _seed(self, name: str, keys: Collection[str], store_fd: int, home_fd: int) -> None:
        if self._lstat(name, home_fd) is not None:
            return
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=store_fd)
        except OSError:
            return
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return
            raw = f.read(SEED_MAX_BYTES + 1)
            if len(raw) > SEED_MAX_BYTES:
                return
            data = _without(raw, keys)
        if data is None:
            return
        tmp = f"{_TEMP}{secrets.token_hex(6)}"
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=home_fd)
        try:
            os.write(out, data)
        finally:
            os.close(out)
        os.rename(tmp, name, src_dir_fd=home_fd, dst_dir_fd=home_fd)

    def drop(self, store: Path, session_id: str, links: tuple[str, ...]) -> bool:
        try:
            store_fd = self._open_dir(store)
        except FileNotFoundError:
            return False
        try:
            try:
                own_fd = self._open_dir(OWN, store_fd)
            except FileNotFoundError:
                return False
            try:
                return self._drop_in(own_fd, session_id, links)
            finally:
                os.close(own_fd)
        finally:
            os.close(store_fd)

    def _drop_in(self, own_fd: int, session_id: str, links: tuple[str, ...]) -> bool:
        st = self._lstat(session_id, own_fd)
        if st is None:
            return False
        if not stat.S_ISDIR(st.st_mode):
            os.unlink(session_id, dir_fd=own_fd)
            return True
        home_fd = self._open_dir(session_id, own_fd)
        try:
            for name in links:
                entry = self._lstat(name, home_fd)
                if entry is not None and stat.S_ISLNK(entry.st_mode):
                    os.unlink(name, dir_fd=home_fd)
            self._empty(home_fd)
        finally:
            os.close(home_fd)
        os.rmdir(session_id, dir_fd=own_fd)
        return True

    def _empty(self, root_fd: int) -> None:
        """Empty ``root_fd`` with no recursion: each pass removes the files of
        the folder it stands in (the root's own first), steps down into a
        folder while there is one, and removes the empty folder it reached
        from its parent. Two descriptors at a time; going back up opens the
        names again from the root, so no ``..`` is ever followed."""
        path: list[str] = []
        while True:
            cur = self._walk(root_fd, path)
            parent: int | None = None
            try:
                while True:
                    sub = self._clear_files(cur)
                    if sub is None:
                        break
                    nxt = self._open_dir(sub, cur)
                    if parent is not None:
                        os.close(parent)
                    parent, cur = cur, nxt
                    path.append(sub)
                if not path:
                    return
                if parent is None:
                    parent = self._walk(root_fd, path[:-1])
                os.rmdir(path.pop(), dir_fd=parent)
            finally:
                os.close(cur)
                if parent is not None:
                    os.close(parent)

    def _walk(self, root_fd: int, path: list[str]) -> int:
        """A descriptor of ``path`` below ``root_fd``, opened name by name."""
        fd = os.dup(root_fd)
        for name in path:
            try:
                nxt = self._open_dir(name, fd)
            finally:
                os.close(fd)
            fd = nxt
        return fd

    def _clear_files(self, dir_fd: int) -> str | None:
        """Unlink every entry of ``dir_fd`` that is not a folder (a link
        among them, never entered); the name of one folder left, if any."""
        folder = None
        for name in os.listdir(dir_fd):
            st = self._lstat(name, dir_fd)
            if st is None:
                continue
            if stat.S_ISDIR(st.st_mode):
                folder = folder or name
            else:
                os.unlink(name, dir_fd=dir_fd)
        return folder

    def sweep(self, store: Path, live: set[str], links: tuple[str, ...]) -> list[str]:
        try:
            store_fd = self._open_dir(store)
        except FileNotFoundError:
            return []
        removed: list[str] = []
        try:
            try:
                own_fd = self._open_dir(OWN, store_fd)
            except FileNotFoundError:
                return []
            try:
                for name in sorted(os.listdir(own_fd)):
                    if valid_id(name) and name not in live and _left_on_failure(
                            self._drop_in, own_fd, name, links):
                        removed.append(name)
            finally:
                os.close(own_fd)
        finally:
            os.close(store_fd)
        return removed


class _PathOps:
    """Windows: path steps, each name checked for a link or a junction
    before it is used; links are junctions (no privilege needed)."""

    @staticmethod
    def _is_link(path: Path) -> bool:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)

    def _real_dir(self, path: Path, *, make: bool, own_only: bool = False) -> None:
        if make:
            with contextlib.suppress(FileExistsError):
                os.mkdir(path, 0o700)
        if self._is_link(path):
            raise HomeRefused(f"{path} is a link")
        if not path.is_dir():
            raise HomeRefused(f"{path} is not a folder")
        if own_only and stat.S_IMODE(os.lstat(path).st_mode) != 0o700:
            with contextlib.suppress(PermissionError):
                os.chmod(path, 0o700)

    @staticmethod
    def _make_link(target: Path, link: Path) -> None:
        if _NT:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
        else:
            os.symlink(os.path.relpath(target, link.parent), link)

    def _remove_link(self, path: Path) -> None:
        if path.is_dir() and _NT:
            os.rmdir(path)
        else:
            os.unlink(path)

    def prepare(self, store: Path, session_id: str, links: tuple[str, ...],
                seed: dict[str, Collection[str]]) -> Path:
        home = home_of(store, session_id)
        self._real_dir(store, make=False)
        self._real_dir(store / OWN, make=True, own_only=True)
        self._real_dir(home, make=True, own_only=True)
        for name in links:
            shared = store / name
            self._real_dir(shared, make=True)
            link = home / name
            if self._is_link(link):
                if os.path.realpath(link) == os.path.realpath(shared):
                    continue
                self._remove_link(link)
            elif link.is_dir():
                self._merge(link, shared, 0)
                os.rmdir(link)
            elif link.exists():
                os.unlink(link)
            self._make_link(shared, link)
        for name, keys in seed.items():
            src, dst = store / name, home / name
            if dst.exists() or self._is_link(dst) or self._is_link(src) or not src.is_file():
                continue
            with open(src, "rb") as fh:
                raw = fh.read(SEED_MAX_BYTES + 1)
            if len(raw) > SEED_MAX_BYTES:
                continue
            data = _without(raw, keys)
            if data is not None:
                dst.write_bytes(data)
        return home

    def _merge(self, src: Path, dst: Path, depth: int) -> None:
        """As ``_FdOps._merge``, by paths, a link or a junction never entered."""
        if depth >= _MERGE_DEPTH:
            raise HomeRefused("a folder in a session's home is nested deeper than a merge follows")
        for entry in os.listdir(src):
            mine, theirs = src / entry, dst / entry
            if (not self._is_link(mine) and not self._is_link(theirs)
                    and mine.is_dir() and theirs.is_dir()):
                self._merge(mine, theirs, depth + 1)
                os.rmdir(mine)
                continue
            dest = theirs
            while dest.exists() or self._is_link(dest):
                dest = dst / f"{entry}.{secrets.token_hex(3)}"
            os.rename(mine, dest)

    def drop(self, store: Path, session_id: str, links: tuple[str, ...]) -> bool:
        own = store / OWN
        if self._is_link(store) or self._is_link(own):
            return False
        return self._drop_at(own / session_id, links)

    def _drop_at(self, home: Path, links: tuple[str, ...]) -> bool:
        if self._is_link(home):
            self._remove_link(home)
            return True
        if not home.exists():
            return False
        for name in links:
            if self._is_link(home / name):
                self._remove_link(home / name)
        self._empty(home)
        os.rmdir(home)
        return True

    def _empty(self, folder: Path) -> None:
        """As ``_FdOps._empty``, by paths, with no recursion: each folder's
        files and links go as it is reached (the home's own first), then
        the folders, deepest first."""
        pending, folders = [folder], []
        while pending:
            here = pending.pop()
            for name in os.listdir(here):
                path = here / name
                if self._is_link(path):
                    self._remove_link(path)
                elif path.is_dir():
                    pending.append(path)
                    folders.append(path)
                else:
                    os.unlink(path)
        for path in reversed(folders):
            os.rmdir(path)

    def sweep(self, store: Path, live: set[str], links: tuple[str, ...]) -> list[str]:
        own = store / OWN
        if self._is_link(store) or self._is_link(own) or not own.is_dir():
            return []
        return [name for name in sorted(os.listdir(own))
                if valid_id(name) and name not in live and _left_on_failure(self._drop_at, own / name, links)]


def _left_on_failure(remove, *args) -> bool:
    """One home's removal for a sweep: one that fails now (a file held
    open, a folder swapped for a link) is left for the next sweep, and the
    sweep goes on to the others."""
    try:
        return bool(remove(*args))
    except OSError:
        return False
