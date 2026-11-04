"""File-based memory primitives (Memory — index + topic files).

Memory lives in markdown TOPIC FILES under a per-scope ``memory/`` directory,
plus one server-GENERATED ``MEMORY.md`` index per scope:

- agent scope: ``agents/{agent}/knowledge/memory/``        (shared across users)
- user scope:  ``agents/{agent}/users/{u}/context/memory/`` (per user, per agent)

Agents read memory from their system prompt (the proxy injects the topic
files whole under a byte budget; past it the newest that fit load whole and
the rest are listed with their index entries) and write it ONLY
through the ``memory`` MCP tool → ``POST /v1/internal/memory/op``, which
calls into this module. The command set mirrors Anthropic's
``memory_20250818`` contract (view / create / str_replace / insert / delete /
rename) including its success/error strings — models are trained on them.

``MEMORY.md`` is deterministic server output (``memory_index``: per topic
the heading, a summary line, the size, the entry count, the newest date and
the ``##`` sections with their line numbers) — no LLM is involved anywhere.
It is regenerated synchronously after every mutation and lazily healed at
injection time when a human hand-edited topic files directly (dashboard /
satellite) or when an older builder wrote it, so it drifts only while its
folder cannot be written (the prompt build logs the failed rewrite).

The caps: a write that leaves a topic SMALLER always passes, so every
oversized topic on a released install stays editable; a write that grows
one meets the hard cap, the growth cap and (for an edit) the entry cap,
the two admin-set caps arriving as ``Caps``.

Humans edit topic files freely via the dashboard Files UI / satellites under
the normal folder roles; agents are denied direct Write/Edit on these paths
by ``path_policy`` so the tool stays the single agent write path (role
matrix, locking, git attribution, index regen).

All functions are synchronous (call via ``asyncio.to_thread``). Writes hold
a per-scope-root ``threading.Lock`` + ``flock`` on a hidden ``.memlock``
file (multi-file ops: topic + index + rename pairs must be atomic as a
group), then write each file via tmp+fsync+rename.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config
from core import layout
from services.infra import safe_fs
from services.infra.path_confinement import (
    PathOutsideRoot,
    join_under,
    normalize_rel_path,
)
from services.memory.memory_index import (  # the callers read these through this module
    ALLOWED_EXTENSIONS,
    INDEX_FILENAME,
    INDEX_HEADER,
    build_index_content,
    has_date,
    iter_topic_files,
    scope_total_bytes,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOCK_FILENAME = ".memlock"  # hidden: excluded from listings, never committed

# Caps — curation pressure replaces any offline consolidator.
TOPIC_SOFT_WARN_BYTES = 16 * 1024     # tool result appends an imperative warning
TOPIC_HARD_CAP_BYTES = 64 * 1024      # a growing write rejected
SCOPE_TOTAL_CAP_BYTES = 512 * 1024    # a growing write rejected
MAX_TOPICS_PER_SCOPE = 100            # create rejected

# The two admin-set caps (memory_settings): a topic over the growth cap no
# longer grows, an edit adds at most the entry cap. The ranges bound the
# setting on both sides.
DEFAULT_TOPIC_GROW_CAP_BYTES = 32 * 1024
DEFAULT_ENTRY_CAP_BYTES = 2 * 1024
GROW_CAP_RANGE = (TOPIC_SOFT_WARN_BYTES, TOPIC_HARD_CAP_BYTES)
ENTRY_CAP_RANGE = (256, TOPIC_HARD_CAP_BYTES)

_VIEW_DIR_DEPTH = 2                   # `view` lists up to 2 levels deep

logger = logging.getLogger("otodock.memory")

# Control characters never legal in memory content (newline + tab excluded).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

DATE_REMINDER = "Date this entry (YYYY-MM-DD) so its age stays visible."


class MemoryOpError(Exception):
    """A command-level failure whose message is returned VERBATIM as the
    tool result text (the model is trained on these strings). Not an HTTP
    error — auth / role / toggle failures raise HTTPException in the API
    layer instead."""


class MemoryCapError(MemoryOpError):
    """A write refused by a size or count cap (logged apart from a bad path)."""


def _in_range(value: Any, bounds: tuple[int, int], default: int) -> int:
    lo, hi = bounds
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return default
    return value


@dataclass(frozen=True)
class Caps:
    """The admin-set caps a write is judged by. ``from_settings`` takes the
    ``memory_settings`` row and falls back per value to the default when
    the row lacks it or holds something out of range."""
    grow_cap: int = DEFAULT_TOPIC_GROW_CAP_BYTES
    entry_cap: int = DEFAULT_ENTRY_CAP_BYTES

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> Caps:
        return cls(
            grow_cap=_in_range(
                settings.get("topic_grow_cap_bytes"), GROW_CAP_RANGE,
                DEFAULT_TOPIC_GROW_CAP_BYTES,
            ),
            entry_cap=_in_range(
                settings.get("entry_cap_bytes"), ENTRY_CAP_RANGE,
                DEFAULT_ENTRY_CAP_BYTES,
            ),
        )


@dataclass
class OpResult:
    """Successful command outcome. ``output`` is returned verbatim as the
    tool result text; ``warnings`` are appended as extra lines; ``changed``
    lists scope-relative paths that were written/created (for git + fan-out)
    and ``deleted`` those that were removed (for tombstones + fan-out);
    ``over_target`` is the topic's size when it was left over the soft
    target (the API logs it), 0 otherwise."""
    output: str
    warnings: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    over_target: int = 0


# ---------------------------------------------------------------------------
# Scope roots + path validation
# ---------------------------------------------------------------------------

def scope_root(agent_dir: Path, scope: str, username: str | None = None) -> Path:
    """On-disk root of a memory scope. Does NOT create it. A username that
    would leave the agent tree is refused (ValueError, like the other
    contract violations here)."""
    if scope == "agent":
        return join_under(agent_dir, layout.KNOWLEDGE, "memory")
    if scope == "user":
        if not username:
            raise ValueError("username required for user-scope memory")
        return join_under(agent_dir, layout.USERS, username, layout.CONTEXT, "memory")
    raise ValueError(f"unknown scope: {scope!r}")


def git_repo_root(agent_dir: Path, scope: str, username: str | None = None) -> Path:
    """The git repo that owns this scope's memory dir.

    agent scope: ``agents/{a}/knowledge/`` (new repo class, lazy-init).
    user scope:  ``agents/{a}/users/{u}/context/`` (existing per-user repo).
    """
    if scope == "agent":
        return join_under(agent_dir, layout.KNOWLEDGE)
    if scope == "user":
        if not username:
            raise ValueError("username required for user-scope memory")
        return join_under(agent_dir, layout.USERS, username, layout.CONTEXT)
    raise ValueError(f"unknown scope: {scope!r}")


def split_virtual_path(path: str) -> tuple[str, str]:
    """Split a tool path like ``/memories/user/preferences.md`` into
    ``(scope, rel)`` where ``rel`` is relative to the scope root ("" for the
    scope dir itself). Raises MemoryOpError on malformed paths.
    """
    p = (path or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    parts = [seg for seg in p.split("/") if seg not in ("",)]
    if not parts:
        return "", ""  # bare "/" or "" — the /memories root itself (view-only)
    if parts[0] != "memories":
        raise MemoryOpError(
            f"The path {path} does not exist. Please provide a valid path "
            "(all memory paths live under /memories)."
        )
    if len(parts) == 1:
        return "", ""  # the /memories root itself (view-only)
    scope = parts[1]
    if scope not in ("user", "agent"):
        raise MemoryOpError(
            f"The path {path} does not exist. Please provide a valid path "
            "(valid scopes: /memories/user, /memories/agent)."
        )
    return scope, "/".join(parts[2:])


def validate_rel(rel: str, *, mutating: bool = False, require_ext: bool = False) -> str:
    """Validate a scope-relative path. Returns the normalized relpath.

    Always rejects traversal + hidden segments (which also makes the lock
    file unreachable). ``mutating`` commands additionally may not target the
    generated index, and any extension present must be an allowed text type.
    ``require_ext`` (create) demands an extension — directories are legal
    targets for delete/rename, so those validate without it. ``rel == ""``
    (the scope dir itself) is legal; callers guard where it isn't.
    """
    if rel in ("", "."):
        return ""
    # ``.`` and empty segments are dropped (a lenient input), then the
    # helper refuses a ``..`` segment or a NUL, and every hidden segment is
    # refused here.
    parts = [seg for seg in rel.split("/") if seg not in ("", ".")]
    norm = "/".join(parts)
    if not norm:
        return ""
    try:
        normalize_rel_path(norm)
    except PathOutsideRoot as exc:
        raise MemoryOpError(
            f"The path contains an invalid segment ({rel!r}). "
            "Please provide a valid path."
        ) from exc
    for seg in parts:
        if seg.startswith("."):
            raise MemoryOpError(
                f"The path contains an invalid segment ({seg!r}). "
                "Please provide a valid path."
            )
    leaf = parts[-1]
    if mutating:
        if leaf == INDEX_FILENAME:
            raise MemoryOpError(
                f"Error: {INDEX_FILENAME} is auto-generated; edit topic files "
                "instead."
            )
        if "." in leaf:  # has an extension → must be an allowed text type
            ext = "." + leaf.rsplit(".", 1)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                raise MemoryOpError(
                    f"Error: {leaf} has an unsupported extension. Memory "
                    "files must be .md or .txt"
                )
        elif require_ext:
            raise MemoryOpError(
                f"Error: {leaf} has no extension. Memory files must be "
                ".md or .txt"
            )
    return norm


def _resolve_strict(root: Path, rel: str) -> Path:
    """Resolve ``root/rel`` and verify the result stays under ``root``
    (symlink-escape proof). The root itself may not exist yet."""
    base = root.resolve()
    target = (root / rel) if rel else root
    resolved = target.resolve()
    if resolved != base and base not in resolved.parents:
        raise MemoryOpError(
            f"The path {rel} does not exist. Please provide a valid path."
        )
    return resolved


def _agents_rel(path: Path) -> str:
    """The rel of a scope-root path or a resolved answer beneath
    ``config.AGENTS_DIR``, the root every memory write opens: a memory root
    outside the agents tree is refused (every real scope root, an agent's,
    a person's or an external caller's, sits under it)."""
    try:
        return safe_fs.rel_under(path, config.AGENTS_DIR)
    except OSError:
        raise MemoryOpError(
            "Error: the memory scope is outside the agents tree."
        ) from None


# ---------------------------------------------------------------------------
# Locking + atomic write (per scope root)
# ---------------------------------------------------------------------------

_root_locks: dict[str, threading.Lock] = {}
_root_locks_guard = threading.Lock()


def _get_root_lock(root: Path) -> threading.Lock:
    key = str(root)
    with _root_locks_guard:
        lock = _root_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _root_locks[key] = lock
        return lock


class _scope_lock:
    """threading.Lock (intra-process) + flock on ``root/.memlock``
    (inter-process). flock alone doesn't serialize two fds in one process —
    see the v3 rationale preserved in git history."""

    def __init__(self, root: Path):
        self.root = root
        self._tlock = _get_root_lock(root)
        self._fd: int | None = None

    def __enter__(self):
        self._tlock.acquire()
        try:
            root_rel = _agents_rel(self.root)
            safe_fs.mkdirs_beneath(config.AGENTS_DIR, root_rel)
            self._fd = safe_fs.open_beneath(
                config.AGENTS_DIR, f"{root_rel}/{LOCK_FILENAME}", os.O_RDWR | os.O_CREAT, 0o644,
            )
        except BaseException:
            self._tlock.release()
            raise
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        self._tlock.release()
        return False


def _atomic_write(path: Path, content: str) -> None:
    """Replace ``path`` (a resolved answer under a scope root) with
    ``content`` beneath the agents root: a ``.partial`` temp created in the
    parent's handle, fsynced, renamed onto the name with no component
    followed; readers see old or new, never partial. The dotted temp is
    hidden from the listings and never staged."""
    safe_fs.atomic_write_beneath(
        config.AGENTS_DIR, _agents_rel(path), content.encode("utf-8"), mkdirs=True, fsync=True,
    )


def _sanitize(text: str) -> str:
    """Strip control chars (keep \\n and \\t); normalize CRLF."""
    return _CONTROL_CHARS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


# ---------------------------------------------------------------------------
# The generated index (its content is memory_index's)
# ---------------------------------------------------------------------------

def regenerate_index(root: Path) -> Path | None:
    """Write the generated index (only when content changed, to keep git
    history quiet). Caller holds the scope lock. Returns the index path when
    (re)written, else None.

    Always leaves the index mtime >= the newest topic mtime, even on the
    content-unchanged shortcut — otherwise a future-dated topic file (clock
    skew, copied files) would read as permanently stale and re-heal on every
    prompt injection."""
    if not root.is_dir():
        return None
    index_path = root / INDEX_FILENAME
    new = build_index_content(root)
    wrote: Path | None = index_path
    with contextlib.suppress(OSError):
        if index_path.exists() and index_path.read_text(encoding="utf-8") == new:
            wrote = None
    if wrote:
        _atomic_write(index_path, new)
    topics = iter_topic_files(root)
    if topics and index_path.exists():
        newest = max(p.stat().st_mtime for p in topics)
        if index_path.stat().st_mtime < newest:
            os.utime(index_path, (newest, newest))
    return wrote


def index_is_stale(root: Path) -> bool:
    """True when a topic file is newer than the index (or index missing) —
    i.e. a human hand-edited files directly — or when an older builder
    wrote the index (its header carries the version). Cheap: stat calls
    and one line."""
    if not root.is_dir():
        return False
    topics = iter_topic_files(root)
    if not topics:
        return False
    index_path = root / INDEX_FILENAME
    if not index_path.exists():
        return True
    try:
        with open(index_path, encoding="utf-8", errors="replace") as f:
            if f.readline() != INDEX_HEADER:
                return True
    except OSError:
        return True
    index_mtime = index_path.stat().st_mtime
    return any(p.stat().st_mtime > index_mtime for p in topics)


def heal_index_if_stale(root: Path) -> bool:
    """Regenerate a stale index (used at prompt-injection time). Returns
    True if it was healed."""
    if not index_is_stale(root):
        return False
    with _scope_lock(root):
        regenerate_index(root)
    return True


# ---------------------------------------------------------------------------
# Commands — Anthropic memory-tool contract semantics + verbatim strings
# ---------------------------------------------------------------------------

def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / (1024 * 1024):.1f}M"


def _list_dir(root: Path, base: Path, prefix: str, depth: int, lines: list[str]) -> None:
    if depth > _VIEW_DIR_DEPTH:
        return
    try:
        entries = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name))
    except OSError:
        return
    for e in entries:
        if e.name.startswith("."):
            continue
        if e.is_dir():
            lines.append(f"{prefix}{e.name}/")
            _list_dir(root, e, prefix + "  ", depth + 1, lines)
        else:
            try:
                size = _human_size(e.stat().st_size)
            except OSError:
                size = "?"
            lines.append(f"{prefix}{e.name}\t{size}")


def view_root(scopes: dict[str, Path]) -> OpResult:
    """``view /memories`` — synthesized listing of the scopes available to
    this session (each is a real directory on disk, possibly empty)."""
    lines = ["Directory: /memories"]
    for name, root in scopes.items():
        n = len(iter_topic_files(root))
        lines.append(f"  {name}/\t{n} topic file{'s' if n != 1 else ''}")
    return OpResult(output="\n".join(lines))


def op_view(root: Path, rel: str, view_range: list[int] | None = None) -> OpResult:
    resolved = _resolve_strict(root, rel)
    if rel == "" or resolved.is_dir():
        if not resolved.is_dir():
            raise MemoryOpError(
                f"The path {rel or '/'} does not exist. Please provide a valid path."
            )
        lines = [f"Directory: {rel or '.'}"]
        _list_dir(root, resolved, "  ", 1, lines)
        return OpResult(output="\n".join(lines))
    if not resolved.is_file():
        raise MemoryOpError(
            f"The path {rel} does not exist. Please provide a valid path."
        )
    if resolved.name == INDEX_FILENAME and index_is_stale(root):
        with _scope_lock(root):
            regenerate_index(root)
    text = resolved.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    start, end = 1, len(all_lines)
    if view_range:
        if (
            len(view_range) != 2
            or view_range[0] < 1
            or (view_range[1] != -1 and view_range[1] < view_range[0])
        ):
            raise MemoryOpError(
                f"Error: Invalid `view_range` parameter: {view_range}. "
                "Expected [start_line, end_line] with start_line >= 1."
            )
        start = view_range[0]
        end = len(all_lines) if view_range[1] == -1 else min(view_range[1], len(all_lines))
    shown = all_lines[start - 1 : end]
    numbered = "\n".join(
        f"{i:>6}\t{line}" for i, line in enumerate(shown, start=start)
    )
    return OpResult(
        output=f"Here's the content of {rel} with line numbers:\n{numbered}"
    )


def _check_caps_for_write(
    root: Path, rel: str, new_bytes: int, *, creating: bool,
    caps: Caps, current: int = 0,
) -> tuple[list[str], int]:
    """The caps raise (``MemoryCapError``); the soft warning is returned
    with the size that earned it. Every size rule judges a GROWING write
    only: a write that leaves the topic smaller always passes, so an
    oversized topic (a hand edit, an older cap) can be brought down.
    ``current`` is the topic's size before the write, measured as
    ``new_bytes`` is (the sanitised text). Caller holds the lock."""
    growing = new_bytes > current
    if growing and new_bytes > TOPIC_HARD_CAP_BYTES:
        raise MemoryCapError(
            f"Error: {rel} would be {new_bytes:,} bytes, over the "
            f"{TOPIC_HARD_CAP_BYTES:,} byte per-file cap. Split it into "
            "smaller topic files or prune stale content."
        )
    if growing and new_bytes > caps.grow_cap:
        raise MemoryCapError(
            f"Error: {rel} would be {new_bytes:,} bytes, over the "
            f"{caps.grow_cap:,} byte growth cap. Bring it under "
            f"{TOPIC_SOFT_WARN_BYTES:,} bytes first: drop entries that no "
            "longer govern, fold duplicates, move a log to a file outside "
            "memory and keep one pointer line. Or split it into a second "
            "topic. Then retry."
        )
    if growing and not creating and new_bytes - current > caps.entry_cap:
        raise MemoryCapError(
            f"Error: this edit adds {new_bytes - current:,} bytes, over the "
            f"{caps.entry_cap:,} byte entry cap. A memory entry is a fact, a "
            "rule or a pointer, not a log. Write the detail to a file outside "
            "memory and keep one line here."
        )
    topics = iter_topic_files(root)
    if creating and len(topics) + 1 > MAX_TOPICS_PER_SCOPE:
        raise MemoryCapError(
            f"Error: this scope already has {len(topics)} topic files "
            f"(cap {MAX_TOPICS_PER_SCOPE}). Consolidate existing topics "
            "before creating new ones."
        )
    if growing:
        existing = root / rel
        on_disk = existing.stat().st_size if existing.exists() else 0
        total_after = scope_total_bytes(root) - on_disk + new_bytes
        if total_after > SCOPE_TOTAL_CAP_BYTES:
            raise MemoryCapError(
                f"Error: this write would put the scope at {total_after:,} "
                f"bytes, over the {SCOPE_TOTAL_CAP_BYTES:,} byte total cap. "
                "Prune or consolidate topics first."
            )
    warnings: list[str] = []
    over = 0
    if new_bytes > TOPIC_SOFT_WARN_BYTES:
        over = new_bytes
        warnings.append(
            f"{rel} is {new_bytes:,} bytes, over the {TOPIC_SOFT_WARN_BYTES:,} "
            "byte target. Bring it under the target now: fold duplicates, "
            "drop superseded trails, move logs out."
        )
    return warnings, over


def _needs_date_reminder(edited_lines: list[str]) -> bool:
    """An edit whose lines say something (not blank, not a heading) and
    carry no date gets the reminder; a date on any edited line clears it."""
    said = [line for line in edited_lines if line.strip() and not line.lstrip().startswith("#")]
    return bool(said) and not any(has_date(line) for line in said)


def op_create(root: Path, rel: str, file_text: str, *, caps: Caps | None = None) -> OpResult:
    content = _sanitize(file_text or "")
    with _scope_lock(root):
        target = _resolve_strict(root, rel)
        if target.exists():
            raise MemoryOpError(f"Error: File {rel} already exists")
        warnings, over = _check_caps_for_write(
            root, rel, len(content.encode("utf-8")), creating=True,
            caps=caps or Caps(),
        )
        _atomic_write(target, content)
        changed = [rel]
        if regenerate_index(root):
            changed.append(INDEX_FILENAME)
        return OpResult(
            output=f"File created successfully at: {rel}",
            warnings=warnings, changed=changed, over_target=over,
        )


def op_str_replace(
    root: Path, rel: str, old_str: str, new_str: str, *, caps: Caps | None = None,
) -> OpResult:
    if not old_str:
        raise MemoryOpError("Error: `old_str` must be non-empty")
    with _scope_lock(root):
        target = _resolve_strict(root, rel)
        if not target.is_file():
            raise MemoryOpError(
                f"The path {rel} does not exist. Please provide a valid path."
            )
        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_str)
        if count == 0:
            raise MemoryOpError(
                f"No replacement was performed, old_str `{old_str}` did not "
                f"appear verbatim in {rel}."
            )
        if count > 1:
            line_numbers = [
                i for i, line in enumerate(text.splitlines(), 1) if old_str in line
            ]
            raise MemoryOpError(
                f"No replacement was performed. Multiple occurrences of "
                f"old_str `{old_str}` in lines: {line_numbers}. Please ensure "
                "it is unique"
            )
        pos = text.find(old_str)
        new_text = _sanitize(text.replace(old_str, new_str, 1))
        warnings, over = _check_caps_for_write(
            root, rel, len(new_text.encode("utf-8")), creating=False,
            caps=caps or Caps(), current=len(_sanitize(text).encode("utf-8")),
        )
        _atomic_write(target, new_text)
        changed = [rel]
        if regenerate_index(root):
            changed.append(INDEX_FILENAME)
        # The lines the replacement spans in the result: the snippet around
        # them (contract shape) and the date reminder read them. A newline
        # leading or trailing the new text touches no line of its own, so
        # the reminder reads the text between them only.
        idx = _sanitize(text[:pos]).count("\n")
        clean = _sanitize(new_str)
        span = clean.count("\n")
        lines = new_text.split("\n")
        if new_text.endswith("\n"):
            lines.pop()
        core = clean.strip("\n")
        if core.strip():
            start = idx + (len(clean) - len(clean.lstrip("\n")))
            if _needs_date_reminder(lines[start : start + core.count("\n") + 1]):
                warnings.append(DATE_REMINDER)
        lo, hi = max(0, idx - 2), min(len(lines), idx + span + 3)
        snippet = "\n".join(
            f"{i:>6}\t{line}" for i, line in enumerate(lines[lo:hi], start=lo + 1)
        )
        return OpResult(
            output=f"The memory file has been edited.\n{snippet}",
            warnings=warnings, changed=changed, over_target=over,
        )


def op_insert(
    root: Path, rel: str, insert_line: int, insert_text: str, *, caps: Caps | None = None,
) -> OpResult:
    # An empty insert would rewrite the file unchanged and answer "edited";
    # a blank line is "\n". Judged after sanitising: text of control
    # characters alone is empty too.
    inserted = _sanitize(insert_text or "").splitlines()
    if not inserted:
        raise MemoryOpError("Error: `insert_text` must be non-empty")
    with _scope_lock(root):
        target = _resolve_strict(root, rel)
        if not target.is_file():
            raise MemoryOpError(
                f"The path {rel} does not exist. Please provide a valid path."
            )
        text = _sanitize(target.read_text(encoding="utf-8", errors="replace"))
        lines = text.splitlines()
        if not isinstance(insert_line, int) or insert_line < 0 or insert_line > len(lines):
            raise MemoryOpError(
                f"Error: Invalid `insert_line` parameter: {insert_line}. It "
                f"should be within the range of lines of the file: "
                f"[0, {len(lines)}]"
            )
        new_lines = lines[:insert_line] + inserted + lines[insert_line:]
        new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") or not text else "")
        warnings, over = _check_caps_for_write(
            root, rel, len(new_text.encode("utf-8")), creating=False,
            caps=caps or Caps(), current=len(text.encode("utf-8")),
        )
        _atomic_write(target, new_text)
        changed = [rel]
        if regenerate_index(root):
            changed.append(INDEX_FILENAME)
        if _needs_date_reminder(inserted):
            warnings.append(DATE_REMINDER)
        return OpResult(
            output=f"The file {rel} has been edited.",
            warnings=warnings, changed=changed, over_target=over,
        )


def op_delete(root: Path, rel: str) -> OpResult:
    if rel == "":
        raise MemoryOpError(
            "Error: cannot delete a memory scope root. Delete individual "
            "topic files instead."
        )
    with _scope_lock(root):
        target = _resolve_strict(root, rel)
        deleted: list[str] = []
        if target.is_file():
            deleted = [rel]
            safe_fs.unlink_beneath(config.AGENTS_DIR, _agents_rel(target))
        elif target.is_dir():
            deleted = [
                (Path(rel) / f.relative_to(target)).as_posix()
                for f in target.rglob("*") if f.is_file() and not f.is_symlink()
            ]
            safe_fs.rmtree_beneath(config.AGENTS_DIR, _agents_rel(target))
        else:
            raise MemoryOpError(
                f"The path {rel} does not exist. Please provide a valid path."
            )
        changed = []
        if regenerate_index(root):
            changed.append(INDEX_FILENAME)
        return OpResult(
            output=f"Successfully deleted {rel}",
            changed=changed, deleted=deleted,
        )


def op_rename(root: Path, old_rel: str, new_rel: str) -> OpResult:
    with _scope_lock(root):
        src = _resolve_strict(root, old_rel)
        dst = _resolve_strict(root, new_rel)
        if not src.exists():
            raise MemoryOpError(
                f"The path {old_rel} does not exist. Please provide a valid path."
            )
        if dst.exists():
            raise MemoryOpError(
                f"Error: The destination {new_rel} already exists"
            )
        if src.is_file() and dst.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise MemoryOpError(
                f"Error: {dst.name} has an unsupported extension. Memory "
                "files must be .md or .txt"
            )
        if src.is_dir():
            # Enumerate per-file so git + fan-out + tombstones stay per-path.
            moved = [
                f.relative_to(src).as_posix()
                for f in src.rglob("*") if f.is_file() and not f.is_symlink()
            ]
            safe_fs.rename_beneath(
                config.AGENTS_DIR, _agents_rel(src), _agents_rel(dst), mkdirs=True,
            )
            changed = [(Path(new_rel) / m).as_posix() for m in moved]
            deleted = [(Path(old_rel) / m).as_posix() for m in moved]
        else:
            safe_fs.rename_beneath(
                config.AGENTS_DIR, _agents_rel(src), _agents_rel(dst), mkdirs=True,
            )
            changed = [new_rel]
            deleted = [old_rel]
        if regenerate_index(root):
            changed.append(INDEX_FILENAME)
        return OpResult(
            output=f"Successfully renamed {old_rel} to {new_rel}",
            changed=changed, deleted=deleted,
        )


def op_promote(
    src_root: Path, dst_root: Path, rel: str, dst_rel: str, *, caps: Caps | None = None,
) -> tuple[OpResult, OpResult]:
    """Move one topic file from a person's scope into the shared scope:
    a create in the destination (its caps apply, the growth cap included),
    then the source's removal, under both scope locks (the source first;
    no other path takes two, so no cycle). A copy then an unlink, never a
    rename: the two folders may sit on different quota projects. When the
    unlink fails the copy is taken back and nothing changed. Returns the
    source's result (the deletion) and the destination's (the new file),
    each with its own index; the caller commits and publishes both roots.
    """
    with _scope_lock(src_root), _scope_lock(dst_root):
        src = _resolve_strict(src_root, rel)
        if src.is_dir():
            raise MemoryOpError(
                f"Error: {rel} is a folder. Promote one topic file at a time."
            )
        if not src.is_file():
            raise MemoryOpError(
                f"The path {rel} does not exist. Please provide a valid path."
            )
        dst = _resolve_strict(dst_root, dst_rel)
        if dst.exists():
            raise MemoryOpError(f"Error: The destination {dst_rel} already exists")
        if dst.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise MemoryOpError(
                f"Error: {dst.name} has an unsupported extension. Memory "
                "files must be .md or .txt"
            )
        content = _sanitize(src.read_text(encoding="utf-8", errors="replace"))
        warnings, over = _check_caps_for_write(
            dst_root, dst_rel, len(content.encode("utf-8")), creating=True,
            caps=caps or Caps(),
        )
        _atomic_write(dst, content)
        try:
            # A source that vanished meanwhile (a hand delete) is gone
            # either way: the copy is the move's result.
            safe_fs.unlink_beneath(config.AGENTS_DIR, _agents_rel(src), missing_ok=True)
        except OSError as exc:
            try:
                safe_fs.unlink_beneath(config.AGENTS_DIR, _agents_rel(dst))
            except OSError:
                logger.warning(
                    "memory promote: %s stayed in both scopes (%s, %s)",
                    rel, src, dst, exc_info=True,
                )
                raise MemoryOpError(
                    f"Error: {rel} could not be moved out of /memories/user/ "
                    f"and a copy stayed at /memories/agent/{dst_rel}. Delete "
                    "one of the two."
                ) from exc
            raise MemoryOpError(
                f"Error: {rel} could not be moved out of /memories/user/ and "
                "stays there. Nothing changed in /memories/agent/."
            ) from exc
        src_changed: list[str] = []
        if regenerate_index(src_root):
            src_changed.append(INDEX_FILENAME)
        dst_changed = [dst_rel]
        if regenerate_index(dst_root):
            dst_changed.append(INDEX_FILENAME)
        return (
            OpResult(output="", changed=src_changed, deleted=[rel]),
            OpResult(
                output=f"Successfully renamed /memories/user/{rel} to /memories/agent/{dst_rel}",
                warnings=warnings, changed=dst_changed, over_target=over,
            ),
        )
