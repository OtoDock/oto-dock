"""Moving a chat's engine session files on and off this machine (0.5.156).

The platform moves a chat to another place with its memory: the engine's
transcript (Claude's ``.claude/projects/<p>/<id>.jsonl`` with the ``<id>/``
folder beside it, Codex's ``.codex/sessions/**/rollout-*-<thread>.jsonl``)
travels with it. The ``session_move`` frame is the ONE way platform bytes
reach an engine home on this machine (``file_push`` refuses every such
path), and it is scoped to a named session of a named chat: the platform
names the engine, the agent, the scope's user and the session's ids; this
module resolves the home itself (``layout.scope_root``) and accepts a file
only when the engines' own layout (``host.session_retention.classify``)
names it a file of those ids. Nothing on the way is a link
(``host.safe_fs``), a file read must be a regular file with one link, and
pushed bytes are staged beneath ``<agent>/.session-move/<move id>/`` (a dot
folder at the agent root: never synced, never walked by retention) until
the commit puts them in place.

The ops (``OPS``): ``list`` the session's files with sizes and hashes,
``stat`` the sizes of its transcripts alone, ``pull`` one of them
(``file_content`` chunks), ``push`` one in chunks into the staging,
``commit`` the staging, ``discard`` it, ``delete`` the session's files.
Every op touches the engine's own folder only. The session manager answers
``busy`` for ``list``, ``pull``, ``commit`` and ``delete`` while a live
session holds one of the ids; ``stat`` and ``discard`` are answered anyway.
A staging folder a day old is swept at the satellite's start
(``sweep_all_staging``) and at a move's first push into the agent.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from .._vendored import layout
from ..config import force_rmtree
from ..engines import ENGINES
from ..host import safe_fs, session_retention

OP_LIST = "list"
OP_STAT = "stat"
OP_PULL = "pull"
OP_PUSH = "push"
OP_COMMIT = "commit"
OP_DISCARD = "discard"
OP_DELETE = "delete"
OPS = frozenset({OP_LIST, OP_STAT, OP_PULL, OP_PUSH, OP_COMMIT, OP_DISCARD, OP_DELETE})

#: The staging folder at an agent's root.
STAGING_DIR = ".session-move"
MAX_FILES = 2000
MAX_MOVE_BYTES = 2 * 1024 ** 3
MAX_SEGMENTS = 8
#: A staging folder older than this is left from a move that never ended.
STAGING_MAX_AGE_S = 86400.0
#: A Claude transcript whose full path would pass this goes under a short key
#: (Windows paths stop at 260 characters; any key resumes).
LONG_PATH = 240
SHORT_KEY = "-m"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE,
)
_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
_WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
)


class MoveRefused(ValueError):
    """A frame this module will not act on; the message is the ack's error."""


def is_session_id(value: object) -> bool:
    """A session or thread id: UUID-shaped."""
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def is_safe_username(value: object) -> bool:
    """Empty (the workspace home) or a name safe to join as one folder."""
    return value == "" or (isinstance(value, str) and bool(_USER_RE.match(value)))


@dataclass(frozen=True)
class Scope:
    """What a frame names, checked: the engine's own folder, the agent, the
    home beneath it, the session's ids and the move."""

    config_dir: str
    agent_slug: str
    home: str              # agent-relative: "workspace" or "users/<u>"
    refs: frozenset[str]
    move_id: str

    @property
    def home_rel(self) -> str:
        """The home, agents-relative."""
        return f"{self.agent_slug}/{self.home}"

    @property
    def staging_rel(self) -> str:
        return f"{self.agent_slug}/{STAGING_DIR}/{self.move_id}"


def parse_scope(msg: dict, *, need_move: bool = False) -> Scope:
    """The checked scope of a ``session_move`` frame, or ``MoveRefused``."""
    eng = ENGINES.get(str(msg.get("engine") or ""))
    if eng is None:
        raise MoveRefused("unknown engine")
    slug = str(msg.get("agent_slug") or "")
    if not _SLUG_RE.match(slug):
        raise MoveRefused("unsafe agent_slug")
    username = msg.get("username") or ""
    if not is_safe_username(username):
        raise MoveRefused("unsafe username")
    raw = msg.get("refs")
    refs = frozenset(r for r in raw if r) if isinstance(raw, list) else frozenset()
    if not refs or len(refs) > 2 or not all(is_session_id(r) for r in refs):
        raise MoveRefused("refs must be one or two session ids")
    move_id = msg.get("move_id") or ""
    if (need_move or move_id) and not is_session_id(move_id):
        raise MoveRefused("unsafe move_id")
    return Scope(config_dir=eng.config_dir, agent_slug=slug, home=layout.scope_root(username),
                 refs=refs, move_id=move_id)


