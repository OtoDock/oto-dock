"""Per-user agent context files (``users/<name>/context/``).

Small Markdown/text notes a user attaches to an agent. Attaches to the
shared package router."""

from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_agent_access, require_auth

from api.agents._common import _get_agent_dir
from api.agents._router import router


CONTEXT_ALLOWED_EXTENSIONS = {".md", ".txt"}


def _get_user_context_dir(agent_name: str, user: UserContext) -> Path:
    """Get or create the user-context directory for this user + agent."""
    from core.session.visibility import is_shared_only
    from storage import database as task_store
    # Shared-only agents have NO per-user scope — this endpoint must not
    # quietly recreate users/<u>/ dirs there (the mkdir below would).
    if is_shared_only(agent_name):
        raise HTTPException(400, "This agent is Shared-only — it has no per-user context")
    username = task_store.get_username_by_sub(user.sub)
    if not username:
        raise HTTPException(400, "User has no username slug")
    agent_dir = _get_agent_dir(agent_name)
    ctx_dir = agent_dir / "users" / username / "context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    return ctx_dir


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
    ctx_dir = _get_user_context_dir(name, u)

    files = []
    for f in sorted(ctx_dir.iterdir()):
        if f.is_file() and f.suffix in CONTEXT_ALLOWED_EXTENSIONS:
            stat = f.stat()
            files.append({
                "name": f.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            })
    return files


@router.get("/v1/agents/{name}/user-context/{filename}")
async def get_user_context_file(
    name: str,
    filename: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Read a user context file."""
    u = require_auth(user)
    require_agent_access(u, name)
    ctx_dir = _get_user_context_dir(name, u)

    # Validate filename
    file_path = (ctx_dir / filename).resolve()
    ctx_root = ctx_dir.resolve()
    if file_path != ctx_root and not file_path.is_relative_to(ctx_root):
        raise HTTPException(403, "Path traversal not allowed")
    if file_path.suffix not in CONTEXT_ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Only {', '.join(CONTEXT_ALLOWED_EXTENSIONS)} files allowed")
    if not file_path.exists():
        raise HTTPException(404, "File not found")

    return {"name": filename, "content": file_path.read_text()}


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
    ctx_dir = _get_user_context_dir(name, u)

    # Validate filename
    if not filename.strip() or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    # Auto-add .md if no valid extension
    path = Path(filename)
    if path.suffix not in CONTEXT_ALLOWED_EXTENSIONS:
        filename = filename + ".md"
    file_path = (ctx_dir / filename).resolve()
    ctx_root = ctx_dir.resolve()
    if file_path != ctx_root and not file_path.is_relative_to(ctx_root):
        raise HTTPException(403, "Path traversal not allowed")

    file_path.write_text(body.content)
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
    ctx_dir = _get_user_context_dir(name, u)

    file_path = (ctx_dir / filename).resolve()
    ctx_root = ctx_dir.resolve()
    if file_path != ctx_root and not file_path.is_relative_to(ctx_root):
        raise HTTPException(403, "Path traversal not allowed")
    if not file_path.exists():
        raise HTTPException(404, "File not found")

    file_path.unlink()
    return {"status": "deleted", "name": filename}
