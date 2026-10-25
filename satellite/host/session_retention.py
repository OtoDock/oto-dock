"""The platform's session-file retention on this machine (0.5.150).

The retention sweep (the proxy's ``services/infra/retention.py``) ages the
chats pinned to this machine out as it does local ones, then asks the
daemon to remove the CLI transcripts no chat and no live session names:
Claude's ``.claude/projects/<p>/<id>.jsonl`` (with the ``<id>/`` folder
beside it) and Codex's ``.codex/sessions/**/rollout-*-<thread>.jsonl``,
under every agent's ``workspace`` and ``users/<u>`` homes. The platform
sends the ids to keep; the daemon adds its own live sessions and keeps
anything younger than the grace by its own clock. A walk, never a sync:
the files are this machine's, and nothing on the way is a link (a folder
that stands for another place is never entered, a file never deleted
through one: ``host/safe_fs``'s rule).
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from .._vendored import layout
from ..config import force_rmtree
from . import safe_fs

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE,
)
# A file touched within the hour is in flight, whatever the platform asks.
MIN_AGE_S = 3600.0
# The platform's orphan grace, for a frame that names none.
DEFAULT_MIN_AGE_S = 7 * 86400.0
# The answer lists at most this many paths that go (or would go).
CANDIDATES_MAX = 50


def _subdirs(path: Path) -> list[Path]:
    """The real subfolders of ``path`` (no link, junction or mount point
    among them), sorted; nothing when it cannot be listed."""
    out: list[Path] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False) and not safe_fs.is_link_like(entry.path):
                    out.append(Path(entry.path))
    except OSError:
        return []
    return sorted(out)


def _files(path: Path, suffix: str = ".jsonl") -> list[Path]:
    """The regular files of ``path`` ending in ``suffix`` (every one for
    ``""``), no link among them, sorted."""
    out: list[Path] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name.endswith(suffix) and entry.is_file(follow_symlinks=False):
                    out.append(Path(entry.path))
    except OSError:
        return []
    return sorted(out)


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not safe_fs.is_link_like(path)


def _homes(agents_dir: Path) -> Iterator[Path]:
    for agent_dir in _subdirs(agents_dir):
        ws = agent_dir / layout.WORKSPACE
        if _real_dir(ws):
            yield ws
        users = agent_dir / layout.USERS
        if _real_dir(users):
            yield from _subdirs(users)


def _walk_dirs(root: Path) -> Iterator[Path]:
    """``root`` and every real folder under it, never through a link."""
    yield root
    for d in _subdirs(root):
        yield from _walk_dirs(d)


class _Layout(NamedTuple):
    """Where one engine keeps its session files under a scope home: its own
    folder, the area beneath it, how many key folders sit between the area
    and a file (``None``: any depth), the file name's prefix, how much of the
    stem is the id (``None``: all of it) and whether the folder beside a
    file, named after its stem, belongs to it."""

    home_dir: str
    area: str
    depth: int | None
    prefix: str
    ref_tail: int | None
    sibling: bool


# Claude's ``.claude/projects/<p>/<id>.jsonl`` (with the ``<id>/`` folder
# beside it) and Codex's ``.codex/sessions/**/rollout-*-<thread>.jsonl``.
LAYOUTS: tuple[_Layout, ...] = (
    _Layout(".claude", "projects", 1, "", None, True),
    _Layout(".codex", "sessions", None, "rollout-", 36, False),
)
#: ``classify``'s kinds: the session file itself, a file in its sibling folder.
MAIN = "main"
SIBLING = "sibling"


def _ref_of(layout: _Layout, name: str) -> str:
    stem = name[: -len(".jsonl")]
    return stem[-layout.ref_tail:] if layout.ref_tail else stem


def iter_home_session_files(home: Path) -> Iterator[tuple[Path, str, Path | None]]:
    """``(file, id, sibling)`` for every transcript under ONE scope home
    (``workspace`` or ``users/<u>``): the id a chat row names (Claude's file
    stem, the last 36 characters of Codex's), and the folder beside the file
    when its layout has one and it is a real one. Every folder on the way is
    a real folder (``_subdirs``)."""
    for lay in LAYOUTS:
        area = home / lay.home_dir / lay.area
        if not (_real_dir(home / lay.home_dir) and _real_dir(area)):
            continue
        dirs = _subdirs(area) if lay.depth == 1 else _walk_dirs(area)
        for d in dirs:
            for f in _files(d):
                if not f.name.startswith(lay.prefix):
                    continue
                sibling = f.with_suffix("") if lay.sibling else None
                yield f, _ref_of(lay, f.name), (sibling if sibling and _real_dir(sibling) else None)


def classify(parts: list[str], refs: set[str]) -> tuple[str, str] | None:
    """``(id, kind)`` when the home-relative path ``parts`` names a session
    file of one of ``refs`` (``MAIN``) or a file inside such a file's sibling
    folder (``SIBLING``) in one of the engines' layouts; ``None`` otherwise.
    Text only: the caller has checked every segment's shape."""
    for lay in LAYOUTS:
        if len(parts) < 3 or parts[0] != lay.home_dir or parts[1] != lay.area:
            continue
        tail = parts[2:]
        name = tail[-1]
        depth_ok = lay.depth is None or len(tail) == lay.depth + 1
        if depth_ok and name.startswith(lay.prefix) and name.endswith(".jsonl"):
            ref = _ref_of(lay, name)
            if ref in refs:
                return ref, MAIN
        if lay.sibling and lay.depth is not None and len(tail) > lay.depth + 1:
            if tail[lay.depth] in refs:
                return tail[lay.depth], SIBLING
    return None


def key_index(parts: list[str]) -> int | None:
    """The index in ``parts`` of the key folder a layout lets a receiver
    rename (Claude's project folder: any key resumes), or ``None``."""
    for lay in LAYOUTS:
        if (lay.depth == 1 and len(parts) >= 4 and parts[0] == lay.home_dir
                and parts[1] == lay.area):
            return 2
    return None


def iter_session_files(agents_dir: Path) -> Iterator[tuple[Path, str, Path | None]]:
    """``(file, id, sibling)`` for every transcript under the agent trees
    (``iter_home_session_files`` over every agent's homes)."""
    for home in _homes(agents_dir):
        yield from iter_home_session_files(home)


def _rel(agents_dir: Path, path: Path) -> str:
    return path.relative_to(agents_dir).as_posix()


def apply(agents_dir: Path, keep: set[str], live: set[str], min_age_s: float,
          dry_run: bool, *, now: float | None = None) -> dict:
    """Remove every transcript whose id is UUID-shaped, in neither ``keep``
    nor ``live`` and older than ``min_age_s``, and prune the project
    folders that emptied. The counts for the ack: ``files`` and ``bytes``
    are what the machine holds after the pass, ``deleted`` and
    ``deleted_bytes`` what went, or what WOULD go on a dry run (which
    removes nothing), ``candidates`` the first paths that go. A delete goes
    beneath the agents root with no component followed
    (``safe_fs.unlink_beneath``); a sibling folder that is a link is left
    where it is."""
    now = time.time() if now is None else now
    stats: dict = {"files": 0, "bytes": 0, "deleted": 0, "deleted_bytes": 0, "candidates": []}
    emptied: set[Path] = set()
    for f, ref, sibling in iter_session_files(agents_dir):
        try:
            st = f.lstat()
        except OSError:
            continue
        aged = (bool(_UUID_RE.match(ref)) and ref not in keep and ref not in live
                and now - st.st_mtime >= min_age_s)
        if not aged:
            stats["files"] += 1
            stats["bytes"] += st.st_size
            continue
        if len(stats["candidates"]) < CANDIDATES_MAX:
            with contextlib.suppress(ValueError):
                stats["candidates"].append(_rel(agents_dir, f))
        if dry_run:
            stats["deleted"] += 1
            stats["deleted_bytes"] += st.st_size
            continue
        try:
            safe_fs.unlink_beneath(agents_dir, _rel(agents_dir, f))
        except (OSError, ValueError):
            stats["files"] += 1
            stats["bytes"] += st.st_size
            continue
        if sibling is not None and not safe_fs.is_link_like(sibling):
            with contextlib.suppress(OSError):
                force_rmtree(sibling)
        stats["deleted"] += 1
        stats["deleted_bytes"] += st.st_size
        emptied.add(f.parent)
    for d in sorted(emptied, key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError, ValueError):
            safe_fs.rmdir_beneath(agents_dir, _rel(agents_dir, d))
    return stats
