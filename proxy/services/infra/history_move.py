"""Take over the histories an earlier release kept inside the tracked folders.

Until 2.0 each history's ``.git`` sat inside the folder it tracked, where
sessions, people (through the file routes) and paired computers write.
Nothing of such a folder is trusted and no git process ever runs in one:
it is read as data, beneath the agents folder with no link followed.

- Taken: the object files, by name, and the branch's tip.
- Rebuilt, never taken: the config, the hooks, the index, every pack's
  index, alternates, reflogs, everything else.
- The copy is never the store. The object files land in a raw store, and
  what the tip reaches is packed from it and indexed by git's strict reader
  into a second, new store, as a push is received: every object is checked
  against its name and its form, a missing one fails the take, and one the
  tip does not reach never arrives. Each of those git calls is bounded in
  time and memory.
- The old folder then leaves the tracked folder. One that held anything an
  honest folder does not hold is copied aside first, and a warning names
  what was found.

A history that fails for what its old folder holds starts fresh, and the
old folder is set aside. A failure on the store's side (no room, no git)
decides nothing: the folder is left as it is and the next start tries
again.

``take`` runs for one folder under its lock, from ``git_writer`` at the
folder's first use and from ``run_pass``, which the boot starts once in the
background. When a pass has seen every folder it writes the marker, and
from then on no ``.git`` in a tracked folder is ever read again: one a
session makes later is its own business.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import config
from core import layout
from services.infra import git_writer, safe_fs
from services.infra.agent_dirs import agent_dirs
from services.infra.git_writer import StoreTrouble

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("otodock.history")

MARKER = "legacy-taken"
# What is left to do with a folder's old ``.git`` once its entry is in
# place: ``remove`` or ``set-aside``. Written into the entry before it is
# published, so a crash between the two is finished at the next start.
PENDING_FILE = "legacy-pending"

MAX_HISTORY_BYTES = 1024 * 1024 * 1024
MAX_HISTORY_FILES = 100_000
MAX_SET_ASIDE_BYTES = 1024 * 1024 * 1024
MAX_SET_ASIDE_ENTRIES = 200_000
STEP_TIMEOUT = 120          # seconds, each git call of a take
_ADDRESS_KIB = 2 * 1024 * 1024

_LOOSE_DIR = re.compile(r"^[0-9a-f]{2}$")
_LOOSE_FILE = re.compile(r"^[0-9a-f]{38}$")
_PACK_FILE = re.compile(r"^pack-[0-9a-f]{40}\.pack$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")

# What a folder made by ``git init`` and used by ``add``, ``commit`` and
# ``gc`` holds. Anything else was written by someone else.
_HONEST_TOP = frozenset({"HEAD", "config", "description", "hooks", "info", "objects", "refs", "index",
                         "logs", "packed-refs", "branches", "COMMIT_EDITMSG", "ORIG_HEAD", "gc.log",
                         "gc.pid"})
_HONEST_INFO = frozenset({"exclude", "refs"})
_HONEST_OBJECTS_INFO = frozenset({"packs", "commit-graph", "commit-graphs"})
_HONEST_CORE_KEYS = frozenset({"repositoryformatversion", "filemode", "bare", "logallrefupdates",
                               "ignorecase", "precomposeunicode", "symlinks"})
_MAX_SIGNS = 12


class _BadHistory(Exception):
    """The old folder cannot be taken, for what it holds."""


def pending() -> bool:
    """Whether histories of an earlier release may still be waiting."""
    return not (git_writer.history_root() / MARKER).exists()


def _bounded() -> list[str]:
    """The prefix every git call of a take runs under: first in the
    kernel's last-resort kill, and an address-space limit, so a pack made to
    exhaust memory ends its own process."""
    from core.sandbox import sandbox_render
    return sandbox_render.oom_raise(
        ["/bin/sh", "-c", f'ulimit -v {_ADDRESS_KIB} 2>/dev/null; exec "$@"', "history-move"])


_SMALL_WINDOWS = ("-c", "core.packedGitWindowSize=32m", "-c", "core.packedGitLimit=256m",
                  "-c", "pack.threads=1")


def _step(store: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return git_writer._git(store, *_SMALL_WINDOWS, *args, data=data,
                               timeout=STEP_TIMEOUT, wrap=_bounded())
    except subprocess.TimeoutExpired as exc:
        raise _BadHistory(f"git {args[0]} took over {STEP_TIMEOUT} s") from exc


# ---------------------------------------------------------------------------
# Reading the old folder, as data
# ---------------------------------------------------------------------------


def _read(rel: str, cap: int) -> bytes:
    return safe_fs.read_bytes_beneath(config.AGENTS_DIR, rel, max_size=cap)


def _tip(old: str) -> str:
    """The commit the old folder's branch names, read from its files."""
    try:
        head = _read(f"{old}/HEAD", 256).decode("ascii", "replace").strip()
    except OSError as exc:
        raise _BadHistory(f"HEAD cannot be read: {exc.strerror or exc}") from exc
    if _SHA.match(head):
        return head
    if not head.startswith("ref: refs/heads/") or not _REF_NAME.match(head[len("ref: refs/heads/"):]):
        raise _BadHistory("HEAD names no branch")
    ref = head[len("ref: "):]
    with contextlib.suppress(OSError):
        loose = _read(f"{old}/{ref}", 64).decode("ascii", "replace").strip()
        if _SHA.match(loose):
            return loose
    with contextlib.suppress(OSError):
        for line in _read(f"{old}/packed-refs", 4 * 1024 * 1024).decode("ascii", "replace").splitlines():
            sha, _, name = line.partition(" ")
            if name == ref and _SHA.match(sha):
                return sha
    raise _BadHistory("the branch has no readable tip")