def check_rel(scope: Scope, rel: object) -> tuple[list[str], str]:
    """The segments of a home-relative ``rel`` the frame names, with its
    ``classify`` kind; ``MoveRefused`` unless it is a file of the scope's
    session in the engine's own folder."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel or ":" in rel:
        raise MoveRefused("unsafe rel")
    parts = rel.split("/")
    if len(parts) > MAX_SEGMENTS:
        raise MoveRefused("rel too deep")
    for seg in parts:
        if (not _SEGMENT_RE.match(seg) or set(seg) == {"."} or seg.endswith(".")
                or seg.split(".")[0].lower() in _WINDOWS_DEVICES):
            raise MoveRefused("unsafe rel")
    if parts[0] != scope.config_dir:
        raise MoveRefused("rel outside the engine's folder")
    hit = session_retention.classify(parts, set(scope.refs))
    if hit is None:
        raise MoveRefused("rel is not a file of this session")
    return parts, hit[1]


def _sha256_fd(fd: int) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with os.fdopen(fd, "rb") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            h.update(block)
            n += len(block)
    return "sha256:" + h.hexdigest(), n


def _home_path(agents_dir: Path, scope: Scope) -> Path:
    return agents_dir.joinpath(*scope.home_rel.split("/"))


def _own_folder(scope: Scope, home: Path, path: Path) -> bool:
    """Whether ``path`` under ``home`` sits in the scope engine's own folder
    (another engine's file of the same id is never touched)."""
    return path.relative_to(home).parts[0] == scope.config_dir


def session_files(agents_dir: Path, scope: Scope) -> list[tuple[str, Path]]:
    """``(rel, path)`` of every file of the scope's session under its home:
    the session files the engines' walk finds for the ids, and every regular
    file in a sibling folder, none reached through a link."""
    home = _home_path(agents_dir, scope)
    out: list[tuple[str, Path]] = []
    for f, ref, sibling in session_retention.iter_home_session_files(home):
        if ref not in scope.refs:
            continue
        out.append((f.relative_to(home).as_posix(), f))
        if sibling is not None:
            for d in session_retention._walk_dirs(sibling):
                for g in session_retention._files(d, suffix=""):
                    out.append((g.relative_to(home).as_posix(), g))
    return [(rel, p) for rel, p in out if rel.split("/")[0] == scope.config_dir]


def list_files(agents_dir: Path, scope: Scope) -> list[dict]:
    """The ``list`` answer: ``[{rel, size, sha256}]``, each file opened
    beneath the agents root with no link followed and one hard link."""
    files = session_files(agents_dir, scope)
    if len(files) > MAX_FILES:
        raise MoveRefused("too many files")
    out: list[dict] = []
    total = 0
    for rel, _path in files:
        fd, st = safe_fs.open_regular_for_read(agents_dir, f"{scope.home_rel}/{rel}")
        if st.st_nlink != 1:
            os.close(fd)
            raise MoveRefused(f"{rel} has more than one link")
        digest, size = _sha256_fd(fd)
        total += size
        if total > MAX_MOVE_BYTES:
            raise MoveRefused("the session is over the move cap")
        out.append({"rel": rel, "size": size, "sha256": digest})
    return out


def stat_files(agents_dir: Path, scope: Scope) -> list[dict]:
    """The ``stat`` answer: ``[{rel, size}]`` of the session files themselves
    (``classify``'s ``MAIN``) of the scope's ids in the engine's own folder,
    each size from an lstat beneath the agents root that follows no link; a
    link or anything but a regular file is left out. No hash: a live
    session may be appending to the file."""
    home = _home_path(agents_dir, scope)
    out: list[dict] = []
    for f, ref, _sibling in session_retention.iter_home_session_files(home):
        if ref not in scope.refs or not _own_folder(scope, home, f):
            continue
        rel = f.relative_to(home).as_posix()
        hit = session_retention.classify(rel.split("/"), set(scope.refs))
        if hit is None or hit[1] != session_retention.MAIN:
            continue
        try:
            st = safe_fs.lstat_beneath(agents_dir, f"{scope.home_rel}/{rel}")
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(st.st_mode):
            out.append({"rel": rel, "size": st.st_size})
    return out


def pull_rel(scope: Scope, rel: object) -> str:
    """The AGENT-relative path a ``pull`` of ``rel`` reads (checked): the
    ``file_pull`` reader's own path shape."""
    parts, _kind = check_rel(scope, rel)
    return f"{scope.home}/{'/'.join(parts)}"


@dataclass
class _Upload:
    hasher: object = field(default_factory=hashlib.sha256)
    next_index: int = 0
    size: int = 0


# The pushes in flight, by (move id, rel): the next chunk each expects and the
# running hash. Frames are written in arrival order (before any await), so a
# chunk out of order is a broken stream, refused.
_uploads: dict[tuple[str, str], _Upload] = {}
_move_bytes: dict[str, int] = {}


def write_chunk(agents_dir: Path, scope: Scope, rel: object, data: bytes, *,
                index: int, total: int, digest: str, max_file: int) -> None:
    """Stage one chunk of a pushed file (synchronously: the caller writes
    before any await). The first chunk unlinks whatever stands at the
    ``.partial`` name; every open is refused unless the staged file has one
    link, and its length is cut only after that check, so the bytes never
    reach a file hard-linked from elsewhere. The last chunk checks the whole
    file's hash and puts the staged file under its name in the staging
    folder."""
    parts, _kind = check_rel(scope, rel)
    key_rel = "/".join(parts)
    if total < 1 or not 0 <= index < total:
        raise MoveRefused("bad chunk index")
    key = (scope.move_id, key_rel)
    if index == 0:
        if scope.move_id not in _move_bytes:
            sweep_staging(agents_dir, scope.agent_slug)
            _move_bytes[scope.move_id] = 0
        _uploads[key] = _Upload()
    up = _uploads.get(key)
    if up is None or up.next_index != index:
        _uploads.pop(key, None)
        raise MoveRefused("chunk out of order")
    up.size += len(data)
    _move_bytes[scope.move_id] = _move_bytes.get(scope.move_id, 0) + len(data)
    if up.size > max_file or _move_bytes[scope.move_id] > MAX_MOVE_BYTES:
        _uploads.pop(key, None)
        raise MoveRefused("over the size cap")
    partial = f"{scope.staging_rel}/{key_rel}.partial"
    if index == 0:
        safe_fs.unlink_beneath(agents_dir, partial, missing_ok=True)
    fd = safe_fs.open_append_beneath(agents_dir, partial, mkdirs=True)
    if os.fstat(fd).st_nlink != 1:
        os.close(fd)
        _uploads.pop(key, None)
        raise MoveRefused("the staged file has more than one link")
    with os.fdopen(fd, "wb") as fh:
        if index == 0:
            os.ftruncate(fh.fileno(), 0)
        fh.write(data)
    up.hasher.update(data)  # type: ignore[attr-defined]
    up.next_index += 1
    if index < total - 1:
        return
    _uploads.pop(key, None)
    got = "sha256:" + up.hasher.hexdigest()  # type: ignore[attr-defined]
    if digest and got != digest:
        with contextlib.suppress(OSError, ValueError):
            safe_fs.unlink_beneath(agents_dir, partial, missing_ok=True)
        raise MoveRefused("hash mismatch")
    safe_fs.rename_beneath(agents_dir, partial, f"{scope.staging_rel}/{key_rel}", replace=True)


def _staged(agents_dir: Path, scope: Scope) -> list[tuple[list[str], str]]:
    """The staged files the engines' layout names for the scope's ids, as
    ``(parts, kind)``; anything else in the staging is ignored."""
    root = agents_dir.joinpath(*scope.staging_rel.split("/"))
    if not session_retention._real_dir(root):
        return []
    out: list[tuple[list[str], str]] = []
    for d in session_retention._walk_dirs(root):
        for f in session_retention._files(d, suffix=""):
            rel = f.relative_to(root).as_posix()
            if rel.endswith(".partial"):
                continue
            try:
                out.append(check_rel(scope, rel))
            except MoveRefused:
                continue
    return out


def _placed(parts: list[str], home: Path) -> list[str]:
    """Where a staged file goes under the home: its own path, or the short
    key for a Claude transcript whose full path would be too long."""
    idx = session_retention.key_index(parts)
    if idx is not None and len(str(home.joinpath(*parts))) > LONG_PATH:
        return [*parts[:idx], SHORT_KEY, *parts[idx + 1:]]
    return parts


def commit(agents_dir: Path, scope: Scope) -> list[dict]:
    """Put the staged files of the move in place: every OTHER copy of the
    session in the engine's own folder under the home goes first (one copy
    per home: a second one makes Claude's resume by id fail; another
    engine's file of the same id stays), then each staged file is renamed
    beneath the agents root, never through a link. Returns the placed files
    with their sizes and hashes."""
    staged = _staged(agents_dir, scope)
    if not any(kind == session_retention.MAIN for _parts, kind in staged):
        raise MoveRefused("nothing staged for this session")
    home = _home_path(agents_dir, scope)
    targets = {"/".join(_placed(parts, home)) for parts, _kind in staged}
    stale_files: list[str] = []
    stale_dirs: list[Path] = []
    for f, ref, sibling in list(session_retention.iter_home_session_files(home)):
        if ref not in scope.refs or not _own_folder(scope, home, f):
            continue
        rel = f.relative_to(home).as_posix()
        if rel not in targets:
            stale_files.append(rel)
        if sibling is None:
            continue
        srel = sibling.relative_to(home).as_posix()
        if not any(t.startswith(srel + "/") for t in targets):
            stale_dirs.append(sibling)
            continue
        for d in session_retention._walk_dirs(sibling):
            for g in session_retention._files(d, suffix=""):
                grel = g.relative_to(home).as_posix()
                if grel not in targets:
                    stale_files.append(grel)
    for rel in stale_files:
        with contextlib.suppress(OSError, ValueError):
            safe_fs.unlink_beneath(agents_dir, f"{scope.home_rel}/{rel}", missing_ok=True)
    for d in stale_dirs:
        with contextlib.suppress(OSError):
            force_rmtree(d)
    placed: list[dict] = []
    for parts, _kind in staged:
        src = f"{scope.staging_rel}/{'/'.join(parts)}"
        dst_parts = _placed(parts, home)
        dst = f"{scope.home_rel}/{'/'.join(dst_parts)}"
        safe_fs.rename_beneath(agents_dir, src, dst, replace=True, mkdirs=True)
        fd, _st = safe_fs.open_regular_for_read(agents_dir, dst)
        digest, size = _sha256_fd(fd)
        placed.append({"rel": "/".join(dst_parts), "size": size, "sha256": digest})
    discard(agents_dir, scope)
    return placed


def discard(agents_dir: Path, scope: Scope) -> None:
    """Drop the move's staging folder and its pushes in flight."""
    for key in [k for k in _uploads if k[0] == scope.move_id]:
        _uploads.pop(key, None)
    _move_bytes.pop(scope.move_id, None)
    root = agents_dir.joinpath(*scope.staging_rel.split("/"))
    if session_retention._real_dir(root):
        with contextlib.suppress(OSError):
            force_rmtree(root)
    parent = root.parent
    with contextlib.suppress(OSError, ValueError):
        if session_retention._real_dir(parent) and not any(parent.iterdir()):
            safe_fs.rmdir_beneath(agents_dir, f"{scope.agent_slug}/{STAGING_DIR}")


def delete(agents_dir: Path, scope: Scope) -> int:
    """Remove the session's files from the engine's own folder under its
    home (the sibling folder walked without links; another engine's file of
    the same id stays). Returns how many files went."""
    home = _home_path(agents_dir, scope)
    gone = 0
    for f, ref, sibling in list(session_retention.iter_home_session_files(home)):
        if ref not in scope.refs or not _own_folder(scope, home, f):
            continue
        with contextlib.suppress(OSError, ValueError):
            safe_fs.unlink_beneath(agents_dir, f"{scope.home_rel}/{f.relative_to(home).as_posix()}")
            gone += 1
        if sibling is not None and not safe_fs.is_link_like(sibling):
            for d in session_retention._walk_dirs(sibling):
                gone += len(session_retention._files(d, suffix=""))
            with contextlib.suppress(OSError):
                force_rmtree(sibling)
    return gone


def sweep_staging(agents_dir: Path, agent_slug: str, *, now: float | None = None) -> int:
    """Drop an agent's staging folders older than ``STAGING_MAX_AGE_S``
    (a move that never committed nor discarded). Returns how many went."""
    root = agents_dir / agent_slug / STAGING_DIR
    if not session_retention._real_dir(root):
        return 0
    now = time.time() if now is None else now
    n = 0
    for d in session_retention._subdirs(root):
        with contextlib.suppress(OSError):
            if now - d.lstat().st_mtime >= STAGING_MAX_AGE_S:
                force_rmtree(d)
                n += 1
    return n


def sweep_all_staging(agents_dir: Path, *, now: float | None = None) -> int:
    """``sweep_staging`` for every agent folder: real folders only (no link
    followed), a name that is not a safe slug skipped. Run once at the
    satellite's start, off the event loop. Returns how many went."""
    now = time.time() if now is None else now
    n = 0
    for d in session_retention._subdirs(agents_dir):
        if _SLUG_RE.match(d.name):
            n += sweep_staging(agents_dir, d.name, now=now)
    return n
