"""Per-user agent context files (``users/<name>/context/``).

Small Markdown/text notes a user attaches to an agent. Attaches to the
shared package router.

The person's own sessions write this folder, so every list, read, write and
removal opens beneath the agent's real folder (``core/agent_folder.py``)
with no link followed: a name is judged where it lands
(``safe_fs.canonical_rel``, so a person's own link to another file of the
folder keeps working) and that answer is opened, inside the folder only."""

import asyncio
import stat
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from api.agents._router import router
from auth.providers import (
    UserContext,
    get_current_user,
    require_agent_access,
    require_auth,
)
from core import agent_folder, layout
from services.infra import safe_fs
from services.infra.path_confinement import PathOutsideRoot, normalize_rel_path

CONTEXT_ALLOWED_EXTENSIONS = {".md", ".txt"}
#: A context file is read whole into the answer, at most this much.
CONTEXT_READ_MAX_BYTES = 1024 * 1024
_OUTSIDE = "That path is outside your files."


def _context_place(agent_name: str, username: str) -> tuple[Path, str]:
    """The agent's real folder and the person's context folder below it,
    made when missing with no link followed. Blocking."""
    root = agent_folder.agent_root(agent_name)
    rel = f"{layout.user_rel(username)}/context"
    try:
        safe_fs.mkdirs_beneath(root, rel)
    except OSError as exc:
        raise HTTPException(403, _OUTSIDE) from exc
    return root, rel


def _get_user_context_dir(agent_name: str, user: UserContext) -> tuple[Path, str]:
    """The context folder of this user on this agent, made when missing."""
    from core.session.visibility import is_shared_only
    from storage import database as task_store
    # Shared-only agents have NO per-user scope — this endpoint must not
    # quietly recreate users/<u>/ dirs there.
    if is_shared_only(agent_name):
        raise HTTPException(400, "This agent is Shared-only — it has no per-user context")
    username = task_store.get_username_by_sub(user.sub)
    if not username:
        raise HTTPException(400, "User has no username slug")
    return _context_place(agent_name, username)


def _file_rel(root: Path, rel: str, filename: str) -> str:
    """Where ``filename`` in the context folder lands, as a rel beneath
    ``root``: a link inside the folder is followed to its target, a name
    or a link that leaves the folder is refused (403)."""
    try:
        name = normalize_rel_path(filename)
        if "/" in name or "\\" in name:
            raise PathOutsideRoot(filename)
        answer = safe_fs.canonical_rel(root, f"{rel}/{name}")
    except (OSError, ValueError, PathOutsideRoot) as exc:
        raise HTTPException(403, _OUTSIDE) from exc
    if not answer.startswith(rel + "/"):
        raise HTTPException(403, _OUTSIDE)
    return answer


def _list_context_files(root: Path, rel: str) -> list[dict]:
    try:
        step = next(safe_fs.walk_beneath(root, rel))
    except (OSError, StopIteration):
        return []
    files = []
    for name in sorted(step.files + step.symlinks):
        if Path(name).suffix not in CONTEXT_ALLOWED_EXTENSIONS:
            continue
        try:
            st = safe_fs.lstat_beneath(root, _file_rel(root, rel, name))
        except (OSError, HTTPException):
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        files.append({
            "name": name,
            "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
    return files


def _read_context_file(root: Path, rel: str, filename: str) -> str:
    answer = _file_rel(root, rel, filename)
    if Path(filename).suffix not in CONTEXT_ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Only {', '.join(CONTEXT_ALLOWED_EXTENSIONS)} files allowed")
    try:
        data = safe_fs.read_bytes_beneath(root, answer, max_size=CONTEXT_READ_MAX_BYTES)
    except FileNotFoundError as exc:
        raise HTTPException(404, "File not found") from exc
    except safe_fs.FileTooLarge as exc:
        raise HTTPException(413, "That file is too large to show here.") from exc
    except OSError as exc:
        raise HTTPException(403, _OUTSIDE) from exc
    return data.decode("utf-8", errors="replace")


def _write_context_file(root: Path, rel: str, filename: str, content: str) -> None:
    safe_fs.atomic_write_beneath(root, _file_rel(root, rel, filename), content.encode("utf-8"),
                                 fsync=False)


def _delete_context_file(root: Path, rel: str, filename: str) -> None:
    answer = _file_rel(root, rel, filename)
    if safe_fs.kind_beneath(root, answer).kind == "missing":
        raise HTTPException(404, "File not found")
    try:
        safe_fs.unlink_beneath(root, answer)
    except FileNotFoundError as exc:
        raise HTTPException(404, "File not found") from exc
    except OSError as exc:
        raise HTTPException(403, _OUTSIDE) from exc


class UserContextFileRequest(BaseModel):
    content: str


@router.get("/v1/agents/{name}/user-context")
async def list_user_context_files(
    name: str,
    user: UserContext | None = Depends(get_current_user),
):
    """List the current user's context files for an agent."""
    u = require_auth(user)
    require_agent_access(u, name)
    root, rel = await asyncio.to_thread(_get_user_context_dir, name, u)
    return await asyncio.to_thread(_list_context_files, root, rel)


@router.get("/v1/agents/{name}/user-context/{filename}")
async def get_user_context_file(
    name: str,
    filename: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Read a user context file."""
    u = require_auth(user)
    require_agent_access(u, name)
    root, rel = await asyncio.to_thread(_get_user_context_dir, name, u)
    content = await asyncio.to_thread(_read_context_file, root, rel, filename)
    return {"name": filename, "content": content}


@router.put("/v1/agents/{name}/user-context/{filename}")
async def set_user_context_file(
    name: str,
    filename: str,
    body: UserContextFileRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Create or update a user context file."""
    u = require_auth(user)
    require_agent_access(u, name)
    root, rel = await asyncio.to_thread(_get_user_context_dir, name, u)

    # Validate filename
    if not filename.strip() or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    # Auto-add .md if no valid extension
    if Path(filename).suffix not in CONTEXT_ALLOWED_EXTENSIONS:
        filename = filename + ".md"
    try:
        await asyncio.to_thread(_write_context_file, root, rel, filename, body.content)
    except OSError as exc:
        raise HTTPException(403, _OUTSIDE) from exc
    return {"status": "ok", "name": filename}


@router.delete("/v1/agents/{name}/user-context/{filename}")
async def delete_user_context_file(
    name: str,
    filename: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Delete a user context file."""
    u = require_auth(user)
    require_agent_access(u, name)
    root, rel = await asyncio.to_thread(_get_user_context_dir, name, u)
    await asyncio.to_thread(_delete_context_file, root, rel, filename)
    return {"status": "deleted", "name": filename}
