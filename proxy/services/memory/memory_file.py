"""File-based memory primitives (Memory — index + topic files).

Memory lives in markdown TOPIC FILES under a per-scope ``memory/`` directory,
plus one server-GENERATED ``MEMORY.md`` index per scope:

- agent scope: ``agents/{agent}/knowledge/memory/``        (shared across users)
- user scope:  ``agents/{agent}/users/{u}/context/memory/`` (per user, per agent)

Agents read memory from their system prompt (the proxy injects either the
full topic files or, past a byte budget, just the index) and write it ONLY
through the ``memory`` MCP tool → ``POST /v1/internal/memory/op``, which
calls into this module. The command set mirrors Anthropic's
``memory_20250818`` contract (view / create / str_replace / insert / delete /
rename) including its success/error strings — models are trained on them.

``MEMORY.md`` is deterministic server output (one line per topic file, taken
from the file's first heading/line) — no LLM is involved anywhere. It is
regenerated synchronously after every mutation and lazily healed at
injection time when a human hand-edited topic files directly (dashboard /
satellite), so it can never drift.

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
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INDEX_FILENAME = "MEMORY.md"
LOCK_FILENAME = ".memlock"  # hidden: excluded from listings, never committed

ALLOWED_EXTENSIONS = {".md", ".txt"}

# Caps — curation pressure replaces any offline consolidator.
TOPIC_SOFT_WARN_BYTES = 16 * 1024     # tool result appends a "tighten" warning
TOPIC_HARD_CAP_BYTES = 64 * 1024      # write rejected
SCOPE_TOTAL_CAP_BYTES = 512 * 1024    # write rejected
MAX_TOPICS_PER_SCOPE = 100            # create rejected

INDEX_MAX_LINES = 200                 # Claude Code parity
INDEX_MAX_BYTES = 25 * 1024
INDEX_SUMMARY_MAX_CHARS = 200

INDEX_HEADER = (
    "# Memory index (auto-generated — edit topic files, not this file)\n"
)

_VIEW_DIR_DEPTH = 2                   # `view` lists up to 2 levels deep

# Control characters never legal in memory content (newline + tab excluded).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MemoryOpError(Exception):
    """A command-level failure whose message is returned VERBATIM as the
    tool result text (the model is trained on these strings). Not an HTTP
    error — auth / role / toggle failures raise HTTPException in the API
    layer instead."""


@dataclass
class OpResult:
    """Successful command outcome. ``output`` is returned verbatim as the
    tool result text; ``warnings`` are appended as extra lines; ``changed``
    lists scope-relative paths that were written/created (for git + fan-out)
    and ``deleted`` those that were removed (for tombstones + fan-out)."""
    output: str
    warnings: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scope roots + path validation
# ---------------------------------------------------------------------------

def scope_root(agent_dir: Path, scope: str, username: str | None = None) -> Path:
    """On-disk root of a memory scope. Does NOT create it."""
    if scope == "agent":
        return agent_dir / "knowledge" / "memory"
    if scope == "user":
        if not username:
            raise ValueError("username required for user-scope memory")
        return agent_dir / "users" / username / "context" / "memory"
    raise ValueError(f"unknown scope: {scope!r}")


def git_repo_root(agent_dir: Path, scope: str, username: str | None = None) -> Path:
    """The git repo that owns this scope's memory dir.

    agent scope: ``agents/{a}/knowledge/`` (new repo class, lazy-init).
    user scope:  ``agents/{a}/users/{u}/context/`` (existing per-user repo).
    """
    if scope == "agent":
        return agent_dir / "knowledge"
    if scope == "user":
        if not username:
            raise ValueError("username required for user-scope memory")
        return agent_dir / "users" / username / "context"
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
    if not parts or parts[0] != "memories":
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
    parts = []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == ".." or seg.startswith("."):
            raise MemoryOpError(
                f"The path contains an invalid segment ({seg!r}). "
                "Please provide a valid path."
            )
        parts.append(seg)
    norm = "/".join(parts)
    if not norm:
        return ""
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
        self.root.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.root / LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o644)
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
    """tmp + fsync + rename — readers see old or new, never partial. The
    ``.tmp`` suffix is gitignored by the per-repo template."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _sanitize(text: str) -> str:
    """Strip control chars (keep \\n and \\t); normalize CRLF."""
    return _CONTROL_CHARS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


