"""Git history admin API — read-only inspection + revert of tracked
agent config / per-user context files. Admin only.

The repos are initialised by ``git_writer.init_if_missing`` at agent
creation + per-user-context dir creation (+ lazily for ``knowledge/`` on
the first agent-scope memory write). Memory-tool commits show up under
the bot author ``otodock-system`` with the acting user in the subject.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

import config as app_config
from auth.providers import UserContext, get_current_user, require_auth
from services.infra import git_writer
from storage import agent_store
from storage import database as task_store


logger = logging.getLogger("claude-proxy.git-history-api")
router = APIRouter(prefix="/v1/internal/git")


def _authz_user_scope(u: UserContext, scope: str, user_sub: str | None) -> None:
    """A manager may read the agent's own ``config/`` history, but a per-user
    ``context/`` repo is private: only an admin — or the owning user reading
    their OWN history — may inspect it. (``/revert`` is already admin-only.)"""
    if scope == "user" and not u.is_admin:
        if not user_sub or user_sub != u.sub:
            raise HTTPException(403, "Can only access your own user-scoped history")


def _resolve_repo(scope: str, agent: str, user_sub: str | None) -> Path:
    if not agent_store.agent_exists(agent):
        raise HTTPException(404, f"agent not found: {agent}")
    agent_dir = app_config.get_agent_dir(agent)
    if scope == "agent":
        return agent_dir / "config"
    if scope == "user":
        if not user_sub:
            raise HTTPException(400, "user-scope git requires user_sub")
        username = task_store.get_username_by_sub(user_sub)
        if not username:
            raise HTTPException(404, f"username not found for {user_sub}")
        return agent_dir / "users" / username / "context"
    raise HTTPException(400, f"unknown scope: {scope!r}")


@router.get("/log")
async def git_log(
    agent: str,
    scope: str = "agent",
    user_sub: str | None = None,
    path: str | None = None,
    limit: int = 50,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    """List commits in the agent's config or user-context repo."""
    u = require_auth(user)
    if not (u.is_admin or u.can_manage_agent(agent)):
        raise HTTPException(403, "manager or admin only")
    _authz_user_scope(u, scope, user_sub)
    repo = _resolve_repo(scope, agent, user_sub)
    capped = max(1, min(int(limit), app_config.MAX_PAGE_SIZE))
    return {
        "commits": await asyncio.to_thread(
            git_writer.log, repo, capped, path,
        )
    }


@router.get("/diff")
async def git_diff(
    agent: str,
    commit: str,
    scope: str = "agent",
    user_sub: str | None = None,
    path: str | None = None,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the diff for a given commit (optionally scoped to one file)."""
    u = require_auth(user)
    if not (u.is_admin or u.can_manage_agent(agent)):
        raise HTTPException(403, "manager or admin only")
    _authz_user_scope(u, scope, user_sub)
    repo = _resolve_repo(scope, agent, user_sub)
    return {"diff": await asyncio.to_thread(git_writer.diff, repo, commit, path)}


@router.post("/revert")
async def git_revert(
    request: Request,
    user: UserContext | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Revert a tracked file to an earlier commit.
    Body: {agent, scope, commit, path, user_sub?}. Admin only — reverts
    are visible to all users so we keep this tighter than read."""
    u = require_auth(user)
    if not u.is_admin:
        raise HTTPException(403, "admin only")
    body = await request.json()
    agent = body.get("agent")
    scope = body.get("scope", "agent")
    commit = body.get("commit")
    path = body.get("path")
    user_sub = body.get("user_sub")
    if not (agent and commit and path):
        raise HTTPException(400, "agent, commit, path required")
    repo = _resolve_repo(scope, agent, user_sub)
    target = (repo / path).resolve()
    # Don't allow escaping the repo via .. — resolve and re-check the
    # parent boundary.
    if repo.resolve() not in target.parents and target != repo.resolve():
        raise HTTPException(400, "path escapes repo")
    new_sha = await asyncio.to_thread(
        git_writer.revert_file_to, repo, commit, target,
        message=f"revert {path} to {commit[:8]} (admin)",
    )
    if not new_sha:
        raise HTTPException(500, "revert failed")
    return {"new_sha": new_sha, "path": path}