def _object_files(old: str) -> list[tuple[str, int]]:
    """``(path under objects/, size)`` of every loose object and pack, by
    name, within the caps."""
    found: list[tuple[str, int]] = []
    total = 0
    try:
        for step in safe_fs.walk_beneath(config.AGENTS_DIR, f"{old}/objects"):
            inner = step.rel[len(old) + len("/objects"):].strip("/")
            if inner == "":
                step.dirs[:] = [d for d in step.dirs if _LOOSE_DIR.match(d) or d == "pack"]
                continue
            step.dirs[:] = []
            names = _PACK_FILE if inner == "pack" else _LOOSE_FILE
            for name in step.files:
                if not names.match(name):
                    continue
                size = os.stat(name, dir_fd=step.dirfd, follow_symlinks=False).st_size
                total += size
                found.append((f"{inner}/{name}", size))
                if len(found) > MAX_HISTORY_FILES or total > MAX_HISTORY_BYTES:
                    raise _BadHistory("the old history is over the size a take allows")
    except OSError as exc:
        raise _BadHistory(f"the object files cannot be listed: {exc.strerror or exc}") from exc
    return found


def _copy_objects(old: str, files: list[tuple[str, int]], raw: Path) -> None:
    for inner, size in files:
        try:
            # One link only: a hard link would bring in another file's bytes.
            fd, _ = safe_fs.open_regular_for_read(config.AGENTS_DIR, f"{old}/objects/{inner}",
                                                  single_link=True, max_size=size)
        except OSError as exc:
            raise _BadHistory(f"an object file cannot be read: {exc.strerror or exc}") from exc
        with os.fdopen(fd, "rb") as src:
            try:
                target = raw / "objects" / inner
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as dst:
                    safe_fs.copy_fd(src, dst, max_size=size)
            except safe_fs.FileTooLarge as exc:
                raise _BadHistory("an object file grew while it was read") from exc
            except OSError as exc:
                raise StoreTrouble(exc.errno, "the history store cannot be written", str(raw)) from exc


