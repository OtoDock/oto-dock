"""Direct-LLM builtin file tools: the virtual-path resolver + text file ops.

In-proxy tools have no bubblewrap under them, so the resolver and the
opens below it ARE the boundary. The resolver consumes
``SandboxBuilder.workspace_mount_table`` (the same ordered ``Mount`` list
bwrap renders for CLI / MCP processes) and refuses what the kernel would
refuse: paths outside every mount, writes under a read-only mount, symlink
escapes (a write never goes through a link at any component; a read judges
an in-mount link by its target), and the runtime / secret subtrees
(``.credentials`` / ``.claude`` / ``.codex`` / ``.git``) the CLI hook keeps
closed as well. Every open then runs beneath the mount root through
``safe_fs``, so what the resolver admitted is what is read or written. The role / RBAC / library rules stay in
``auth.path_policy.check_tool_access`` (Pass-1 of the builtin gate in
``builtins.py``); this module only answers "where on disk, and may I write
there", then performs the operation.

Virtual paths are the sandbox form every prompt uses (``/workspace/…``,
``/knowledge/…``, ``/users/<u>/…``, ``/config/…``); a relative path resolves
against the session cwd (``/users/<u>`` for a user mount, ``/workspace`` for
the agent scope — ``SandboxBuilder.get_cwd``).
"""

from __future__ import annotations

import errno
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from core.sandbox.sandbox import Mount, SandboxBuilder, SandboxConfig

# Path components no tool may traverse: MCP OAuth tokens, the CLI state dirs
# (hooks + settings live there), and git internals. Mirrors the sandbox's own
# protected-destination list + the path-policy credential gate.
REFUSED_COMPONENTS = frozenset({".credentials", ".claude", ".codex", ".git"})
# Directories Glob never descends into (runtime noise, never workspace content).
GLOB_SKIP_DIRS = REFUSED_COMPONENTS | frozenset({
    "node_modules", "__pycache__", ".venv", "venv", ".quarantine", ".uv-python",
})

READ_DEFAULT_LINES = 2000
READ_MAX_BYTES = 256 * 1024
WRITE_MAX_BYTES = 1024 * 1024
GLOB_MAX_ENTRIES = 200


class FileToolError(Exception):
    """A refusal or failure whose message goes back to the model verbatim."""


@dataclass(frozen=True)
class Resolved:
    virtual: str    # normalized virtual path
    host: Path      # host path (may not exist yet for a write)
    mount: Mount    # the mount that admitted it


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def mount_table(cfg: SandboxConfig) -> list[Mount]:
    """The session's mount decisions — the same list bwrap renders."""
    return SandboxBuilder(cfg).workspace_mount_table()


def session_cwd(cfg: SandboxConfig) -> str:
    return SandboxBuilder(cfg).get_cwd()


def has_writable_mount(mounts: list[Mount]) -> bool:
    """Whether any TOOL-REACHABLE mount is writable: the CLI state dirs the
    table stacks RW on a read-only root (a Shared-only viewer's
    ``/workspace/.claude``) are refused above, so they do not make a
    session writable for the person."""
    return any(
        m.rw and not any(p in REFUSED_COMPONENTS for p in m.sandbox.split("/"))
        for m in mounts
    )


def normalize_virtual(raw: str, cwd: str) -> str:
    """Absolute, ``..``-collapsed virtual path (relative → against ``cwd``)."""
    raw = (raw or "").strip()
    if not raw:
        raise FileToolError("file_path is required")
    if raw.startswith("~"):
        raise FileToolError(
            "'~' is not a session folder here — use /workspace, /knowledge, "
            "/users/<you>, /caller or /config"
        )
    if not raw.startswith("/"):
        raw = posixpath.join(cwd, raw)
    norm = posixpath.normpath(raw)
    if not norm.startswith("/"):
        raise FileToolError(f"cannot resolve path: {raw}")
    return norm


def _matching_mount(mounts: list[Mount], vpath: str) -> Mount | None:
    """Longest ``sandbox`` prefix wins; on a tie the LATER entry (bwrap's
    later-bind precedence — the RO root + RW subdir stacking)."""
    best: Mount | None = None
    for m in mounts:
        dest = m.sandbox.rstrip("/") or "/"
        if vpath == dest or vpath.startswith(dest + "/"):
            if best is None or len(dest) >= len(best.sandbox.rstrip("/") or "/"):
                best = m
    return best


def _check_no_symlink_escape(host: Path, host_root: Path) -> None:
    """The deepest EXISTING ancestor of ``host`` (bounded by the mount root)
    must resolve inside the real mount root — a symlinked component would
    otherwise carry the operation outside the session's tree."""
    try:
        root_real = host_root.resolve()
    except OSError as e:
        raise FileToolError(f"cannot resolve the session folder: {e}") from e
    probe = host
    while not probe.exists():
        if probe == host_root or host_root not in probe.parents:
            break
        probe = probe.parent
    try:
        real = probe.resolve()
    except OSError as e:
        raise FileToolError(f"cannot resolve path: {e}") from e
    if not (real == root_real or real.is_relative_to(root_real)):
        raise FileToolError("path leaves the session folder (symlink) — refused")


