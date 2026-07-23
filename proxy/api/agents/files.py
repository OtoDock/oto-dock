"""Agent workspace file operations.

File-tree listing, read/write/create/mkdir, delete/rename/move/copy,
zip + zip-url downloads, and the recover-bin (soft-delete) endpoints,
with the role/OAuth path-permission helpers they share. Attaches to
the shared package router."""

import asyncio
import io
import logging
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import config
from auth.providers import UserContext, get_current_user, require_agent_access, require_auth
from storage import database as task_store

from api.agents._common import _get_agent_dir
from api.agents._router import router

logger = logging.getLogger("claude-proxy.agents")


TEXT_EXTENSIONS = {
    ".md", ".json", ".txt", ".py", ".yaml", ".yml", ".sh",
    ".conf", ".cfg", ".ini", ".toml", ".env", ".log",
}


IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


SKIP_DIRS = {"__pycache__", "venv", "node_modules"}


def _check_path_traversal(resolved: Path, agent_dir: Path) -> None:
    """Raise 403 if the resolved path escapes the agent directory."""
    if not resolved.is_relative_to(agent_dir):
        raise HTTPException(
            status_code=403, detail="Path traversal not allowed"
        )


def _fs_error_reason(e: OSError | shutil.Error) -> str:
    """Client-safe reason for a failed file operation — raw OSError text
    embeds server-side absolute paths, so return only the errno description."""
    if isinstance(e, OSError) and e.strerror:
        return e.strerror
    return "file operation failed (see proxy logs)"


def _build_tree(directory: Path, base: Path, depth: int, max_depth: int) -> list[dict]:
    """Recursively build a directory tree structure.

    Excludes:
      * Hidden entries (`.foo`)
      * `SKIP_DIRS` (node_modules, venv, etc.)
      * **Protected OAuth credentials_dir subpaths** — e.g. `google-tokens/`
        for workspace-mcp. Manifest-driven via
        ``mcp_registry.get_protected_credentials_subpaths()``. Filtered
        for EVERY role (including admin) because raw OAuth tokens have
        no UX value in the file tree — the OAuth connect/disconnect UI
        is the intended management surface.
    """
    if depth > max_depth:
        return []

    try:
        entries = list(directory.iterdir())
    except PermissionError:
        return []

    # Look up the protected credentials_dir subpath set ONCE per call
    # (it's a frozenset of a few strings; cost is negligible). Doing it
    # here keeps the lookup current with any manifest reload.
    from services.mcp import mcp_registry
    protected_subpaths = mcp_registry.get_protected_credentials_subpaths()

    # Separate dirs and files, filter hidden and skip dirs
    dirs = []
    files = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            if entry.name in SKIP_DIRS:
                continue
            if entry.name in protected_subpaths:
                continue
            dirs.append(entry)
        elif entry.is_file():
            files.append(entry)

    # Sort alphabetically
    dirs.sort(key=lambda p: p.name)
    files.sort(key=lambda p: p.name)

    result = []

    # Dirs first
    for d in dirs:
        rel = d.relative_to(base)
        stat = d.stat()
        node = {
            "name": d.name,
            "type": "dir",
            "path": str(rel),
            "size": 0,
            "modified": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
            "children": _build_tree(d, base, depth + 1, max_depth),
        }
        result.append(node)

    # Then files
    for f in files:
        rel = f.relative_to(base)
        stat = f.stat()
        node = {
            "name": f.name,
            "type": "file",
            "path": str(rel),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
            "children": [],
        }
        result.append(node)

    return result


def _filter_tree(nodes: list[dict], role: str, username: str = "") -> list[dict]:
    """Filter file tree based on role permissions (3-tier model).

    - Viewer: sees knowledge/, workspace/, own users/{username}/.
      Can WRITE only own users/{username}/.
    - Editor: sees knowledge/, workspace/, own users/{username}/.
      Can WRITE workspace + own users/{username}/; knowledge is read-only.
    - Manager (= owner): all four subtrees including config/, full RW.
    - Admin: owner-tier view — config/ visible, users/ filtered to their
      OWN folder like everyone else. Other users' personal dirs are
      private-by-default even from admins in the browsing UI; targeted
      admin cross-user path access (recover-bin restores) stays available
      via _check_file_role.

    `/config/` is OWNER-only (admin + manager) — editor + viewer don't
    see it at all (no tree entry, no read, no write). Config shapes
    agent behavior — owner curation, not workspace collaboration.
    """
    result = []
    # Owner-tier (manager + admin) sees config too. Editor + viewer
    # omit it entirely.
    owner_tier = role in ("manager", "admin")
    for node in nodes:
        if node["type"] == "dir" and node["name"] == "users" and node.get("children"):
            # Show users/ but filter to own username only
            filtered = node.copy()
            filtered["children"] = [
                c for c in node["children"]
                if c["type"] == "dir" and c["name"] == username
            ] if username else []
            if filtered["children"]:
                result.append(filtered)
        elif node["name"] == "config":
            if owner_tier:
                result.append(node)
            # else: hidden from editor + viewer
        elif node["name"] in ("workspace", "knowledge"):
            # All non-admin user roles see these two top-level subtrees.
            # Write access is decided per-role in _check_file_role.
            result.append(node)
    return result


def _check_oauth_protected(*paths: str) -> None:
    """Refuse the request if ANY of the supplied paths references a
    registered ``credentials_dir`` subpath.

    Manifest-driven (``mcp_registry.get_protected_credentials_subpaths``).
    Fires for EVERY role (even admin) because the OAuth connect/disconnect
    UI is the intended management surface; raw token JSON has no UX value
    and exposing it via the file API enables exfiltration.

    Accepts agent-relative paths (``workspace/google-tokens/x.json``,
    ``users/alice/google-tokens/y.json``) — splits on '/' to inspect
    each segment. Empty paths and the agent root are no-ops.

    Raises ``HTTPException(403)`` on a match.
    """
    from services import path_roles
    for p in paths:
        if not p:
            continue
        if path_roles.is_protected_credentials_path(p):
            raise HTTPException(
                status_code=403,
                detail=(
                    "OAuth credentials are protected. "
                    "Manage accounts via Settings → Integrations."
                ),
            )


