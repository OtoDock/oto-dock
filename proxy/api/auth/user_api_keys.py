"""User API key CRUD.

A person's keys: ``triggers`` fires their own webhooks, the other scopes
reach the public routes (``api/public/``) as that person, on the agents the
key names. A key is minted at the dashboard only, with an expiry, shown
once; any scope past ``triggers`` (or a key that runs without asking) asks
for the password or the second step again (``auth.confirm.confirm_human``).
An admin's view of every key is ``api/admin/api_keys.py``.

Endpoints (scoped to the current user):
  POST   /v1/user-api-keys
  GET    /v1/user-api-keys
  DELETE /v1/user-api-keys/{key_id}
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
    require_user,
)
from services.infra import api_key_manager
from storage.identity import api_key_store
from storage.pg import run_db

logger = logging.getLogger("otodock.user-api-keys")
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])


class CreateUserKeyRequest(BaseModel):
    name: str
    permissions: list[str] = ["triggers"]
    agents: list[str] = []
    expires_in_days: int
    run_without_asking: bool = False
    password: str = ""
    confirm_token: str = ""


def _key_view(r: dict) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "prefix": r["prefix"],
        "permissions": api_key_store.parse_permissions(r["permissions"]),
        "agents": api_key_store.parse_agents(r.get("agents")),
        "expires_at": r.get("expires_at"),
        "run_without_asking": bool(r.get("run_without_asking")),
        "created_at": r["created_at"],
        "last_used_at": r.get("last_used_at"),
        "revoked_at": r.get("revoked_at"),
    }


@router.post("/v1/user-api-keys")
async def create_user_key(
    req: CreateUserKeyRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key or u.api_key_id:
        # User keys are owned by humans. Server-to-server callers shouldn't
        # mint user keys directly — go through admin if needed.
        raise HTTPException(403, "User API keys must be created by a logged-in user")
    perms = set(req.permissions or ["triggers"])
    for agent in req.agents:
        if not u.can_access_agent(agent):
            raise HTTPException(400, f"You do not have the agent {agent!r}.")
    if api_key_manager.TASKS_PERMISSION in perms and not req.run_without_asking:
        raise HTTPException(400, api_key_manager.TASKS_NEED_UNASKED)
    if perms & api_key_manager.CONFIRMED_USER_PERMISSIONS or req.run_without_asking:
        from auth.confirm import confirm_human
        await confirm_human(u, password=req.password, confirm_token=req.confirm_token)
    # A cost-12 bcrypt hash: on a plain worker thread, not the run_db lane.
    try:
        row, raw = await asyncio.to_thread(
            api_key_manager.create_user_key,
            user_sub=u.sub,
            name=req.name,
            permissions=req.permissions,
            agents=req.agents,
            expires_in_days=req.expires_in_days,
            run_without_asking=req.run_without_asking,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "created", **_key_view(row), "key": raw}  # the key: SHOWN ONCE


@router.get("/v1/user-api-keys")
async def list_user_keys(
    include_revoked: bool = Query(False),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key or u.api_key_id:
        raise HTTPException(403, "Listing user API keys requires session auth")
    rows = await run_db(
        api_key_store.list_user_api_keys,
        user_sub=u.sub, include_revoked=include_revoked,
    )
    return {"keys": [_key_view(r) for r in rows]}


@router.delete("/v1/user-api-keys/{key_id}")
async def revoke_user_key(
    key_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key or u.api_key_id:
        raise HTTPException(403, "Revoking user API keys requires session auth")

    def _job() -> str:
        row = api_key_store.get_user_api_key(key_id)
        # Strict ownership: an admin revokes another person's key through
        # ``DELETE /v1/admin/api-keys/user/{key_id}``.
        if not row or row.get("user_sub") != u.sub:
            return "missing"
        if row.get("revoked_at"):
            return "already_revoked"
        api_key_manager.revoke_user_key(key_id)
        return "revoked"

    outcome = await run_db(_job)
    if outcome == "missing":
        raise HTTPException(404, "You have no such key.")
    created, total = await run_db(api_key_store.key_created, key_id)
    if outcome == "already_revoked":
        return {"status": "already_revoked", "id": key_id, "created": created,
                "created_total": total}
    logger.info("Revoked user_api_key %s for user %s", key_id[:8], u.sub[:8])
    return {"status": "revoked", "id": key_id, "created": created, "created_total": total}