def _listing(rel: str) -> safe_fs.WalkStep | None:
    """The entries of one folder, by kind, or None when it is not one."""
    walk = safe_fs.walk_beneath(config.AGENTS_DIR, rel)
    try:
        return next(walk)
    except (OSError, StopIteration):
        return None
    finally:
        walk.close()


def _config_signs(old: str) -> list[str]:
    try:
        text = _read(f"{old}/config", 64 * 1024).decode("utf-8", "replace")
    except FileNotFoundError:
        return ["no config"]
    except OSError:
        return ["a config that is not a plain file"]
    signs: list[str] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
            if section != "core":
                signs.append(f"config section [{section[:40]}]")
            continue
        key = line.split("=", 1)[0].strip().lower()
        if section == "core" and key not in _HONEST_CORE_KEYS:
            signs.append(f"config key core.{key[:40]}")
    return signs


def signs(old: str) -> list[str]:
    """What the old folder holds that ``git init``, ``add``, ``commit`` and
    ``gc`` never put there. Names only, never a value."""
    found: list[str] = []

    def extras(where: str, honest: Callable[[str], bool]) -> None:
        folder = f"{old}/{where}".rstrip("/")
        step = _listing(folder)
        if step is None:
            return
        for name in (*step.dirs, *step.files, *step.other):
            if not honest(name):
                found.append(f"{where}{name[:60]}")
        for name in step.symlinks:
            target = "?"
            with contextlib.suppress(OSError):
                target = safe_fs.readlink_beneath(config.AGENTS_DIR, f"{folder}/{name}")
            found.append(f"a link {where}{name[:60]} to {target[:120]}")

    extras("", lambda n: n in _HONEST_TOP)
    extras("hooks/", lambda n: n.endswith(".sample"))
    extras("info/", lambda n: n in _HONEST_INFO)
    extras("objects/info/", lambda n: n in _HONEST_OBJECTS_INFO)
    found += _config_signs(old)
    if len(found) > _MAX_SIGNS:
        found = [*found[:_MAX_SIGNS], f"and {len(found) - _MAX_SIGNS} more"]
    return found


# ---------------------------------------------------------------------------
# One folder
# ---------------------------------------------------------------------------


def _room_for(size: int) -> None:
    """A take writes the raw copy and the new store: both must fit above
    the install's free-space floor."""
    try:
        free = shutil.disk_usage(git_writer._root()).free
    except StoreTrouble:
        raise
    except OSError as exc:
        raise StoreTrouble(exc.errno, "the history folder cannot be read", "") from exc
    if free < config.MIN_FREE_DISK_MB * 1024 * 1024 + 2 * size:
        raise StoreTrouble(0, "not enough free space to take a history "
                              f"({free // (1024 * 1024)} MB free)", str(git_writer.history_root()))


