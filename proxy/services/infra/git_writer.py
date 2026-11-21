"""Git-based audit trail for agent config, knowledge, + per-user context.

Three histories per agent, one per tracked folder:
  - ``agents/{slug}/config/``                    (agent.md, context/ docs)
  - ``agents/{slug}/knowledge/``                 (incl. memory/ topic files)
  - ``agents/{slug}/users/{username}/context/``  (user docs + memory/)

Each history is a bare repository in the proxy's own data
(``SESSIONS_DIR/history/agents/...``), never inside the folder it tracks,
and git is never given a tracked folder: not as its git folder, not as a
work tree, not as the directory it runs in. A tracked folder is written by
sessions, by people through the file routes and by paired computers, and
git obeys what it finds in a repository and in the work tree around it
(git(1), SECURITY). So a write is committed by reading each file's bytes
here, with no link followed, and handing them to ``git fast-import`` on a
pipe; a revert reads the old bytes from git and writes the file here.

``_git`` starts every git process of the proxy: a fixed environment (none of
the server's own is inherited but PATH and TZ), no global and no system
config, and every setting that could name a program or a file turned off on
the command line. The store's own config is written by ``git init`` and by
nothing else.

A folder outside the three shapes above has no history: every function
answers its "nothing" value for it. All functions sync (called from
``asyncio.to_thread`` in handlers).
"""

from __future__ import annotations

import contextlib
import fnmatch
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import config
from core import layout
from services.infra import safe_fs
from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

logger = logging.getLogger("otodock.git-writer")

# Commit identifiers reaching `git show` MUST be plain hex SHAs. Anything
# else (a leading `-`, e.g. `--output=/etc/foo`) would be parsed by git as an
# OPTION rather than a revision — argument injection → arbitrary file write.
# Callers always pass a SHA from the log; symbolic refs are intentionally not
# accepted at these sinks.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_AUTHOR_NAME = "otodock-system"
_AUTHOR_EMAIL = "system@otodock"
BRANCH = "refs/heads/main"

HISTORY_DIRNAME = "history"
# The store's own names beside ``agents/``: no agent's name can be one of them.
ENTRIES = "agents"
INCOMING = "incoming"
SET_ASIDE = "set-aside"
OFFBOARDED = "offboarded"
_HOME = "home"

TIMEOUT = 30        # seconds: a commit, a read
GC_TIMEOUT = 120
# One file, and one commit: past either the rest is skipped and logged.
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_COMMIT_BYTES = 64 * 1024 * 1024
MAX_COMMIT_ENTRIES = 5000
# What a directory commit leaves out (the rules the tracked folder's own
# ``.gitignore`` held while git read the folder itself).
_IGNORED_FILES = ("*.tmp", "*.bak", ".~*")
_IGNORED_DIRS = frozenset({"__pycache__"})

# Every setting through which a repository, a work tree or a person's own
# config can make git run a program or read a file elsewhere, off for every
# call. The store's config is the server's own, so most lines guard a way no
# writer reaches: they hold if that ever stops being true.
SETTINGS: tuple[str, ...] = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.attributesFile=/dev/null",
    "core.excludesFile=/dev/null",
    # A name that resolves to no tree gives an empty one (git-config(1)):
    # no attribute is read from a history's own files in a bare repository.
    "attr.tree=refs/otodock/none",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "log.showSignature=false",
    # A bare repository reads HEAD:.mailmap, a file of the history itself.
    "log.mailmap=false",
    "mailmap.file=",
    "mailmap.blob=",
    "core.pager=cat",
    "core.editor=true",
    "core.sshCommand=false",
    "core.askPass=",
    "credential.helper=",
    "protocol.allow=never",
    # A gc that starts runs inside the call and leaves no process behind.
    "gc.autoDetach=false",
    "init.defaultRefFormat=files",
)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
# Names the stores this process is building under ``incoming/``.
PROCESS_TOKEN = secrets.token_hex(4)