def _check_file_role(path: str, role: str, writing: bool = False, username: str = "") -> None:
    """Enforce role-based file access restrictions (3-tier model).

    Read access:
      - Viewer / Editor: knowledge/, workspace/, own users/{username}/.
        NO access to config/ — it's owner-only.
      - Manager: knowledge/, workspace/, config/, own users/{username}/.
      - Admin: full access (other users too).

    Write access:
      - Viewer: own users/{username}/ only.
      - Editor: own users/{username}/ + workspace/.
      - Manager: own users/{username}/ + workspace/ + config/ + knowledge/.
      - Admin: full access.

    OAuth credential dirs (manifest-driven, see
    ``mcp_registry.get_protected_credentials_subpaths``) are checked
    FIRST and rejected for every role (including admin) — raw token JSON
    is not surfaceable via the file API.
    """
    # OAuth credentials gate — universal (even admin). Must come before
    # the admin shortcut below.
    _check_oauth_protected(path)

    if role == "admin":
        return

    def _in_scope(p: str, scope: str) -> bool:
        return p == scope or p.startswith(scope + "/")

    # Determine which scopes this role is allowed to READ from.
    # Viewer + editor can read workspace + knowledge + own user dir;
    # manager can additionally read /config/. Config is owner-only.
    own_user_scope = f"users/{username}" if username else ""
    owner_tier = role == "manager"  # admin already returned above
    read_allowed = (
        (own_user_scope and _in_scope(path, own_user_scope))
        or _in_scope(path, "knowledge")
        or _in_scope(path, "workspace")
        or (owner_tier and _in_scope(path, "config"))
    )
    if not read_allowed:
        if _in_scope(path, "config"):
            raise HTTPException(
                status_code=403,
                detail="Agent config is owner-only and not accessible to editors or viewers",
            )
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed scope")

    if not writing:
        return

    # Write tier checks
    if role == "viewer":
        if not own_user_scope or not _in_scope(path, own_user_scope):
            raise HTTPException(
                status_code=403,
                detail="Viewers can write only to their own user directory",
            )
    elif role == "editor":
        # Editor can write own user dir + workspace/. Knowledge is owner-curated.
        if (
            (own_user_scope and _in_scope(path, own_user_scope))
            or _in_scope(path, "workspace")
        ):
            return
        raise HTTPException(
            status_code=403,
            detail="Editors cannot modify agent knowledge (owner-only)",
        )
    elif role == "manager":
        # Manager can write own user dir + workspace/ + config/ + knowledge/.
        if (
            (own_user_scope and _in_scope(path, own_user_scope))
            or _in_scope(path, "config")
            or _in_scope(path, "knowledge")
            or _in_scope(path, "workspace")
        ):
            return
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed scope")


def safe_agent_path(
    agent_dir: Path, name: str, raw_path: str, user: UserContext, *, writing: bool = False,
) -> tuple[Path, str]:
    """Resolve a user-supplied agent-relative path to a safe absolute Path and
    authorize the RESOLVED location against the caller's role.

    Canonicalize first (reject NUL / '.' / '..', then follow symlinks via
    ``resolve()``), confine to the agent tree, and only THEN run the role check
    — on the post-resolution agent-relative path. Authorizing the resolved path
    (not the raw one) is what defeats both ``..`` traversal and a symlink that
    escapes the caller's scope (e.g. ``workspace/link -> ../config``): the role
    check sees the real target, not the scope the caller named. OAuth credential
    dirs are denied for every principal.

    A real user (dashboard cookie / USER_SESSION) is gated by its per-agent
    role; a SERVICE / AGENT_SESSION caller gets full access to the single agent
    it acts on (file work is inherent to running that agent). Returns
    ``(resolved_path, username)`` — username is "" for non-user principals.

    Raises HTTPException(400/403) on a bad or out-of-scope path.
    """
    if "\x00" in raw_path:
        raise HTTPException(status_code=400, detail="Invalid path")
    norm = _normalize_path(raw_path)  # rejects empty / '.' / '..' segments
    agent_root = agent_dir.resolve()
    resolved = (agent_root / norm).resolve()
    if not resolved.is_relative_to(agent_root):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    rel = resolved.relative_to(agent_root).as_posix()
    _check_oauth_protected(rel)  # OAuth token dirs are off-limits to EVERY principal
    uname = ""
    if user.acting_sub is not None:
        uname = task_store.get_username_by_sub(user.sub) or ""
        _check_file_role(rel, user.get_agent_role(name), writing=writing, username=uname)
    return resolved, uname


async def _record_platform_write(agent_slug: str, rel_path: str, writer: str | None) -> None:
    """Versioned-sync bookkeeping for a platform-side write: retire any tombstone
    (the path is live again) and record the author (username slug) for cross-user
    conflict attribution. Best-effort; runs regardless of remote targets."""
    from storage import file_tombstones_store, file_author_store
    await asyncio.to_thread(file_tombstones_store.drop, agent_slug, rel_path)
    if writer:
        await asyncio.to_thread(file_author_store.record, agent_slug, rel_path, writer)


async def _tombstone_path(agent_slug: str, rel_path: str) -> None:
    """Record a delete tombstone + forget the author for one platform file path,
    so an idle satellite APPLIES the delete (never resurrects it) at next sync."""
    import time as _t
    from storage import file_tombstones_store, file_author_store
    await asyncio.to_thread(
        file_tombstones_store.record, agent_slug, rel_path, _t.time(), origin="dashboard",
    )
    await asyncio.to_thread(file_author_store.clear, agent_slug, rel_path)


async def _tombstone_subtree(agent_slug: str, agent_dir: "Path", src: "Path") -> None:
    """Tombstone every file under ``src`` (a file or dir) BEFORE it is deleted /
    moved / renamed on disk — so an idle satellite removes the old path(s) instead
    of resurrecting them. Per-file (a directory has no file hash to key on)."""
    base = agent_dir.resolve()
    if src.is_file():
        files = [src]
    elif src.is_dir():
        files = [f for f in src.rglob("*") if f.is_file() and not f.is_symlink()]
    else:
        return
    for f in files:
        try:
            rel = f.resolve().relative_to(base).as_posix()
        except (OSError, ValueError):
            continue
        await _tombstone_path(agent_slug, rel)


def _dashboard_writer(u) -> str | None:
    """The username slug to record as ``file_author`` for a dashboard write, or
    None for an API-key / agent-scope write (no human identity)."""
    if getattr(u, "is_api_key", False):
        return None
    from storage import database as task_store
    return task_store.get_username_by_sub(u.sub) or None