# ---------------------------------------------------------------------------
# Topic-file scanning + the generated index
# ---------------------------------------------------------------------------

def iter_topic_files(root: Path) -> list[Path]:
    """All topic files in a scope (recursive), excluding the index, hidden
    files/dirs, and non-text extensions. Sorted by relpath for stability."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root)
        if any(seg.startswith(".") for seg in rel.parts):
            continue
        if p.name == INDEX_FILENAME and len(rel.parts) == 1:
            continue
        if p.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        out.append(p)
    return out


def scope_total_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in iter_topic_files(root))


def _topic_summary(path: Path) -> str:
    """First non-empty line, stripped of markdown heading markers, truncated
    to INDEX_SUMMARY_MAX_CHARS — the topic's one-line index entry."""
    with contextlib.suppress(OSError):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip().lstrip("#").strip()
                if s:
                    return (
                        s[: INDEX_SUMMARY_MAX_CHARS - 1] + "…"
                        if len(s) > INDEX_SUMMARY_MAX_CHARS else s
                    )
    return "(empty)"


def build_index_content(root: Path) -> str:
    """Deterministic index body for a scope — pure code, no LLM, ever."""
    lines = [INDEX_HEADER.rstrip("\n")]
    for p in iter_topic_files(root):
        rel = p.relative_to(root).as_posix()
        updated = datetime.fromtimestamp(
            p.stat().st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        lines.append(f"- {rel} — {_topic_summary(p)} (updated {updated})")
    body = ""
    count = 0
    for i, line in enumerate(lines):
        candidate = body + line + "\n"
        count += 1
        if count > INDEX_MAX_LINES or len(candidate.encode("utf-8")) > INDEX_MAX_BYTES:
            omitted = len(lines) - i
            body += (
                f"… ({omitted} more topics not indexed — index over budget; "
                "consolidate topic files)\n"
            )
            break
        body = candidate
    return body


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
    i.e. a human hand-edited files directly. Cheap: stat calls only."""
    if not root.is_dir():
        return False
    topics = iter_topic_files(root)
    if not topics:
        return False
    index_path = root / INDEX_FILENAME
    if not index_path.exists():
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
) -> list[str]:
    """Hard caps raise; soft warnings are returned. Caller holds the lock."""
    if new_bytes > TOPIC_HARD_CAP_BYTES:
        raise MemoryOpError(
            f"Error: {rel} would be {new_bytes} bytes — over the "
            f"{TOPIC_HARD_CAP_BYTES}-byte per-file cap. Split it into "
            "smaller topic files or prune stale content."
        )
    topics = iter_topic_files(root)
    if creating and len(topics) + 1 > MAX_TOPICS_PER_SCOPE:
        raise MemoryOpError(
            f"Error: this scope already has {len(topics)} topic files "
            f"(cap {MAX_TOPICS_PER_SCOPE}). Consolidate existing topics "
            "before creating new ones."
        )
    existing = root / rel
    current = existing.stat().st_size if existing.exists() else 0
    total_after = scope_total_bytes(root) - current + new_bytes
    if total_after > SCOPE_TOTAL_CAP_BYTES:
        raise MemoryOpError(
            f"Error: this write would put the scope at {total_after} bytes — "
            f"over the {SCOPE_TOTAL_CAP_BYTES}-byte total cap. Prune or "
            "consolidate topics first."
        )
    warnings: list[str] = []
    if new_bytes > TOPIC_SOFT_WARN_BYTES:
        warnings.append(
            f"WARN: {rel} is {new_bytes} bytes (soft limit "
            f"{TOPIC_SOFT_WARN_BYTES}). Consider tightening this topic — "
            "merge duplicates, drop stale entries."
        )
    return warnings


def op_create(root: Path, rel: str, file_text: str) -> OpResult:
    content = _sanitize(file_text or "")
    with _scope_lock(root):
        target = _resolve_strict(root, rel)
        if target.exists():
            raise MemoryOpError(f"Error: File {rel} already exists")
        warnings = _check_caps_for_write(
            root, rel, len(content.encode("utf-8")), creating=True,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, content)
        changed = [rel]
        if regenerate_index(root):
            changed.append(INDEX_FILENAME)
        return OpResult(
            output=f"File created successfully at: {rel}",
            warnings=warnings, changed=changed,
        )


def op_str_replace(root: Path, rel: str, old_str: str, new_str: str) -> OpResult:
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
        new_text = _sanitize(text.replace(old_str, new_str, 1))
        warnings = _check_caps_for_write(
            root, rel, len(new_text.encode("utf-8")), creating=False,
        )
        _atomic_write(target, new_text)
        changed = [rel]
        if regenerate_index(root):
            changed.append(INDEX_FILENAME)
        # Snippet around the edit, line-numbered (contract shape).
        idx = new_text[: new_text.find(new_str) if new_str else 0].count("\n")
        lines = new_text.splitlines()
        lo, hi = max(0, idx - 2), min(len(lines), idx + new_str.count("\n") + 3)
        snippet = "\n".join(
            f"{i:>6}\t{line}" for i, line in enumerate(lines[lo:hi], start=lo + 1)
        )
        return OpResult(
            output=f"The memory file has been edited.\n{snippet}",
            warnings=warnings, changed=changed,
        )


def op_insert(root: Path, rel: str, insert_line: int, insert_text: str) -> OpResult:
    with _scope_lock(root):
        target = _resolve_strict(root, rel)
        if not target.is_file():
            raise MemoryOpError(
                f"The path {rel} does not exist. Please provide a valid path."
            )
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if not isinstance(insert_line, int) or insert_line < 0 or insert_line > len(lines):
            raise MemoryOpError(
                f"Error: Invalid `insert_line` parameter: {insert_line}. It "
                f"should be within the range of lines of the file: "
                f"[0, {len(lines)}]"
            )
        inserted = _sanitize(insert_text or "").splitlines()
        new_lines = lines[:insert_line] + inserted + lines[insert_line:]
        new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") or not text else "")
        warnings = _check_caps_for_write(
            root, rel, len(new_text.encode("utf-8")), creating=False,
        )
        _atomic_write(target, new_text)
        changed = [rel]
        if regenerate_index(root):
            changed.append(INDEX_FILENAME)
        return OpResult(
            output=f"The file {rel} has been edited.",
            warnings=warnings, changed=changed,
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
            target.unlink()
        elif target.is_dir():
            deleted = [
                (Path(rel) / f.relative_to(target)).as_posix()
                for f in target.rglob("*") if f.is_file() and not f.is_symlink()
            ]
            shutil.rmtree(target)
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
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            # Enumerate per-file so git + fan-out + tombstones stay per-path.
            moved = [
                f.relative_to(src).as_posix()
                for f in src.rglob("*") if f.is_file() and not f.is_symlink()
            ]
            src.rename(dst)
            changed = [(Path(new_rel) / m).as_posix() for m in moved]
            deleted = [(Path(old_rel) / m).as_posix() for m in moved]
        else:
            src.rename(dst)
            changed = [new_rel]
            deleted = [old_rel]
        if regenerate_index(root):
            changed.append(INDEX_FILENAME)
        return OpResult(
            output=f"Successfully renamed {old_rel} to {new_rel}",
            changed=changed, deleted=deleted,
        )
