"""A chat's engine session files, read, written and moved between places.

A place is the platform's own agent tree (``local``) or a paired machine.
The files are the chat's engine session: the transcript the engine resumes
(Claude's ``.claude/projects/<key>/<id>.jsonl`` with the ``<id>/`` folder
beside it; Codex's ``.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<thread>.jsonl``),
named by the engine's own enumeration (the layer's ``chat_session_files``
here, the retention walk on a machine) and kept at the SAME path relative
to the scope home (``users/<u>`` or ``workspace``) on every place: Claude
finds a transcript under any project key, and Codex's index names the
rollout by its path. Nothing is read or written through a link
(``services.infra.safe_fs`` here; ``session_move``'s own rule on a
machine), a file read must have one link, and a list a machine answers is
checked before a byte of it is pulled: a source picks the bytes, never the
place. ``copy_session`` moves the set and proves it landed;
``services/remote/chat_move.py`` decides when.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import config
from core import layout, placement

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("otodock.remote")

#: The staging folder at an agent's root (never synced, never mounted into a
#: sandbox, never walked by retention).
STAGING_DIR = ".session-move"
MAX_FILES = 2000
MAX_MOVE_BYTES = 2 * 1024 ** 3
MAX_SEGMENTS = 8

# The frame's ops (the satellite's ``sessions/session_move.OPS``).
OP_LIST, OP_PULL, OP_PUSH, OP_COMMIT, OP_DISCARD, OP_DELETE, OP_STAT = (
    "list", "pull", "push", "commit", "discard", "delete", "stat")
FRAME = "session_move"
#: The ``why`` of a place that answered busy: a live session there holds
#: the ids (``storage/move_schema.WHY_BUSY``).
WHY_BUSY = "busy"

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE,
)
_WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
)


class TransferError(RuntimeError):
    """The files could not move; ``why`` is the move's summary reason,
    ``place`` the place that refused, ``committed`` whether the target may
    already hold placed files."""

    def __init__(self, why: str, detail: str = "", *, place: str = "",
                 committed: bool = False) -> None:
        super().__init__(detail or why)
        self.why = why
        self.place = place
        self.committed = committed


@dataclass(frozen=True)
class SessionScope:
    """A chat's session on one agent: its engine, the scope home's user
    (``""`` for the workspace home) and the chat row's two ids."""

    agent: str
    engine: str
    home_user: str
    session_id: str
    resume_handle: str

    @property
    def refs(self) -> list[str]:
        """The UUID-shaped ids (an id joined into a glob or a frame is never
        anything else)."""
        return [r for r in dict.fromkeys((self.session_id, self.resume_handle))
                if r and _UUID_RE.match(r)]

    @property
    def sid(self) -> str:
        return self.session_id if self.session_id in self.refs else ""

    @property
    def handle(self) -> str:
        return self.resume_handle if self.resume_handle in self.refs else ""

    @property
    def home_rel(self) -> str:
        """The home, agents-relative."""
        return f"{self.agent}/{layout.scope_root(self.home_user)}"

    def frame(self, op: str, **extra) -> dict:
        return {"type": FRAME, "op": op, "engine": self.engine, "agent_slug": self.agent,
                "username": self.home_user, "refs": self.refs, **extra}


@dataclass(frozen=True)
class SessionFile:
    rel: str
    size: int
    sha256: str


@dataclass
class CopyResult:
    files: list[SessionFile] = field(default_factory=list)
    bytes: int = 0
    main_rel: str = ""
    main_size: int = 0


def _config_dir(engine: str) -> str:
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(engine)
    return caps.runtime.config_dir_name if caps is not None else ""