async def _push_file_write_to_remote(
    agent_slug: str, rel_path: str, host_path: "Path", *, writer: str | None = None,
) -> None:
    """Publish a written/created FILE: record platform authorship + retire any
    tombstone, then push to active remote sessions so a dashboard edit reaches the
    satellite immediately — not only at the next end-of-turn manifest sync.

    Routes the push through ``services/remote/workspace_fanout`` so the SAME per-user /
    per-role isolation that gates session-start sync applies here too: a write
    under ``users/{alice}/`` or ``config/`` only reaches machines whose active
    session is allowed to see it. The author/tombstone bookkeeping runs even when
    no remote session is active (it's platform state, not a push)."""
    await _record_platform_write(agent_slug, rel_path, writer)
    from services.remote import workspace_fanout
    if not workspace_fanout.has_fanout_candidates(agent_slug, rel_path, include_idle=True):
        return
    try:
        content = host_path.read_bytes()
    except OSError as e:
        logger.warning("Cannot read %s for satellite push: %s", host_path, e)
        return
    await workspace_fanout.fan_out_write(agent_slug, rel_path, content, include_idle=True)


async def _push_file_delete_to_remote(agent_slug: str, rel_path: str) -> None:
    """Push a delete (file or dir) to active remote sessions of this agent, via
    the isolation-aware fan-out (reaches only allowed machines). The delete
    tombstone is written separately at the delete source (per file)."""
    from services.remote import workspace_fanout
    await workspace_fanout.fan_out_delete(agent_slug, rel_path, include_idle=True)


async def _push_tree_write_to_remote(
    agent_slug: str, root: "Path", agent_dir: "Path", *, writer: str | None = None,
) -> None:
    """Publish a written FILE — or every file under a moved/copied DIRECTORY: record
    platform authorship + retire any tombstone per file, then fan out to active
    remote sessions so a dashboard move/copy reaches the satellite immediately
    instead of only at the next manifest sync. Each file is fanned out with
    per-file isolation; the disk read happens only when a file has an allowed
    target. Best-effort."""
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = [f for f in root.rglob("*") if f.is_file()]
    else:
        return
    from services.remote import workspace_fanout
    base = agent_dir.resolve()
    for f in files:
        try:
            rel = f.relative_to(base).as_posix()
        except ValueError:
            continue
        await _record_platform_write(agent_slug, rel, writer)
        if not workspace_fanout.has_fanout_candidates(agent_slug, rel, include_idle=True):
            continue
        try:
            content = f.read_bytes()
        except OSError as e:
            logger.warning("Cannot read %s for satellite push: %s", f, e)
            continue
        await workspace_fanout.fan_out_write(agent_slug, rel, content, include_idle=True)


def _scope_root(path: str) -> str:
    """Return the top-level scope segment of an agent-relative path.

    Used to enforce that recursive operations don't cross scope boundaries
    (e.g. a manager recursive-deleting `users/` would otherwise wipe every
    user's dir). Scopes: `config`, `workspace`, `users/<username>`.
    """
    parts = path.strip("/").split("/")
    if not parts or not parts[0]:
        return ""
    if parts[0] == "users":
        return f"users/{parts[1]}" if len(parts) > 1 else "users"
    return parts[0]


def _resolve_conflict(target: Path) -> Path:
    """Return `target` if free, else append `_1`, `_2`, ... before the suffix.

    Mirrors `api.media.uploads._resolve_conflict`. Works for both files (where
    `.suffix` is the extension) and directories (where `.suffix` is empty
    and the bare stem gets the numeric suffix). Caps at 99 attempts to
    avoid pathological loops.
    """
    if not target.exists():
        return target
    stem = target.stem
    ext = target.suffix
    parent = target.parent
    for i in range(1, 100):
        candidate = parent / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=409, detail="Too many name conflicts in destination")


def _assert_no_symlink_escape(root: Path, scope_root: Path) -> None:
    """Walk `root`'s subtree; raise 403 if any entry resolves outside `scope_root`.

    Mirrors the recursive-delete safeguard so move/copy/zip can't be used to
    pull data across a scope boundary via a malicious symlink. Pure-filesystem
    check — no DB / no auth.
    """
    if root.is_file():
        candidates = [root]
    else:
        candidates = [root] + list(root.rglob("*"))
    for child in candidates:
        try:
            resolved = child.resolve()
        except (OSError, RuntimeError):
            raise HTTPException(
                status_code=400, detail="Cannot resolve a path in the subtree",
            )
        if not resolved.is_relative_to(scope_root):
            raise HTTPException(
                status_code=403,
                detail="Subtree contains a symlink escaping the scope",
            )


def _normalize_path(p: str) -> str:
    """Strip leading/trailing slashes; reject empty / `.` / `..` segments."""
    norm = p.strip("/")
    if not norm:
        raise HTTPException(status_code=400, detail="Empty path")
    parts = norm.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise HTTPException(status_code=400, detail=f"Invalid path: {p}")
    return norm


def _resolve_user_session_info(u: UserContext, agent: str) -> tuple[str, str]:
    """Return (role, username) for the calling session, used by file-role checks."""
    from storage import database as task_store
    role = u.get_agent_role(agent)
    username = task_store.get_username_by_sub(u.sub) or ""
    return role, username