class StoreTrouble(OSError):
    """The store itself cannot be written (no room, no git program): not the
    tracked folder's fault, so nothing about that folder is decided."""


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def history_root() -> Path:
    """The folder that holds every history: the proxy's own, never bound
    into a session, never mounted into the compute service, never synced."""
    return config.SESSIONS_DIR / HISTORY_DIRNAME


def _root() -> Path:
    """``history_root``, made on first use for the proxy's user alone."""
    root = history_root()
    if not root.is_dir():
        try:
            root.mkdir(parents=True, exist_ok=True)
            root.chmod(0o700)
        except OSError as exc:
            raise StoreTrouble(exc.errno, "the history folder cannot be made", str(root)) from exc
    return root


def entry_rel(repo_dir: Path) -> str | None:
    """``repo_dir`` relative to the agents folder when it is one of the
    three tracked folders, else None: ``<slug>/config``,
    ``<slug>/knowledge``, ``<slug>/users/<username>/context``."""
    try:
        rel = safe_fs.rel_under(os.path.normpath(os.fspath(repo_dir)), config.AGENTS_DIR)
    except (OSError, ValueError):
        return None
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts):
        return None
    if len(parts) == 2 and parts[1] in (layout.CONFIG, layout.KNOWLEDGE):
        return rel
    if len(parts) == 4 and parts[1] == layout.USERS and parts[3] == layout.CONTEXT:
        return rel
    return None


def store_path(rel: str) -> Path:
    """The bare repository of the tracked folder ``rel`` (an ``entry_rel``)."""
    return history_root() / ENTRIES / f"{rel}.git"


def entry_lock(rel: str) -> threading.Lock:
    """The lock a commit, a revert and the move of one history take."""
    with _locks_guard:
        return _locks.setdefault(rel, threading.Lock())


def _environment() -> dict[str, str]:
    home = _root() / _HOME
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_LITERAL_PATHSPECS": "1",
    }
    if os.environ.get("TZ"):
        env["TZ"] = os.environ["TZ"]
    return env


def command(store: Path, *args: str) -> list[str]:
    """The git command every call runs: the store named outright, then the
    fixed settings, then the call's own words."""
    cmd = ["git", f"--git-dir={store}"]
    for setting in SETTINGS:
        cmd += ["-c", setting]
    return [*cmd, *args]


