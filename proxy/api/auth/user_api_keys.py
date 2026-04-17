"""User API key CRUD.

Per-user keys for user-scoped trigger webhook auth and (future) alternative
input layers (WhatsApp / Telegram / Slack). Permission scopes restrict
what each key can do; v1 wires up only ``triggers``.

Endpoints (scoped to current user — no admin override here; admin uses
admin/userApiKeys page):
  POST   /v1/user-api-keys
  GET    /v1/user-api-keys
  DELETE /v1/user-api-keys/{key_id}
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from storage import api_key_store
from services.infra import api_key_manager
from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
)

logger = logging.getLogger("claude-proxy.user-api-keys")
router = APIRouter()


class CreateUserKeyRequest(BaseModel):
    name: str
    permissions: list[str] = ["triggers"]


@router.post("/v1/user-api-keys")
async def create_user_key(
    req: CreateUserKeyRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key:
        # User keys are owned by humans. Server-to-server callers shouldn't
        # mint user keys directly — go through admin if needed.
        raise HTTPException(403, "User API keys must be created by a logged-in user")
    try:
        row, raw = api_key_manager.create_user_key(
            user_sub=u.sub,
            name=req.name,
            permissions=req.permissions,
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
        "key": raw,  # SHOWN ONCE
    }


@router.get("/v1/user-api-keys")
async def list_user_keys(
    include_revoked: bool = Query(False),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Listing user API keys requires session auth")
    rows = api_key_store.list_user_api_keys(
        user_sub=u.sub, include_revoked=include_revoked,
    )
    return {
        "keys": [
            {
                "id": r["id"],
                "name": r["name"],
                "prefix": r["prefix"],
                "permissions": api_key_store.parse_permissions(r["permissions"]),
                "created_at": r["created_at"],
                "last_used_at": r.get("last_used_at"),
                "revoked_at": r.get("revoked_at"),
            }
            for r in rows
        ]
    }


@router.delete("/v1/user-api-keys/{key_id}")
async def revoke_user_key(
    key_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if u.is_api_key:
        raise HTTPException(403, "Revoking user API keys requires session auth")
    row = api_key_store.get_user_api_key(key_id)
    if not row:
        raise HTTPException(404, "Key not found")
    if row.get("user_sub") != u.sub:
        # Strict ownership — admin can't revoke other users' keys via this
        # endpoint (would need a separate admin endpoint).
        raise HTTPException(404, "Key not found")
    if row.get("revoked_at"):
        return {"status": "already_revoked", "id": key_id}
    api_key_manager.revoke_user_key(key_id)
    logger.info(f"Revoked user_api_key {key_id[:8]} for user {u.sub[:8]}")
    return {"status": "revoked", "id": key_id}