def _validate_op_paths(
    src_paths: list[str],
    dest_dir: str,
    *,
    agent_dir: Path,
    role: str,
    username: str,
    writing_on_source: bool,
) -> tuple[list[tuple[str, Path]], Path]:
    """Validate src_paths + dest_dir for a move/copy op.

    Cross-scope ops are explicitly allowed by the API contract — each source
    is validated against its own role-scope, dest against its own. Returns
    the normalized + resolved sources alongside the resolved dest Path so
    the caller can pass them to `_assert_no_symlink_escape`.
    """
    if not src_paths:
        raise HTTPException(status_code=400, detail="src_paths cannot be empty")

    # Destination first — exists, is a dir, writable for the role.
    dest_norm = _normalize_path(dest_dir)
    _check_file_role(dest_norm, role, writing=True, username=username)
    dest_resolved = (agent_dir / dest_norm).resolve()
    _check_path_traversal(dest_resolved, agent_dir)
    if not dest_resolved.exists():
        raise HTTPException(status_code=404, detail=f"Destination not found: {dest_dir}")
    if not dest_resolved.is_dir():
        raise HTTPException(status_code=400, detail="Destination must be a directory")

    # Each source: normalize, role check (read for copy, write for move via flag),
    # resolve, traversal check, exists, no-loop (dest not inside source).
    resolved: list[tuple[str, Path]] = []
    for raw in src_paths:
        norm = _normalize_path(raw)
        _check_file_role(norm, role, writing=writing_on_source, username=username)
        src_resolved = (agent_dir / norm).resolve()
        _check_path_traversal(src_resolved, agent_dir)
        if not src_resolved.exists():
            raise HTTPException(status_code=404, detail=f"Source not found: {raw}")
        # Loop guard: dest must not equal or be inside any source.
        if dest_resolved == src_resolved or dest_resolved.is_relative_to(src_resolved):
            raise HTTPException(
                status_code=400,
                detail=f"Destination is inside source path: {raw}",
            )
        resolved.append((norm, src_resolved))

    return resolved, dest_resolved


def _build_zip_response(
    name: str,
    paths: list[str],
    role: str,
    username: str,
) -> StreamingResponse:
    """Validate paths + build the zip archive + return a streaming response.

    Shared by `POST /v1/agents/{name}/zip` (browser path) and
    `GET /v1/agents/{name}/zip-download` (Android-friendly token flow).
    All path-traversal / role / symlink checks happen here.
    """
    if not paths:
        raise HTTPException(status_code=400, detail="paths cannot be empty")

    agent_dir = _get_agent_dir(name)
    resolved_sources: list[tuple[str, Path]] = []
    for raw in paths:
        norm = _normalize_path(raw)
        _check_file_role(norm, role, writing=False, username=username)
        src_resolved = (agent_dir / norm).resolve()
        _check_path_traversal(src_resolved, agent_dir)
        if not src_resolved.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {raw}")
        src_scope = _scope_root(norm)
        if not src_scope:
            raise HTTPException(status_code=400, detail=f"Invalid scope for path: {raw}")
        scope_root = (agent_dir / src_scope).resolve()
        _assert_no_symlink_escape(src_resolved, scope_root)
        resolved_sources.append((norm, src_resolved))

    buf = io.BytesIO()
    used_arcnames: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for _src_norm, src_resolved in resolved_sources:
            base = src_resolved.name
            unique_base = base
            i = 1
            while unique_base in used_arcnames:
                stem = Path(base).stem
                suffix = Path(base).suffix
                unique_base = f"{stem}_{i}{suffix}"
                i += 1
            used_arcnames.add(unique_base)
            if src_resolved.is_file():
                zf.write(str(src_resolved), arcname=unique_base)
            else:
                zf.writestr(zipfile.ZipInfo(unique_base + "/"), b"")
                for path in src_resolved.rglob("*"):
                    rel = path.relative_to(src_resolved)
                    arc = f"{unique_base}/{rel.as_posix()}"
                    if path.is_dir():
                        zf.writestr(zipfile.ZipInfo(arc + "/"), b"")
                    elif path.is_file():
                        zf.write(str(path), arcname=arc)
    buf.seek(0)

    if len(resolved_sources) == 1:
        zip_name = f"{Path(resolved_sources[0][1].name).stem or 'archive'}.zip"
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M")
        zip_name = f"workspace-files-{ts}.zip"

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "Content-Length": str(buf.getbuffer().nbytes),
        },
    )


def _create_zip_token(
    agent: str,
    paths: list[str],
    user_sub: str,
    role: str,
    username: str,
) -> str:
    """Mint a short-lived JWT carrying the validated paths + user context.
    Used by the GET /zip-download endpoint to authorize a direct download
    without re-sending the path list in the URL (avoids URL-length limits).
    """
    import jwt as _jwt
    import time as _time
    payload = {
        "agent": agent,
        "paths": paths,
        "user_sub": user_sub,
        "role": role,
        "username": username,
        "exp": int(_time.time()) + 120,  # 2-minute TTL — plenty for click → fetch
    }
    return _jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


# Minimal valid blank Office templates (zip-based, <2KB each)
_BLANK_TEMPLATES: dict[str, bytes] = {}


