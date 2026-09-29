"""Agent workspace file operations.

File-tree listing, read/write/create/mkdir, delete/rename/move/copy,
zip + zip-url downloads, and the recover-bin (soft-delete) endpoints,
with the role/OAuth path-permission helpers they share. Attaches to
the shared package router."""

import asyncio
import contextlib
import errno
import json
import logging
import os
import shutil
import stat
import tempfile
import time
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import config
from auth.providers import (
    PrincipalKind, UserContext, get_current_user, require_agent_access, require_auth,
    session_bound_to,
)
from services.infra import safe_fs
from storage import database as task_store
from storage.pg import run_db

from api.agents._common import _get_agent_dir
from api.agents._router import router

# A session token acts only on the agent it was started for; every
# files route carries the binding (a cookie, the master key and an agent's
# own session are unchanged).
_BOUND = [Depends(session_bound_to("name"))]
from api.media.media import FdFileResponse
# The write / delete bookkeeping every platform writer shares (tombstones,
# authorship, library projection, satellite fan-out, recover-bin capture)
# lives in services/infra/file_bookkeeping — the Direct-LLM builtin file
# tools run the same sequence through the same module.
from services.infra import file_bookkeeping
from auth.request_path import has_traversal
from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path, resolve_under
from auth import roles
from core import layout

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


def _dir_node(name: str, rel: str, mtime: float, children: list[dict]) -> dict:
    return {
        "name": name,
        "type": "dir",
        "path": rel,
        "size": 0,
        "modified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        "children": children,
    }


def _file_node(name: str, rel: str, size: int, mtime: float) -> dict:
    return {
        "name": name,
        "type": "file",
        "path": rel,
        "size": size,
        "modified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        "children": [],
    }


def _build_tree(directory: Path, base: Path, depth: int, max_depth: int, *,
                keep: Callable[[str, int, bool], bool] | None = None,
                link_stat: Callable[[str], os.stat_result | None] | None = None,
                ) -> list[dict]:
    """Recursively build a directory tree structure (one ``stat`` per entry,
    no symlink followed).

    Excludes:
      * Hidden entries (`.foo`)
      * `SKIP_DIRS` (node_modules, venv, etc.)
      * **Protected OAuth credentials_dir subpaths** — e.g. `google-tokens/`
        for workspace-mcp. Manifest-driven via
        ``mcp_registry.get_protected_credentials_subpaths()``. Filtered
        for EVERY role (including admin) because raw OAuth tokens have
        no UX value in the file tree — the OAuth connect/disconnect UI
        is the intended management surface.
      * Entries ``keep(rel, depth, is_dir)`` refuses: the scoped walk
        decides at depth 1 what the caller may see before descending.
      * Symlinks, unless ``link_stat(rel)`` returns the stat of a regular
        file the caller may read: the link is then listed as that file.
    """
    if depth > max_depth:
        return []

    try:
        with os.scandir(directory) as it:
            entries = list(it)
    except PermissionError:
        return []

    # Look up the protected credentials_dir subpath set ONCE per call
    # (it's a frozenset of a few strings; cost is negligible). Doing it
    # here keeps the lookup current with any manifest reload.
    from services.mcp import mcp_registry
    protected_subpaths = mcp_registry.get_protected_credentials_subpaths()

    base_rel = "" if directory == base else directory.relative_to(base).as_posix()

    def _rel(name: str) -> str:
        return f"{base_rel}/{name}" if base_rel else name

    # Separate dirs and files, filter hidden and skip dirs
    dirs: list[tuple[str, str, os.stat_result]] = []
    files: list[tuple[str, str, os.stat_result]] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_symlink():
                if link_stat is None:
                    continue
                st = link_stat(_rel(entry.name))
                if st is None:
                    continue
                files.append((entry.name, _rel(entry.name), st))
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in SKIP_DIRS:
                    continue
                if entry.name in protected_subpaths:
                    continue
                if keep is not None and not keep(_rel(entry.name), depth, True):
                    continue
                dirs.append((entry.name, _rel(entry.name), entry.stat(follow_symlinks=False)))
            elif entry.is_file(follow_symlinks=False):
                if keep is not None and not keep(_rel(entry.name), depth, False):
                    continue
                files.append((entry.name, _rel(entry.name), entry.stat(follow_symlinks=False)))
        except OSError:
            continue

    # Sort alphabetically
    dirs.sort(key=lambda t: t[0])
    files.sort(key=lambda t: t[0])

    result = []

    # Dirs first
    for name, rel, st in dirs:
        result.append(_dir_node(
            name, rel, st.st_mtime,
            _build_tree(directory / name, base, depth + 1, max_depth,
                        keep=keep, link_stat=link_stat),
        ))

    # Then files
    for name, rel, st in files:
        result.append(_file_node(name, rel, st.st_size, st.st_mtime))

    return result


def _build_tree_scoped(name: str, role: str, username: str, *,
                       full: bool = False) -> list[dict]:
    """The tree ``GET /v1/agents/{name}/files`` returns: the walk decides at
    depth 1 what the caller may see (`_filter_tree`'s rule) and never enters
    other users' folders, ``config`` for a non-owner or the platform-only
    trees, so its cost follows the caller's view, not the agent's size.
    ``full`` (an API-key caller) keeps every tree but the platform-only ones.
    ``_filter_tree`` still runs last as the safety net."""
    from core.remote.file_sync import is_platform_only_tree
    agent_dir = config.get_agent_dir(name)
    owner_tier = roles.can_manage(role)
    own_users = layout.user_rel(username) if username else ""

    def keep(rel: str, depth: int, is_dir: bool) -> bool:
        if depth == 1:
            if is_platform_only_tree(rel):
                return False
            if full:
                return True
            if rel in (layout.WORKSPACE, layout.KNOWLEDGE):
                return True
            if rel == layout.CONFIG:
                return owner_tier
            if rel == layout.USERS:
                return is_dir and bool(own_users)
            return False
        if depth == 2 and not full and rel.startswith(layout.USERS + "/"):
            return is_dir and rel == own_users
        return True

    def link_stat(rel: str) -> os.stat_result | None:
        # A link is listed only where its target is a regular file inside
        # this agent's tree that the caller may read (the read route opens
        # the target through the same resolved-path check); anything else,
        # a directory included, stays out of the listing.
        try:
            canonical = safe_fs.canonical_rel(config.AGENTS_DIR, f"{name}/{rel}")
        except OSError:
            return None
        first, _, agent_rel = canonical.partition("/")
        if first != name or not agent_rel or is_platform_only_tree(agent_rel):
            return None
        try:
            if full:
                _check_oauth_protected(agent_rel)
                _check_engine_state(agent_rel)
            else:
                _check_file_role(agent_rel, role, writing=False, username=username)
        except HTTPException:
            return None
        try:
            st = os.stat(agent_dir / agent_rel)
        except OSError:
            return None
        return st if stat.S_ISREG(st.st_mode) else None

    tree = _build_tree(agent_dir, agent_dir, depth=1, max_depth=20,
                       keep=keep, link_stat=link_stat)
    tree = [e for e in tree if not is_platform_only_tree(e.get("path") or "")]
    if not full:
        tree = _filter_tree(tree, role, username=username)
    return tree


