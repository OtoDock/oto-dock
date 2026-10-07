"""Agent API key CRUD.

Manager-managed keys for agent-scoped trigger webhook auth. The raw key is
returned ONCE on creation; subsequent reads only see the prefix +
metadata. A key outlives the session that could ask for it and fires the
agent's webhooks from anywhere, so every route is a person's decision at
the dashboard: bearer principals (session tokens, the master key) are
refused.

Endpoints:
  POST   /v1/agents/{agent}/api-keys                 — create (manager+)
  GET    /v1/agents/{agent}/api-keys                 — list active keys
  DELETE /v1/agents/{agent}/api-keys/{key_id}        — revoke (soft)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from storage.identity import api_key_store
from storage.pg import run_db
from services.infra import api_key_manager
from auth.providers import (
    UserContext,
    get_current_user,
    require_human,
    require_user,
)

logger = logging.getLogger("claude-proxy.agent-api-keys")
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])


class CreateAgentKeyRequest(BaseModel):
    name: str
    permissions: list[str] = ["triggers"]


def _check_manager(user: UserContext, agent: str) -> None:
    if user.is_admin:
        return
    if not user.can_manage_agent(agent):
        raise HTTPException(
            403,
            f"Agent API keys require manager or admin role for '{agent}'",
        )


@router.post("/v1/agents/{agent}/api-keys")
async def create_agent_key(
    agent: str,
    req: CreateAgentKeyRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_human(user)
    _check_manager(u, agent)
    try:
        row, raw = await run_db(
            api_key_manager.create_agent_key,
            agent=agent,
            name=req.name,
            permissions=req.permissions,
            created_by=u.sub,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "status": "created",
        "id": row["id"],
        "prefix": row["prefix"],
        "name": row["name"],
        "permissions": req.permissions,
        "created_at": row["created_at"],
        "key": raw,  # SHOWN ONCE — caller must surface this to the user
    }


@router.get("/v1/agents/{agent}/api-keys")
async def list_agent_keys(
    agent: str,
    include_revoked: bool = Query(False),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_human(user)
    _check_manager(u, agent)
    rows = await run_db(
        api_key_store.list_agent_api_keys, agent=agent, include_revoked=include_revoked,
    )
    return {
        "keys": [
            {
                "id": r["id"],
                "agent": r["agent"],
                "name": r["name"],
                "prefix": r["prefix"],
                "permissions": api_key_store.parse_permissions(r["permissions"]),
                "created_by": r["created_by"],
                "created_at": r["created_at"],
                "last_used_at": r.get("last_used_at"),
                "revoked_at": r.get("revoked_at"),
            }
            for r in rows
        ]
    }


@router.delete("/v1/agents/{agent}/api-keys/{key_id}")
async def revoke_agent_key(
    agent: str,
    key_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_human(user)
    _check_manager(u, agent)
    row = await run_db(api_key_store.get_agent_api_key, key_id)
    if not row:
        raise HTTPException(404, "Key not found")
    if row.get("agent") != agent:
        # Don't leak agent association across managers.
        raise HTTPException(404, "Key not found")
    if row.get("revoked_at"):
        return {"status": "already_revoked", "id": key_id}
    await run_db(api_key_manager.revoke_agent_key, key_id)
    logger.info(f"Revoked agent_api_key {key_id[:8]} for agent {agent}")
    return {"status": "revoked", "id": key_id}