def resolve(
    mounts: list[Mount], raw: str, *, cwd: str, writing: bool,
) -> Resolved:
    """Map a virtual path onto the host through the mount table.

    Raises :class:`FileToolError` with the reason the model should see.
    """
    vpath = normalize_virtual(raw, cwd)
    parts = [p for p in vpath.split("/") if p]
    hit = next((p for p in parts if p in REFUSED_COMPONENTS), None)
    if hit:
        raise FileToolError(f"{vpath}: the {hit} subtree is not accessible to tools")
    mount = _matching_mount(mounts, vpath)
    if mount is None:
        roots = ", ".join(sorted({m.sandbox for m in mounts})) or "(none)"
        raise FileToolError(
            f"{vpath} is not inside a session folder (available: {roots})"
        )
    if writing and not mount.rw:
        if not has_writable_mount(mounts):
            raise FileToolError(
                "no writable location in this session — every folder is read-only"
            )
        raise FileToolError(f"{vpath} is read-only in this session")
    dest = mount.sandbox.rstrip("/") or "/"
    rel = vpath[len(dest):].lstrip("/")
    host_root = Path(mount.host)
    host = host_root / rel if rel else host_root
    if writing:
        # The mount table trusts its host paths by name (bwrap binds the
        # same strings), and production builds them from the real agents
        # dir: a mount root that resolves elsewhere was swapped underneath
        # the session, so nothing is written through it.
        if os.path.realpath(host_root) != str(host_root):
            raise FileToolError("the session folder moved (symlink); refused")
        # A link at the leaf, dangling or not, is never written through.
        if host.is_symlink():
            raise FileToolError(f"{vpath} is a symlink; write the real path instead")
    _check_no_symlink_escape(host, host_root)
    return Resolved(virtual=vpath, host=host, mount=mount)


# ---------------------------------------------------------------------------
# Operations (host-side; the resolver has already admitted the path)
# ---------------------------------------------------------------------------

def _fs_reason(e: OSError) -> str:
    # The layer packages import no service at module level (the import-
    # direction rule); safe_fs is reached function-locally.
    from services.infra import safe_fs
    if isinstance(e, safe_fs.SymlinkRefused):
        return "a symlink is in the path; use the real path instead"
    if isinstance(e, safe_fs.SafeFsError):
        return e.strerror or "refused"
    if e.errno in (errno.EDQUOT, errno.ENOSPC):
        return "not enough storage in the agent's bucket for this write"
    if e.errno == errno.EACCES or e.errno == errno.EROFS:
        return "the file system refused the write (read-only or no permission)"
    return e.strerror or str(e)


def _mount_rel(res: Resolved) -> tuple[Path, str]:
    """``(mount root, rel)`` of a resolved path for the strict opens below."""
    from services.infra import safe_fs
    root = Path(res.mount.host)
    return root, safe_fs.rel_under(res.host, root)


def _read_beneath(res: Resolved, limit: int) -> bytes:
    """Up to ``limit`` bytes of the file behind ``res``, judged where it
    lands (an in-mount alias reads its target) and opened with no symlink
    followed, so a swap after the resolve is refused at the open."""
    from services.infra import safe_fs
    root, rel = _mount_rel(res)
    try:
        canonical = safe_fs.canonical_rel(root, rel)
    except safe_fs.EscapeRefused:
        raise FileToolError("path leaves the session folder (symlink); refused")
    fd, _st = safe_fs.open_regular_for_read(root, canonical)
    with os.fdopen(fd, "rb") as fh:
        return fh.read(limit)


def _write_beneath(res: Resolved, payload: bytes) -> None:
    """Create or overwrite the file behind ``res`` in place (parents
    created), never through a link at any component."""
    from services.infra import safe_fs
    root, rel = _mount_rel(res)
    parent = posixpath.dirname(rel)
    if parent:
        safe_fs.mkdirs_beneath(root, parent)
    fd = safe_fs.open_beneath(root, rel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)