def _git(store: Path, *args: str, data: bytes | None = None, timeout: int = TIMEOUT,
         wrap: Sequence[str] = ()) -> subprocess.CompletedProcess[bytes]:
    """Run one git call on ``store``. ``wrap`` is a prefix the command runs
    under (the move's limits). Raises ``StoreTrouble`` when git cannot be
    started, ``subprocess.TimeoutExpired`` past ``timeout``."""
    root = _root()
    try:
        return subprocess.run(
            [*wrap, *command(store, *args)],
            cwd=root, env=_environment(), input=data,
            stdin=subprocess.DEVNULL if data is None else None,
            capture_output=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise StoreTrouble(exc.errno, "git could not be started", str(exc.filename)) from exc


def _text(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stderr.decode(errors="replace").strip()


def new_store() -> Path:
    """A fresh bare repository under ``incoming/``, with nothing in it:
    ``publish`` moves it to its place."""
    path = _root() / INCOMING / f"{PROCESS_TOKEN}-{secrets.token_hex(8)}"
    try:
        path.parent.mkdir(exist_ok=True)
    except OSError as exc:
        raise StoreTrouble(exc.errno, "the history folder cannot be written", str(path)) from exc
    # No template: no hooks folder, no info folder. Formats pinned, so a
    # later git's new defaults do not change a store's shape.
    result = _git(path, "init", "-q", "--bare", "--template=", "--object-format=sha1",
                  "-b", "main", str(path))
    if result.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        raise StoreTrouble(0, f"git init failed: {_text(result)}", str(path))
    return path


def publish(built: Path, rel: str) -> Path:
    """Move a finished store to its place by one rename. One that is
    already there wins: ``built`` is removed."""
    store = store_path(rel)
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        os.rename(built, store)
    except OSError as exc:
        shutil.rmtree(built, ignore_errors=True)
        if not store.is_dir():
            raise StoreTrouble(exc.errno, "the history could not be put in place", str(store)) from exc
    return store


def _fresh(rel: str) -> Path:
    """A new history with one empty commit, so a log works at once."""
    built = new_store()
    if _write_commit(built, "init", []) is None:
        shutil.rmtree(built, ignore_errors=True)
        raise StoreTrouble(0, "the first commit of a history failed", str(built))
    return publish(built, rel)


def _ensure_state(rel: str, *, create: bool) -> tuple[Path | None, str]:
    """The store of ``rel`` and how it came to be: ``existing``, ``taken``
    (an earlier release's history was moved in, history_move), ``fresh`` or
    ``none``. Called under the entry's lock."""
    from services.infra import history_move
    store = store_path(rel)
    try:
        if store.is_dir():
            if (store / history_move.PENDING_FILE).exists():
                history_move.finish(rel)
            return store, "existing"
        if history_move.pending() and history_move.take(rel):
            return store, "taken"
        if create:
            return _fresh(rel), "fresh"
        return None, "none"
    except StoreTrouble as exc:
        logger.error("history: the store of %s cannot be written: %s", rel, exc)
        return None, "none"


def _ensure(rel: str, *, create: bool) -> Path | None:
    """The store of ``rel``, under its lock. While the histories of an
    earlier release are still being taken, a folder that has none here and
    an old one in place gets it moved first. ``create`` makes a fresh one
    where nothing exists. None when there is none, or when the store cannot
    be written."""
    return _ensure_state(rel, create=create)[0]


def set_aside(path: Path, slug: str, label: str) -> Path | None:
    """Move a store out of use, kept under ``set-aside/<slug>/``."""
    dst = history_root() / SET_ASIDE / slug / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(path, dst)
    except OSError:
        logger.warning("history: %s could not be set aside", path, exc_info=True)
        return None
    # A rename keeps the folder's own date: its keep counts from today.
    with contextlib.suppress(OSError):
        os.utime(dst)
    return dst


# ---------------------------------------------------------------------------
# Repo init + writes
# ---------------------------------------------------------------------------


def init_if_missing(repo_dir: Path) -> bool:
    """Make ``repo_dir`` and its history. Returns True if a new history was
    created. Idempotent — no-op if it exists."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    rel = entry_rel(repo_dir)
    if rel is None:
        logger.warning("git init: %s is not a tracked folder", repo_dir)
        return False
    with entry_lock(rel):
        return _ensure_state(rel, create=True)[1] == "fresh"


def commit_file(repo_dir: Path, file_path: Path, message: str) -> str | None:
    """Commit ``file_path`` (relative to ``repo_dir`` or absolute).
    Returns the resulting commit sha, or None on failure."""
    return commit_paths(repo_dir, [file_path], message)


def is_tracked(repo_dir: Path, rel_path: str) -> bool:
    """True when ``rel_path`` (a file, or a folder that holds one) is in the
    history's newest commit."""
    rel = entry_rel(repo_dir)
    if rel is None:
        return False
    with entry_lock(rel):
        store = _ensure(rel, create=False)
    if store is None:
        return False
    try:
        result = _git(store, "ls-tree", "-z", "--name-only", BRANCH, "--", rel_path)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout)


def commit_paths(repo_dir: Path, paths: Sequence[Path | str], message: str) -> str | None:
    """Commit several paths as ONE commit. Used by memory ops where a single
    logical write touches multiple files (topic + regenerated index, or a
    rename's old + new path). A path that is gone is recorded as deleted, a
    folder with everything in it. Returns the resulting commit sha, or None
    on failure.
    """
    rel = entry_rel(repo_dir)
    if rel is None:
        logger.warning("commit_paths: %s is not a tracked folder", repo_dir)
        return None
    rels: list[str] = []
    for p in paths:
        path = Path(p)
        try:
            rels.append((path.relative_to(repo_dir) if path.is_absolute() else path).as_posix())
        except ValueError:
            logger.warning("commit_paths: path %s not under repo %s", p, repo_dir)
            return None
    if not rels:
        return None
    try:
        with entry_lock(rel):
            store = _ensure(rel, create=True)
            if store is None:
                return None
            sha = _write_commit(store, message, _changes(rel, rels))
            if sha is None and not _tip_readable(store):
                # A store that came back half-written (a restore, a full
                # disk): every later write would go unrecorded in silence.
                logger.warning("history: the history of %s cannot be read and is set aside; "
                               "a new one starts", rel)
                if set_aside(store, rel.split("/")[0], _label(rel) + ".unreadable") is None:
                    return None
                store = _fresh(rel)
                sha = _write_commit(store, message, _changes(rel, rels))
            if sha is not None:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    _git(store, "gc", "--auto", "--quiet", timeout=GC_TIMEOUT)
            return sha
    except (OSError, subprocess.SubprocessError):
        logger.exception("commit_paths failed for %s %s", repo_dir, rels)
        return None


def _label(rel: str) -> str:
    return "-".join(rel.split("/")[1:])


def _unsafe_part(path: str) -> bool:
    """A path git must never be handed as a name: not a clean relative path
    (``normalize_rel_path``), or one with a part named ``.git`` in any case."""
    try:
        if normalize_rel_path(path) != path:
            return True
    except PathOutsideRoot:
        return True
    return any(part.lower() == ".git" for part in path.split("/"))


def _quoted(path: str) -> bytes:
    """``path`` as fast-import's C-quoted string: no byte of a name can end
    the field or start a line of its own."""
    out = bytearray(b'"')
    for byte in os.fsencode(path):
        if byte in (0x22, 0x5C):
            out += b"\\" + bytes([byte])
        elif byte < 0x20 or byte == 0x7F:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    return bytes(out + b'"')


def _changes(rel: str, paths: Sequence[str]) -> Iterator[bytes]:
    """The fast-import lines for ``paths`` of the tracked folder ``rel``,
    read beneath the agents folder with no link followed. A file that is
    gone is a delete. What cannot be read safely is skipped, never recorded
    as gone: a folder committed whole is first removed and written again
    only when its walk read everything, else its lines alone are written
    and the history keeps what it had."""
    root = config.AGENTS_DIR
    budget = [MAX_COMMIT_ENTRIES, MAX_COMMIT_BYTES]
    over_cap = [0]

    def entry(path: str, at: str, st: os.stat_result) -> tuple[bytes | None, bool]:
        """The file's line, and whether it was left out (a refused read, a
        cap) rather than a kind a history never takes."""
        if budget[0] <= 0:
            over_cap[0] += 1
            return None, True
        try:
            if stat.S_ISLNK(st.st_mode):
                mode, data = b"120000", os.fsencode(safe_fs.readlink_beneath(root, at))
            elif stat.S_ISREG(st.st_mode):
                mode = b"100755" if st.st_mode & 0o111 else b"100644"
                data = safe_fs.read_bytes_beneath(root, at, max_size=min(MAX_FILE_BYTES, budget[1]))
            else:
                return None, False
        except OSError as exc:
            logger.warning("history: %s/%s is left out of the commit: %s", rel, path, exc)
            return None, True
        budget[0] -= 1
        budget[1] -= len(data)
        return b"M %s inline %s\ndata %d\n%s\n" % (mode, _quoted(path), len(data), data), False

    for path in paths:
        if _unsafe_part(path):
            logger.warning("history: %s/%s is not a name a history takes", rel, path)
            continue
        at = f"{rel}/{path}"
        try:
            st = safe_fs.lstat_beneath(root, at)
        except (FileNotFoundError, NotADirectoryError):
            yield b"D %s\n" % _quoted(path)
            continue
        except OSError as exc:
            logger.warning("history: %s is left out of the commit: %s", at, exc)
            continue
        if not stat.S_ISDIR(st.st_mode):
            line, _left_out = entry(path, at, st)
            if line is not None:
                yield line
            continue
        lines: list[bytes] = []
        left_out = [False]

        def unreadable(_exc: OSError, flag: list[bool] = left_out) -> None:
            flag[0] = True

        for step in safe_fs.walk_beneath(root, at, onerror=unreadable):
            step.dirs[:] = [d for d in step.dirs if d not in _IGNORED_DIRS and d.lower() != ".git"]
            for name in (*step.files, *step.symlinks):
                if name.lower() == ".git" or any(fnmatch.fnmatch(name, pat) for pat in _IGNORED_FILES):
                    continue
                try:
                    child = os.stat(name, dir_fd=step.dirfd, follow_symlinks=False)
                except OSError:
                    left_out[0] = True
                    continue
                line, missed = entry(step.path(name)[len(rel) + 1:], step.path(name), child)
                left_out[0] = left_out[0] or missed
                if line is not None:
                    lines.append(line)
        if left_out[0]:
            logger.warning("history: %s was not read whole; what was left out keeps its last version", at)
        else:
            yield b"D %s\n" % _quoted(path)
        yield from lines
    if over_cap[0]:
        logger.warning("history: a commit of %s is over %d entries; %d left out",
                       rel, MAX_COMMIT_ENTRIES, over_cap[0])


def _tip_readable(store: Path) -> bool:
    """Whether the branch names a commit git can read, or does not exist yet."""
    try:
        if _git(store, "rev-parse", "-q", "--verify", BRANCH).returncode != 0:
            return (store / "HEAD").is_file()
        return _git(store, "cat-file", "-e", BRANCH + "^{tree}").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _write_commit(store: Path, message: str, changes: Iterator[bytes] | Sequence[bytes]) -> str | None:
    """One commit on the branch, from its tip, through ``git fast-import``:
    no work tree, no index, no commit hook, and the branch only ever moves
    forward. A commit with nothing changed is still made. Returns the new
    tip, or None."""
    has_tip = _git(store, "rev-parse", "-q", "--verify", BRANCH + "^{commit}").returncode == 0
    text = message.encode("utf-8", "replace")
    stream = bytearray()
    stream += b"commit %s\n" % BRANCH.encode()
    stream += b"committer %s <%s> %d %s\n" % (
        _AUTHOR_NAME.encode(), _AUTHOR_EMAIL.encode(), int(time.time()),
        time.strftime("%z").encode() or b"+0000")
    stream += b"data %d\n%s\n" % (len(text), text)
    if has_tip:
        stream += b"from %s^0\n" % BRANCH.encode()
    for line in changes:
        stream += line
    stream += b"done\n"
    result = _git(store, "fast-import", "--quiet", "--done", "--date-format=raw", data=bytes(stream))
    if result.returncode != 0:
        logger.warning("git fast-import failed in %s: %s", store, _text(result))
        # Its crash report names the paths of the commit.
        for report in store.glob("fast_import_crash_*"):
            report.unlink(missing_ok=True)
        return None
    return _head_sha(store)


# ---------------------------------------------------------------------------
# Read API (used by dashboard git tab)
# ---------------------------------------------------------------------------


def _existing(repo_dir: Path) -> Path | None:
    rel = entry_rel(repo_dir)
    if rel is None:
        return None
    with entry_lock(rel):
        return _ensure(rel, create=False)


def log(repo_dir: Path, limit: int = 50, path: str | None = None) -> list[dict[str, Any]]:
    """Return up to ``limit`` recent commits. ``path``, if given, scopes
    the log to a single file (relative to repo_dir).
    """
    store = _existing(repo_dir)
    if store is None:
        return []
    # Records end with a NUL byte: no character of a subject starts a row.
    args = [
        "log", "-z", "--no-mailmap",
        f"--max-count={int(limit)}",
        "--pretty=format:%H%x09%aI%x09%an%x09%s",
        BRANCH,
    ]
    if path:
        args += ["--", path]
    try:
        result = _git(store, *args)
    except (OSError, subprocess.SubprocessError):
        logger.exception("git log failed for %s", repo_dir)
        return []
    if result.returncode != 0:
        logger.warning("git log failed for %s: %s", repo_dir, _text(result))
        return []
    out: list[dict[str, Any]] = []
    for record in result.stdout.decode(errors="replace").split("\0"):
        parts = record.split("\t", 3)
        if len(parts) != 4:
            continue
        sha, date, author, subject = parts
        out.append({
            "sha": sha, "short_sha": sha[:8],
            "date": date, "author": author, "subject": subject,
        })
    return out


def diff(repo_dir: Path, commit: str, path: str | None = None) -> str:
    """Return ``git show`` output for ``commit`` (optionally scoped to one
    file). Returns the diff body as text.
    """
    store = _existing(repo_dir)
    if store is None:
        return ""
    if not _COMMIT_SHA_RE.match(commit):
        logger.warning("git diff: rejected non-SHA commit %r", commit)
        return ""
    args = ["show", "--no-color", "--no-ext-diff", "--no-textconv", "--no-mailmap",
            "--end-of-options", commit]
    if path:
        args += ["--", path]
    try:
        result = _git(store, *args)
    except (OSError, subprocess.SubprocessError):
        logger.exception("git show failed for %s @ %s", repo_dir, commit)
        return ""
    if result.returncode != 0:
        logger.warning("git show failed for %s @ %s: %s", repo_dir, commit, _text(result))
        return ""
    return result.stdout.decode(errors="replace")


def _blob_at(store: Path, commit: str, rel_path: str) -> tuple[bool, bytes] | None:
    """``(executable, bytes)`` of the plain file ``rel_path`` in ``commit``,
    which must be on the branch. None for anything else: a link, a folder, a
    path that commit does not hold, a file over the cap."""
    full = _git(store, "rev-parse", "-q", "--verify", "--end-of-options", commit + "^{commit}")
    sha = full.stdout.decode(errors="replace").strip()
    if full.returncode != 0 or not _FULL_SHA_RE.match(sha):
        return None
    if _git(store, "merge-base", "--is-ancestor", sha, BRANCH).returncode != 0:
        logger.warning("git revert: %s is not a commit of this history", commit)
        return None
    listing = _git(store, "ls-tree", "-z", sha, "--", rel_path)
    entries = [e for e in listing.stdout.split(b"\0") if e]
    if listing.returncode != 0 or len(entries) != 1:
        return None
    meta, _, name = entries[0].partition(b"\t")
    fields = meta.split()
    # Those two modes are a plain file's alone: a link, a folder and a
    # nested repository each carry their own.
    if len(fields) != 3 or fields[0] not in (b"100644", b"100755") or name != os.fsencode(rel_path):
        return None
    blob = fields[2].decode()
    size = _git(store, "cat-file", "-s", blob)
    if size.returncode != 0 or int(size.stdout.strip() or b"0") > MAX_FILE_BYTES:
        return None
    content = _git(store, "cat-file", "blob", blob)
    if content.returncode != 0:
        return None
    return fields[0] == b"100755", content.stdout


def revert_file_to(
    repo_dir: Path, commit: str, file_path: Path,
    *, message: str | None = None,
) -> str | None:
    """Write ``file_path`` as it was in ``commit``, then commit. Returns new
    HEAD. Used by the admin "Revert to commit X" button. Only a plain file
    is put back, written here with no link followed.
    """
    entry = entry_rel(repo_dir)
    store = _existing(repo_dir)
    if entry is None or store is None:
        return None
    if not _COMMIT_SHA_RE.match(commit):
        logger.warning("git revert: rejected non-SHA commit %r", commit)
        return None
    try:
        rel = (file_path.relative_to(repo_dir) if file_path.is_absolute() else file_path).as_posix()
    except ValueError:
        logger.warning("revert_file_to: path %s not under repo %s",
                       file_path, repo_dir)
        return None
    if _unsafe_part(rel):
        return None
    try:
        found = _blob_at(store, commit, rel)
        if found is None:
            logger.warning("git revert: %s holds no plain file %s", commit, rel)
            return None
        executable, data = found
        at = f"{entry}/{rel}"
        mode: int | None = None
        with contextlib.suppress(OSError):
            now = safe_fs.lstat_beneath(config.AGENTS_DIR, at)
            if stat.S_ISREG(now.st_mode):
                bits = stat.S_IMODE(now.st_mode)
                mode = bits | 0o111 if executable else bits & ~0o111
        if mode is None and executable:
            mode = 0o755
        safe_fs.atomic_write_beneath(config.AGENTS_DIR, at, data, mode=mode, mkdirs=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.exception("git revert failed for %s @ %s", file_path, commit)
        return None
    return commit_paths(
        repo_dir, [rel],
        message or f"revert {rel} to {commit[:8]}",
    )


def _head_sha(store: Path) -> str | None:
    try:
        result = _git(store, "rev-parse", "-q", "--verify", BRANCH)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.decode(errors="replace").strip()
    return sha if result.returncode == 0 and sha else None


# ---------------------------------------------------------------------------
# A history follows its folder
# ---------------------------------------------------------------------------


def _plain_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\x00" not in name and not name.startswith(".")


def forget_agent(slug: str) -> None:
    """Remove every history of an agent that is gone, and what was set
    aside for it."""
    if not _plain_name(slug):
        return
    for part in (ENTRIES, SET_ASIDE):
        shutil.rmtree(history_root() / part / slug, ignore_errors=True)


def set_aside_agent(slug: str) -> None:
    """Histories found under the name of an agent that is being created
    belong to an earlier one: kept aside, never shown as the new agent's."""
    if not _plain_name(slug):
        return
    old = history_root() / ENTRIES / slug
    if old.is_dir():
        logger.warning("history: agent %s is new and histories under its name exist; they are set aside", slug)
        if set_aside(old, slug, "earlier-agent") is None:
            shutil.rmtree(old, ignore_errors=True)


def archive_person(agent: str, username: str) -> None:
    """A person's folder left ``agent`` for the archive: their history
    leaves its place too, so the name given out again meets none."""
    if not (_plain_name(agent) and _plain_name(username)):
        return
    src = history_root() / ENTRIES / agent / layout.USERS / username
    if not src.is_dir():
        return
    dst = history_root() / OFFBOARDED / username / agent
    try:
        shutil.rmtree(dst, ignore_errors=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)
    except OSError:
        logger.warning("history: the history of %s on %s was not archived", username, agent, exc_info=True)


#: A set-aside copy is kept this long for a look, then removed.
SET_ASIDE_KEEP_DAYS = 180


def remove_orphans(agents_with_a_row: set[str], *, dry_run: bool = False) -> int:
    """Remove what no folder stands for any more: the histories and the
    set-aside copies of an agent that has no row and no folder, and a
    set-aside copy older than ``SET_ASIDE_KEEP_DAYS``. Returns how many were
    removed (or would be, under ``dry_run``). An agents folder that cannot
    be read removes nothing."""
    root = history_root()
    # Listed once: a folder that exists and cannot be listed would answer
    # every name as absent.
    try:
        folders = set(os.listdir(config.AGENTS_DIR))
    except OSError:
        return 0
    doomed: list[Path] = []
    for part in (ENTRIES, SET_ASIDE):
        if not (root / part).is_dir():
            continue
        for entry in (root / part).iterdir():
            if entry.name not in agents_with_a_row and entry.name not in folders:
                doomed.append(entry)
    cutoff = time.time() - SET_ASIDE_KEEP_DAYS * 86400
    if (root / SET_ASIDE).is_dir():
        for agent in (root / SET_ASIDE).iterdir():
            if agent in doomed or not agent.is_dir():
                continue
            with contextlib.suppress(OSError):
                doomed += [copy for copy in agent.iterdir() if copy.lstat().st_mtime < cutoff]
    if not dry_run:
        for path in doomed:
            shutil.rmtree(path, ignore_errors=True)
    return len(doomed)


def forget_archived(username: str) -> None:
    """Remove the histories of a person whose archive is deleted."""
    if _plain_name(username):
        shutil.rmtree(history_root() / OFFBOARDED / username, ignore_errors=True)