def _repacked(old: str) -> Path:
    """A new store that holds what the old folder's tip reaches, checked."""
    tip = _tip(old)
    files = _object_files(old)
    _room_for(sum(size for _, size in files))
    raw = git_writer.new_store()
    built: Path | None = None
    try:
        _copy_objects(old, files, raw)
        # Each old pack on its own: every object checked for its form. A pack
        # git's clean-up made later names objects of an earlier one, so the
        # links are checked once, below, in the store repacked from the tip.
        for inner, size in files:
            if inner.startswith("pack/"):
                done = _step(raw, "index-pack", "--fsck-objects", f"--max-input-size={max(size, 1)}",
                             str(raw / "objects" / inner))
                if done.returncode != 0:
                    raise _BadHistory("a pack of the old history does not index")
        built = git_writer.new_store()
        base = built / "objects" / "pack" / "pack"
        packed = _step(raw, "pack-objects", "--revs", "-q", str(base), data=tip.encode() + b"\n")
        name = packed.stdout.decode("ascii", "replace").strip()
        if packed.returncode != 0 or not _SHA.match(name):
            raise _BadHistory("the old history is not complete from its tip")
        # pack-objects wrote its own index beside the pack, naming each object
        # as the raw store named it. The strict reader would find a name
        # there and count the object as present: it must see the pack alone.
        for beside in base.parent.glob(f"pack-{name}.*"):
            if beside.suffix != ".pack":
                beside.unlink()
        # The strict reader checks the pack from its own bytes: every object
        # hashed, checked for its form and for the objects it names, and a
        # missing one fails it.
        checked = _step(built, "index-pack", "--strict", f"--max-input-size={MAX_HISTORY_BYTES}",
                        f"{base}-{name}.pack")
        if checked.returncode != 0:
            raise _BadHistory("an object of the old history is not what its name says")
        if _step(built, "update-ref", git_writer.BRANCH, tip).returncode != 0:
            raise _BadHistory("the old history's tip is not a commit")
        return built
    except BaseException:
        if built is not None:
            shutil.rmtree(built, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(raw, ignore_errors=True)


def take(rel: str) -> bool:
    """Move the history an earlier release kept in ``rel/.git`` into the
    store. Called under the entry's lock, while no store of ``rel`` exists.
    True when one exists afterwards. Raises ``StoreTrouble`` when the store
    cannot be written: nothing is decided then."""
    old = f"{rel}/.git"
    try:
        st = safe_fs.lstat_beneath(config.AGENTS_DIR, old)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        _drop_pointer(old, st)
        return False
    found = signs(old)
    try:
        built = _repacked(old)
        reason = ""
    except _BadHistory as exc:
        reason = str(exc)
        built = git_writer.new_store()
        if git_writer._write_commit(built, "init", []) is None:
            shutil.rmtree(built, ignore_errors=True)
            raise StoreTrouble(0, "the first commit of a history failed", str(built)) from exc
    try:
        (built / PENDING_FILE).write_text("set-aside\n" if (found or reason) else "remove\n")
    except OSError as exc:
        shutil.rmtree(built, ignore_errors=True)
        raise StoreTrouble(exc.errno, "the history store cannot be written", str(built)) from exc
    git_writer.publish(built, rel)
    if reason:
        logger.warning("history: the old history of %s could not be taken (%s). "
                       "A new one starts, the old folder is set aside", rel, reason)
    if found:
        logger.warning("history: the old git folder of %s held what the platform never writes "
                       "(%s). It is set aside for a look. This can be harmless", rel, "; ".join(found))
    finish(rel)
    if not reason:
        logger.info("history: took the history of %s", rel)
    return True


def _drop_pointer(old: str, st: os.stat_result) -> None:
    """A ``.git`` that is a file or a link holds no history: it goes, and
    the log says where it pointed."""
    said = ""
    with contextlib.suppress(OSError):
        if stat.S_ISLNK(st.st_mode):
            said = "a link to " + safe_fs.readlink_beneath(config.AGENTS_DIR, old)[:200]
        elif stat.S_ISREG(st.st_mode):
            said = "a file that reads " + _read(old, 512).decode("utf-8", "replace").strip()[:200]
    try:
        safe_fs.unlink_beneath(config.AGENTS_DIR, old, missing_ok=True)
    except OSError:
        logger.warning("history: %s is not a folder and could not be removed", old, exc_info=True)
        return
    logger.warning("history: %s was not a git folder (%s) and was removed: nothing to take", old, said or "no folder")


def set_aside_rel(rel: str) -> str:
    slug, _, rest = rel.partition("/")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{git_writer.SET_ASIDE}/{slug}/{rest.replace('/', '-')}.old-git-{stamp}"


def finish(rel: str) -> None:
    """Do what an entry's pending file says with the folder's old ``.git``,
    then drop the file. Called under the entry's lock."""
    flag = git_writer.store_path(rel) / PENDING_FILE
    try:
        action = flag.read_text().strip()
    except OSError:
        return
    old = f"{rel}/.git"
    try:
        st = safe_fs.lstat_beneath(config.AGENTS_DIR, old)
    except OSError:
        st = None
    if st is not None and stat.S_ISDIR(st.st_mode):
        kept = True
        if action == "set-aside":
            kept = _copy_aside(rel, old)
        if kept:
            try:
                safe_fs.rmtree_beneath(config.AGENTS_DIR, old, missing_ok=True)
            except OSError:
                logger.warning("history: the old git folder %s could not be removed. "
                               "Nothing reads it any more", old, exc_info=True)
    with contextlib.suppress(OSError):
        flag.unlink()


def _copy_aside(rel: str, old: str) -> bool:
    """Copy the old folder into the store's set-aside place, links skipped.
    False when the copy failed: the old folder then stays where it is."""
    dst = set_aside_rel(rel)
    try:
        if shutil.disk_usage(git_writer._root()).free < config.MIN_FREE_DISK_MB * 1024 * 1024:
            raise OSError(0, "not enough free space")
        safe_fs.copytree_beneath(
            config.AGENTS_DIR, old, git_writer.history_root(), dst, symlinks="skip",
            budget=safe_fs.CopyBudget(max_entries=MAX_SET_ASIDE_ENTRIES, max_bytes=MAX_SET_ASIDE_BYTES))
    except OSError as exc:
        logger.warning("history: the old git folder %s could not be set aside (%s). It stays where "
                       "it is. Nothing reads it any more", old, exc.strerror or exc)
        return False
    logger.warning("history: the old git folder of %s is kept at %s", rel, git_writer.history_root() / dst)
    return True


# ---------------------------------------------------------------------------
# Every folder, once
# ---------------------------------------------------------------------------


def tracked_folders() -> list[str]:
    """Every tracked folder that exists, relative to the agents folder."""
    found: list[str] = []
    for agent in sorted(agent_dirs(config.AGENTS_DIR), key=lambda p: p.name):
        slug = agent.name
        for name in (layout.CONFIG, layout.KNOWLEDGE):
            found.append(f"{slug}/{name}")
        people = _listing(f"{slug}/{layout.USERS}")
        for person in (people.dirs if people else []):
            if not person.startswith("."):
                found.append(f"{slug}/{layout.USERS}/{person}/{layout.CONTEXT}")
    return [rel for rel in found if _is_folder(rel)]


def _is_folder(rel: str) -> bool:
    try:
        return stat.S_ISDIR(safe_fs.lstat_beneath(config.AGENTS_DIR, rel).st_mode)
    except OSError:
        return False


def _sweep_incoming() -> None:
    """Stores a process that is gone was building. One this process builds
    right now carries its token and is left alone."""
    incoming = git_writer.history_root() / git_writer.INCOMING
    if not incoming.is_dir():
        return
    for path in incoming.iterdir():
        if not path.name.startswith(git_writer.PROCESS_TOKEN + "-"):
            shutil.rmtree(path, ignore_errors=True)


def run_pass() -> dict[str, int]:
    """Give every tracked folder its entry, taking what an earlier release
    left, then write the marker. Run once per start, in the background.
    A failure on the store's side stops the pass with no marker: the next
    start goes on from there."""
    counts = {"existing": 0, "taken": 0, "fresh": 0, "none": 0}
    if not pending():
        return counts
    try:
        _sweep_incoming()
        for rel in tracked_folders():
            with git_writer.entry_lock(rel):
                store, state = git_writer._ensure_state(rel, create=True)
            if store is None:
                logger.error("history: the pass stops at %s. The next start goes on from there", rel)
                return counts
            counts[state] += 1
        root = git_writer._root()
        temp = root / f"{MARKER}.{git_writer.PROCESS_TOKEN}"
        temp.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
        os.replace(temp, root / MARKER)
    except OSError:
        logger.exception("history: the pass over the earlier histories failed. The next start tries again")
        return counts
    if counts["taken"] or counts["fresh"]:
        logger.info("history: %d taken from the folders, %d started, %d already in place",
                    counts["taken"], counts["fresh"], counts["existing"])
    return counts