def read_numbered(res: Resolved, offset: int | None = None, limit: int | None = None) -> str:
    """``cat -n`` style text read — the CLI Read tool's shape (1-based
    ``offset`` line, ``limit`` lines, default 2000; 256 KB byte cap; binary
    refused)."""
    host = res.host
    if not host.exists():
        raise FileToolError(f"File not found: {res.virtual}")
    if host.is_dir():
        raise FileToolError(f"{res.virtual} is a folder — use Glob to list it")
    try:
        data = _read_beneath(res, READ_MAX_BYTES + 1)
    except OSError as e:
        raise FileToolError(f"cannot read {res.virtual}: {_fs_reason(e)}") from e
    truncated = len(data) > READ_MAX_BYTES
    data = data[:READ_MAX_BYTES]
    if b"\x00" in data[:8192]:
        raise FileToolError(
            f"{res.virtual} is a binary file — Read returns text only "
            "(documents and images go through the file-tools MCP)"
        )
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return "(empty file)"
    start = offset if isinstance(offset, int) and offset > 0 else 1
    count = limit if isinstance(limit, int) and limit > 0 else READ_DEFAULT_LINES
    window = lines[start - 1:start - 1 + count]
    if not window:
        return f"(no lines at offset {start} — the file has {len(lines)} lines)"
    out = "\n".join(f"{start + i:6d}\t{line}" for i, line in enumerate(window))
    remaining = len(lines) - (start - 1 + len(window))
    if remaining > 0:
        out += f"\n... ({remaining} more lines — continue with offset={start + len(window)})"
    if truncated:
        out += f"\n... (read stopped at {READ_MAX_BYTES // 1024} KB)"
    return out


def glob_paths(res_dir: Resolved, pattern: str) -> list[str]:
    """Files under ``res_dir`` whose mount-relative path matches ``pattern``
    (``**`` supported), as virtual paths, sorted, capped at
    ``GLOB_MAX_ENTRIES``. Runtime dirs and symlinks that leave the mount are
    skipped."""
    pattern = (pattern or "").strip()
    if not pattern:
        raise FileToolError("pattern is required (e.g. **/*.md)")
    if pattern.startswith("/") or ".." in pattern.split("/"):
        raise FileToolError("pattern must be relative to the folder (e.g. **/*.md)")
    if not res_dir.host.is_dir():
        raise FileToolError(f"{res_dir.virtual} is not a folder")
    try:
        root_real = Path(res_dir.mount.host).resolve()
    except OSError as e:
        raise FileToolError(f"cannot resolve the session folder: {e}") from e
    base = res_dir.host
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in GLOB_SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(base)
            if not PurePosixPath(rel.as_posix()).full_match(pattern):
                continue
            try:
                real = p.resolve()
            except OSError:
                continue
            if not (real == root_real or real.is_relative_to(root_real)):
                continue
            out.append(posixpath.join(res_dir.virtual, rel.as_posix()))
            if len(out) >= GLOB_MAX_ENTRIES:
                return sorted(out)
    return sorted(out)


def write_text(res: Resolved, content: str) -> int:
    """Create or overwrite a text file (parents created). Returns bytes written."""
    if not isinstance(content, str):
        raise FileToolError("content must be a string")
    payload = content.encode("utf-8")
    if len(payload) > WRITE_MAX_BYTES:
        raise FileToolError(
            f"content is {len(payload) // 1024} KB — Write accepts up to "
            f"{WRITE_MAX_BYTES // 1024} KB (use the file-tools MCP for larger documents)"
        )
    if res.host.is_dir():
        raise FileToolError(f"{res.virtual} is a folder")
    try:
        _write_beneath(res, payload)
    except OSError as e:
        raise FileToolError(f"cannot write {res.virtual}: {_fs_reason(e)}") from e
    return len(payload)


def edit_text(res: Resolved, old: str, new: str, replace_all: bool = False) -> int:
    """Exact-string replacement; ``old`` must be unique unless ``replace_all``.
    Returns the number of replacements."""
    if not isinstance(old, str) or not old:
        raise FileToolError("old_string is required")
    if not isinstance(new, str):
        raise FileToolError("new_string must be a string")
    if old == new:
        raise FileToolError("old_string and new_string are identical — nothing to change")
    if not res.host.exists():
        raise FileToolError(f"File not found: {res.virtual}")
    if res.host.is_dir():
        raise FileToolError(f"{res.virtual} is a folder")
    from services.infra import safe_fs
    try:
        root, rel = _mount_rel(res)
        data = safe_fs.read_bytes_beneath(root, rel, max_size=WRITE_MAX_BYTES)
    except safe_fs.FileTooLarge:
        raise FileToolError(
            f"{res.virtual} is larger than {WRITE_MAX_BYTES // 1024} KB — Edit works on text files up to that size"
        )
    except OSError as e:
        raise FileToolError(f"cannot read {res.virtual}: {_fs_reason(e)}") from e
    if b"\x00" in data[:8192]:
        raise FileToolError(f"{res.virtual} is a binary file — Edit works on text files")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise FileToolError(f"{res.virtual} is not valid UTF-8 text") from e
    n = text.count(old)
    if n == 0:
        raise FileToolError(f"old_string was not found in {res.virtual}")
    if n > 1 and not replace_all:
        raise FileToolError(
            f"old_string appears {n} times in {res.virtual} — add surrounding "
            "context to make it unique, or set replace_all"
        )
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        _write_beneath(res, updated.encode("utf-8"))
    except OSError as e:
        raise FileToolError(f"cannot write {res.virtual}: {_fs_reason(e)}") from e
    return n if replace_all else 1