def _init_blank_templates():
    import io, zipfile
    def _zip(files: dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, content in files.items():
                z.writestr(name, content)
        return buf.getvalue()

    _BLANK_TEMPLATES['.docx'] = _zip({
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        'word/document.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t></w:t></w:r></w:p></w:body></w:document>',
    })
    _BLANK_TEMPLATES['.xlsx'] = _zip({
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>',
    })
    _BLANK_TEMPLATES['.pptx'] = _zip({
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
        'ppt/presentation.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>',
    })


_init_blank_templates()


class WriteFileRequest(BaseModel):
    content: str


class MkdirRequest(BaseModel):
    path: str


class DeleteRequest(BaseModel):
    path: str
    recursive: bool = False


class RenameRequest(BaseModel):
    old_path: str
    new_path: str


class MovePathsRequest(BaseModel):
    src_paths: list[str]
    dest_dir: str


class CopyPathsRequest(BaseModel):
    src_paths: list[str]
    dest_dir: str


class ZipPathsRequest(BaseModel):
    paths: list[str]


class RecoverRestoreRequest(BaseModel):
    entry_ids: list[str]


class CreateFileRequest(BaseModel):
    path: str
    file_type: str = ""  # extension like ".docx", ".xlsx", ".pptx" — if empty, creates text file


@router.get("/v1/agents/{name}/files")
async def list_agent_files(name: str, user: UserContext | None = Depends(get_current_user)):
    """Return a recursive directory tree of the agent's folder."""
    u = require_auth(user)
    require_agent_access(u, name)

    agent_dir = _get_agent_dir(name)
    # max_depth=20 covers virtually every real workspace tree. The previous
    # cap of 5 caused two visible bugs once the workspace UI grew to support
    # cut/copy/paste and drag-to-move: pasting a folder into a path already
    # at depth 4+ left its contents past the cap, so the frontend showed an
    # empty folder while the disk had files — leading to "Directory is not
    # empty" 400s on subsequent delete attempts. If perf ever becomes a
    # concern we should switch to lazy per-folder fetches instead of
    # eagerly shipping a tree this deep.
    tree = _build_tree(agent_dir, agent_dir, depth=1, max_depth=20)
    if not u.is_api_key:
        from storage import database as task_store
        username = task_store.get_username_by_sub(u.sub) or ""
        tree = _filter_tree(tree, u.get_agent_role(name), username=username)
    return {"tree": tree}


@router.get("/v1/agents/{name}/files/{path:path}")
async def read_agent_file(
    name: str,
    path: str,
    download: bool = False,
    user: UserContext | None = Depends(get_current_user),
):
    """Read a file from the agent's directory. Use ?download=true for binary download."""
    u = require_auth(user)
    require_agent_access(u, name)
    agent_dir = _get_agent_dir(name)
    file_path, _ = safe_agent_path(agent_dir, name, path, u, writing=False)

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Binary download mode — any file type
    if download:
        # Detect MIME from filename. Android's DownloadManager uses MIME to
        # decide the file extension when one isn't in Content-Disposition; a
        # blanket `application/octet-stream` makes it save EVERYTHING as
        # `.bin`. `guess_type` returns proper types for `.md`, `.pdf`, etc.,
        # and falls back to octet-stream for truly unknown extensions.
        import mimetypes
        mime, _ = mimetypes.guess_type(file_path.name)
        return FileResponse(
            str(file_path),
            filename=file_path.name,
            media_type=mime or "application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    suffix = file_path.suffix.lower()

    # Image files: return as FileResponse. ``nosniff`` stops the browser from
    # re-interpreting a mistyped image as HTML. SVG is special — it can embed
    # script and execute when opened as a top-level document — so it is NEVER
    # served inline: forcing a filename sets ``Content-Disposition: attachment``
    # (an <img> still renders it; a direct navigation downloads it instead).
    if suffix in IMAGE_MIME:
        headers = {"X-Content-Type-Options": "nosniff"}
        if suffix == ".svg":
            return FileResponse(
                str(file_path), media_type=IMAGE_MIME[suffix],
                filename=file_path.name, headers=headers,
            )
        return FileResponse(str(file_path), media_type=IMAGE_MIME[suffix], headers=headers)

    # Text files
    if suffix in TEXT_EXTENSIONS:
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=400, detail="File is not valid UTF-8 text"
            )
        return {"content": content, "encoding": "utf-8"}

    raise HTTPException(
        status_code=400,
        detail=f"Unsupported file extension: {suffix}",
    )


@router.get("/v1/agents/{name}/recover-bin")
async def list_recover_bin(
    name: str,
    user: UserContext | None = Depends(get_current_user),
):
    """List the recoverable files for this agent that the caller may restore.

    Scope is server-enforced: everyone sees their own ``users/<slug>/``
    entries; editors additionally see shared ``workspace/`` entries; managers
    and admins additionally see ``knowledge/`` / ``config/`` entries. Nobody —
    admins included — sees another user's personal files. Entries expire
    after 7 days.
    """
    u = require_auth(user)
    require_agent_access(u, name)
    from storage import recover_bin_store
    entries = await asyncio.to_thread(
        recover_bin_store.list_for,
        name, u.sub, u.can_edit_agent(name), u.can_manage_agent(name), u.is_admin,
    )
    return {"entries": [
        {
            "entry_id": e["entry_id"],
            "rel_path": e["rel_path"],
            "original_name": e["original_name"],
            "reason": e["reason"],
            "scope": e["scope"],
            "size": e["size"],
            "binned_at": e["binned_at"],
        }
        for e in entries
    ]}


@router.post("/v1/agents/{name}/recover-bin/restore")
async def restore_recover_bin(
    name: str,
    req: RecoverRestoreRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Restore selected recover-bin entries to their original paths.

    Each entry's scope is RE-checked server-side (own user file; editor tier
    for shared workspace; manager/admin for knowledge + config — personal
    files are owner-only, even for admins) — the client's selection is never
    trusted. A
    restored file goes back to its exact original path; if something now
    occupies that path it is written alongside as ``name (recovered).ext``
    (NEVER overwritten, so concurrent work is preserved). Restored files re-sync
    to any satellites. Returns the restored / renamed / denied breakdown.
    """
    u = require_auth(user)
    require_agent_access(u, name)
    from storage import recover_bin_store
    from services.remote import workspace_fanout

    agent_dir = _get_agent_dir(name)
    agent_root = agent_dir.resolve()
    is_edit = u.can_edit_agent(name)
    is_mgr = u.can_manage_agent(name)
    restored: list[dict] = []
    renamed: list[dict] = []
    denied: list[str] = []

    for entry_id in req.entry_ids:
        entry = await asyncio.to_thread(recover_bin_store.get, entry_id)
        if entry is None or entry.get("agent_slug") != name:
            denied.append(entry_id)
            continue
        # Server-enforced tier re-check — never trust the client's selection.
        if not recover_bin_store.can_restore(
            entry, u.sub, is_edit, is_mgr, u.is_admin,
        ):
            denied.append(entry_id)
            continue
        content = await asyncio.to_thread(recover_bin_store.read_bytes, entry)
        if content is None:
            denied.append(entry_id)  # bytes already reaped / lost
            continue

        rel_path = entry["rel_path"]
        dest = (agent_dir / rel_path).resolve()
        try:
            dest.relative_to(agent_root)
        except ValueError:
            denied.append(entry_id)  # traversal guard (defensive)
            continue

        # Restore to the original path, or to a "(recovered)" sibling if
        # something now occupies it — never override existing content.
        final_rel = rel_path
        if dest.exists():
            n = 1
            while True:
                tag = " (recovered)" if n == 1 else f" (recovered {n})"
                cand = dest.with_name(f"{dest.stem}{tag}{dest.suffix}")
                if not cand.exists():
                    break
                n += 1
            dest = cand
            final_rel = dest.relative_to(agent_root).as_posix()
            renamed.append({
                "entry_id": entry_id,
                "original": rel_path,
                "restored_as": final_rel,
            })

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        except OSError:
            logger.exception("recover-bin restore write failed for %s", final_rel)
            denied.append(entry_id)
            continue

        # Re-sync the restored file to any satellites (per-user/role routing;
        # no-ops when the agent has no active machines).
        try:
            await workspace_fanout.fan_out_write(name, final_rel, content)
        except Exception:
            logger.exception("recover-bin restore fan-out failed for %s", final_rel)

        await asyncio.to_thread(recover_bin_store.delete, entry_id)
        restored.append({"entry_id": entry_id, "rel_path": final_rel})

    return {"restored": restored, "renamed": renamed, "denied": denied}


@router.post("/v1/agents/{name}/recover-bin/discard")
async def discard_recover_bin(
    name: str,
    req: RecoverRestoreRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Permanently drop selected recover-bin entries WITHOUT restoring them.

    Same per-entry scope re-check as restore (own user file, or manager for a
    shared file, or admin) — a user can only discard what they could restore.
    The captured bytes + row are removed. Returns {discarded, denied}.
    """
    u = require_auth(user)
    require_agent_access(u, name)
    from storage import recover_bin_store

    is_edit = u.can_edit_agent(name)
    is_mgr = u.can_manage_agent(name)
    discarded: list[str] = []
    denied: list[str] = []
    for entry_id in req.entry_ids:
        entry = await asyncio.to_thread(recover_bin_store.get, entry_id)
        if entry is None or entry.get("agent_slug") != name:
            denied.append(entry_id)
            continue
        if not recover_bin_store.can_restore(
            entry, u.sub, is_edit, is_mgr, u.is_admin,
        ):
            denied.append(entry_id)
            continue
        await asyncio.to_thread(recover_bin_store.delete, entry_id)
        discarded.append(entry_id)
    return {"discarded": discarded, "denied": denied}


@router.put("/v1/agents/{name}/files/{path:path}")
async def write_agent_file(
    name: str,
    path: str,
    req: WriteFileRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Write content to a file in the agent's directory."""
    u = require_auth(user)
    require_agent_access(u, name)
    agent_dir = _get_agent_dir(name)
    file_path, uname = safe_agent_path(agent_dir, name, path, u, writing=True)

    # Create parent directories if needed
    file_path.parent.mkdir(parents=True, exist_ok=True)

    file_path.write_text(req.content, encoding="utf-8")
    logger.info(f"Wrote file: {file_path}")

    rel = file_path.relative_to(agent_dir).as_posix()
    await _push_file_write_to_remote(name, rel, file_path, writer=uname or None)
    return {"status": "saved", "path": rel}


@router.post("/v1/agents/{name}/create-file")
async def create_agent_file(
    name: str,
    req: CreateFileRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Create a new file. Text types get empty content; Office types get blank templates."""
    u = require_auth(user)
    require_agent_access(u, name)
    agent_dir = _get_agent_dir(name)
    file_path, _ = safe_agent_path(agent_dir, name, req.path, u, writing=True)

    if file_path.exists():
        raise HTTPException(status_code=409, detail="File already exists")

    file_path.parent.mkdir(parents=True, exist_ok=True)

    ext = req.file_type or file_path.suffix.lower()
    template = _BLANK_TEMPLATES.get(ext)
    if template:
        file_path.write_bytes(template)
    else:
        file_path.write_text("", encoding="utf-8")

    logger.info(f"Created file: {file_path}")
    rel = file_path.relative_to(agent_dir).as_posix()
    await _push_file_write_to_remote(name, rel, file_path, writer=_dashboard_writer(u))
    return {"status": "created", "path": rel}


@router.post("/v1/agents/{name}/mkdir")
async def create_agent_directory(
    name: str,
    req: MkdirRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Create a directory inside the agent's folder."""
    u = require_auth(user)
    require_agent_access(u, name)
    agent_dir = _get_agent_dir(name)
    dir_path, _ = safe_agent_path(agent_dir, name, req.path, u, writing=True)

    dir_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"Created directory: {dir_path}")

    return {"status": "created", "path": str(dir_path.relative_to(agent_dir))}


@router.post("/v1/agents/{name}/delete")
async def delete_agent_path(
    name: str,
    req: DeleteRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Delete a file or directory from the agent's folder.

    With `recursive=true`, removes a non-empty directory and its contents
    after validating that everything stays within one scope (no symlink
    escapes, no cross-scope wipes). Without it, only files and empty dirs
    are removed.
    """
    u = require_auth(user)
    require_agent_access(u, name)
    agent_dir = _get_agent_dir(name)
    target, _ = safe_agent_path(agent_dir, name, req.path, u, writing=True)

    # Prevent deleting the agent root itself
    if target == agent_dir:
        raise HTTPException(status_code=403, detail="Cannot delete agent root directory")

    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    if target.is_file():
        rel = target.relative_to(agent_dir.resolve()).as_posix()
        # Recover-bin: keep a copy of the deleted bytes (best-effort; a manual
        # dashboard delete is voluntary → no notification). Read before unlink.
        try:
            _content = target.read_bytes()
        except OSError:
            _content = b""
        if _content:
            from storage import recover_bin_store
            await asyncio.to_thread(
                recover_bin_store.capture, name, rel, _content, "deleted",
            )
        target.unlink()
        logger.info(f"Deleted file: {target}")
        await _tombstone_path(name, rel)  # idle satellites apply the delete
        await _push_file_delete_to_remote(name, rel)
        return {"status": "deleted", "path": req.path, "type": "file"}

    if target.is_dir():
        if not any(target.iterdir()):
            rel = target.relative_to(agent_dir.resolve()).as_posix()
            target.rmdir()
            logger.info(f"Deleted empty directory: {target}")
            await _push_file_delete_to_remote(name, rel)
            return {"status": "deleted", "path": req.path, "type": "dir"}

        if not req.recursive:
            raise HTTPException(
                status_code=400, detail="Directory is not empty"
            )

        # Recursive delete: never permit wiping a whole scope root
        # (`config/`, `workspace/`, `users/`, `users/<username>/`).
        scope = _scope_root(req.path)
        if not scope or req.path.strip("/") in {scope, "users"}:
            raise HTTPException(
                status_code=403,
                detail="Cannot recursively delete a scope root",
            )

        # Walk the subtree and reject any symlink that escapes the scope.
        scope_root = (agent_dir / scope).resolve()
        for child in target.rglob("*"):
            try:
                resolved = child.resolve()
            except (OSError, RuntimeError):
                raise HTTPException(
                    status_code=400,
                    detail="Cannot resolve a path in the subtree",
                )
            if not resolved.is_relative_to(scope_root):
                raise HTTPException(
                    status_code=403,
                    detail="Subtree contains a symlink escaping the scope",
                )

        rel = target.relative_to(agent_dir.resolve()).as_posix()
        # Recover-bin: back up each file in the subtree before removal so the
        # folder can be restored file-by-file (best-effort; voluntary delete →
        # no notification). Skip symlinks — the loop above already rejected
        # escaping ones, and intra-scope symlinks aren't real content. capture()
        # enforces the size cap internally.
        from storage import recover_bin_store
        _root_resolved = agent_dir.resolve()
        for _child in target.rglob("*"):
            if not _child.is_file() or _child.is_symlink():
                continue
            try:
                _crel = _child.resolve().relative_to(_root_resolved).as_posix()
                _cbytes = _child.read_bytes()
            except (OSError, ValueError):
                continue
            if _cbytes:
                await asyncio.to_thread(
                    recover_bin_store.capture, name, _crel, _cbytes, "deleted",
                )
            # Per-file tombstone so an idle satellite removes each path (a dir has
            # no file hash, so the merge can't key a delete on the folder itself).
            await _tombstone_path(name, _crel)
        shutil.rmtree(target)
        logger.info(f"Recursively deleted directory: {target}")
        await _push_file_delete_to_remote(name, rel)
        return {"status": "deleted", "path": req.path, "type": "dir", "recursive": True}

    raise HTTPException(status_code=400, detail="Unknown path type")


@router.post("/v1/agents/{name}/rename")
async def rename_agent_path(
    name: str,
    req: RenameRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Rename a file or directory within the same parent folder.

    Same-parent only — moves between folders are not allowed here; that
    would let a user shuffle a path across scope boundaries. Use future
    move/copy endpoints for that.
    """
    u = require_auth(user)
    require_agent_access(u, name)

    old_norm = req.old_path.strip("/")
    new_norm = req.new_path.strip("/")
    if not old_norm or not new_norm:
        raise HTTPException(status_code=400, detail="Empty path")

    old_parent = os.path.dirname(old_norm)
    new_parent = os.path.dirname(new_norm)
    if old_parent != new_parent:
        raise HTTPException(
            status_code=400,
            detail="Rename must keep the same parent directory",
        )
    new_name = os.path.basename(new_norm)
    if not new_name or new_name in (".", "..") or "/" in new_name or "\\" in new_name:
        raise HTTPException(status_code=400, detail="Invalid new name")

    # Authorize + resolve each side against its POST-resolution path (role is
    # checked on the resolved location, defeating symlink scope-escape).
    agent_dir = _get_agent_dir(name)
    old_path, _ = safe_agent_path(agent_dir, name, old_norm, u, writing=True)
    new_path, _ = safe_agent_path(agent_dir, name, new_norm, u, writing=True)

    if not old_path.exists():
        raise HTTPException(status_code=404, detail="Source path not found")
    if new_path.exists():
        raise HTTPException(status_code=409, detail="Target path already exists")

    # Tombstone the old path(s) BEFORE the move so an idle satellite removes the
    # source instead of resurrecting it (per-file for a dir rename).
    await _tombstone_subtree(name, agent_dir, old_path)
    old_path.rename(new_path)
    logger.info(f"Renamed: {old_path} -> {new_path}")
    # Mirror to active remote sessions: drop the old path; publish the new file(s).
    await _push_file_delete_to_remote(name, old_path.relative_to(agent_dir.resolve()).as_posix())
    if new_path.is_file():
        await _push_file_write_to_remote(
            name, new_path.relative_to(agent_dir.resolve()).as_posix(), new_path,
            writer=_dashboard_writer(u),
        )
    else:
        await _push_tree_write_to_remote(name, new_path, agent_dir, writer=_dashboard_writer(u))
    return {
        "status": "renamed",
        "old_path": str(old_path.relative_to(agent_dir)),
        "new_path": str(new_path.relative_to(agent_dir)),
    }


@router.post("/v1/agents/{name}/move")
async def move_agent_paths(
    name: str,
    req: MovePathsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Move (cut+paste) one or more files/directories into `dest_dir`.

    Cross-scope moves are allowed when the user has write access on BOTH
    sides (e.g. a manager moving from `users/<u>/workspace/` into
    `workspace/`). The endpoint validates each source for write access
    because move = delete-after-write on the source's scope. Per-item
    failures are returned in `failed[]`; partial success returns 200.
    """
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.is_api_key:
        # _validate_op_paths → _check_file_role enforces per-tier
        # write rules on every source + dest path.
        role, username = _resolve_user_session_info(u, name)
    else:
        role, username = "admin", ""

    agent_dir = _get_agent_dir(name)
    sources, dest_resolved = _validate_op_paths(
        req.src_paths, req.dest_dir,
        agent_dir=agent_dir, role=role, username=username,
        writing_on_source=True,
    )

    moved: list[dict] = []
    failed: list[dict] = []
    for src_norm, src_resolved in sources:
        try:
            src_scope = _scope_root(src_norm)
            if not src_scope:
                raise HTTPException(status_code=400, detail=f"Invalid source scope: {src_norm}")
            scope_root = (agent_dir / src_scope).resolve()
            _assert_no_symlink_escape(src_resolved, scope_root)

            # No-op when the source already sits in the dest directory:
            # cut+paste-into-same-folder shouldn't create `_1` copies.
            if src_resolved.parent == dest_resolved:
                moved.append({
                    "src": src_norm,
                    "dest": str(src_resolved.relative_to(agent_dir)),
                    "noop": True,
                })
                continue

            target = _resolve_conflict(dest_resolved / src_resolved.name)
            # Tombstone the source path(s) BEFORE the move so an idle satellite
            # removes the old location instead of resurrecting it.
            await _tombstone_subtree(name, agent_dir, src_resolved)
            shutil.move(str(src_resolved), str(target))
            logger.info(f"Moved: {src_resolved} -> {target}")
            # Mirror to active remote sessions: drop the old subtree, push the
            # new one (recursively for directories) so the satellite updates
            # immediately rather than waiting for the next manifest sync.
            await _push_file_delete_to_remote(
                name, src_resolved.relative_to(agent_dir.resolve()).as_posix(),
            )
            await _push_tree_write_to_remote(name, target, agent_dir, writer=_dashboard_writer(u))
            moved.append({
                "src": src_norm,
                "dest": str(target.relative_to(agent_dir)),
            })
        except HTTPException as e:
            failed.append({"src": src_norm, "reason": e.detail})
        except (OSError, shutil.Error) as e:
            logger.warning("Move failed for %s: %s", src_norm, e)
            failed.append({"src": src_norm, "reason": _fs_error_reason(e)})

    return {"moved": moved, "failed": failed}


@router.post("/v1/agents/{name}/copy")
async def copy_agent_paths(
    name: str,
    req: CopyPathsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Copy one or more files/directories into `dest_dir`.

    Cross-scope copies are allowed when the destination is writable for the
    user. Source needs only read access. Symlinks inside source subtrees
    that point outside the source's scope are rejected so copies cannot
    smuggle data across scope boundaries. Name collisions in `dest_dir`
    are auto-suffixed (`foo.md` → `foo_1.md`).
    """
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.is_api_key:
        # _validate_op_paths → _check_file_role enforces per-tier
        # write rules on the dest dir (sources need read perms only).
        role, username = _resolve_user_session_info(u, name)
    else:
        role, username = "admin", ""

    agent_dir = _get_agent_dir(name)
    sources, dest_resolved = _validate_op_paths(
        req.src_paths, req.dest_dir,
        agent_dir=agent_dir, role=role, username=username,
        writing_on_source=False,
    )

    copied: list[dict] = []
    failed: list[dict] = []
    for src_norm, src_resolved in sources:
        try:
            src_scope = _scope_root(src_norm)
            if not src_scope:
                raise HTTPException(status_code=400, detail=f"Invalid source scope: {src_norm}")
            scope_root = (agent_dir / src_scope).resolve()
            _assert_no_symlink_escape(src_resolved, scope_root)

            target = _resolve_conflict(dest_resolved / src_resolved.name)
            if src_resolved.is_file():
                shutil.copy2(str(src_resolved), str(target))
            else:
                # symlinks=True preserves symlinks as-is — we already verified
                # nothing in the subtree escapes the source's scope.
                shutil.copytree(str(src_resolved), str(target), symlinks=True)
            logger.info(f"Copied: {src_resolved} -> {target}")
            # Mirror to active remote sessions: push the new file/subtree so the
            # satellite sees the copy immediately, not only at the next sync.
            # (Copy keeps the source — no tombstone.)
            await _push_tree_write_to_remote(name, target, agent_dir, writer=_dashboard_writer(u))
            copied.append({
                "src": src_norm,
                "dest": str(target.relative_to(agent_dir)),
            })
        except HTTPException as e:
            failed.append({"src": src_norm, "reason": e.detail})
        except (OSError, shutil.Error) as e:
            logger.warning("Copy failed for %s: %s", src_norm, e)
            failed.append({"src": src_norm, "reason": _fs_error_reason(e)})

    return {"copied": copied, "failed": failed}


@router.post("/v1/agents/{name}/zip")
async def zip_agent_paths(
    name: str,
    req: ZipPathsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Build a zip archive of the requested files/directories and stream it.

    Read-only operation — `require_write` is NOT applied so viewers can
    download their own files. Each path is validated against the user's
    read scope. The archive is built in memory (suitable for typical
    workspace sizes); a streaming-zip generator is the v2 plan if very
    large archives become common.

    The browser path uses this POST endpoint to receive the zip directly as
    a blob. Capacitor/Android can't download blob: URLs (DownloadManager
    only accepts http/https), so the dashboard uses `POST /zip-url` +
    `GET /zip-download` instead — same validation + builder, just split so
    DownloadManager has a real http URL to hit.
    """
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.is_api_key:
        role, username = _resolve_user_session_info(u, name)
    else:
        role, username = "admin", ""

    return _build_zip_response(name, req.paths, role, username)


@router.post("/v1/agents/{name}/zip-url")
async def request_zip_url(
    name: str,
    req: ZipPathsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Validate paths + mint a short-lived signed download URL.

    The dashboard POSTs paths here, then navigates the user to the returned
    URL via an `<a>` click. Android's DownloadManager (which can't handle
    blob URLs from the POST flow) hits the URL as a normal GET and writes
    the response to Downloads. Browser users also work — the GET endpoint
    sets Content-Disposition so the browser triggers a download.
    """
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.is_api_key:
        role, username = _resolve_user_session_info(u, name)
    else:
        role, username = "admin", ""

    if not req.paths:
        raise HTTPException(status_code=400, detail="paths cannot be empty")

    # Pre-validate paths so the user gets immediate feedback on bad input
    # instead of a download that 403s 2 minutes later. We don't keep the
    # resolved Path objects — `_build_zip_response` re-validates at fire time
    # (paths could be deleted between mint and click).
    agent_dir = _get_agent_dir(name)
    for raw in req.paths:
        norm = _normalize_path(raw)
        _check_file_role(norm, role, writing=False, username=username)
        src_resolved = (agent_dir / norm).resolve()
        _check_path_traversal(src_resolved, agent_dir)

    import urllib.parse as _urlparse
    token = _create_zip_token(name, req.paths, u.sub, role, username)
    filename = (
        f"{Path(req.paths[0]).name}.zip" if len(req.paths) == 1
        else f"workspace-files-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    )
    return {
        "download_url": (
            f"/v1/agents/{name}/zip-download"
            f"?t={token}&fn={_urlparse.quote(filename)}"
        ),
        "filename": filename,
    }


@router.get("/v1/agents/{name}/zip-download")
async def zip_download(
    name: str,
    t: str,
    fn: str | None = None,
):
    """Validate the token from `/zip-url` and stream the zip.

    No session/cookie auth needed — the JWT signature is proof. Bound to
    the agent name in the URL path AND in the token so a token minted for
    agent A can't be used against agent B.
    """
    import jwt as _jwt
    try:
        claims = _jwt.decode(t, config.JWT_SECRET, algorithms=["HS256"])
    except _jwt.ExpiredSignatureError:
        raise HTTPException(status_code=410, detail="Download link expired")
    except _jwt.InvalidTokenError:
        raise HTTPException(status_code=403, detail="Invalid download token")

    if claims.get("agent") != name:
        raise HTTPException(status_code=403, detail="Token / agent mismatch")

    paths = claims.get("paths") or []
    role = claims.get("role") or "viewer"
    username = claims.get("username") or ""
    # `fn` is informational for the client; the real filename comes from
    # _build_zip_response via Content-Disposition.
    _ = fn
    return _build_zip_response(name, paths, role, username)