def _tree_json(name: str, role: str, username: str, full: bool) -> bytes:
    tree = _build_tree_scoped(name, role, username, full=full)
    return json.dumps({"tree": tree}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


# Bounded concurrency for the walks and builds that run off the loop: one
# semaphore per (name, running loop). A module-level asyncio.Semaphore binds
# to the first loop that waits on it and raises on the next; a stale loop's
# entry is dropped when a new loop shows up.
_slot_tables: dict[str, dict[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}


def _loop_slots(name: str, n: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    table = _slot_tables.setdefault(name, {})
    sem = table.get(loop)
    if sem is None:
        for stale in [l for l in table if l.is_closed()]:
            table.pop(stale, None)
        sem = table[loop] = asyncio.Semaphore(n)
    return sem


@contextlib.asynccontextmanager
async def _slot(name: str, n: int, wait_s: float, busy_detail: str):
    sem = _loop_slots(name, n)
    try:
        async with asyncio.timeout(wait_s):
            await sem.acquire()
    except TimeoutError:
        raise HTTPException(status_code=503, detail=busy_detail)
    try:
        yield
    finally:
        sem.release()


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
    owner_tier = roles.can_manage(role)
    for node in nodes:
        if node["type"] == "dir" and node["name"] == layout.USERS and node.get("children"):
            # Show users/ but filter to own username only
            filtered = node.copy()
            filtered["children"] = [
                c for c in node["children"]
                if c["type"] == "dir" and c["name"] == username
            ] if username else []
            if filtered["children"]:
                result.append(filtered)
        elif node["name"] == layout.CONFIG:
            if owner_tier:
                result.append(node)
            # else: hidden from editor + viewer
        elif node["name"] in (layout.WORKSPACE, layout.KNOWLEDGE):
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


def _check_engine_state(path: str) -> None:
    """403 for a scope-root ``.claude``/``.codex`` path, EVERY principal: the
    engines' state there (the subscription login, the MCP config carrying a
    session token, the hook scripts every session of that scope runs) is
    platform-written and never served or written through the files API."""
    from services import path_roles
    if path_roles.in_session_state_dir(path):
        raise HTTPException(status_code=403, detail="Access denied: engine state is not accessible")


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
    _check_engine_state(path)

    if roles.is_admin(role):
        return

    def _in_scope(p: str, scope: str) -> bool:
        return p == scope or p.startswith(scope + "/")

    # Determine which scopes this role is allowed to READ from.
    # Viewer + editor can read workspace + knowledge + own user dir;
    # manager can additionally read /config/. Config is owner-only.
    own_user_scope = layout.user_rel(username) if username else ""
    owner_tier = roles.can_manage(role)  # admin already returned above
    read_allowed = (
        (own_user_scope and _in_scope(path, own_user_scope))
        or _in_scope(path, layout.KNOWLEDGE)
        or _in_scope(path, layout.WORKSPACE)
        or (owner_tier and _in_scope(path, layout.CONFIG))
    )
    if not read_allowed:
        if _in_scope(path, layout.CONFIG):
            raise HTTPException(
                status_code=403,
                detail="Agent config is owner-only and not accessible to editors or viewers",
            )
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed scope")

    if not writing:
        return

    # Write tier checks: a role the table does not know writes as a viewer.
    if not roles.can_write_workspace(role):
        if not own_user_scope or not _in_scope(path, own_user_scope):
            raise HTTPException(
                status_code=403,
                detail="Viewers can write only to their own user directory",
            )
    elif not roles.can_manage(role):
        # Editor / contributor can write own user dir + workspace/. Knowledge
        # is owner-curated.
        if (
            (own_user_scope and _in_scope(path, own_user_scope))
            or _in_scope(path, layout.WORKSPACE)
        ):
            return
        raise HTTPException(
            status_code=403,
            detail="Editors and contributors cannot modify agent knowledge (owner-only)",
        )
    else:
        # Manager / admin (the owner tier): own user dir + workspace/ + config/ + knowledge/.
        if (
            (own_user_scope and _in_scope(path, own_user_scope))
            or _in_scope(path, layout.CONFIG)
            or _in_scope(path, layout.KNOWLEDGE)
            or _in_scope(path, layout.WORKSPACE)
        ):
            return
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed scope")


def safe_agent_path(
    agent_dir: Path, name: str, raw_path: str, user: UserContext, *, writing: bool = False,
    username: str | None = None,
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
    ``username`` skips the store lookup (a caller that fetched it on the DB
    lane, ``_username_of``, passes it).

    The answer is the canonical location; a caller opens it beneath
    ``AGENTS_DIR`` through ``safe_fs`` with the rel ``_agent_rel`` builds
    (``canonical_rel`` then the strict open, SAFE-FS.md), never by name.

    Raises HTTPException(400/403) on a bad or out-of-scope path.
    """
    if "\x00" in raw_path:
        raise HTTPException(status_code=400, detail="Invalid path")
    norm = _normalize_path(raw_path)  # rejects empty / '.' / '..' segments
    agent_root = Path(os.path.realpath(agent_dir))
    try:
        resolved = resolve_under(agent_root / norm, agent_root)
    except PathOutsideRoot:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    rel = resolved.relative_to(agent_root).as_posix()
    _check_oauth_protected(rel)  # OAuth token dirs are off-limits to EVERY principal
    _check_engine_state(rel)
    # The platform-only siblings of the workspace (release copies, chat
    # snapshots, external callers' trees) are served by their own routes and
    # never through the files API, admins included (SHARING.md).
    from core.remote.file_sync import is_platform_only_tree
    if is_platform_only_tree(rel):
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed scope")
    if writing:
        # Knowledge-library mirrors gate on the attachment's writable flag,
        # for EVERY principal (incl. admin + agent sessions): mirror content
        # is projector-owned, and a read-only mirror is edited at its SOURCE.
        _check_library_mirror_write(rel, name)
    uname = ""
    if user.acting_sub is not None:
        uname = (task_store.get_username_by_sub(user.sub) or "") if username is None else username
        _check_file_role(rel, user.acting_role(name), writing=writing, username=uname)
    return resolved, uname


def _check_library_mirror_write(rel: str, agent: str) -> None:
    """403 for writes into ``knowledge/shared/`` unless the target lies
    inside an RW-attached library SUBTREE (segment-wise — libraries are
    per-subtree). The bare ``shared/`` namespace, the slug level, and any
    path outside every attached subtree are reserved (projector-owned)."""
    parts = rel.split("/")
    if len(parts) < 2 or parts[0] != layout.KNOWLEDGE or parts[1] != "shared":
        return
    if len(parts) == 2:
        raise HTTPException(
            status_code=403,
            detail="knowledge/shared/ is reserved for shared knowledge "
                   "library mirrors",
        )
    src, sub_rel = parts[2], "/".join(parts[3:])
    from storage.knowledge import db_knowledge_libraries
    att = db_knowledge_libraries.attachment_covering(src, agent, sub_rel)
    if att is None or not att["writable"]:
        raise HTTPException(
            status_code=403,
            detail=f"Read-only shared knowledge library mirror — edit this "
                   f"content on the '{src}' agent (the library source).",
        )


def _dashboard_writer(u, username: str | None = None) -> str | None:
    """The username slug to record as ``file_author`` for a dashboard write, or
    None for an API-key / agent-scope write (no human identity). ``username``
    is the value ``_username_of`` fetched off the loop."""
    if getattr(u, "is_api_key", False):
        return None
    if username is not None:
        return username or None
    from storage import database as task_store
    return task_store.get_username_by_sub(u.sub) or None


def _acts_as_person(u: UserContext) -> bool:
    """Whether a file operation is judged at a person's role: a cookie, or a
    session token that names a person. The master key and an agent's own
    session keep the admin tier with no person."""
    return not u.is_api_key or u.kind == PrincipalKind.USER_SESSION


async def _username_of(u: UserContext) -> str:
    """The caller's username slug, read on the DB lane (the one store hop the
    write routes need; ``""`` for a principal with no person)."""
    if u.acting_sub is None:
        return ""
    return await run_db(task_store.get_username_by_sub, u.sub) or ""


def _agent_rel(name: str, resolved: Path, agent_dir: Path) -> str:
    """The rel the helpers open beneath ``AGENTS_DIR`` for ``safe_agent_path``'s
    answer: the agent's own NAME, then the answer below the agent root's
    realpath. The first component is the name, never the realpath's first
    segment, so an agent folder swapped for a link is refused at the open."""
    sub = resolved.relative_to(Path(os.path.realpath(agent_dir))).as_posix()
    return name if sub in ("", ".") else f"{name}/{sub}"


def _sub_of(name: str, agents_rel: str) -> str:
    """The agent-relative form of a ``_agent_rel`` answer (the API's path shape)."""
    return agents_rel[len(name) + 1:] if agents_rel != name else ""


def _fs_refusal(exc: OSError, name: str) -> HTTPException:
    """The HTTP answer for a filesystem step that failed beneath the root: a
    refusal of the helpers (a link met on the way, an escape) is the same 403
    the string check gives; a missing path 404; a name in use 409 (a file
    standing where a directory of the path should be included); a full
    bucket 507; anything else its errno text."""
    if isinstance(exc, safe_fs.SafeFsError):
        return HTTPException(status_code=403, detail="Path traversal not allowed")
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail="Path not found")
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail="Target path already exists")
    if isinstance(exc, NotADirectoryError):
        return HTTPException(status_code=409, detail="A file is in the way of that path")
    if isinstance(exc, IsADirectoryError):
        return HTTPException(status_code=409, detail="A directory is in the way of that path")
    if exc.errno in (errno.EDQUOT, errno.ENOSPC):
        return HTTPException(
            status_code=507,
            detail=f"Not enough storage in '{name}'s bucket for this write.",
        )
    return HTTPException(status_code=500, detail=_fs_error_reason(exc))


_FILE_OPS_SLOTS = 4


@contextlib.asynccontextmanager
async def _file_ops_slot():
    async with _slot("file-ops", _FILE_OPS_SLOTS, 30.0,
                     "the server is busy with file operations, try again in a moment"):
        yield


def _scope_root(path: str) -> str:
    """Return the top-level scope segment of an agent-relative path.

    Used to enforce that recursive operations don't cross scope boundaries
    (e.g. a manager recursive-deleting `users/` would otherwise wipe every
    user's dir). Scopes: `config`, `workspace`, `users/<username>`.
    """
    parts = path.strip("/").split("/")
    if not parts or not parts[0]:
        return ""
    if parts[0] == layout.USERS:
        return layout.user_rel(parts[1]) if len(parts) > 1 else layout.USERS
    return parts[0]


def _candidate_names(name: str):
    """``name``, then ``stem_1``, ``stem_2``, ... up to 99: the destination
    names a move, a copy or a restore tries in order, each reserved with an
    exclusive create or rename (never probed for). Mirrors
    ``api.media.uploads``' conflict suffixes."""
    yield name
    stem, ext = os.path.splitext(name)
    for i in range(1, 100):
        yield f"{stem}_{i}{ext}"


def _under_scope(sub: str, scope: str) -> bool:
    return bool(scope) and (sub == scope or sub.startswith(scope + "/"))


def _assert_no_symlink_escape(agents_rel: str, scope_rel: str) -> None:
    """Walk the subtree at ``agents_rel`` (beneath ``AGENTS_DIR``) and raise
    403 when a link inside it resolves outside ``scope_rel`` (both forms are
    ``<agent>/...``). The walk never follows a link; regular entries are not
    resolved (the root is already the resolved answer), only the links are,
    once each. A file source needs no walk. Pure filesystem, no DB, no auth."""
    agents_real = os.path.realpath(config.AGENTS_DIR)
    scope_real = os.path.realpath(os.path.join(agents_real, scope_rel))
    try:
        st = safe_fs.lstat_beneath(config.AGENTS_DIR, agents_rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Path not found")
    except OSError:
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not stat.S_ISDIR(st.st_mode):
        return

    def _unreadable(err: OSError) -> None:
        raise HTTPException(status_code=400, detail="Cannot resolve a path in the subtree")

    for step in safe_fs.walk_beneath(config.AGENTS_DIR, agents_rel, onerror=_unreadable):
        for lname in step.symlinks:
            try:
                target = os.readlink(lname, dir_fd=step.dirfd)
            except OSError:
                raise HTTPException(status_code=400, detail="Cannot resolve a path in the subtree")
            landed = os.path.realpath(os.path.join(agents_real, step.rel, target))
            if landed != scope_real and not landed.startswith(scope_real + os.sep):
                raise HTTPException(
                    status_code=403,
                    detail="Subtree contains a symlink escaping the scope",
                )


def _normalize_path(p: str) -> str:
    """Strip leading/trailing slashes; reject empty / `.` / `..` segments and
    a NUL (``path_confinement.normalize_rel_path``)."""
    if not p.strip("/"):
        raise HTTPException(status_code=400, detail="Empty path")
    try:
        return normalize_rel_path(p)
    except PathOutsideRoot:
        raise HTTPException(status_code=400, detail=f"Invalid path: {p}")


def _resolve_user_session_info(u: UserContext, agent: str) -> tuple[str, str]:
    """Return (role, username) for the calling session, used by file-role checks."""
    from storage import database as task_store
    role = u.acting_role(agent)
    username = task_store.get_username_by_sub(u.sub) or ""
    return role, username


def _check_resolved_role(resolved: Path, agent_dir: Path, role: str, *, writing: bool,
                         username: str) -> None:
    """``_check_file_role`` on the agent-relative form of a resolved path
    (the traversal check already confined it to the agent tree)."""
    rel = resolved.relative_to(agent_dir).as_posix()
    if rel in ("", "."):
        raise HTTPException(status_code=403, detail="Access denied: path outside allowed scope")
    _check_file_role(rel, role, writing=writing, username=username)


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
    # The write lands where the path RESOLVES: a symlinked destination
    # (``workspace/ctx -> ../config/context``) is judged as its target.
    _check_resolved_role(dest_resolved, agent_dir, role, writing=True, username=username)
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
        _check_resolved_role(src_resolved, agent_dir, role, writing=writing_on_source,
                             username=username)
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


class _ZipBudget:
    """What one archive may take: bytes and entries from the caps, counted
    while the walk runs (the walk is the only pass)."""

    def __init__(self) -> None:
        self.max_bytes = config.ZIP_MAX_INPUT_MB * 1024 * 1024
        self.max_entries = config.ZIP_MAX_ENTRIES
        self.bytes = 0
        self.entries = 0
        self.skipped = 0

    @property
    def left(self) -> int | None:
        return None if self.max_bytes <= 0 else max(0, self.max_bytes - self.bytes)

    def too_big(self) -> HTTPException:
        return HTTPException(
            status_code=413,
            detail=f"the selection is larger than {config.ZIP_MAX_INPUT_MB} MB; "
                   "download fewer folders",
        )

    def add_entry(self) -> None:
        self.entries += 1
        if self.max_entries > 0 and self.entries > self.max_entries:
            raise HTTPException(
                status_code=413,
                detail=f"the selection has more than {self.max_entries} files; "
                       "download fewer folders",
            )

    def add_bytes(self, n: int) -> None:
        self.bytes += n
        if self.max_bytes > 0 and self.bytes > self.max_bytes:
            raise self.too_big()


def _zip_add_regular_file(zf: zipfile.ZipFile, dirfd: int, name: str, arcname: str,
                          budget: _ZipBudget) -> bool:
    """Add ``name`` (a path below ``dirfd``) to the archive from a descriptor
    opened with no symlink followed: the file the listing saw is the file
    copied. A link, a FIFO or a file that vanished is skipped (counted),
    never followed. Returns whether an entry was written."""
    try:
        fd, st = safe_fs.open_regular_for_read(dirfd, name)
    except (safe_fs.SafeFsError, FileNotFoundError):
        budget.skipped += 1
        return False
    try:
        budget.add_entry()
        date_time = time.localtime(st.st_mtime)[:6]
        if date_time[0] < 1980:
            date_time = (1980, 1, 1, 0, 0, 0)
        zi = zipfile.ZipInfo(arcname, date_time=date_time)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.compress_level = 1
        zi.external_attr = (st.st_mode & 0xFFFF) << 16
        with os.fdopen(fd, "rb") as src:
            fd = -1
            with zf.open(zi, "w", force_zip64=st.st_size >= 0x7FFFFFFF) as dst:
                try:
                    copied = safe_fs.copy_fd(src, dst, max_size=budget.left)
                except safe_fs.FileTooLarge:
                    raise budget.too_big()
        budget.add_bytes(copied)
        return True
    finally:
        if fd >= 0:
            os.close(fd)


def _zip_sources(paths: list[str]) -> list[str]:
    """The selection, normalized, without duplicates and without a path that
    sits inside another selected one."""
    norms: list[str] = []
    for raw in paths:
        norm = _normalize_path(raw)
        if norm not in norms:
            norms.append(norm)
    kept = [p for p in norms if not any(p.startswith(q + "/") for q in norms if q != p)]
    if config.ZIP_MAX_PATHS > 0 and len(kept) > config.ZIP_MAX_PATHS:
        raise HTTPException(
            status_code=413,
            detail=f"too many paths in one download ({len(kept)}, the limit is "
                   f"{config.ZIP_MAX_PATHS})",
        )
    return kept


def _zip_tmp_dir() -> Path:
    """A proxy-owned place for the unnamed archive file (never the agent
    tree): under the sessions dir, which no sandbox mounts."""
    d = Path(config.SESSIONS_DIR) / "zip-tmp"
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _build_zip_file(name: str, sources: list[str], role: str, username: str,
                    ) -> tuple[int, os.stat_result, str]:
    """Validate the sources and write the archive into an unnamed temp file,
    in one descriptor-based pass. Returns ``(fd, stat, zip name)``; the
    caller owns ``fd``. Blocking: run it in a thread."""
    agent_dir = _get_agent_dir(name)
    resolved_sources: list[tuple[str, str, Path]] = []
    for norm in sources:
        _check_file_role(norm, role, writing=False, username=username)
        src_resolved = (agent_dir / norm).resolve()
        _check_path_traversal(src_resolved, agent_dir)
        if not src_resolved.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {norm}")
        # The walk opens the RESOLVED path, so the role is checked there too:
        # a link in workspace/ to another user's folder is judged as that
        # folder (as move and copy judge theirs).
        _check_resolved_role(src_resolved, agent_dir, role, writing=False, username=username)
        try:
            src_rel = safe_fs.rel_under(src_resolved, agent_dir)
        except OSError:
            raise HTTPException(status_code=403, detail="Path traversal not allowed")
        if not src_rel:
            raise HTTPException(status_code=400, detail=f"Invalid scope for path: {norm}")
        resolved_sources.append((norm, src_rel, src_resolved))

    tmp_dir = _zip_tmp_dir()
    if config.MIN_FREE_DISK_MB > 0 and \
            shutil.disk_usage(tmp_dir).free < config.MIN_FREE_DISK_MB * 1024 * 1024:
        raise HTTPException(status_code=507, detail="not enough space to prepare the download")

    from services import path_roles
    budget = _ZipBudget()

    def _gated(agent_rel: str) -> bool:
        # The per-path gates hold inside the tree too: a zip of workspace/
        # must not carry its engine state or tokens.
        return (path_roles.in_session_state_dir(agent_rel)
                or path_roles.is_protected_credentials_path(agent_rel))

    def _unreadable(err: OSError) -> None:
        raise HTTPException(
            status_code=400,
            detail=f"the folder {err.filename} cannot be read; fix its permissions "
                   "or leave it out of the selection",
        )

    tmp = tempfile.TemporaryFile(dir=tmp_dir)
    try:
        used_arcnames: set[str] = set()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf, \
                safe_fs.open_root(config.AGENTS_DIR, name) as agent_fd:
            for _norm, src_rel, src_resolved in resolved_sources:
                base = src_resolved.name
                unique_base = base
                i = 1
                while unique_base in used_arcnames:
                    stem = Path(base).stem
                    suffix = Path(base).suffix
                    unique_base = f"{stem}_{i}{suffix}"
                    i += 1
                used_arcnames.add(unique_base)
                if not src_resolved.is_dir():
                    _zip_add_regular_file(zf, agent_fd, src_rel, unique_base, budget)
                    continue
                zf.writestr(zipfile.ZipInfo(unique_base + "/"), b"")
                prefix = len(src_rel) + 1
                for step in safe_fs.walk_beneath(agent_fd, src_rel, onerror=_unreadable):
                    step.dirs[:] = [d for d in step.dirs if not _gated(step.path(d))]
                    for d in step.dirs:
                        zf.writestr(zipfile.ZipInfo(f"{unique_base}/{step.path(d)[prefix:]}/"), b"")
                    for f in step.files:
                        agent_rel = step.path(f)
                        if _gated(agent_rel):
                            continue
                        _zip_add_regular_file(
                            zf, step.dirfd, f, f"{unique_base}/{agent_rel[prefix:]}", budget,
                        )
                    budget.skipped += len(step.symlinks) + len(step.other)
        tmp.flush()
        fd = os.dup(tmp.fileno())
        st = os.fstat(fd)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HTTPException(status_code=507, detail="not enough space to prepare the download")
        raise
    finally:
        tmp.close()
    if budget.skipped:
        logger.info("zip for %s left out %d links or special files", name, budget.skipped)

    if len(resolved_sources) == 1:
        zip_name = f"{Path(resolved_sources[0][2].name).stem or 'archive'}.zip"
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M")
        zip_name = f"workspace-files-{ts}.zip"
    return fd, st, zip_name


# One archive in flight per requester; the build slots are `_slot("zip")`.
_zip_inflight: set[str] = set()


async def _build_zip_response(
    name: str,
    paths: list[str],
    role: str,
    username: str,
    user_key: str,
) -> FileResponse:
    """Validate paths + build the zip archive off the loop + return a file
    response streamed from the archive's descriptor.

    Shared by `POST /v1/agents/{name}/zip` (browser path) and
    `GET /v1/agents/{name}/zip-download` (Android-friendly token flow).
    All path-traversal / role / symlink checks happen here.
    """
    if not paths:
        raise HTTPException(status_code=400, detail="paths cannot be empty")
    sources = _zip_sources(paths)
    if user_key in _zip_inflight:
        raise HTTPException(status_code=429, detail="a zip is already being prepared for you")
    _zip_inflight.add(user_key)
    try:
        async with _slot("zip", config.ZIP_MAX_CONCURRENT, 30.0,
                         "the server is busy preparing other downloads, try again in a moment"):
            fd, st, zip_name = await asyncio.to_thread(
                _build_zip_file, name, sources, role, username,
            )
    finally:
        _zip_inflight.discard(user_key)
    try:
        return FdFileResponse(fd, st, media_type="application/zip", filename=zip_name)
    except BaseException:
        os.close(fd)
        raise


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


@router.get("/v1/agents/{name}/files", dependencies=_BOUND)
async def list_agent_files(name: str, user: UserContext | None = Depends(get_current_user)):
    """Return a recursive directory tree of the agent's folder."""
    u = require_auth(user)
    require_agent_access(u, name)

    _get_agent_dir(name)
    # max_depth=20 covers virtually every real workspace tree. The previous
    # cap of 5 caused two visible bugs once the workspace UI grew to support
    # cut/copy/paste and drag-to-move: pasting a folder into a path already
    # at depth 4+ left its contents past the cap, so the frontend showed an
    # empty folder while the disk had files — leading to "Directory is not
    # empty" 400s on subsequent delete attempts. If perf ever becomes a
    # concern we should switch to lazy per-folder fetches instead of
    # eagerly shipping a tree this deep.
    username = ""
    if _acts_as_person(u):
        username = await run_db(task_store.get_username_by_sub, u.sub) or ""
    # The walk, the filter and the JSON encoding all run in one thread; at
    # most two walks run at once (a workspace panel refetches on every
    # file_updated event, and a burst of them must not hold the executor).
    async with _slot("tree", 2, 30.0, "the server is busy listing files, try again in a moment"):
        body = await asyncio.to_thread(
            _tree_json, name, u.acting_role(name), username, not _acts_as_person(u),
        )
    return Response(body, media_type="application/json")


@router.get("/v1/agents/{name}/files/{path:path}", dependencies=_BOUND)
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
    file_path, _ = safe_agent_path(agent_dir, name, path, u, writing=False,
                                   username=await _username_of(u))
    # The resolved path is the one authorized; it is opened beneath the
    # agents root with no symlink followed, so the file checked is the file
    # served, and everything from the open to the JSON runs off the loop.
    rel = _agent_rel(name, file_path, agent_dir)
    return await asyncio.to_thread(_read_agent_file_sync, rel, file_path.name, download)


def _read_agent_file_sync(rel: str, leaf: str, download: bool):
    try:
        fd, st = safe_fs.open_regular_for_read(config.AGENTS_DIR, rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except OSError as exc:
        logger.warning("file read refused for %s: %s", rel, type(exc).__name__)
        raise HTTPException(status_code=404, detail="File not found")
    try:
        headers = {"X-Content-Type-Options": "nosniff"}
        # Binary download mode: any file type
        if download:
            # Detect MIME from filename. Android's DownloadManager uses MIME
            # to decide the file extension when one isn't in
            # Content-Disposition; a blanket `application/octet-stream` makes
            # it save EVERYTHING as `.bin`. `guess_type` returns proper types
            # for `.md`, `.pdf`, etc., and falls back to octet-stream for
            # truly unknown extensions.
            import mimetypes
            mime, _ = mimetypes.guess_type(leaf)
            return FdFileResponse(
                fd, st, filename=leaf, media_type=mime or "application/octet-stream",
                headers=headers,
            )

        suffix = Path(leaf).suffix.lower()

        # Image files: served from the descriptor. ``nosniff`` stops the
        # browser from re-interpreting a mistyped image as HTML. SVG is
        # special: it can embed script and execute when opened as a
        # top-level document, so it is NEVER served inline: forcing a
        # filename sets ``Content-Disposition: attachment`` (an <img> still
        # renders it; a direct navigation downloads it instead).
        if suffix in IMAGE_MIME:
            if suffix == ".svg":
                return FdFileResponse(fd, st, media_type=IMAGE_MIME[suffix],
                                      filename=leaf, headers=headers)
            return FdFileResponse(fd, st, media_type=IMAGE_MIME[suffix], headers=headers)

        # Text files: an inline preview is capped; past the cap the client
        # downloads instead (the JSON encoding of a big file is what stalled
        # the loop, not the read).
        if suffix in TEXT_EXTENSIONS:
            cap = config.INLINE_TEXT_MAX_BYTES
            if cap > 0 and st.st_size > cap:
                raise HTTPException(
                    status_code=413,
                    detail=f"this file is {st.st_size / (1024 * 1024):.1f} MB, larger than "
                           f"the {cap // (1024 * 1024)} MB preview limit; download it instead",
                )
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                data = fh.read(cap + 1 if cap > 0 else -1)
            if cap > 0 and len(data) > cap:
                raise HTTPException(
                    status_code=413,
                    detail=f"this file is larger than the {cap // (1024 * 1024)} MB "
                           "preview limit; download it instead",
                )
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400, detail="File is not valid UTF-8 text"
                )
            body = json.dumps({"content": content, "encoding": "utf-8"},
                              ensure_ascii=False, separators=(",", ":"))
            return Response(body.encode("utf-8"), media_type="application/json")

        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension: {suffix}",
        )
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


@router.get("/v1/agents/{name}/recover-bin", dependencies=_BOUND)
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
    from storage.files import recover_bin_store
    entries = await asyncio.to_thread(
        recover_bin_store.list_for,
        name, u.sub, u.can_write_workspace(name), u.can_manage_agent(name), u.is_admin,
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


@router.post("/v1/agents/{name}/recover-bin/restore", dependencies=_BOUND)
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
    (NEVER overwritten, so concurrent work is preserved: the name is reserved
    with an exclusive write, never probed). Restored files re-sync to any
    satellites. Returns the restored / renamed / denied breakdown.
    """
    u = require_auth(user)
    require_agent_access(u, name)
    from storage.files import recover_bin_store

    is_edit = u.can_write_workspace(name)
    is_mgr = u.can_manage_agent(name)
    username = await _username_of(u)
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
        # A binned pre-1.4 persona restores under the current filename so it
        # is actually read again (readers prefer agent.md; a bare prompt.md
        # would sit ignored next to the live persona).
        if rel_path == "config/prompt.md":
            rel_path = "config/agent.md"
        # The entry's own path is the authorized destination, written at that
        # NAME beneath the agents root with no link followed: resolving it
        # first would let a link inside the caller's tree carry the bytes
        # into a scope the entry was never checked against.
        if has_traversal(rel_path) or "//" in rel_path:
            denied.append(entry_id)
            continue

        # Knowledge-library mirrors gate on the attachment's writable flag,
        # exactly like every other platform write does at the path-resolve
        # chokepoint. Restore wrote to the filesystem directly and skipped
        # it, so a manager could restore INTO a read-only mirror — content
        # the projector owns and heals away on its next sweep. Denied per
        # entry rather than aborting: one ineligible file must not sink the
        # rest of the selection.
        try:
            _check_library_mirror_write(rel_path, name)
        except HTTPException:
            denied.append(entry_id)
            continue

        try:
            async with _file_ops_slot():
                final_rel = await asyncio.to_thread(_restore_write_sync, name, rel_path, content)
        except HTTPException:
            denied.append(entry_id)
            continue
        if final_rel != rel_path:
            renamed.append({
                "entry_id": entry_id,
                "original": rel_path,
                "restored_as": final_rel,
            })

        # Publish it the way EVERY other platform write is published. Doing
        # its own fan-out call meant a restore skipped the rest of
        # record_platform_write: the delete tombstone stayed, so an idle
        # satellite would re-apply the delete and undo the restore; the
        # author was never recorded; and a restore into an RW library mirror
        # never reached the source, so the next reconcile healed it away.
        # This also picks up include_idle, which the bare call lacked.
        try:
            await file_bookkeeping.push_file_write(
                name, final_rel, config.AGENTS_DIR / name / final_rel,
                writer=_dashboard_writer(u, username))
        except Exception:
            logger.exception("recover-bin restore publish failed for %s", final_rel)

        await asyncio.to_thread(recover_bin_store.delete, entry_id)
        restored.append({"entry_id": entry_id, "rel_path": final_rel})

    return {"restored": restored, "renamed": renamed, "denied": denied}


def _restore_write_sync(name: str, sub: str, content: bytes) -> str:
    """Write a restored entry at ``sub`` (agent-relative), or at the first free
    `` (recovered)`` / `` (recovered N)`` sibling when the name is taken; each
    candidate is an exclusive atomic write beneath the agents root. Returns
    the agent-relative path written; raises the route's HTTPException."""
    parent, _, leaf = sub.rpartition("/")
    stem, ext = os.path.splitext(leaf)
    for n in range(0, 21):
        tag = "" if n == 0 else (" (recovered)" if n == 1 else f" (recovered {n})")
        cand = f"{parent}/{stem}{tag}{ext}" if parent else f"{stem}{tag}{ext}"
        try:
            safe_fs.atomic_write_beneath(
                config.AGENTS_DIR, f"{name}/{cand}", content, exclusive=True, mkdirs=True,
                fsync=False,
            )
            return cand
        except FileExistsError:
            continue
        except OSError as exc:
            logger.warning("recover-bin restore write refused for %s: %s", cand, type(exc).__name__)
            raise _fs_refusal(exc, name)
    raise HTTPException(status_code=409, detail="Too many name conflicts in destination")


@router.post("/v1/agents/{name}/recover-bin/discard", dependencies=_BOUND)
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
    from storage.files import recover_bin_store

    is_edit = u.can_write_workspace(name)
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


def _write_sync(name: str, agents_rel: str, content: bytes, *, exclusive: bool) -> None:
    """One atomic write beneath the agents root (a new file with ``exclusive``:
    the name is reserved by the create, never probed). No fsync: the files
    API never flushed a save to disk, and a batch restore of hundreds of
    entries must not pay one per file; the rename keeps the replace atomic."""
    try:
        safe_fs.atomic_write_beneath(
            config.AGENTS_DIR, agents_rel, content, mkdirs=True, exclusive=exclusive,
            fsync=False,
        )
    except FileExistsError:
        raise HTTPException(status_code=409, detail="File already exists")
    except OSError as exc:
        logger.warning("file write refused for %s: %s", agents_rel, type(exc).__name__)
        raise _fs_refusal(exc, name)


@router.put("/v1/agents/{name}/files/{path:path}", dependencies=_BOUND)
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
    uname = await _username_of(u)
    file_path, _ = safe_agent_path(agent_dir, name, path, u, writing=True, username=uname)
    agents_rel = _agent_rel(name, file_path, agent_dir)
    sub = _sub_of(name, agents_rel)
    async with _file_ops_slot():
        await asyncio.to_thread(
            _write_sync, name, agents_rel, req.content.encode("utf-8"), exclusive=False,
        )
    logger.info("Wrote file: %s", agents_rel)
    await file_bookkeeping.push_file_write(
        name, sub, config.AGENTS_DIR / agents_rel, writer=uname or None,
    )
    return {"status": "saved", "path": sub}


@router.post("/v1/agents/{name}/create-file", dependencies=_BOUND)
async def create_agent_file(
    name: str,
    req: CreateFileRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Create a new file. Text types get empty content; Office types get blank templates."""
    u = require_auth(user)
    require_agent_access(u, name)
    agent_dir = _get_agent_dir(name)
    uname = await _username_of(u)
    file_path, _ = safe_agent_path(agent_dir, name, req.path, u, writing=True, username=uname)
    agents_rel = _agent_rel(name, file_path, agent_dir)
    sub = _sub_of(name, agents_rel)

    ext = req.file_type or file_path.suffix.lower()
    template = _BLANK_TEMPLATES.get(ext) or b""
    async with _file_ops_slot():
        await asyncio.to_thread(_write_sync, name, agents_rel, template, exclusive=True)

    logger.info("Created file: %s", agents_rel)
    await file_bookkeeping.push_file_write(
        name, sub, config.AGENTS_DIR / agents_rel, writer=_dashboard_writer(u, uname),
    )
    return {"status": "created", "path": sub}


def _mkdir_sync(name: str, agents_rel: str) -> None:
    try:
        safe_fs.mkdirs_beneath(config.AGENTS_DIR, agents_rel)
    except (FileExistsError, NotADirectoryError):
        raise HTTPException(status_code=409, detail="A file is in the way of that directory")
    except OSError as exc:
        logger.warning("mkdir refused for %s: %s", agents_rel, type(exc).__name__)
        raise _fs_refusal(exc, name)


@router.post("/v1/agents/{name}/mkdir", dependencies=_BOUND)
async def create_agent_directory(
    name: str,
    req: MkdirRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Create a directory inside the agent's folder."""
    u = require_auth(user)
    require_agent_access(u, name)
    agent_dir = _get_agent_dir(name)
    dir_path, _ = safe_agent_path(agent_dir, name, req.path, u, writing=True,
                                  username=await _username_of(u))
    agents_rel = _agent_rel(name, dir_path, agent_dir)
    async with _file_ops_slot():
        await asyncio.to_thread(_mkdir_sync, name, agents_rel)
    logger.info("Created directory: %s", agents_rel)
    return {"status": "created", "path": _sub_of(name, agents_rel)}


def _delete_kind_sync(agents_rel: str) -> str:
    """``"file"``, ``"dir"`` or ``"other"`` for the entry at ``agents_rel``
    itself (never followed); a missing entry is the route's 404."""
    try:
        st = safe_fs.lstat_beneath(config.AGENTS_DIR, agents_rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Path not found")
    except OSError as exc:
        raise _fs_refusal(exc, agents_rel.partition("/")[0])
    if stat.S_ISREG(st.st_mode):
        return "file"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    return "other"


def _rmdir_empty_sync(name: str, agents_rel: str) -> None:
    """Remove an EMPTY directory with ``rmdir`` from its parent's handle: a
    child that lands between the listing and the removal keeps it (ENOTEMPTY
    is the route's 400), and nothing is ever captured or tombstoned here."""
    parent_rel, _, leaf = agents_rel.rpartition("/")
    try:
        pfd = safe_fs.open_dir_beneath(config.AGENTS_DIR, parent_rel)
    except OSError as exc:
        raise _fs_refusal(exc, name)
    try:
        os.rmdir(leaf, dir_fd=pfd)
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            raise HTTPException(status_code=400, detail="Directory is not empty")
        raise _fs_refusal(exc, name)
    finally:
        os.close(pfd)


def _delete_tree_sync(name: str, agents_rel: str, scope_rel: str) -> tuple[int, list[str]]:
    """The recursive delete beneath the agents root: the escape walk, then per
    regular file the recover-bin capture (read from the walk's handle, capped)
    and the tombstone, then the tree removal that never follows a link.
    Returns (files skipped by the bin cap, agent-relative paths tombstoned)."""
    from storage.files import recover_bin_store
    _assert_no_symlink_escape(agents_rel, f"{name}/{scope_rel}")
    cap = config.RECOVER_BIN_MAX_BYTES
    skipped = 0
    tombstoned: list[str] = []
    try:
        for step in safe_fs.walk_beneath(config.AGENTS_DIR, agents_rel):
            base = _sub_of(name, step.rel)
            for fname in step.files:
                crel = f"{base}/{fname}" if base else fname
                content: bytes | None = None
                try:
                    fd, _st = safe_fs.open_regular_for_read(step.dirfd, fname, max_size=cap)
                except safe_fs.FileTooLarge:
                    skipped += 1
                except OSError:
                    continue  # vanished, or no longer a regular file: nothing to capture
                else:
                    with os.fdopen(fd, "rb") as fh:
                        content = fh.read(cap + 1)
                    if len(content) > cap:
                        skipped += 1
                        content = None
                if content:
                    recover_bin_store.capture(name, crel, content, "deleted")
                file_bookkeeping.tombstone_path_sync(name, crel)
                tombstoned.append(crel)
        safe_fs.rmtree_beneath(config.AGENTS_DIR, agents_rel)
    except HTTPException:
        raise
    except OSError as exc:
        logger.warning("recursive delete refused for %s: %s", agents_rel, type(exc).__name__)
        raise _fs_refusal(exc, name)
    return skipped, tombstoned


@router.post("/v1/agents/{name}/delete", dependencies=_BOUND)
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
    target, _ = safe_agent_path(agent_dir, name, req.path, u, writing=True,
                                username=await _username_of(u))
    agents_rel = _agent_rel(name, target, agent_dir)
    sub = _sub_of(name, agents_rel)

    # Prevent deleting the agent root itself
    if not sub:
        raise HTTPException(status_code=403, detail="Cannot delete agent root directory")

    async with _file_ops_slot():
        kind = await asyncio.to_thread(_delete_kind_sync, agents_rel)

        if kind == "file":
            # Recover-bin capture + unlink + tombstone + fan-out: the ONE platform
            # delete sequence (shared with the Direct-LLM Delete tool). Files above
            # the bin cap are not captured (Windows-Recycle-Bin-style); the
            # dashboard warns "cannot be undone" from the flag.
            try:
                bin_skipped = await file_bookkeeping.delete_platform_file(
                    name, agent_dir, config.AGENTS_DIR / agents_rel,
                )
            except OSError as exc:
                raise _fs_refusal(exc, name)
            return {
                "status": "deleted", "path": req.path, "type": "file",
                "recover_bin_skipped": bin_skipped,
            }

        if kind != "dir":
            raise HTTPException(status_code=404, detail="Path not found")

        if not req.recursive:
            await asyncio.to_thread(_rmdir_empty_sync, name, agents_rel)
            logger.info("Deleted empty directory: %s", agents_rel)
            await file_bookkeeping.push_file_delete(name, sub)
            return {"status": "deleted", "path": req.path, "type": "dir"}

        # Recursive delete: never permit wiping a whole scope root
        # (`config/`, `workspace/`, `users/`, `users/<username>/`), judged on
        # the path as named AND on the answer (a link named in one scope that
        # lands in another is refused, as the walk refuses a link inside).
        scope = _scope_root(req.path)
        if not scope or req.path.strip("/") in {scope, layout.USERS}:
            raise HTTPException(
                status_code=403,
                detail="Cannot recursively delete a scope root",
            )
        if sub in {scope, layout.USERS} or not _under_scope(sub, scope):
            raise HTTPException(
                status_code=403,
                detail="Subtree contains a symlink escaping the scope",
            )

        # The escape walk, the recover-bin captures (best-effort; voluntary
        # delete → no notification), the per-file tombstones and the removal
        # run in one thread; the projections and the push follow on the loop.
        bin_skipped, tombstoned = await asyncio.to_thread(
            _delete_tree_sync, name, agents_rel, scope,
        )
    for crel in tombstoned:
        file_bookkeeping.schedule_library_projection(name, crel, deleted=True)
    logger.info("Recursively deleted directory: %s", agents_rel)
    await file_bookkeeping.push_file_delete(name, sub)
    return {
        "status": "deleted", "path": req.path, "type": "dir",
        "recursive": True, "recover_bin_skipped": bin_skipped,
    }


def _rename_sync(name: str, old_rel: str, new_rel: str, scope_rel: str) -> list[str]:
    """The rename beneath the agents root: the escape walk on a directory
    source, the tombstones of every file under the source (an idle satellite
    removes the old paths instead of resurrecting them), then one rename that
    refuses an existing target. Returns the agent-relative paths tombstoned."""
    _assert_no_symlink_escape(old_rel, f"{name}/{scope_rel}")
    tombstoned = file_bookkeeping.tombstone_subtree_sync(name, config.AGENTS_DIR / name, _sub_of(name, old_rel))
    try:
        safe_fs.rename_beneath(config.AGENTS_DIR, old_rel, new_rel)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Target path already exists")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Source path not found")
    except OSError as exc:
        logger.warning("rename refused for %s: %s", old_rel, type(exc).__name__)
        raise _fs_refusal(exc, name)
    return tombstoned


@router.post("/v1/agents/{name}/rename", dependencies=_BOUND)
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
    uname = await _username_of(u)
    old_path, _ = safe_agent_path(agent_dir, name, old_norm, u, writing=True, username=uname)
    new_path, _ = safe_agent_path(agent_dir, name, new_norm, u, writing=True, username=uname)
    old_rel = _agent_rel(name, old_path, agent_dir)
    new_rel = _agent_rel(name, new_path, agent_dir)
    old_sub, new_sub = _sub_of(name, old_rel), _sub_of(name, new_rel)
    scope = _scope_root(old_norm)
    if not scope or not _under_scope(old_sub, scope) or not _under_scope(new_sub, scope):
        raise HTTPException(status_code=403, detail="Subtree contains a symlink escaping the scope")

    async with _file_ops_slot():
        tombstoned = await asyncio.to_thread(_rename_sync, name, old_rel, new_rel, scope)
    for crel in tombstoned:
        file_bookkeeping.schedule_library_projection(name, crel, deleted=True)
    logger.info("Renamed: %s -> %s", old_rel, new_rel)
    # Mirror to active remote sessions: drop the old path; publish the new file(s).
    await file_bookkeeping.push_file_delete(name, old_sub)
    await file_bookkeeping.push_tree_write(
        name, config.AGENTS_DIR / new_rel, agent_dir, writer=_dashboard_writer(u, uname),
    )
    return {
        "status": "renamed",
        "old_path": old_sub,
        "new_path": new_sub,
    }


def _move_one_sync(name: str, src_rel: str, dest_rel: str, scope_rel: str) -> tuple[str, list[str]]:
    """Move one source into ``dest_rel`` beneath the agents root: the escape
    walk, the tombstones, then ``move_beneath`` onto the first free candidate
    name (reserved by the rename itself; across a filesystem boundary a copy
    that is removed only when whole). Returns (destination agents rel,
    tombstoned agent-relative paths)."""
    _assert_no_symlink_escape(src_rel, f"{name}/{scope_rel}")
    tombstoned = file_bookkeeping.tombstone_subtree_sync(name, config.AGENTS_DIR / name, _sub_of(name, src_rel))
    leaf = src_rel.rsplit("/", 1)[-1]
    for cand in _candidate_names(leaf):
        target = f"{dest_rel}/{cand}"
        try:
            safe_fs.move_beneath(config.AGENTS_DIR, src_rel, target)
            return target, tombstoned
        except FileExistsError:
            continue
    raise HTTPException(status_code=409, detail="Too many name conflicts in destination")


@router.post("/v1/agents/{name}/move", dependencies=_BOUND)
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
    username = await _username_of(u)
    if _acts_as_person(u):
        # _validate_op_paths → _check_file_role enforces per-tier
        # write rules on every source + dest path.
        role = u.acting_role(name)
    else:
        role, username = "admin", ""

    agent_dir = _get_agent_dir(name)
    sources, dest_resolved = await asyncio.to_thread(
        _validate_op_paths, req.src_paths, req.dest_dir,
        agent_dir=agent_dir, role=role, username=username, writing_on_source=True,
    )
    dest_rel = _agent_rel(name, dest_resolved, agent_dir)

    moved: list[dict] = []
    failed: list[dict] = []
    writer = _dashboard_writer(u, username)
    for src_norm, src_resolved in sources:
        try:
            src_rel = _agent_rel(name, src_resolved, agent_dir)
            src_sub = _sub_of(name, src_rel)
            src_scope = _scope_root(src_norm)
            if not src_scope:
                raise HTTPException(status_code=400, detail=f"Invalid source scope: {src_norm}")
            if src_sub == src_scope or not _under_scope(src_sub, src_scope):
                raise HTTPException(
                    status_code=403, detail="Subtree contains a symlink escaping the scope",
                )

            # No-op when the source already sits in the dest directory:
            # cut+paste-into-same-folder shouldn't create `_1` copies.
            if src_rel.rpartition("/")[0] == dest_rel:
                moved.append({"src": src_norm, "dest": src_sub, "noop": True})
                continue

            async with _file_ops_slot():
                target_rel, tombstoned = await asyncio.to_thread(
                    _move_one_sync, name, src_rel, dest_rel, src_scope,
                )
            for crel in tombstoned:
                file_bookkeeping.schedule_library_projection(name, crel, deleted=True)
            logger.info("Moved: %s -> %s", src_rel, target_rel)
            # Mirror to active remote sessions: drop the old subtree, push the
            # new one (recursively for directories) so the satellite updates
            # immediately rather than waiting for the next manifest sync.
            await file_bookkeeping.push_file_delete(name, src_sub)
            await file_bookkeeping.push_tree_write(
                name, config.AGENTS_DIR / target_rel, agent_dir, writer=writer,
            )
            moved.append({"src": src_norm, "dest": _sub_of(name, target_rel)})
        except HTTPException as e:
            failed.append({"src": src_norm, "reason": e.detail})
        except (OSError, shutil.Error) as e:
            logger.warning("Move failed for %s: %s", src_norm, e)
            failed.append({"src": src_norm, "reason": _fs_error_reason(e)})

    return {"moved": moved, "failed": failed}


def _copy_one_sync(name: str, src_rel: str, dest_rel: str, scope_rel: str) -> str:
    """Copy one source into ``dest_rel`` beneath the agents root: the escape
    walk, then the copy onto the first free candidate name (a file with an
    exclusive write; a directory with ``copytree_beneath``, which recreates a
    link only where its text stays inside the tree). Returns the destination
    agents rel."""
    _assert_no_symlink_escape(src_rel, f"{name}/{scope_rel}")
    st = safe_fs.lstat_beneath(config.AGENTS_DIR, src_rel)
    leaf = src_rel.rsplit("/", 1)[-1]
    for cand in _candidate_names(leaf):
        target = f"{dest_rel}/{cand}"
        try:
            if stat.S_ISDIR(st.st_mode):
                with safe_fs.open_root(config.AGENTS_DIR, name) as rootfd:
                    safe_fs.copytree_beneath(
                        rootfd, _sub_of(name, src_rel), rootfd, _sub_of(name, target),
                        symlinks="copy",
                    )
            else:
                safe_fs.copy_file_beneath(
                    config.AGENTS_DIR, src_rel, config.AGENTS_DIR, target, exclusive=True,
                )
            return target
        except FileExistsError:
            continue
    raise HTTPException(status_code=409, detail="Too many name conflicts in destination")


@router.post("/v1/agents/{name}/copy", dependencies=_BOUND)
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
    username = await _username_of(u)
    if _acts_as_person(u):
        # _validate_op_paths → _check_file_role enforces per-tier
        # write rules on the dest dir (sources need read perms only).
        role = u.acting_role(name)
    else:
        role, username = "admin", ""

    agent_dir = _get_agent_dir(name)
    sources, dest_resolved = await asyncio.to_thread(
        _validate_op_paths, req.src_paths, req.dest_dir,
        agent_dir=agent_dir, role=role, username=username, writing_on_source=False,
    )
    dest_rel = _agent_rel(name, dest_resolved, agent_dir)

    copied: list[dict] = []
    failed: list[dict] = []
    writer = _dashboard_writer(u, username)
    for src_norm, src_resolved in sources:
        try:
            src_rel = _agent_rel(name, src_resolved, agent_dir)
            src_sub = _sub_of(name, src_rel)
            src_scope = _scope_root(src_norm)
            if not src_scope:
                raise HTTPException(status_code=400, detail=f"Invalid source scope: {src_norm}")
            if not _under_scope(src_sub, src_scope):
                raise HTTPException(
                    status_code=403, detail="Subtree contains a symlink escaping the scope",
                )
            async with _file_ops_slot():
                target_rel = await asyncio.to_thread(
                    _copy_one_sync, name, src_rel, dest_rel, src_scope,
                )
            logger.info("Copied: %s -> %s", src_rel, target_rel)
            # Mirror to active remote sessions: push the new file/subtree so the
            # satellite sees the copy immediately, not only at the next sync.
            # (Copy keeps the source — no tombstone.)
            await file_bookkeeping.push_tree_write(
                name, config.AGENTS_DIR / target_rel, agent_dir, writer=writer,
            )
            copied.append({"src": src_norm, "dest": _sub_of(name, target_rel)})
        except HTTPException as e:
            failed.append({"src": src_norm, "reason": e.detail})
        except (OSError, shutil.Error) as e:
            logger.warning("Copy failed for %s: %s", src_norm, e)
            failed.append({"src": src_norm, "reason": _fs_error_reason(e)})

    return {"copied": copied, "failed": failed}


@router.post("/v1/agents/{name}/zip", dependencies=_BOUND)
async def zip_agent_paths(
    name: str,
    req: ZipPathsRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Build a zip archive of the requested files/directories and stream it.

    Read-only operation — `require_write` is NOT applied so viewers can
    download their own files. Each path is validated against the user's
    read scope. The archive is built off the loop into an unnamed temp
    file, bounded by `ZIP_MAX_INPUT_MB` / `ZIP_MAX_ENTRIES` (413) and
    `ZIP_MAX_CONCURRENT` builds at once, and streamed from its descriptor.

    The browser path uses this POST endpoint to receive the zip directly as
    a blob. Capacitor/Android can't download blob: URLs (DownloadManager
    only accepts http/https), so the dashboard uses `POST /zip-url` +
    `GET /zip-download` instead — same validation + builder, just split so
    DownloadManager has a real http URL to hit.
    """
    u = require_auth(user)
    require_agent_access(u, name)
    if _acts_as_person(u):
        role, username = await run_db(_resolve_user_session_info, u, name)
    else:
        role, username = "admin", ""

    return await _build_zip_response(name, req.paths, role, username, u.sub)


@router.post("/v1/agents/{name}/zip-url", dependencies=_BOUND)
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
    if _acts_as_person(u):
        role, username = await run_db(_resolve_user_session_info, u, name)
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
    role = claims.get("role") or roles.VIEWER
    username = claims.get("username") or ""
    # `fn` is informational for the client; the real filename comes from
    # _build_zip_response via Content-Disposition.
    _ = fn
    user_key = claims.get("user_sub") or f"{role}:{username}"
    return await _build_zip_response(name, paths, role, username, user_key)