def check_rel(scope: SessionScope, rel: object) -> str:
    """A home-relative path a place answered, checked as a name only: one
    the move may write beneath a staging folder (the engine's own folder
    first, no traversal, no odd segment). ``TransferError`` otherwise."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel or ":" in rel:
        raise TransferError("transfer_failed", f"unsafe path {rel!r}")
    parts = rel.split("/")
    if len(parts) > MAX_SEGMENTS:
        raise TransferError("transfer_failed", f"path too deep {rel!r}")
    for seg in parts:
        if (not _SEGMENT_RE.match(seg) or set(seg) == {"."} or seg.endswith(".")
                or seg.split(".")[0].lower() in _WINDOWS_DEVICES):
            raise TransferError("transfer_failed", f"unsafe path {rel!r}")
    cdir = _config_dir(scope.engine)
    if not cdir or parts[0] != cdir:
        raise TransferError("transfer_failed", f"path outside the engine's folder {rel!r}")
    return rel


def is_main(scope: SessionScope, rel: str) -> bool:
    """The session file itself (named by one of the ids), as opposed to a
    file in the folder beside it."""
    name = rel.rsplit("/", 1)[-1]
    return any(name.endswith(f"{ref}.jsonl") for ref in scope.refs)


# -- the platform's own tree ------------------------------------------------

def _agents_root() -> str:
    return str(config.AGENTS_DIR)


def _sha256_fd(fd: int) -> tuple[str, int]:
    import hashlib
    h = hashlib.sha256()
    n = 0
    with os.fdopen(fd, "rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
            n += len(block)
    return "sha256:" + h.hexdigest(), n


def _candidates(scope: SessionScope, home: Path) -> list[str]:
    """Home-relative paths of the session's files under ``home`` (a real
    home or a staging folder standing for one): the engine's own
    enumeration, with the folder beside each file named after its stem
    walked without following a link. Only paths under ``home``."""
    import stat as _stat

    from core.session.session_manager import UnknownExecutionPath, get_layer_by_path
    from services.infra import safe_fs
    if not scope.refs:
        return []
    try:
        layer = get_layer_by_path(scope.engine)
    except UnknownExecutionPath:
        return []
    found = layer.chat_session_files(home, scope.sid, scope.handle)
    out: list[str] = []
    root = _agents_root()
    for f in found:
        try:
            rel = Path(f).relative_to(home).as_posix()
        except ValueError:
            continue  # outside the home (a legacy location): never moved
        out.append(rel)
        sib_rel = os.path.relpath(Path(f).with_suffix(""), root)
        try:
            st = safe_fs.lstat_beneath(root, sib_rel)
        except OSError:
            continue
        if not _stat.S_ISDIR(st.st_mode):
            continue  # none, or a link: never walked
        try:
            for step in safe_fs.walk_beneath(root, sib_rel):
                for name in step.files:
                    out.append(Path(os.path.join(root, step.path(name))).relative_to(home).as_posix())
        except OSError:
            continue
    return out


def local_home(scope: SessionScope) -> Path:
    return Path(_agents_root()) / scope.home_rel


def local_list(scope: SessionScope) -> list[SessionFile]:
    """The session's files on the platform, each opened beneath the agents
    root with no link followed and one link (a linked or doubly linked file
    is left out: it is never the engine's own file). Synchronous."""
    from services.infra import safe_fs
    home = local_home(scope)
    out: list[SessionFile] = []
    total = 0
    for rel in _candidates(scope, home):
        try:
            check_rel(scope, rel)
            fd, _st = safe_fs.open_regular_for_read(_agents_root(), f"{scope.home_rel}/{rel}",
                                                    single_link=True)
        except (OSError, TransferError) as e:
            logger.warning("session move: %s left out on the platform (%s)", rel, e)
            continue
        digest, size = _sha256_fd(fd)
        total += size
        out.append(SessionFile(rel, size, digest))
    if len(out) > MAX_FILES or total > MAX_MOVE_BYTES:
        raise TransferError("transfer_failed", "the session is over the move cap")
    return out


def _staging_rel(scope: SessionScope, move_id: str) -> str:
    uuid.UUID(move_id)
    return f"{scope.agent}/{STAGING_DIR}/{move_id}"


def local_stage_from_home(scope: SessionScope, move_id: str, rel: str) -> Path:
    """Copy one of the platform's session files into the move's staging (the
    bytes a push then reads: never the live name). Returns the staged path."""
    from services.infra import safe_fs
    check_rel(scope, rel)
    dst = f"{_staging_rel(scope, move_id)}/{rel}"
    safe_fs.copy_file_beneath(_agents_root(), f"{scope.home_rel}/{rel}", _agents_root(), dst,
                              single_link=True, mkdirs=True, fsync=False)
    return Path(_agents_root()) / dst


def staged_path(scope: SessionScope, move_id: str, rel: str) -> Path:
    """Where a pulled file lands in the platform's staging (the name checked)."""
    check_rel(scope, rel)
    return Path(_agents_root()) / _staging_rel(scope, move_id) / rel


def local_commit(scope: SessionScope, move_id: str) -> list[SessionFile]:
    """Put the staged files in place on the platform: the staging classified
    by the engine's own enumeration (anything it does not name for the
    chat's ids is dropped), every OTHER copy of the session under the home
    removed with its ``<id>/`` folder (one copy per home), then each file
    written beside its name and renamed there (never across folders:
    project quotas refuse that), never through a link. Synchronous."""
    from services.infra import safe_fs
    root = _agents_root()
    staging_rel = _staging_rel(scope, move_id)
    staged = [rel for rel in _candidates(scope, Path(root) / staging_rel)]
    if not any(is_main(scope, r) for r in staged):
        raise TransferError("transfer_failed", "nothing staged for this session")
    targets = set(staged)
    home = local_home(scope)
    stale = [rel for rel in _candidates(scope, home) if rel not in targets]
    for rel in stale:
        try:
            safe_fs.unlink_beneath(root, f"{scope.home_rel}/{rel}", missing_ok=True)
        except OSError as e:
            logger.warning("session move: could not remove the old copy %s: %s", rel, e)
    for rel in stale:
        _drop_sibling(scope, rel, keep=targets)
    placed: list[SessionFile] = []
    for rel in staged:
        dst = f"{scope.home_rel}/{rel}"
        safe_fs.copy_file_beneath(root, f"{staging_rel}/{rel}", root, dst, single_link=True,
                                  mkdirs=True, fsync=True)
        fd, _st = safe_fs.open_regular_for_read(root, dst, single_link=True)
        digest, size = _sha256_fd(fd)
        placed.append(SessionFile(rel, size, digest))
    local_discard(scope, move_id)
    return placed


def local_discard(scope: SessionScope, move_id: str) -> None:
    """Drop the platform's staging of the move."""
    from services.infra import safe_fs
    try:
        safe_fs.rmtree_beneath(_agents_root(), _staging_rel(scope, move_id), missing_ok=True)
    except OSError as e:
        logger.warning("session move %s: staging not removed: %s", move_id[:8], e)


def _drop_sibling(scope: SessionScope, rel: str, *,
                  keep: set[str] | frozenset[str] = frozenset()) -> None:
    """The ``<id>/`` folder beside a removed session file (its files already
    gone), unless a kept file lives in it; never through a link."""
    from services.infra import safe_fs
    if not is_main(scope, rel):
        return
    sib = rel[:-len(".jsonl")]
    if any(k.startswith(sib + "/") for k in keep):
        return
    try:
        safe_fs.rmtree_beneath(_agents_root(), f"{scope.home_rel}/{sib}", missing_ok=True)
    except OSError as e:
        logger.info("session move: the folder beside %s stays: %s", rel, e)


def local_delete(scope: SessionScope) -> int:
    """Remove the session's files from the platform's home, with the
    ``<id>/`` folder beside each. Synchronous."""
    from services.infra import safe_fs
    root = _agents_root()
    gone = 0
    rels = _candidates(scope, local_home(scope))
    for rel in rels:
        try:
            safe_fs.unlink_beneath(root, f"{scope.home_rel}/{rel}", missing_ok=True)
            gone += 1
        except OSError as e:
            logger.warning("session move: %s not removed: %s", rel, e)
    for rel in rels:
        _drop_sibling(scope, rel)
    return gone


def local_stat(scope: SessionScope) -> list[tuple[str, int]]:
    """``(rel, size)`` of the session file itself on the platform, from an
    lstat beneath the agents root (no hashing: the move's proof asks every
    minute). Synchronous."""
    import stat as _stat

    from services.infra import safe_fs
    out: list[tuple[str, int]] = []
    for rel in _candidates(scope, local_home(scope)):
        if not is_main(scope, rel):
            continue
        try:
            st = safe_fs.lstat_beneath(_agents_root(), f"{scope.home_rel}/{rel}")
        except OSError:
            continue
        if _stat.S_ISREG(st.st_mode):
            out.append((rel, st.st_size))
    return out


def _staging_moves(root: str, st_rel: str) -> list[str]:
    """The move folders in one agent's staging (none when it is a link)."""
    import stat as _stat

    from services.infra import safe_fs
    if not _stat.S_ISDIR(safe_fs.lstat_beneath(root, st_rel).st_mode):
        return []
    walk = safe_fs.walk_beneath(root, st_rel)
    try:
        return list(next(walk).dirs)
    finally:
        walk.close()


def sweep_staging(max_age_s: float = 86400.0, *, now: float | None = None) -> int:
    """Drop the platform's staging folders older than ``max_age_s`` (a move
    the proxy never finished: a restart), never through a link.
    Synchronous."""
    import time

    from services.infra import safe_fs
    now = time.time() if now is None else now
    root = _agents_root()
    if not os.path.isdir(root):
        return 0
    gone = 0
    for agent in os.listdir(root):
        st_rel = f"{agent}/{STAGING_DIR}"
        try:
            names = _staging_moves(root, st_rel)
        except (OSError, StopIteration):
            continue
        for name in names:
            rel = f"{st_rel}/{name}"
            try:
                if now - safe_fs.lstat_beneath(root, rel).st_mtime < max_age_s:
                    continue
                safe_fs.rmtree_beneath(root, rel, missing_ok=True)
                gone += 1
            except OSError:
                continue
    return gone


# -- a paired machine ------------------------------------------------------

def _cm():
    from core.remote.satellite_connection import get_connection_manager
    return get_connection_manager()


async def _command(machine_id: str, frame: dict, *, timeout: float) -> dict:
    ack = await _cm().send_command(machine_id, frame, timeout=timeout)
    if ack.get("status") != "ok":
        error = str(ack.get("error") or ack.get("status"))
        # The satellite's refusal while a live session holds the ids.
        why = WHY_BUSY if error.startswith("busy") else "transfer_failed"
        raise TransferError(why, error, place=machine_id)
    return ack


async def remote_list(machine_id: str, scope: SessionScope) -> list[SessionFile]:
    ack = await _command(machine_id, scope.frame(OP_LIST), timeout=120.0)
    out = []
    for f in ack.get("files") or []:
        if not isinstance(f, dict):
            raise TransferError("transfer_failed", "a malformed list")
        try:
            size = int(f.get("size") or 0)
        except (TypeError, ValueError) as e:
            raise TransferError("transfer_failed", f"a malformed list: {e}") from e
        out.append(SessionFile(check_rel(scope, f.get("rel")), size, str(f.get("sha256") or "")))
    if len(out) > MAX_FILES or sum(f.size for f in out) > MAX_MOVE_BYTES:
        raise TransferError("transfer_failed", "the session is over the move cap")
    return out


async def remote_pull(machine_id: str, scope: SessionScope, move_id: str, f: SessionFile, *,
                      progress: Callable[[int, int], object] | None = None) -> Path:
    """Pull one file into the platform's staging; returns its staged path.
    ``progress(bytes_received, bytes_expected)`` after each chunk."""
    from services.path_policy_v2 import PathRef
    dest = staged_path(scope, move_id, f.rel)
    ok = await _cm().pull_file_to_path(
        machine_id, PathRef(kind="agent_tree", value=f.rel), dest, agent_slug=scope.agent,
        max_bytes=f.size, request_fields=scope.frame(OP_PULL, rel=f.rel), progress_cb=progress,
    )
    if not ok:
        raise TransferError("transfer_failed", f"pull of {f.rel} failed")
    return dest


async def remote_push(machine_id: str, scope: SessionScope, move_id: str, rel: str,
                      src: Path, digest: str, *,
                      progress: Callable[[int, int], object] | None = None) -> None:
    """Push one staged file; ``progress(bytes_sent, bytes_total)`` after
    each acked chunk."""
    from services.path_policy_v2 import PathRef
    ok = await _cm().push_file(
        machine_id, PathRef(kind="agent_tree", value=rel), src, agent_slug=scope.agent,
        content_hash=digest, frame_fields=scope.frame(OP_PUSH, rel=rel, move_id=move_id),
        progress_cb=progress,
    )
    if not ok:
        raise TransferError("transfer_failed", f"push of {rel} failed")


async def remote_commit(machine_id: str, scope: SessionScope, move_id: str) -> list[SessionFile]:
    ack = await _command(machine_id, scope.frame(OP_COMMIT, move_id=move_id), timeout=300.0)
    return [SessionFile(str(f.get("rel") or ""), int(f.get("size") or 0), str(f.get("sha256") or ""))
            for f in ack.get("files") or [] if isinstance(f, dict)]


async def remote_discard(machine_id: str, scope: SessionScope, move_id: str) -> None:
    try:
        await _command(machine_id, scope.frame(OP_DISCARD, move_id=move_id), timeout=60.0)
    except (TransferError, RuntimeError) as e:
        logger.info("session move %s: discard on %s: %s", move_id[:8], machine_id[:8], e)


async def remote_delete(machine_id: str, scope: SessionScope) -> int:
    ack = await _command(machine_id, scope.frame(OP_DELETE), timeout=120.0)
    return int(ack.get("deleted") or 0)


async def remote_stat(machine_id: str, scope: SessionScope) -> list[tuple[str, int]]:
    """``(rel, size)`` of the session file itself on a machine (sizes only,
    never answered busy: a live session appending to the file is what the
    proof waits for)."""
    ack = await _command(machine_id, scope.frame(OP_STAT), timeout=60.0)
    out: list[tuple[str, int]] = []
    for f in ack.get("files") or []:
        if not isinstance(f, dict):
            raise TransferError("transfer_failed", "a malformed answer")
        try:
            out.append((check_rel(scope, f.get("rel")), int(f.get("size") or 0)))
        except (TypeError, ValueError) as e:
            raise TransferError("transfer_failed", f"a malformed answer: {e}") from e
    return out


# -- any place ---------------------------------------------------------------

async def list_files(place: str, scope: SessionScope) -> list[SessionFile]:
    import asyncio
    if placement.is_local(place):
        return await asyncio.to_thread(local_list, scope)
    return await remote_list(place, scope)


async def delete_files(place: str, scope: SessionScope) -> int:
    import asyncio
    if placement.is_local(place):
        return await asyncio.to_thread(local_delete, scope)
    return await remote_delete(place, scope)


async def discard(place: str, scope: SessionScope, move_id: str) -> None:
    import asyncio
    if placement.is_local(place):
        await asyncio.to_thread(local_discard, scope, move_id)
    else:
        await remote_discard(place, scope, move_id)


def _same_set(a: list[SessionFile], b: list[SessionFile]) -> bool:
    return sorted((f.size, f.sha256) for f in a) == sorted((f.size, f.sha256) for f in b)


async def copy_session(scope: SessionScope, *, source: str, target: str, move_id: str,
                       progress: Callable[[int, int, str], object] | None = None,
                       ) -> CopyResult:
    """Copy the chat's session files from ``source`` to ``target`` (each
    ``local`` or a machine id) and put them in place there. The source is
    listed again before the commit (a writer that slipped in fails the
    copy) and the placed set is compared with the one read. Raises
    ``TransferError``; the caller discards both stagings on a failure.

    ``progress(done, total, phase)`` follows the whole move: ``total`` is
    the listed bytes once per leg on the wire (twice from a machine to a
    machine: pulled, then pushed), ``phase`` a ``wire.MOVE_PHASES`` word.
    Called often (each chunk, in the receive loop for a pull): the caller
    throttles, and must be quick."""
    import asyncio

    from ws import wire_events as wire
    files = await list_files(source, scope)
    if not any(is_main(scope, f.rel) for f in files):
        raise TransferError("no_files", "no session file at the source")
    legs = (0 if placement.is_local(source) else 1) + (0 if placement.is_local(target) else 1)
    total = sum(f.size for f in files) * legs
    done = 0

    def _tell(n: int, phase: str) -> None:
        if progress is not None:
            progress(min(n, total), total, phase)

    def _from(base: int, phase: str):
        return lambda moved, _expected: _tell(base + moved, phase)

    staged: dict[str, Path] = {}
    for f in files:
        if placement.is_local(source):
            _tell(done, wire.MOVE_PHASE_STAGE)
            staged[f.rel] = await asyncio.to_thread(local_stage_from_home, scope, move_id, f.rel)
        else:
            staged[f.rel] = await remote_pull(source, scope, move_id, f,
                                              progress=_from(done, wire.MOVE_PHASE_COPY))
            done += f.size
    if not placement.is_local(target):
        for f in files:
            await remote_push(target, scope, move_id, f.rel, staged[f.rel], f.sha256,
                              progress=_from(done, wire.MOVE_PHASE_COPY))
            done += f.size
    again = await list_files(source, scope)
    if not _same_set(files, again):
        raise TransferError("transfer_failed", "the session changed at the source during the move")
    _tell(total, wire.MOVE_PHASE_COMMIT)
    try:
        if placement.is_local(target):
            placed = await asyncio.to_thread(local_commit, scope, move_id)
        else:
            placed = await remote_commit(target, scope, move_id)
    except TransferError as e:
        # A busy refusal comes before the satellite renames anything.
        e.committed = e.why != WHY_BUSY
        raise
    except Exception as e:  # a commit cut short may have placed some files
        raise TransferError("transfer_failed", f"the commit failed: {e}", committed=True) from e
    if not placement.is_local(target):
        await asyncio.to_thread(local_discard, scope, move_id)
    if not _same_set(files, placed):
        raise TransferError("transfer_failed", "the placed files differ from the source",
                            committed=True)
    main = next((f for f in placed if is_main(scope, f.rel)), None)
    return CopyResult(files=placed, bytes=sum(f.size for f in placed),
                      main_rel=main.rel if main else "", main_size=main.size if main else 0)


async def main_size(place: str, scope: SessionScope, main_rel: str) -> int | None:
    """The size of the session file at a place now (``None`` when it is not
    there), from sizes only. The move's proof: the engine appended to the
    file it placed (found under any project key: the engine resumes it
    wherever it lies)."""
    import asyncio
    if placement.is_local(place):
        files = await asyncio.to_thread(local_stat, scope)
    else:
        files = await remote_stat(place, scope)
    name = main_rel.rsplit("/", 1)[-1]
    for rel, size in files:
        if rel.rsplit("/", 1)[-1] == name:
            return size
    return None
